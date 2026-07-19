"""Shared utilities for market scanners.

Contains:
- Deal dataclass
- ScanResult dataclass  
- Ceiling calculation logic
- History parsing helpers
- Risk flag evaluation
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from enum import Enum

from core.calculate import (
    TradingSkills, DEFAULT_SKILLS,
    calculate_break_even, calculate_profit_per_unit, calculate_margin_percent
)


# =============================================================================
# CONSTANTS
# =============================================================================

# Steal detection: buy/sell ratio above this = fat finger
STEAL_RATIO_THRESHOLD = 0.98  # sell within ~2% of buy

# Jita cap: ceiling can't exceed this % of Jita reference
JITA_CAP_PERCENT = 1.05  # 105%

# Market crash detection: 7d avg this much below 30d = crashing
CRASH_THRESHOLD_PERCENT = 0.10  # 10%

# Volume fraction for estimated flip
VOLUME_CAP_FRACTION = 0.30  # 30% of daily volume

# Ceiling cap warning: if ceiling was reduced by this much, flag it
CEILING_CAP_WARNING_PERCENT = 0.20  # 20%


# =============================================================================
# ENUMS
# =============================================================================

class StealColor(Enum):
    """Color coding for steal risk level."""
    GREEN = "green"   # 0 risk flags
    YELLOW = "yellow" # 1 risk flag
    RED = "red"       # 2+ risk flags


class RiskFlag(Enum):
    """Individual risk factors."""
    LOW_VELOCITY = "low_velocity"
    NO_JITA_DATA = "no_jita_data"
    MARKET_CRASHING = "market_crashing"
    CEILING_CAPPED_HARD = "ceiling_capped_hard"
    SPORADIC_TRADING = "sporadic_trading"
    BUY_ABOVE_AVERAGE = "buy_above_average"  # Buy price > local conservative avg (lower of 7d/30d)
    # Cross-hub specific flags
    ABOVE_SELL_AVG = "above_sell_avg"      # Buy price > sell station conservative avg (1 strike)
    ABOVE_JITA_AVG = "above_jita_avg"      # Buy price > Jita conservative avg (2 strikes)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class HistoryStats:
    """Parsed historical data for an item."""
    avg_price_7d: float = 0.0
    avg_price_30d: float = 0.0
    avg_volume_7d: float = 0.0
    avg_volume_30d: float = 0.0
    trading_days_30d: int = 0
    # Fallback price from most recent trade (used when no recent trades)
    fallback_price: float = 0.0
    
    @property
    def safe_velocity(self) -> float:
        """Conservative velocity = lower of 7d or 30d."""
        if self.avg_volume_7d > 0 and self.avg_volume_30d > 0:
            return min(self.avg_volume_7d, self.avg_volume_30d)
        return max(self.avg_volume_7d, self.avg_volume_30d)
    
    @property
    def optimistic_price(self) -> float:
        """Higher of 7d/30d avg for ceiling calc. Falls back to fallback_price if both zero."""
        if self.avg_price_7d > 0 and self.avg_price_30d > 0:
            return max(self.avg_price_7d, self.avg_price_30d)
        result = max(self.avg_price_7d, self.avg_price_30d)
        if result == 0:
            return self.fallback_price
        return result
    
    @property
    def conservative_price(self) -> float:
        """Lower of 7d/30d avg for Jita cap. Falls back to fallback_price if both zero."""
        if self.avg_price_7d > 0 and self.avg_price_30d > 0:
            return min(self.avg_price_7d, self.avg_price_30d)
        result = max(self.avg_price_7d, self.avg_price_30d)
        if result == 0:
            return self.fallback_price
        return result
    
    @property
    def is_crashing(self) -> bool:
        """True if 7d avg is 10%+ below 30d avg."""
        if self.avg_price_7d > 0 and self.avg_price_30d > 0:
            drop = (self.avg_price_30d - self.avg_price_7d) / self.avg_price_30d
            return drop >= CRASH_THRESHOLD_PERCENT
        return False


@dataclass
class Candidate:
    """Raw candidate before category processing."""
    type_id: int
    system_id: int
    local_sell: float       # Lowest sell price (what you pay)
    local_sell_2nd: float   # 2nd lowest (competition to undercut)
    local_buy: float        # Highest buy order
    jita_sell: float        # Live Jita sell price
    volume: int             # Volume at lowest sell
    
    @property
    def steal_ratio(self) -> float:
        """Buy/sell ratio. Higher = tighter spread = more likely fat finger."""
        if self.local_sell > 0 and self.local_buy > 0:
            return self.local_buy / self.local_sell
        return 0.0
    
    @property
    def is_steal(self) -> bool:
        """True if this looks like a fat finger mistake."""
        return self.steal_ratio >= STEAL_RATIO_THRESHOLD


@dataclass
class Deal:
    """A processed trading opportunity ready for display."""
    type_id: int
    name: str
    system_id: int
    system_name: str
    
    # Prices
    buy_price: float        # What you pay (local_sell)
    ceiling_price: float    # Your relist target
    break_even: float       # Minimum to not lose money
    local_buy: float        # Highest buy order (for reference)
    jita_sell: float        # Live Jita price (for reference)
    
    # Profit metrics
    gross_profit: float     # ceiling - buy, before fees
    net_profit: float       # Per unit after fees
    total_profit: float     # net_profit * effective_volume
    margin_percent: float   # net_profit / buy_price * 100
    
    # Volume
    volume: int             # Effective volume (capped by velocity)
    raw_volume: int         # Actual volume available
    
    # History stats
    avg_price_7d: float = 0.0
    avg_price_30d: float = 0.0
    avg_volume_7d: float = 0.0
    avg_volume_30d: float = 0.0
    trading_days_30d: int = 0
    
    # Risk info
    steal_ratio: float = 0.0
    risk_flags: list = field(default_factory=list)
    steal_color: Optional[StealColor] = None
    
    # For compatibility with old code
    local_sell: float = 0.0
    local_sell_2nd: float = 0.0
    jita_sell_2nd: float = 0.0
    
    @property
    def days_to_sell(self) -> float:
        """Estimated days to flip using conservative velocity."""
        safe_vel = min(self.avg_volume_7d, self.avg_volume_30d) if self.avg_volume_7d > 0 and self.avg_volume_30d > 0 else max(self.avg_volume_7d, self.avg_volume_30d)
        if safe_vel > 0:
            return self.volume / safe_vel
        return float("inf")
    
    @property
    def total_cost(self) -> float:
        """Total ISK to buy the volume."""
        return self.buy_price * self.volume


@dataclass
class ScanResult:
    """Result of a market scan with categorized deals."""
    steals: list[Deal]
    low_risk: list[Deal]
    high_risk: list[Deal]
    local_orders: list[dict]  # Raw orders (full region)
    local_orders_filtered: list[dict]  # High-sec only orders


# =============================================================================
# HISTORY PARSING
# =============================================================================

def parse_history_stats(history: list[dict], reference_date: str = None) -> HistoryStats:
    """
    Parse ESI history into stats object.
    
    Uses calendar date filtering to ensure we only average recent data,
    not trades from months/years ago.
    
    Args:
        history: List of daily records from ESI/bulk
                 Each record: {date, average, highest, lowest, volume, order_count}
        reference_date: Date string (YYYY-MM-DD) to use as "today" for filtering.
                       If None, uses current date. Use cache date for bulk data.
    
    Returns:
        HistoryStats with calculated averages
    """
    if not history:
        return HistoryStats()
    
    # Determine reference date (use cache date or today)
    if reference_date:
        try:
            ref_date = datetime.strptime(reference_date, "%Y-%m-%d").date()
        except ValueError:
            ref_date = datetime.now().date()
    else:
        ref_date = datetime.now().date()
    
    # Calculate cutoff dates
    cutoff_30 = (ref_date - timedelta(days=30)).isoformat()
    cutoff_7 = (ref_date - timedelta(days=7)).isoformat()
    
    # Sort by date descending (newest first)
    sorted_hist = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
    
    # Filter to calendar windows
    recent_30 = [h for h in sorted_hist if h.get("date", "") >= cutoff_30]
    recent_7 = [h for h in sorted_hist if h.get("date", "") >= cutoff_7]
    
    # Get fallback price from most recent trade (regardless of date)
    fallback_price = 0.0
    if sorted_hist:
        fallback_price = sorted_hist[0].get("average", 0)
    
    # 30-day calculations
    if recent_30:
        # Volume: divide by calendar days (30), not record count
        total_volume_30 = sum(h.get("volume", 0) for h in recent_30)
        avg_volume_30d = total_volume_30 / 30
        
        # Price: volume-weighted average (only actual transactions count)
        if total_volume_30 > 0:
            avg_price_30d = sum(h.get("average", 0) * h.get("volume", 0) for h in recent_30) / total_volume_30
        else:
            avg_price_30d = 0
        
        # Trading days: count of days with actual trades in the window
        trading_days_30d = len(recent_30)
    else:
        avg_price_30d = 0
        avg_volume_30d = 0
        trading_days_30d = 0
    
    # 7-day calculations
    if recent_7:
        # Volume: divide by calendar days (7), not record count
        total_volume_7 = sum(h.get("volume", 0) for h in recent_7)
        avg_volume_7d = total_volume_7 / 7
        
        # Price: volume-weighted average
        if total_volume_7 > 0:
            avg_price_7d = sum(h.get("average", 0) * h.get("volume", 0) for h in recent_7) / total_volume_7
        else:
            avg_price_7d = 0
    else:
        avg_price_7d = 0
        avg_volume_7d = 0
    
    return HistoryStats(
        avg_price_7d=avg_price_7d,
        avg_price_30d=avg_price_30d,
        avg_volume_7d=avg_volume_7d,
        avg_volume_30d=avg_volume_30d,
        trading_days_30d=trading_days_30d,
        fallback_price=fallback_price
    )


# =============================================================================
# CEILING CALCULATION
# =============================================================================

def calculate_ceiling(
    candidate: Candidate,
    local_stats: HistoryStats,
    jita_stats: HistoryStats,
    skills: TradingSkills = None
) -> tuple[float, list[RiskFlag]]:
    """
    Calculate safe ceiling price for relisting.
    
    Logic:
    1. Start with local 2nd lowest sell (competition to undercut)
    2. Validate against higher of local 7d/30d avg
    3. Cap at 105% of lower Jita 7d/30d avg
    4. Cap at 105% of live Jita price (hard safety)
    
    Args:
        candidate: Raw candidate data
        local_stats: Amarr history stats
        jita_stats: Jita history stats
        skills: For break-even calculation
    
    Returns:
        (ceiling_price, list of risk flags triggered)
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    flags = []
    
    # Start with competition price (undercut by 0.1% or 0.01 ISK)
    undercut = max(0.01, candidate.local_sell_2nd * 0.001)
    ceiling = candidate.local_sell_2nd - undercut
    
    # Track original for cap warning
    original_ceiling = ceiling
    
    # Use local optimistic price as reference (higher of 7d/30d)
    local_ref = local_stats.optimistic_price
    
    # If competition is way below historical, don't inflate - flag it
    # (This replaces the old BOTTOM FALLBACK that inflated prices)
    if local_ref > 0 and ceiling < (local_ref * 0.90):
        flags.append(RiskFlag.MARKET_CRASHING)
    
    # Jita conservative reference (lower of 7d/30d)
    jita_ref = jita_stats.conservative_price
    
    # Cap at 105% of Jita historical average
    if jita_ref > 0:
        jita_cap = jita_ref * JITA_CAP_PERCENT
        ceiling = min(ceiling, jita_cap)
    else:
        # No Jita history data
        flags.append(RiskFlag.NO_JITA_DATA)
    
    # Hard cap at 105% of live Jita price
    if candidate.jita_sell > 0:
        live_cap = candidate.jita_sell * JITA_CAP_PERCENT
        ceiling = min(ceiling, live_cap)
    else:
        # No live Jita price
        if RiskFlag.NO_JITA_DATA not in flags:
            flags.append(RiskFlag.NO_JITA_DATA)
    
    # Check if ceiling was capped hard (reduced by 20%+)
    if original_ceiling > 0 and ceiling < (original_ceiling * (1 - CEILING_CAP_WARNING_PERCENT)):
        flags.append(RiskFlag.CEILING_CAPPED_HARD)
    
    return ceiling, flags


