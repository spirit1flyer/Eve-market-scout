"""Jita-specific scanner for EVE Market Scout.

Jita is the reference hub, so ceiling calculation differs:
- Ceiling = lower of 7d/30d historical avg (conservative)
- No Jita caps (we ARE Jita)
- Same risk flags and profit filters as other hubs
"""

from typing import Optional

from scanning.scanner_common import (
    Candidate, Deal, HistoryStats, RiskFlag, StealColor,
    evaluate_risk_flags, get_steal_color,
    passes_profit_filters, parse_history_stats,
    STEAL_RATIO_THRESHOLD, VOLUME_CAP_FRACTION
)
from core.calculate import (
    TradingSkills, DEFAULT_SKILLS,
    calculate_break_even, calculate_profit_per_unit, calculate_margin_percent
)


def calculate_jita_ceiling(
    candidate: Candidate,
    local_stats: HistoryStats,
    skills: TradingSkills = None
) -> tuple[float, list[RiskFlag]]:
    """
    Calculate ceiling price for Jita deals.
    
    Jita-specific logic:
    - Ceiling = lower of 7d/30d avg (conservative historical)
    - No external hub caps (we ARE the reference)
    - Still flag if competition is way below historical
    
    Args:
        candidate: Raw candidate data
        local_stats: Jita history stats
        skills: For calculations (unused here but kept for signature consistency)
    
    Returns:
        (ceiling_price, list of risk flags triggered)
    """
    flags = []
    
    # Use conservative historical price (lower of 7d/30d)
    ceiling = local_stats.conservative_price
    
    # If no history data, fall back to 2nd lowest sell
    if ceiling <= 0:
        undercut = max(0.01, candidate.local_sell_2nd * 0.001)
        ceiling = candidate.local_sell_2nd - undercut
        flags.append(RiskFlag.NO_JITA_DATA)  # Reuse flag - means no history
    
    # If competition (2nd lowest) is way below historical, flag as crashing
    if local_stats.optimistic_price > 0:
        competition_price = candidate.local_sell_2nd
        if competition_price < (local_stats.optimistic_price * 0.90):
            flags.append(RiskFlag.MARKET_CRASHING)
    
    return ceiling, flags


def build_jita_deal(
    candidate: Candidate,
    name: str,
    system_name: str,
    ceiling: float,
    local_stats: HistoryStats,
    risk_flags: list[RiskFlag],
    skills: TradingSkills = None,
    is_steal: bool = False
) -> Deal:
    """
    Build a Deal object for Jita.
    
    Same as regular build_deal but passes local_stats as both local and jita
    since they're the same for Jita.
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


def process_jita_steals(
    candidates: list[Candidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    local_history: dict[int, list[dict]],
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    skills: TradingSkills = None,
    reference_date: str = None
) -> list[Deal]:
    """
    Process Jita candidates to find steals (fat finger mistakes).
    
    Same logic as regular steals, but uses Jita ceiling calculation.
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    deals = []
    
    for candidate in candidates:
        if not candidate.is_steal:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        sys_info = system_cache.get(candidate.system_id, {})
        system_name = sys_info.get("name", "Unknown")
        
        # Parse history with reference date for accurate filtering
        local_stats = parse_history_stats(local_history.get(type_id, []), reference_date)
        
        # Calculate Jita-specific ceiling
        ceiling, ceiling_flags = calculate_jita_ceiling(
            candidate, local_stats, skills
        )
        
        # Evaluate risk flags (pass local_stats as jita_stats too)
        all_flags = evaluate_risk_flags(
            local_stats, local_stats, min_velocity, ceiling_flags,
            buy_price=candidate.local_sell
        )
        
        # Build the deal
        deal = build_jita_deal(
            candidate=candidate,
            name=name,
            system_name=system_name,
            ceiling=ceiling,
            local_stats=local_stats,
            risk_flags=all_flags,
            skills=skills,
            is_steal=True
        )
        
        # Apply profit filters
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    return deals


def process_jita_low_risk(
    candidates: list[Candidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    local_history: dict[int, list[dict]],
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    skills: TradingSkills = None,
    reference_date: str = None
) -> list[Deal]:
    """
    Process Jita candidates to find low risk deals.
    
    Low Risk = zero risk flags. Uses Jita ceiling calculation.
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    deals = []
    
    for candidate in candidates:
        if candidate.is_steal:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        sys_info = system_cache.get(candidate.system_id, {})
        system_name = sys_info.get("name", "Unknown")
        
        # Parse history with reference date for accurate filtering
        local_stats = parse_history_stats(local_history.get(type_id, []), reference_date)
        
        # Calculate Jita-specific ceiling
        ceiling, ceiling_flags = calculate_jita_ceiling(
            candidate, local_stats, skills
        )
        
        # Evaluate risk flags
        all_flags = evaluate_risk_flags(
            local_stats, local_stats, min_velocity, ceiling_flags,
            buy_price=candidate.local_sell
        )
        
        # LOW RISK: Must have NO risk flags
        if len(all_flags) > 0:
            continue
        
        # Build the deal
        deal = build_jita_deal(
            candidate=candidate,
            name=name,
            system_name=system_name,
            ceiling=ceiling,
            local_stats=local_stats,
            risk_flags=all_flags,
            skills=skills,
            is_steal=False
        )
        
        # Apply profit filters
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    return deals


def process_jita_high_risk(
    candidates: list[Candidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    local_history: dict[int, list[dict]],
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    skills: TradingSkills = None,
    reference_date: str = None
) -> list[Deal]:
    """
    Process Jita candidates to find high risk deals.
    
    High Risk = has at least one risk flag. Uses Jita ceiling calculation.
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    deals = []
    
    for candidate in candidates:
        if candidate.is_steal:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        sys_info = system_cache.get(candidate.system_id, {})
        system_name = sys_info.get("name", "Unknown")
        
        # Parse history with reference date for accurate filtering
        local_stats = parse_history_stats(local_history.get(type_id, []), reference_date)
        
        # Calculate Jita-specific ceiling
        ceiling, ceiling_flags = calculate_jita_ceiling(
            candidate, local_stats, skills
        )
        
        # Evaluate risk flags
        all_flags = evaluate_risk_flags(
            local_stats, local_stats, min_velocity, ceiling_flags,
            buy_price=candidate.local_sell
        )
        
        # HIGH RISK: Must have at least one risk flag
        if len(all_flags) == 0:
            continue
        
        # Build the deal
        deal = build_jita_deal(
            candidate=candidate,
            name=name,
            system_name=system_name,
            ceiling=ceiling,
            local_stats=local_stats,
            risk_flags=all_flags,
            skills=skills,
            is_steal=False
        )
        
        # Apply profit filters
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    return deals
