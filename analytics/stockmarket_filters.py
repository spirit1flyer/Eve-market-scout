"""Stock Market filtering for EVE Market Scout.

Filters stock market items based on profitability after fees,
volume requirements, and other criteria.

Uses cached skills/standings from JSON (saved by Tracking tab) for fee calculations.

Material Analysis Integration:
    Items that pass base filters for "low risk" get checked against
    material cost correlation. If TBC is also dropping, the item is
    promoted to "medium risk" (floor not yet established).
    
Material Filter Optimization:
    The material filter runs only ONCE per hub per day (first scan).
    Tracked via MaterialFilterTracker singleton.
"""

import json
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any, Set, Tuple

from core.sound_manager import get_data_dir
from core.calculate import (
    TradingSkills, DEFAULT_SKILLS,
    get_broker_fee_rate, get_sales_tax_rate,
    calculate_break_even, load_cached_skills
)
from analytics import material_risk_storage


def _check_thread(context: str):
    """Debug helper - warn if not on main thread."""
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from {current.name}")
        import traceback
        traceback.print_stack(limit=8)


# Filter settings file
FILTERS_FILE = get_data_dir() / "stockmarket_filters.json"

# Defaults
DEFAULT_MIN_MARGIN_PCT = 8.0       # Minimum margin % after fees
DEFAULT_MIN_DAILY_VOLUME = 5       # Minimum avg daily volume
DEFAULT_MIN_BAND_WIDTH_PCT = 10.0  # Minimum spread between floor/ceiling
DEFAULT_MAX_PRICE = 0              # 0 = no limit
DEFAULT_MIN_PRICE = 0              # 0 = no limit
DEFAULT_STABILITY_THRESHOLD = 20.0 # Max % deviation for "stable" trend
DEFAULT_MATERIAL_ANALYSIS_ENABLED = True  # Enable material cost analysis