# =============================================================================
# RISK EVALUATION
# =============================================================================

def evaluate_risk_flags(
    local_stats: HistoryStats,
    jita_stats: HistoryStats,
    min_velocity: float,
    existing_flags: list[RiskFlag] = None,
    buy_price: float = 0.0
) -> list[RiskFlag]:
    """
    Evaluate all risk flags for a candidate.
    
    Args:
        local_stats: Amarr history
        jita_stats: Jita history
        min_velocity: Minimum daily volume threshold
        existing_flags: Flags already set (e.g., from ceiling calc)
        buy_price: The price we'd pay (candidate.local_sell)
    
    Returns:
        Complete list of risk flags
    """
    flags = list(existing_flags) if existing_flags else []
    
    # Low velocity
    if local_stats.safe_velocity < min_velocity:
        if RiskFlag.LOW_VELOCITY not in flags:
            flags.append(RiskFlag.LOW_VELOCITY)
    
    # Market crashing (7d > 10% below 30d)
    if local_stats.is_crashing:
        if RiskFlag.MARKET_CRASHING not in flags:
            flags.append(RiskFlag.MARKET_CRASHING)
    
    # Sporadic trading (fewer than 15 of 30 days)
    if local_stats.trading_days_30d < 15:
        if RiskFlag.SPORADIC_TRADING not in flags:
            flags.append(RiskFlag.SPORADIC_TRADING)
    
    # No Jita data (check both history and live)
    if jita_stats.avg_price_30d == 0 and jita_stats.avg_price_7d == 0:
        if RiskFlag.NO_JITA_DATA not in flags:
            flags.append(RiskFlag.NO_JITA_DATA)
    
    # Buy price above local historical average (conservative = lower of 7d/30d)
    if buy_price > 0 and local_stats.conservative_price > 0:
        if buy_price > local_stats.conservative_price:
            if RiskFlag.BUY_ABOVE_AVERAGE not in flags:
                flags.append(RiskFlag.BUY_ABOVE_AVERAGE)
    
    return flags


