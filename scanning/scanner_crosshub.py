"""Cross-Hub Arbitrage Scanner for EVE Market Scout.

Handles buying in one station and selling in another:
- Low Risk: Guaranteed profit - sell station has buy order covering purchase + fees
- High Risk: No guaranteed buyer - relisting at sell station with velocity risk

Does NOT handle Steals - those are same-station only (fat-finger detection).
"""

from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from core.calculate import (
    TradingSkills, DEFAULT_SKILLS,
    calculate_arbitrage_profit, get_broker_fee_rate, get_sales_tax_rate
)
from scanning.scanner_common import (
    Candidate, Deal, HistoryStats, RiskFlag, StealColor,
    parse_history_stats, VOLUME_CAP_FRACTION, JITA_CAP_PERCENT
)


# =============================================================================
# CROSS-HUB SPECIFIC STRUCTURES
# =============================================================================

@dataclass
class CrossHubCandidate:
    """Candidate for cross-hub arbitrage."""
    type_id: int
    
    # Buy station data (source)
    buy_station_sell: float      # Lowest sell order at buy station (what we pay)
    buy_station_sell_volume: int # Volume available
    buy_station_system_id: int
    
    # Sell station data (destination)
    sell_station_buy: float       # Highest buy order at sell station (guaranteed sale)
    sell_station_buy_volume: int  # Volume wanted by buy order
    sell_station_sell: float      # Lowest sell at destination (for relisting)
    sell_station_sell_2nd: float  # 2nd lowest (competition to undercut)
    sell_station_system_id: int


@dataclass
class CrossHubDeal:
    """A cross-hub arbitrage opportunity."""
    type_id: int
    name: str
    
    # Stations
    buy_station: str
    sell_station: str
    buy_system_name: str
    sell_system_name: str
    
    # Prices
    buy_price: float          # What we pay at source
    sell_price: float         # What we sell for (buy order or relist target)
    break_even: float         # Minimum sell price to not lose money
    
    # Profit metrics (accounts for both characters' fees)
    net_profit: float         # Per unit after all fees
    total_profit: float       # net_profit * volume
    margin_percent: float     # net_profit / buy_price * 100
    
    # Volume
    volume: int               # Effective volume (limited by buyer demand or velocity)
    raw_volume: int           # Actual volume available at source
    guaranteed_volume: int    # Volume guaranteed by buy orders (Low Risk)
    
    # Costs breakdown
    buy_broker_fee: float     # Buyer's broker fee (0 for instant buy)
    sell_broker_fee: float    # Seller's broker fee
    sales_tax: float          # Seller's sales tax
    total_fees: float
    
    # History at SELL station (for velocity)
    avg_volume_7d: float = 0.0
    avg_volume_30d: float = 0.0
    avg_price_7d: float = 0.0
    avg_price_30d: float = 0.0
    trading_days_30d: int = 0
    
    # History at BUY station (for dual-row GUI display)
    buy_avg_volume_7d: float = 0.0
    buy_avg_volume_30d: float = 0.0
    buy_avg_price_7d: float = 0.0
    buy_avg_price_30d: float = 0.0
    buy_trading_days_30d: int = 0
    
    # Risk info
    is_guaranteed: bool = False  # True if selling to buy order (Low Risk)
    risk_flags: list = field(default_factory=list)
    
    # Destination buy order (for GUI display)
    sell_station_buy: float = 0.0
    
    # Source buy order (for GUI display)
    buy_station_buy: float = 0.0
    
    # =================================================================
    # Deal-compatible properties for GUI display
    # =================================================================
    
    @property
    def system_name(self) -> str:
        """For GUI compatibility - returns sell station name."""
        return self.sell_system_name
    
    @property
    def system_id(self) -> int:
        """For GUI compatibility."""
        return 0  # Not tracked for cross-hub
    
    @property
    def ceiling_price(self) -> float:
        """For GUI compatibility - same as sell_price."""
        return self.sell_price
    
    @property
    def local_buy(self) -> float:
        """For GUI compatibility - returns buy order at sell station."""
        return self.sell_station_buy
    
    @property
    def local_sell(self) -> float:
        """For GUI compatibility."""
        return self.buy_price
    
    @property
    def local_sell_2nd(self) -> float:
        """For GUI compatibility."""
        return 0.0
    
    @property
    def jita_sell(self) -> float:
        """For GUI compatibility."""
        return 0.0
    
    @property
    def jita_sell_2nd(self) -> float:
        """For GUI compatibility."""
        return 0.0
    
    @property
    def gross_profit(self) -> float:
        """For GUI compatibility."""
        return self.sell_price - self.buy_price
    
    @property
    def steal_ratio(self) -> float:
        """For GUI compatibility - not applicable."""
        return 0.0
    
    @property
    def steal_color(self):
        """For GUI compatibility - not applicable."""
        return None
    
    # =================================================================
    # Actual CrossHubDeal methods
    # =================================================================
    
    @property
    def days_to_sell(self) -> float:
        """Estimated days to flip (only meaningful for High Risk)."""
        safe_vel = min(self.avg_volume_7d, self.avg_volume_30d) if self.avg_volume_7d > 0 and self.avg_volume_30d > 0 else max(self.avg_volume_7d, self.avg_volume_30d)
        if safe_vel > 0:
            return self.volume / safe_vel
        return float("inf")
    
    @property
    def total_cost(self) -> float:
        """Total ISK to buy the volume."""
        return self.buy_price * self.volume