@dataclass
class StockMarketFilters:
    """Stock market filtering criteria."""
    
    # Profitability filters
    min_margin_pct: float = DEFAULT_MIN_MARGIN_PCT
    min_band_width_pct: float = DEFAULT_MIN_BAND_WIDTH_PCT
    
    # Volume filters
    min_daily_volume: int = DEFAULT_MIN_DAILY_VOLUME
    
    # Price filters
    min_price: float = DEFAULT_MIN_PRICE
    max_price: float = DEFAULT_MAX_PRICE  # 0 = no limit
    
    # Trend filters
    stability_threshold_pct: float = DEFAULT_STABILITY_THRESHOLD
    
    # Material analysis
    material_analysis_enabled: bool = DEFAULT_MATERIAL_ANALYSIS_ENABLED
    
    # Skills for fee calculation (loaded from cached JSON)
    broker_fee_pct: float = 3.0    # Default ~3% with no skills
    sales_tax_pct: float = 4.5     # Default ~4.5% with no skills
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "StockMarketFilters":
        """Create from dictionary."""
        return cls(
            min_margin_pct=data.get("min_margin_pct", DEFAULT_MIN_MARGIN_PCT),
            min_band_width_pct=data.get("min_band_width_pct", DEFAULT_MIN_BAND_WIDTH_PCT),
            min_daily_volume=data.get("min_daily_volume", DEFAULT_MIN_DAILY_VOLUME),
            min_price=data.get("min_price", DEFAULT_MIN_PRICE),
            max_price=data.get("max_price", DEFAULT_MAX_PRICE),
            stability_threshold_pct=data.get("stability_threshold_pct", DEFAULT_STABILITY_THRESHOLD),
            material_analysis_enabled=data.get("material_analysis_enabled", DEFAULT_MATERIAL_ANALYSIS_ENABLED),
            broker_fee_pct=data.get("broker_fee_pct", 3.0),
            sales_tax_pct=data.get("sales_tax_pct", 4.5),
        )
    
    def load_from_cached_skills(self, hub_key: str, slot: str = "seller"):
        """Load fee rates from cached skills JSON (no ESI calls).
        
        Args:
            hub_key: Hub key (e.g., 'amarr', 'jita')
            slot: "seller" or "buyer"
        """
        _check_thread(f"StockMarketFilters.load_from_cached_skills({hub_key})")
        skills = load_cached_skills(hub_key, slot)
        self.broker_fee_pct = get_broker_fee_rate(skills)
        self.sales_tax_pct = get_sales_tax_rate(skills)
        print(f"[Filters] Loaded for {hub_key}: broker={self.broker_fee_pct:.2f}%, tax={self.sales_tax_pct:.2f}%")
    
    def calculate_profit_per_unit(self, buy_price: float, sell_price: float) -> float:
        """Calculate expected profit per unit after all fees.
        
        For station trading (buy order -> sell order):
        - Pay broker fee on buy order
        - Pay broker fee on sell order
        - Pay sales tax on sale
        
        Args:
            buy_price: Price to buy at (floor/buy target)
            sell_price: Price to sell at (ceiling/sell target)
            
        Returns:
            Profit per unit in ISK (can be negative)
        """
        if buy_price <= 0 or sell_price <= 0:
            return 0.0
        
        broker_rate = self.broker_fee_pct / 100.0
        tax_rate = self.sales_tax_pct / 100.0
        
        # Fees
        buy_broker = buy_price * broker_rate
        sell_broker = sell_price * broker_rate
        sales_tax = sell_price * tax_rate
        
        total_fees = buy_broker + sell_broker + sales_tax
        gross_profit = sell_price - buy_price
        
        return gross_profit - total_fees
    
    def calculate_signal_profit(self, current_price: float, floor: float, ceiling: float) -> Optional[tuple]:
        """Calculate profit for buy/sell signal based on current price.
        
        Returns:
            (signal, profit_per_unit) or None if no signal
            signal is 'B' for buy, 'S' for sell
        """
        if current_price <= 0 or floor <= 0 or ceiling <= 0:
            return None
        
        if current_price < floor:
            # Buy signal - profit if we buy now and sell at ceiling
            profit = self.calculate_profit_per_unit(current_price, ceiling)
            return ('B', profit)
        elif current_price > ceiling:
            # Sell signal - profit if we bought at floor and sell now
            profit = self.calculate_profit_per_unit(floor, current_price)
            return ('S', profit)
        
        return None
    
    def get_total_fee_pct(self) -> float:
        """Get total round-trip fee percentage.
        
        For station trading:
        - Buy: broker fee on buy order
        - Sell: broker fee on sell order + sales tax
        
        Total = 2 * broker_fee + sales_tax
        """
        return (2 * self.broker_fee_pct) + self.sales_tax_pct
    
    def get_min_spread_pct(self) -> float:
        """Get minimum required spread to achieve target margin.
        
        If you buy at floor and sell at ceiling:
        profit = ceiling - floor - fees
        margin = profit / floor * 100
        
        To achieve min_margin_pct after fees:
        required_spread = min_margin_pct + total_fees
        """
        return self.min_margin_pct + self.get_total_fee_pct()


def load_filters() -> StockMarketFilters:
    """Load filters from disk, or return defaults."""
    if not FILTERS_FILE.exists():
        return StockMarketFilters()
    
    try:
        with open(FILTERS_FILE, "r") as f:
            data = json.load(f)
        return StockMarketFilters.from_dict(data)
    except Exception as e:
        print(f"[Filters] Error loading filters: {e}")
        return StockMarketFilters()


def save_filters(filters: StockMarketFilters) -> bool:
    """Save filters to disk."""
    try:
        FILTERS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FILTERS_FILE, "w") as f:
            json.dump(filters.to_dict(), f, indent=2)
        return True
    except Exception as e:
        print(f"[Filters] Error saving filters: {e}")
        return False


# =============================================================================
# Material Analysis Integration
# =============================================================================