def get_steal_color(flags: list[RiskFlag]) -> StealColor:
    """
    Determine steal color based on risk flag count.
    
    Args:
        flags: List of risk flags
    
    Returns:
        GREEN (0 flags), YELLOW (1 flag), or RED (2+ flags)
    """
    count = len(flags)
    if count == 0:
        return StealColor.GREEN
    elif count == 1:
        return StealColor.YELLOW
    else:
        return StealColor.RED


# =============================================================================
# DEAL BUILDING
# =============================================================================

def build_deal(
    candidate: Candidate,
    name: str,
    system_name: str,
    ceiling: float,
    local_stats: HistoryStats,
    jita_stats: HistoryStats,
    risk_flags: list[RiskFlag],
    skills: TradingSkills = None,
    is_steal: bool = False
) -> Deal:
    """
    Build a Deal object from processed data.
    
    Args:
        candidate: Raw candidate
        name: Item name
        system_name: System name
        ceiling: Calculated ceiling price
        local_stats: Local hub history
        jita_stats: Jita history (for reference)
        risk_flags: Risk flags for this deal
        skills: For fee calculations
        is_steal: Whether this is a steal
    
    Returns:
        Fully populated Deal object
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    buy_price = candidate.local_sell
    
    # Calculate profits
    gross_profit = ceiling - buy_price
    net_profit = calculate_profit_per_unit(buy_price, ceiling, skills)
    margin_pct = calculate_margin_percent(buy_price, ceiling, skills)
    break_even = calculate_break_even(buy_price, 1, 0.0, skills)
    
    # Cap volume by velocity (30% of daily volume)
    safe_velocity = local_stats.safe_velocity
    if safe_velocity > 0:
        effective_volume = min(candidate.volume, int(safe_velocity * VOLUME_CAP_FRACTION))
        effective_volume = max(1, effective_volume)
    else:
        effective_volume = candidate.volume
    
    total_profit = net_profit * effective_volume
    
    # Steal color if applicable
    steal_color = get_steal_color(risk_flags) if is_steal else None
    
    return Deal(
        type_id=candidate.type_id,
        name=name,
        system_id=candidate.system_id,
        system_name=system_name,
        buy_price=buy_price,
        ceiling_price=ceiling,
        break_even=break_even,
        local_buy=candidate.local_buy,
        jita_sell=candidate.jita_sell,
        gross_profit=gross_profit,
        net_profit=net_profit,
        total_profit=total_profit,
        margin_percent=margin_pct,
        volume=effective_volume,
        raw_volume=candidate.volume,
        avg_price_7d=local_stats.avg_price_7d,
        avg_price_30d=local_stats.avg_price_30d,
        avg_volume_7d=local_stats.avg_volume_7d,
        avg_volume_30d=local_stats.avg_volume_30d,
        trading_days_30d=local_stats.trading_days_30d,
        steal_ratio=candidate.steal_ratio,
        risk_flags=risk_flags,
        steal_color=steal_color,
        # Compatibility fields
        local_sell=buy_price,
        local_sell_2nd=candidate.local_sell_2nd,
        jita_sell_2nd=candidate.jita_sell,
    )


# =============================================================================
# FILTER HELPERS
# =============================================================================

def passes_profit_filters(
    deal: Deal,
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float
) -> bool:
    """
    Check if a deal passes the profit-based filters.
    
    Args:
        deal: The deal to check
        min_profit_per_unit: Minimum ISK profit per unit
        min_total_profit: Minimum total ISK profit
        min_margin_percent: Minimum margin percentage
    
    Returns:
        True if passes all filters
    """
    if deal.net_profit < min_profit_per_unit:
        return False
    
    if deal.total_profit < min_total_profit:
        return False
    
    if min_margin_percent > 0 and deal.margin_percent < min_margin_percent:
        return False
    
    return True
