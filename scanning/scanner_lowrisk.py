"""Low Risk processor for EVE Market Scout.

Handles deals that pass all safety checks:
- Good velocity (>= min_vol)
- Has Jita data for validation
- Market not crashing
- Ceiling not capped excessively
"""

from typing import Optional

from scanning.scanner_common import (
    Candidate, Deal, HistoryStats, RiskFlag,
    calculate_ceiling, evaluate_risk_flags,
    build_deal, passes_profit_filters, parse_history_stats
)
from core.calculate import TradingSkills, DEFAULT_SKILLS


def process_low_risk(
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
    Process candidates to find low risk deals.
    
    Low Risk criteria:
    - NOT a steal (those go to steals processor)
    - Velocity >= min_vol (using conservative estimate)
    - Has Jita data for price validation
    - Market not crashing (7d not >10% below 30d)
    - Ceiling not capped excessively by Jita
    - Passes all profit filters
    
    Args:
        candidates: Raw candidates from first pass
        names: type_id -> item name mapping
        system_cache: system_id -> {name, security} mapping
        local_history: type_id -> Amarr history
        jita_history: type_id -> Jita history
        min_profit_per_unit: Filter threshold
        min_total_profit: Filter threshold
        min_margin_percent: Filter threshold
        min_velocity: Minimum daily volume for Low Risk
        skills: For fee calculations
        reference_date: Date string (YYYY-MM-DD) for history filtering
    
    Returns:
        List of Deal objects that are low risk
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    deals = []
    
    for candidate in candidates:
        # Skip steals - they have their own processor
        if candidate.is_steal:
            continue
        
        type_id = candidate.type_id
        name = names.get(type_id, f"Unknown ({type_id})")
        sys_info = system_cache.get(candidate.system_id, {})
        system_name = sys_info.get("name", "Unknown")
        
        # Parse history with reference date for accurate filtering
        local_stats = parse_history_stats(local_history.get(type_id, []), reference_date)
        jita_stats = parse_history_stats(jita_history.get(type_id, []), reference_date)
        
        # Calculate ceiling
        ceiling, ceiling_flags = calculate_ceiling(
            candidate, local_stats, jita_stats, skills
        )
        
        # Evaluate all risk flags
        all_flags = evaluate_risk_flags(
            local_stats, jita_stats, min_velocity, ceiling_flags,
            buy_price=candidate.local_sell
        )
        
        # LOW RISK REQUIREMENT: Must have NO risk flags
        if len(all_flags) > 0:
            continue
        
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
            is_steal=False
        )
        
        # Apply profit filters
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    # Sort by total profit descending
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    
    return deals