# Cache for material analysis results (cleared on app restart)
_material_risk_cache: Dict[Tuple[int, int], str] = {}


def _mf_tag(hub_key: Optional[str], region_id: int) -> str:
    """Build a log prefix tag for material-filter output.

    When hub_key is supplied, includes it directly so concurrent hub
    runs are distinguishable in interleaved console output. Falls back
    to region_id when hub_key isn't known. Once custom stations land
    (multiple stations per region) hub_key becomes the only reliable
    discriminator, so callers should always pass it.
    """
    if hub_key:
        return f"[MaterialFilter:{hub_key}]"
    return f"[MaterialFilter:region={region_id}]"


def check_material_risk(
    type_id: int,
    region_id: int,
    recent_floor_cache: Optional[Dict[int, float]] = None,
    baseline_floor_cache: Optional[Dict[int, float]] = None,
    item_floor_recent_cache: Optional[Dict[int, float]] = None,
    item_floor_baseline_cache: Optional[Dict[int, float]] = None,
    hub_key: Optional[str] = None,
    persist: bool = True,
) -> str:
    """Check material correlation for risk adjustment.
    
    Results are cached per (type_id, region_id) for the session to avoid
    redundant analysis when multiple panels evaluate the same item.
    
    Args:
        type_id: Item type ID
        region_id: Region ID for item price lookup
        recent_floor_cache: Optional pre-computed {material_type_id: floor}
            dict for the recent (0-3 month) period.  Pass through to
            avoid per-material SQL queries.
        baseline_floor_cache: Optional pre-computed dict for the baseline
            (3-6 month) period.
        hub_key: Optional hub identifier (e.g. 'jita', 'amarr') used
            only for log tagging so concurrent hub runs are
            distinguishable in interleaved output.
        
    Returns:
        'low' - inputs stable, demand dip, good buy opportunity
        'medium' - inputs moving, wait for clarity
        'skip' - can't analyze (no blueprint, no dip, no data)
    """
    cache_key = (type_id, region_id)
    if cache_key in _material_risk_cache:
        return _material_risk_cache[cache_key]

    tag = _mf_tag(hub_key, region_id)

    try:
        from sde.sde_industry import get_sde_industry_db
        from analytics.material_analysis import analyze_material_dip
        
        # Check if industry data is available
        industry_db = get_sde_industry_db()
        if not industry_db.is_available():
            print(f"{tag} Industry data not available, skipping check for {type_id}")
            result = 'skip'
            _material_risk_cache[cache_key] = result
            if persist:
                material_risk_storage.save_entry(type_id, region_id, result)
            return result
        
        # Run analysis (passes pre-computed floor caches when provided)
        analysis = analyze_material_dip(
            type_id, region_id,
            recent_floor_cache=recent_floor_cache,
            baseline_floor_cache=baseline_floor_cache,
            item_floor_recent_cache=item_floor_recent_cache,
            item_floor_baseline_cache=item_floor_baseline_cache,
        )
        
        if analysis.classification == 'buy':
            # Inputs stable, item dipping = real demand dip = low risk
            print(f"{tag} {type_id}: BUY signal (item dip {analysis.item_dip_pct:.1f}%, TBC change {analysis.tbc_change_pct:.1f}%)")
            result = 'low'
        
        elif analysis.classification in ('wait', 'caution'):
            # Inputs moving = promote to medium risk
            print(f"{tag} {type_id}: {analysis.classification.upper()} (item dip {analysis.item_dip_pct:.1f}%, TBC change {analysis.tbc_change_pct:.1f}%) -> medium risk")
            result = 'medium'
        
        else:
            # no_blueprint, no_dip, no_data - can't analyze, trust existing filters
            result = 'skip'
        
        _material_risk_cache[cache_key] = result
        if persist:
            material_risk_storage.save_entry(type_id, region_id, result)
        return result

    except ImportError as e:
        print(f"{tag} Module not available: {e}")
        return 'skip'
    except Exception as e:
        print(f"{tag} Error analyzing {type_id}: {e}")
        return 'skip'