# =============================================================================
# CANDIDATE BUILDING
# =============================================================================

def build_crosshub_candidates(
    buy_station_data: dict[int, dict],
    sell_station_data: dict[int, dict],
    max_cost: float = None
) -> list[CrossHubCandidate]:
    """
    Build cross-hub candidates from two stations' order data.
    
    Args:
        buy_station_data: Processed orders from source station
                          {type_id: {sell, sell_2nd, buy, volume, system_id}}
        sell_station_data: Processed orders from destination station
        max_cost: Maximum cost filter
    
    Returns:
        List of CrossHubCandidate objects
    """
    candidates = []
    
    for type_id, buy_info in buy_station_data.items():
        buy_sell = buy_info.get("sell", float("inf"))
        buy_volume = buy_info.get("volume", 0)
        buy_system_id = buy_info.get("system_id", 0)
        
        # Must have valid sell order at buy station
        if buy_sell == float("inf") or buy_volume == 0:
            continue
        
        # Get sell station data
        sell_info = sell_station_data.get(type_id)
        if not sell_info:
            continue
        
        sell_buy = sell_info.get("buy", 0)
        sell_buy_volume = sell_info.get("buy_volume", 0)  # Need to track this
        sell_sell = sell_info.get("sell", float("inf"))
        sell_sell_2nd = sell_info.get("sell_2nd", float("inf"))
        sell_system_id = sell_info.get("system_id", 0)
        
        # Must have either a buy order OR sell orders at destination
        if sell_buy == 0 and sell_sell == float("inf"):
            continue
        
        # Max cost filter
        total_cost = buy_sell * buy_volume
        if max_cost is not None and total_cost > max_cost:
            continue
        
        candidates.append(CrossHubCandidate(
            type_id=type_id,
            buy_station_sell=buy_sell,
            buy_station_sell_volume=buy_volume,
            buy_station_system_id=buy_system_id,
            sell_station_buy=sell_buy,
            sell_station_buy_volume=sell_buy_volume,
            sell_station_sell=sell_sell,
            sell_station_sell_2nd=sell_sell_2nd,
            sell_station_system_id=sell_system_id,
        ))
    
    return candidates


# =============================================================================
# LOW RISK (GUARANTEED PROFIT)
# =============================================================================

