"""Steals processor for EVE Market Scout.

Handles fat-finger mistakes where someone lists near the buy order price.
Color-codes based on risk factors even for steals.
"""

from typing import Optional

from scanning.scanner_common import (
    Candidate, Deal, HistoryStats, StealColor, RiskFlag,
    calculate_ceiling, evaluate_risk_flags, get_steal_color,
    build_deal, passes_profit_filters, parse_history_stats,
    STEAL_RATIO_THRESHOLD
)
from core.calculate import TradingSkills, DEFAULT_SKILLS


def process_steals(
    candidates: list[Candidate],
    names: dict[int, str],
    system_cache: dict[int, dict],
    local_history: dict[int, list[dict]],
    jita_history: dict[int, list[dict]],
    min_profit_per_unit: float,
    min_total_profit: float,
    min_margin_percent: float,
    min_velocity: float,
    skills: TradingSkills = None,
    reference_date: str = None
) -> list[Deal]:
    """
    Process candidates to find steals (fat finger mistakes).
    
    A steal is when someone lists an item near the buy order price,
    likely by mistake. We can buy it and relist at normal prices.
    
    Color coding:
    - GREEN: Passes all Low Risk checks (safe to flip)
    - YELLOW: Fails 1 risk check (minor concern)
    - RED: Fails 2+ risk checks (risky even as a steal)
    
    Args:
        candidates: Raw candidates from first pass
        names: type_id -> item name mapping
        system_cache: system_id -> {name, security} mapping
        local_history: type_id -> Amarr history
        jita_history: type_id -> Jita history
        min_profit_per_unit: Filter threshold
        min_total_profit: Filter threshold
        min_margin_percent: Filter threshold
        min_velocity: For risk flag evaluation
        skills: For fee calculations
        reference_date: Date string (YYYY-MM-DD) for history filtering
    
    Returns:
        List of Deal objects that are steals, with color coding
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    deals = []
    
    for candidate in candidates:
        # Check if this is a steal
        if not candidate.is_steal:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        sys_info = system_cache.get(candidate.system_id, {})
        system_name = sys_info.get("name", "Unknown")
        
        # Parse history with reference date for accurate filtering
        local_stats = parse_history_stats(local_history.get(type_id, []), reference_date)
        jita_stats = parse_history_stats(jita_history.get(type_id, []), reference_date)
        
        # Calculate ceiling (same logic as Low Risk)
        ceiling, ceiling_flags = calculate_ceiling(
            candidate, local_stats, jita_stats, skills
        )
        
        # Evaluate all risk flags
        all_flags = evaluate_risk_flags(
            local_stats, jita_stats, min_velocity, ceiling_flags,
            buy_price=candidate.local_sell
        )
        
        # Build the deal
        deal = build_deal(
            candidate=candidate,
            name=name,
            system_name=system_name,
            ceiling=ceiling,
            local_stats=local_stats,
            jita_stats=jita_stats,
            risk_flags=all_flags,
            skills=skills,
            is_steal=True
        )
        
        # Apply profit filters - steals must still be profitable
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    # Sort by total profit descending
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    
    return deals