def clear_material_risk_cache_for_region(region_id: int, hub_key: Optional[str] = None):
    """Clear cached material risk results for a specific region.

    Called by apply_material_filter() before re-analyzing to flush
    stale 'skip' results that were cached from incomplete data.

    Also deletes today's persisted rows for this region so the next
    has_today_data() check correctly reports False — preventing the
    tracker from skipping a fresh rerun based on stale persisted data.

    Args:
        region_id: Region ID to clear.
        hub_key: Optional hub identifier for log tagging.
    """
    global _material_risk_cache
    keys_to_remove = [k for k in _material_risk_cache if k[1] == region_id]
    for k in keys_to_remove:
        del _material_risk_cache[k]
    tag = _mf_tag(hub_key, region_id)
    print(f"{tag} Cleared cache for region {region_id} "
          f"({len(keys_to_remove)} entries)")
    material_risk_storage.delete_today_for_region(region_id)
    data = _mf_load_run_times()
    from core.config import TRADE_HUBS
    hk = next((k for k, c in TRADE_HUBS.items() if c["region_id"] == region_id), None)
    if hk and hk in data:
        del data[hk]
        _mf_save_run_times(data)


def get_cached_material_risk(type_id: int, region_id: int):
    """Check material risk cache without triggering new analysis.

    Returns cached result ('low', 'medium', 'skip') or None if not
    cached.  Used by async display paths that should read pre-populated
    results but never trigger fresh analysis themselves.
    """
    return _material_risk_cache.get((type_id, region_id))