def process_crosshub_low_risk(
    candidates: list[CrossHubCandidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    sell_station_history: dict[int, list[dict]],
    buy_station_history: dict[int, list[dict]],
    buy_station_data: dict[int, dict],
    buy_station_key: str,
    sell_station_key: str,
    buy_skills: TradingSkills,
    sell_skills: TradingSkills,
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_guaranteed_volume: int = 5,  # Minimum buy order volume at destination
    reference_date: Optional[str] = None,
) -> list[CrossHubDeal]:
    """
    Find guaranteed profit cross-hub deals.
    
    Low Risk criteria:
    - Sell station has buy order >= buy price + all fees
    - Buy order volume >= min_guaranteed_volume (default 5)
    
    Args:
        candidates: CrossHubCandidate list
        names: type_id -> name
        system_cache: system_id -> {name, security}
        sell_station_history: History at sell station (for reference)
        buy_station_history: History at buy station (for dual-row display)
        buy_station_data: Buy station order data (for buy order info)
        buy_station_key: Hub key for buy station (e.g., "jita")
        sell_station_key: Hub key for sell station (e.g., "amarr")
        buy_skills: Buyer character's skills (with buy station standings)
        sell_skills: Seller character's skills (with sell station standings)
        min_profit_per_unit: Filter
        min_total_profit: Filter
        min_margin_percent: Filter
        min_guaranteed_volume: Minimum buy order volume to consider
        reference_date: History DB's latest date (YYYY-MM-DD) — anchors the
                        7d/30d windows so a lagging DB doesn't deflate them

    Returns:
        List of CrossHubDeal (guaranteed profit deals)
    """
    deals = []
    
    for candidate in candidates:
        # Must have buy order at destination
        if candidate.sell_station_buy <= 0:
            continue
        
        # Must have enough volume wanted
        if candidate.sell_station_buy_volume < min_guaranteed_volume:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        
        buy_sys_info = system_cache.get(candidate.buy_station_system_id, {})
        sell_sys_info = system_cache.get(candidate.sell_station_system_id, {})
        buy_system_name = buy_sys_info.get("name", "Unknown")
        sell_system_name = sell_sys_info.get("name", "Unknown")
        
        # Calculate profit with both characters' fees
        # Volume limited by: what's available AND what buyer wants
        effective_volume = min(
            candidate.buy_station_sell_volume,
            candidate.sell_station_buy_volume
        )
        
        arb = calculate_arbitrage_profit(
            buy_price=candidate.buy_station_sell,
            sell_price=candidate.sell_station_buy,
            quantity=effective_volume,
            buy_skills=buy_skills,
            sell_skills=sell_skills,
            buy_is_instant=True,  # Buying from sell order
        )
        
        # Must be profitable
        if arb["profit_per_unit"] < min_profit_per_unit:
            continue
        if arb["net_profit"] < min_total_profit:
            continue
        if min_margin_percent > 0 and arb["margin_percent"] < min_margin_percent:
            continue
        
        # Get sell station history for reference
        sell_stats = parse_history_stats(
            sell_station_history.get(type_id, []), reference_date
        )

        # Get buy station history for dual-row display
        buy_stats = parse_history_stats(
            buy_station_history.get(type_id, []), reference_date
        )
        
        # Get buy station buy order for display
        buy_station_info = buy_station_data.get(type_id, {})
        buy_station_buy_price = buy_station_info.get("buy", 0)
        
        deal = CrossHubDeal(
            type_id=type_id,
            name=name,
            buy_station=buy_station_key,
            sell_station=sell_station_key,
            buy_system_name=buy_system_name,
            sell_system_name=sell_system_name,
            buy_price=candidate.buy_station_sell,
            sell_price=candidate.sell_station_buy,
            break_even=arb["break_even_sell"],
            net_profit=arb["profit_per_unit"],
            total_profit=arb["net_profit"],
            margin_percent=arb["margin_percent"],
            volume=effective_volume,
            raw_volume=candidate.buy_station_sell_volume,
            guaranteed_volume=candidate.sell_station_buy_volume,
            buy_broker_fee=arb["buy_broker_fee"],
            sell_broker_fee=arb["sell_broker_fee"],
            sales_tax=arb["sales_tax"],
            total_fees=arb["total_fees"],
            avg_volume_7d=sell_stats.avg_volume_7d,
            avg_volume_30d=sell_stats.avg_volume_30d,
            avg_price_7d=sell_stats.avg_price_7d,
            avg_price_30d=sell_stats.avg_price_30d,
            trading_days_30d=sell_stats.trading_days_30d,
            buy_avg_volume_7d=buy_stats.avg_volume_7d,
            buy_avg_volume_30d=buy_stats.avg_volume_30d,
            buy_avg_price_7d=buy_stats.avg_price_7d,
            buy_avg_price_30d=buy_stats.avg_price_30d,
            buy_trading_days_30d=buy_stats.trading_days_30d,
            is_guaranteed=True,
            risk_flags=[],
            sell_station_buy=candidate.sell_station_buy,
            buy_station_buy=buy_station_buy_price,
        )
        
        deals.append(deal)
    
    # Sort by total profit
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    return deals


# =============================================================================
# HIGH RISK (RELIST AT DESTINATION)
# =============================================================================

def process_crosshub_high_risk(
    candidates: list[CrossHubCandidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    sell_station_history: dict[int, list[dict]],
    buy_station_history: dict[int, list[dict]],
    jita_history: dict[int, list[dict]],
    buy_station_data: dict[int, dict],
    buy_station_key: str,
    sell_station_key: str,
    buy_skills: TradingSkills,
    sell_skills: TradingSkills,
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    reference_date: Optional[str] = None,
) -> list[CrossHubDeal]:
    """
    Find high risk cross-hub deals (relist at destination).
    
    High Risk = profitable but no guaranteed buyer:
    - Either no buy order, or buy order doesn't cover fees
    - Must relist as sell order at destination
    - Velocity checked at sell station
    
    Risk flags:
    - LOW_VELOCITY: Sell station velocity below min (1 strike)
    - MARKET_CRASHING: Sell station 7d 10%+ below 30d (1 strike)
    - SPORADIC_TRADING: Sell station < 15 trading days (1 strike)
    - ABOVE_SELL_AVG: Buy price > sell station conservative avg (1 strike)
    - ABOVE_JITA_AVG: Buy price > Jita conservative avg (2 strikes)
    
    Args:
        candidates: CrossHubCandidate list
        names: type_id -> name
        system_cache: system_id -> {name, security}
        sell_station_history: History at sell station (for velocity + ceiling)
        buy_station_history: History at buy station (for dual-row display)
        jita_history: Jita history (for ceiling cap + price validation)
        buy_station_data: Buy station order data (for buy order info)
        buy_station_key: Hub key for buy station
        sell_station_key: Hub key for sell station
        buy_skills: Buyer's skills
        sell_skills: Seller's skills
        min_profit_per_unit: Filter
        min_total_profit: Filter
        min_margin_percent: Filter
        min_velocity: Minimum daily volume at sell station
        reference_date: History DB's latest date (YYYY-MM-DD) — anchors the
                        7d/30d windows so a lagging DB doesn't deflate them

    Returns:
        List of CrossHubDeal (high risk deals)
    """
    deals = []
    
    for candidate in candidates:
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        
        # Get sell station history - essential for High Risk
        sell_stats = parse_history_stats(
            sell_station_history.get(type_id, []), reference_date
        )

        # Get Jita history for ceiling cap and price validation
        jita_stats = parse_history_stats(
            jita_history.get(type_id, []), reference_date
        )
        
        # Calculate ceiling at sell station (undercut competition)
        if candidate.sell_station_sell_2nd < float("inf"):
            undercut = max(0.01, candidate.sell_station_sell_2nd * 0.001)
            ceiling = candidate.sell_station_sell_2nd - undercut
        elif candidate.sell_station_sell < float("inf"):
            # Only one seller - undercut them
            undercut = max(0.01, candidate.sell_station_sell * 0.001)
            ceiling = candidate.sell_station_sell - undercut
        else:
            # No sell orders at destination - use history or skip
            if sell_stats.optimistic_price > 0:
                ceiling = sell_stats.optimistic_price
            else:
                continue  # Can't determine ceiling
        
        # Cap ceiling at 105% of Jita conservative avg (lower of 7d/30d)
        jita_conservative = jita_stats.conservative_price
        if jita_conservative > 0:
            jita_cap = jita_conservative * JITA_CAP_PERCENT
            ceiling = min(ceiling, jita_cap)
        
        # Build risk flags
        risk_flags = []
        
        # Buy price vs sell station conservative avg (1 strike)
        sell_conservative = sell_stats.conservative_price
        if sell_conservative > 0 and candidate.buy_station_sell > sell_conservative:
            risk_flags.append(RiskFlag.ABOVE_SELL_AVG)
        
        # Buy price vs Jita conservative avg (2 strikes - handled in GUI color calc)
        if jita_conservative > 0 and candidate.buy_station_sell > jita_conservative:
            risk_flags.append(RiskFlag.ABOVE_JITA_AVG)
        
        # Velocity check at SELL station
        safe_velocity = sell_stats.safe_velocity
        if safe_velocity < min_velocity:
            risk_flags.append(RiskFlag.LOW_VELOCITY)
        
        # Market crashing
        if sell_stats.is_crashing:
            risk_flags.append(RiskFlag.MARKET_CRASHING)
        
        # Sporadic trading
        if sell_stats.trading_days_30d < 15:
            risk_flags.append(RiskFlag.SPORADIC_TRADING)
        
        # Volume cap based on velocity
        if safe_velocity > 0:
            effective_volume = min(
                candidate.buy_station_sell_volume,
                int(safe_velocity * VOLUME_CAP_FRACTION)
            )
            effective_volume = max(1, effective_volume)
        else:
            effective_volume = candidate.buy_station_sell_volume
        
        # Calculate profit
        arb = calculate_arbitrage_profit(
            buy_price=candidate.buy_station_sell,
            sell_price=ceiling,
            quantity=effective_volume,
            buy_skills=buy_skills,
            sell_skills=sell_skills,
            buy_is_instant=True,
        )
        
        # Must be profitable
        if arb["profit_per_unit"] < min_profit_per_unit:
            continue
        if arb["net_profit"] < min_total_profit:
            continue
        if min_margin_percent > 0 and arb["margin_percent"] < min_margin_percent:
            continue
        
        buy_sys_info = system_cache.get(candidate.buy_station_system_id, {})
        sell_sys_info = system_cache.get(candidate.sell_station_system_id, {})
        buy_system_name = buy_sys_info.get("name", "Unknown")
        sell_system_name = sell_sys_info.get("name", "Unknown")
        
        # Get buy station history for dual-row display
        buy_stats = parse_history_stats(
            buy_station_history.get(type_id, []), reference_date
        )

        # Get buy station buy order for display
        buy_station_info = buy_station_data.get(type_id, {})
        buy_station_buy_price = buy_station_info.get("buy", 0)

        deal = CrossHubDeal(
            type_id=type_id,
            name=name,
            buy_station=buy_station_key,
            sell_station=sell_station_key,
            buy_system_name=buy_system_name,
            sell_system_name=sell_system_name,
            buy_price=candidate.buy_station_sell,
            sell_price=ceiling,
            break_even=arb["break_even_sell"],
            net_profit=arb["profit_per_unit"],
            total_profit=arb["net_profit"],
            margin_percent=arb["margin_percent"],
            volume=effective_volume,
            raw_volume=candidate.buy_station_sell_volume,
            guaranteed_volume=0,  # No guarantee
            buy_broker_fee=arb["buy_broker_fee"],
            sell_broker_fee=arb["sell_broker_fee"],
            sales_tax=arb["sales_tax"],
            total_fees=arb["total_fees"],
            avg_volume_7d=sell_stats.avg_volume_7d,
            avg_volume_30d=sell_stats.avg_volume_30d,
            avg_price_7d=sell_stats.avg_price_7d,
            avg_price_30d=sell_stats.avg_price_30d,
            trading_days_30d=sell_stats.trading_days_30d,
            buy_avg_volume_7d=buy_stats.avg_volume_7d,
            buy_avg_volume_30d=buy_stats.avg_volume_30d,
            buy_avg_price_7d=buy_stats.avg_price_7d,
            buy_avg_price_30d=buy_stats.avg_price_30d,
            buy_trading_days_30d=buy_stats.trading_days_30d,
            is_guaranteed=False,
            risk_flags=risk_flags,
            sell_station_buy=candidate.sell_station_buy,
            buy_station_buy=buy_station_buy_price,
        )
        
        deals.append(deal)
    
    # Sort by total profit
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    return deals


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def process_crosshub(
    buy_station_data: dict[int, dict],
    sell_station_data: dict[int, dict],
    names: dict[int, str],
    system_cache: dict[int, dict],
    sell_station_history: dict[int, list[dict]],
    buy_station_history: dict[int, list[dict]],
    jita_history: dict[int, list[dict]],
    buy_station_key: str,
    sell_station_key: str,
    buy_skills: TradingSkills,
    sell_skills: TradingSkills,
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    max_cost: float = None,
    min_guaranteed_volume: int = 5,
    reference_date: Optional[str] = None,
) -> tuple[list[CrossHubDeal], list[CrossHubDeal]]:
    """
    Main entry point for cross-hub scanning.

    reference_date: History DB's latest date (YYYY-MM-DD), threaded into all
    parse_history_stats calls (same as build_demand_rows) so the everef DB's
    1-4 day lag doesn't deflate the 7d/30d velocity windows.

    Returns:
        (low_risk_deals, high_risk_deals)
    """
    # Build candidates
    candidates = build_crosshub_candidates(
        buy_station_data,
        sell_station_data,
        max_cost
    )
    
    if not candidates:
        return [], []
    
    # Process Low Risk (guaranteed profit)
    low_risk = process_crosshub_low_risk(
        candidates=candidates,
        names=names,
        system_cache=system_cache,
        sell_station_history=sell_station_history,
        buy_station_history=buy_station_history,
        buy_station_data=buy_station_data,
        buy_station_key=buy_station_key,
        sell_station_key=sell_station_key,
        buy_skills=buy_skills,
        sell_skills=sell_skills,
        min_profit_per_unit=min_profit_per_unit,
        min_total_profit=min_total_profit,
        min_margin_percent=min_margin_percent,
        min_guaranteed_volume=min_guaranteed_volume,
        reference_date=reference_date,
    )
    
    # Process High Risk (relist at destination)
    high_risk = process_crosshub_high_risk(
        candidates=candidates,
        names=names,
        system_cache=system_cache,
        sell_station_history=sell_station_history,
        buy_station_history=buy_station_history,
        jita_history=jita_history,
        buy_station_data=buy_station_data,
        buy_station_key=buy_station_key,
        sell_station_key=sell_station_key,
        buy_skills=buy_skills,
        sell_skills=sell_skills,
        min_profit_per_unit=min_profit_per_unit,
        min_total_profit=min_total_profit,
        min_margin_percent=min_margin_percent,
        min_velocity=min_velocity,
        reference_date=reference_date,
    )
    
    # Remove duplicates - if something is Low Risk, don't also show in High Risk
    low_risk_ids = {d.type_id for d in low_risk}
    high_risk = [d for d in high_risk if d.type_id not in low_risk_ids]
    
    return low_risk, high_risk
