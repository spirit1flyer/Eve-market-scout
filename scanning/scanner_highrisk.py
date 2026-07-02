"""High Risk processor for EVE Market Scout.

Handles deals that are profitable but failed Low Risk checks:
- Low velocity (below min_vol)
- No Jita data
- Market crashing
- Ceiling capped hard by Jita
- Sporadic trading history
"""

from typing import Optional

from scanning.scanner_common import (
    Candidate, Deal, HistoryStats, RiskFlag,
    calculate_ceiling, evaluate_risk_flags,
    build_deal, passes_profit_filters, parse_history_stats
)
from core.calculate import TradingSkills, DEFAULT_SKILLS


def process_high_risk(
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
    Process candidates to find high risk deals.
    
    High Risk = profitable but has at least one risk flag:
    - LOW_VELOCITY: Daily volume below min_vol
    - NO_JITA_DATA: Can't validate ceiling against Jita
    - MARKET_CRASHING: 7d avg >10% below 30d avg
    - CEILING_CAPPED_HARD: Jita forced ceiling down >20%
    - SPORADIC_TRADING: Fewer than 15 of 30 days had trades
    
    These are still potentially good deals, just carry more risk.
    
    Args:
        candidates: Raw candidates from first pass
        names: type_id -> item name mapping
        system_cache: system_id -> {name, security} mapping
        local_history: type_id -> Amarr history
        jita_history: type_id -> Jita history
        min_profit_per_unit: Filter threshold
        min_total_profit: Filter threshold
        min_margin_percent: Filter threshold
        min_velocity: For risk flag evaluation (items below this get flagged)
        skills: For fee calculations
        reference_date: Date string (YYYY-MM-DD) for history filtering
    
    Returns:
        List of Deal objects that are high risk
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
        
        # HIGH RISK REQUIREMENT: Must have at least one risk flag
        # (If zero flags, it would be Low Risk)
        if len(all_flags) == 0:
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
        
        # Apply profit filters - high risk must still be profitable
        if not passes_profit_filters(
            deal, min_profit_per_unit, min_total_profit, min_margin_percent
        ):
            continue
        
        deals.append(deal)
    
    # Sort by total profit descending
    deals.sort(key=lambda d: d.total_profit, reverse=True)
    
    return deals