class StockMarketFilterEngine:
    """Applies filters to stock market items."""
    
    def __init__(self, filters: StockMarketFilters):
        self.filters = filters
        # Cache for material analysis results
        self._material_cache: Dict[int, str] = {}
    
    def clear_material_cache(self):
        """Clear cached material analysis results."""
        self._material_cache.clear()
    
    def get_material_risk(self, type_id: int, region_id: int) -> str:
        """Get material risk classification, using cache.
        
        Returns 'low', 'medium', or 'skip'.
        """
        if not self.filters.material_analysis_enabled:
            return 'skip'
        
        cache_key = type_id
        if cache_key in self._material_cache:
            return self._material_cache[cache_key]
        
        result = check_material_risk(type_id, region_id)
        self._material_cache[cache_key] = result
        return result
    
    def adjust_risk_for_materials(
        self,
        type_id: int,
        region_id: int,
        base_risk: str
    ) -> str:
        """Adjust risk category based on material analysis.
        
        Only promotes items from low -> medium. Never demotes.
        
        Args:
            type_id: Item type ID
            region_id: Region ID
            base_risk: Original risk category ('low', 'medium', 'high')
            
        Returns:
            Adjusted risk category
        """
        # Only check low risk items - we're looking to promote them to medium
        if base_risk != 'low':
            return base_risk
        
        material_risk = self.get_material_risk(type_id, region_id)
        
        if material_risk == 'medium':
            # Material analysis says inputs are moving - promote to medium
            return 'medium'
        
        # Either 'low' (confirmed buy) or 'skip' (can't analyze) - keep as low
        return base_risk
    
    def passes_filter(
        self,
        floor: float,
        ceiling: float,
        avg_daily_volume: float,
        current_price: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Check if an item passes all filters.
        
        Args:
            floor: Profile floor price (buy target)
            ceiling: Profile ceiling price (sell target)
            avg_daily_volume: Average daily volume
            current_price: Current market price (optional)
            
        Returns:
            (passes, reason) - True if passes, reason string if fails
        """
        # Price filters
        if self.filters.min_price > 0:
            check_price = current_price if current_price else floor
            if check_price < self.filters.min_price:
                return False, f"Price {check_price:,.0f} < min {self.filters.min_price:,.0f}"
        
        if self.filters.max_price > 0:
            check_price = current_price if current_price else ceiling
            if check_price > self.filters.max_price:
                return False, f"Price {check_price:,.0f} > max {self.filters.max_price:,.0f}"
        
        # Volume filter
        if avg_daily_volume < self.filters.min_daily_volume:
            return False, f"Volume {avg_daily_volume:.1f}/day < min {self.filters.min_daily_volume}"
        
        # Band width filter (spread between floor and ceiling)
        if floor > 0 and ceiling > 0:
            band_width_pct = ((ceiling - floor) / floor) * 100
            
            if band_width_pct < self.filters.min_band_width_pct:
                return False, f"Band {band_width_pct:.1f}% < min {self.filters.min_band_width_pct:.1f}%"
            
            # Margin filter (can we actually profit after fees?)
            min_required_spread = self.filters.get_min_spread_pct()
            if band_width_pct < min_required_spread:
                return False, f"Band {band_width_pct:.1f}% < required {min_required_spread:.1f}% (fees + margin)"
        
        return True, ""
    
    def filter_profiles(
        self,
        profiles: List[Any],
        live_prices: Dict[int, float] = None,
    ) -> List[Any]:
        """Filter a list of profiles, returning only those that pass.
        
        Args:
            profiles: List of ComputedProfile objects
            live_prices: Optional dict of type_id -> current price
            
        Returns:
            Filtered list of profiles
        """
        if live_prices is None:
            live_prices = {}
        
        passed = []
        for profile in profiles:
            floor = getattr(profile, 'weighted_floor', None) or getattr(profile, 'weighted_p_low', 0)
            ceiling = getattr(profile, 'weighted_ceiling', None) or getattr(profile, 'weighted_p_high', 0)
            volume = getattr(profile, 'avg_daily_volume', 0)
            current = live_prices.get(profile.type_id, 0)
            
            passes, reason = self.passes_filter(floor, ceiling, volume, current)
            if passes:
                passed.append(profile)
        
        return passed
    
    def get_filter_summary(self) -> str:
        """Get human-readable filter summary."""
        total_fees = self.filters.get_total_fee_pct()
        min_spread = self.filters.get_min_spread_pct()
        
        lines = [
            f"Broker Fee: {self.filters.broker_fee_pct:.2f}%",
            f"Sales Tax: {self.filters.sales_tax_pct:.2f}%",
            f"Total Fees: {total_fees:.2f}%",
            f"Min Margin: {self.filters.min_margin_pct:.1f}%",
            f"Required Spread: {min_spread:.1f}%",
            f"Min Volume: {self.filters.min_daily_volume}/day",
        ]
        
        if self.filters.min_price > 0:
            lines.append(f"Min Price: {self.filters.min_price:,.0f}")
        if self.filters.max_price > 0:
            lines.append(f"Max Price: {self.filters.max_price:,.0f}")
        
        if self.filters.material_analysis_enabled:
            lines.append("Material Analysis: ON")
        else:
            lines.append("Material Analysis: OFF")
        
        return "\n".join(lines)


# =============================================================================
# Material Filter Tracker (Once Per Hub Per Day)
# =============================================================================

_MF_RUN_TIMES_PATH = get_data_dir() / "mf_run_times.json"
_MF_MAX_AGE_SECONDS = 86400


def _mf_load_run_times() -> dict:
    try:
        with open(_MF_RUN_TIMES_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _mf_save_run_times(data: dict):
    try:
        _MF_RUN_TIMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MF_RUN_TIMES_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[MaterialFilter] Error saving run times: {e}")


def _mf_is_within_24h(iso_str: str) -> bool:
    try:
        ts = datetime.fromisoformat(iso_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() < _MF_MAX_AGE_SECONDS
    except Exception:
        return False


class MaterialFilterTracker:
    """Tracks which hubs have had material filter run today.
    
    Singleton pattern ensures consistent tracking across all components.
    Resets automatically on new day or app restart.
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ran_today: Set[str] = set()
        return cls._instance

    def should_run(self, hub_key: str) -> bool:
        """Return True if material filter should run for this hub.

        Uses a 24h rolling window (JSON timestamp file) instead of a
        calendar-day reset, so a midnight relaunch doesn't re-run MF
        an hour after the previous run.
        """
        if hub_key in self._ran_today:
            return False

        data = _mf_load_run_times()
        if hub_key in data and _mf_is_within_24h(data[hub_key]):
            self._ran_today.add(hub_key)
            ts = datetime.fromisoformat(data[hub_key])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            print(f"[MaterialFilter] {hub_key}: skipping (ran {age_h:.1f}h ago)")
            return False

        print(f"[MaterialFilter] {hub_key}: will run")
        return True

    def mark_complete(self, hub_key: str):
        """Mark hub as having completed material filter."""
        self._ran_today.add(hub_key)
        data = _mf_load_run_times()
        data[hub_key] = datetime.now(timezone.utc).isoformat()
        _mf_save_run_times(data)
        print(f"[MaterialFilter] {hub_key}: marked complete")

    def has_run(self, hub_key: str) -> bool:
        """Check if hub has run within the last 24h (no side effects)."""
        if hub_key in self._ran_today:
            return True
        data = _mf_load_run_times()
        return hub_key in data and _mf_is_within_24h(data[hub_key])

    def get_status(self) -> Dict[str, Any]:
        """Get current tracking status for debugging."""
        return {
            "last_run_times": _mf_load_run_times(),
            "hubs_completed_session": list(self._ran_today),
        }


# Singleton accessor
_material_tracker_instance: Optional[MaterialFilterTracker] = None


def get_material_filter_tracker() -> MaterialFilterTracker:
    """Get the global MaterialFilterTracker instance."""
    global _material_tracker_instance
    if _material_tracker_instance is None:
        _material_tracker_instance = MaterialFilterTracker()
    return _material_tracker_instance


_BURST_MAX_AGE_SECONDS = 86400  # 24 hours


class DailyHubBurstTracker:
    """Tracks whether the order burst has run for each hub within the last 24h.

    Cross-launch: uses the disk cache timestamp so a relaunch within 24h
    of the last pull skips the burst entirely.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ran_this_session: Set[str] = set()
        return cls._instance

    def should_run(self, hub_key: str, order_cache=None) -> bool:
        """Return True if this hub's cache is older than 24h and needs a pull."""
        if hub_key in self._ran_this_session:
            return False
        if order_cache is not None:
            from core.config import TRADE_HUBS
            region_id = TRADE_HUBS.get(hub_key, {}).get("region_id")
            if region_id:
                entry = order_cache._order_cache.get(region_id, {})
                ts = entry.get("timestamp")
                if ts:
                    age = (datetime.now(timezone.utc) - ts).total_seconds()
                    if age < _BURST_MAX_AGE_SECONDS:
                        self._ran_this_session.add(hub_key)
                        return False
        return True

    def mark_complete(self, hub_key: str):
        self._ran_this_session.add(hub_key)

    def all_complete(self, order_cache=None) -> bool:
        """Return True if every enabled hub has been pulled within 24h."""
        from core.config import TRADE_HUBS
        return not any(
            self.should_run(hk, order_cache)
            for hk, cfg in TRADE_HUBS.items()
            if cfg.get("enabled", True)
        )


_hub_burst_tracker_instance: Optional["DailyHubBurstTracker"] = None


def get_hub_burst_tracker() -> DailyHubBurstTracker:
    """Get the global DailyHubBurstTracker instance."""
    global _hub_burst_tracker_instance
    if _hub_burst_tracker_instance is None:
        _hub_burst_tracker_instance = DailyHubBurstTracker()
    return _hub_burst_tracker_instance
