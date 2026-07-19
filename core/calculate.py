"""Centralized trading calculations for EVE Market Scout.

All fee, profit, and break-even math lives here. Skill levels modify the base rates.
"""

from dataclasses import dataclass
from typing import Optional


# =============================================================================
# BASE RATES (before skills)
# =============================================================================

# Base broker fee: 3% base, modified by Broker Relations and station standing
BASE_BROKER_FEE_PERCENT = 3.0

# Base sales tax: 7.5% base, modified by Accounting skill
BASE_SALES_TAX_PERCENT = 7.5

# Relist fee: percentage of the ORDER PRICE DIFFERENCE, not full price
# Modified by Advanced Broker Relations
RELIST_FEE_BASE_PERCENT = 100.0  # 100% of the broker fee on the price difference


# =============================================================================
# SKILL EFFECTS
# =============================================================================

# Broker Relations: -0.3% per level (5 levels = -1.5%, so 3% -> 1.5% at L5)
BROKER_RELATIONS_REDUCTION_PER_LEVEL = 0.3

# Advanced Broker Relations: -5% relist cost per level (not implemented in basic calc)
ADVANCED_BROKER_REDUCTION_PER_LEVEL = 5.0

# Accounting: -11% sales tax per level (7.5% -> 3.375% at L5)
ACCOUNTING_REDUCTION_PER_LEVEL = 11.0  # percentage reduction, not flat


# =============================================================================
# TRADING SKILLS
# =============================================================================

@dataclass
class TradingSkills:
    """Character's trading-related skill levels (0-5 each)."""
    
    broker_relations: int = 0       # Reduces broker fee
    accounting: int = 0             # Reduces sales tax
    advanced_broker_relations: int = 0  # Reduces relist fees
    
    # Station standing (0.0 to 10.0) - also affects broker fees
    station_standing: float = 0.0
    faction_standing: float = 0.0
    
    # Manual fee overrides (None = use calculated values)
    manual_broker_fee: Optional[float] = None  # Override broker fee %
    manual_sales_tax: Optional[float] = None   # Override sales tax %
    
    def __post_init__(self):
        """Clamp values to valid ranges."""
        self.broker_relations = max(0, min(5, self.broker_relations))
        self.accounting = max(0, min(5, self.accounting))
        self.advanced_broker_relations = max(0, min(5, self.advanced_broker_relations))
        self.station_standing = max(-10.0, min(10.0, self.station_standing))
        self.faction_standing = max(-10.0, min(10.0, self.faction_standing))
        # Clamp manual fees if set
        if self.manual_broker_fee is not None:
            self.manual_broker_fee = max(0.0, min(10.0, self.manual_broker_fee))
        if self.manual_sales_tax is not None:
            self.manual_sales_tax = max(0.0, min(10.0, self.manual_sales_tax))


# Default skills (no training) - for when ESI isn't connected
DEFAULT_SKILLS = TradingSkills()

# Placeholder for "max skills" testing
MAX_SKILLS = TradingSkills(
    broker_relations=5,
    accounting=5,
    advanced_broker_relations=5,
    station_standing=0.0,  # Standing varies, keep at 0 for safe estimate
    faction_standing=0.0
)


# =============================================================================
# FEE CALCULATIONS
# =============================================================================

def get_broker_fee_rate(skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate broker fee percentage based on skills and standings.
    
    If manual_broker_fee is set, returns that value instead.
    
    Formula: max(0.01%, 3% - 0.3% * BrokerRelations - 0.03% * FactionStanding - 0.02% * CorpStanding)

    The standings must be BASE standings (unmodified by Connections/
    Diplomacy) — the in-game fee ignores social skills. Verified in-game
    2026-07-03: Jita 1.46% = 1.5% - 0.03 x 1.41 base faction standing.

    Returns: Fee as percentage (e.g., 1.48 means 1.48%)
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    # Use manual override if set
    if skills.manual_broker_fee is not None:
        return skills.manual_broker_fee
    
    # Start with base
    rate = BASE_BROKER_FEE_PERCENT
    
    # Broker Relations reduction
    rate -= BROKER_RELATIONS_REDUCTION_PER_LEVEL * skills.broker_relations
    
    # Standing reductions (smaller effect): 0.03 applies to FACTION,
    # 0.02 to the station-owner CORP (they were swapped until 2026-07-03).
    rate -= 0.03 * skills.faction_standing
    rate -= 0.02 * skills.station_standing
    
    # Minimum 0.01%
    return max(0.01, rate)


def get_sales_tax_rate(skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate sales tax percentage based on Accounting skill.
    
    If manual_sales_tax is set, returns that value instead.
    
    Formula: 7.5% * (1 - 0.11 * AccountingLevel)
    At L5: 7.5% * (1 - 0.55) = 7.5% * 0.45 = 3.375%
    
    Returns: Tax as percentage (e.g., 3.375 means 3.375%)
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    # Use manual override if set
    if skills.manual_sales_tax is not None:
        return skills.manual_sales_tax
    
    reduction_multiplier = 1.0 - (ACCOUNTING_REDUCTION_PER_LEVEL / 100.0 * skills.accounting)
    return BASE_SALES_TAX_PERCENT * reduction_multiplier


def get_relist_fee_rate(skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate relist (order modification) fee rate.
    
    Relist fee = broker fee on the PRICE INCREASE, reduced by Advanced Broker Relations.
    ABR reduces the relist cost by 5% per level.
    
    Returns: Multiplier for the base broker fee (e.g., 0.75 at ABR 5)
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    reduction = ADVANCED_BROKER_REDUCTION_PER_LEVEL * skills.advanced_broker_relations
    return 1.0 - (reduction / 100.0)


# =============================================================================
# ISK CALCULATIONS
# =============================================================================

def calculate_broker_fee(price: float, quantity: int = 1, 
                         skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate broker fee in ISK for placing an order.
    
    Args:
        price: Price per unit
        quantity: Number of units
        skills: Character's trading skills
    
    Returns: Broker fee in ISK
    """
    rate = get_broker_fee_rate(skills) / 100.0
    return price * quantity * rate


def calculate_sales_tax(price: float, quantity: int = 1,
                        skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate sales tax in ISK for a sale.
    
    Args:
        price: Sale price per unit
        quantity: Number of units sold
        skills: Character's trading skills
    
    Returns: Sales tax in ISK
    """
    rate = get_sales_tax_rate(skills) / 100.0
    return price * quantity * rate


def calculate_relist_fee(old_price: float, new_price: float, quantity: int = 1,
                         skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate fee for modifying an order's price.
    
    Only charged when INCREASING price. Price decreases are free.
    Fee = broker_fee_rate * price_increase * abr_reduction
    
    Args:
        old_price: Previous order price per unit
        new_price: New order price per unit
        quantity: Order quantity
        skills: Character's trading skills
    
    Returns: Relist fee in ISK (0 if price decreased)
    """
    if new_price <= old_price:
        return 0.0
    
    price_increase = new_price - old_price
    broker_rate = get_broker_fee_rate(skills) / 100.0
    abr_multiplier = get_relist_fee_rate(skills)
    
    return price_increase * quantity * broker_rate * abr_multiplier


def calculate_total_fees(sell_price: float, quantity: int = 1,
                         skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate total fees for a simple buy-and-relist trade.
    
    Includes: listing broker fee + sales tax
    Does NOT include: buy-side fees (instant buys have no broker fee), relists
    
    Args:
        sell_price: Your sell order price per unit
        quantity: Number of units
        skills: Character's trading skills
    
    Returns: Total fees in ISK
    """
    broker = calculate_broker_fee(sell_price, quantity, skills)
    tax = calculate_sales_tax(sell_price, quantity, skills)
    return broker + tax


# =============================================================================
# PROFIT & BREAK-EVEN
# =============================================================================

def calculate_break_even(buy_price: float, quantity: int = 1,
                         fees_paid: float = 0.0,
                         skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate minimum sell price to break even.
    
    Accounts for:
    - Total cost (buy_price * quantity + any fees already paid)
    - Broker fee on sell order
    - Sales tax on sale
    
    Args:
        buy_price: Price paid per unit
        quantity: Number of units
        fees_paid: Any broker/relist fees already paid (e.g., from listing)
        skills: Character's trading skills
    
    Returns: Minimum sell price per unit to break even
    """
    if quantity <= 0:
        return 0.0
    
    total_cost = (buy_price * quantity) + fees_paid
    
    # We need: sell_price * qty - broker - tax = total_cost
    # broker = sell_price * qty * broker_rate
    # tax = sell_price * qty * tax_rate
    # sell_price * qty * (1 - broker_rate - tax_rate) = total_cost
    # sell_price = total_cost / (qty * (1 - broker_rate - tax_rate))
    
    broker_rate = get_broker_fee_rate(skills) / 100.0
    tax_rate = get_sales_tax_rate(skills) / 100.0
    
    net_rate = 1.0 - broker_rate - tax_rate
    
    if net_rate <= 0:
        return float('inf')  # Fees exceed 100%, impossible to profit
    
    return total_cost / (quantity * net_rate)


def calculate_profit(buy_price: float, sell_price: float, quantity: int = 1,
                     extra_fees: float = 0.0,
                     skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate net profit for a trade.
    
    Args:
        buy_price: Price paid per unit
        sell_price: Price sold per unit
        quantity: Number of units
        extra_fees: Additional fees (relists, etc.)
        skills: Character's trading skills
    
    Returns: Net profit in ISK
    """
    revenue = sell_price * quantity
    cost = buy_price * quantity
    
    broker = calculate_broker_fee(sell_price, quantity, skills)
    tax = calculate_sales_tax(sell_price, quantity, skills)
    
    return revenue - cost - broker - tax - extra_fees


def calculate_profit_per_unit(buy_price: float, sell_price: float,
                              skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate profit per unit (ignores quantity-independent fees).
    
    Args:
        buy_price: Price paid per unit
        sell_price: Price to sell per unit
        skills: Character's trading skills
    
    Returns: Net profit per unit in ISK
    """
    return calculate_profit(buy_price, sell_price, 1, 0.0, skills)


def calculate_margin_percent(buy_price: float, sell_price: float,
                             skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate profit margin as percentage of buy price.
    
    Args:
        buy_price: Price paid per unit
        sell_price: Price to sell per unit
        skills: Character's trading skills
    
    Returns: Margin as percentage (e.g., 15.0 means 15%)
    """
    if buy_price <= 0:
        return 0.0
    
    profit = calculate_profit_per_unit(buy_price, sell_price, skills)
    return (profit / buy_price) * 100.0


# =============================================================================
# DEAL EVALUATION
# =============================================================================

def evaluate_deal(buy_price: float, sell_price: float, volume: int,
                  daily_volume: float = 0.0,
                  skills: Optional[TradingSkills] = None) -> dict:
    """
    Evaluate a potential trading deal.
    
    Returns comprehensive metrics for display/filtering.
    
    Args:
        buy_price: Price to buy at (lowest sell order)
        sell_price: Price to sell at (ceiling/relist price)
        volume: Volume available to buy
        daily_volume: Average daily volume for days-to-sell calc
        skills: Character's trading skills
    
    Returns: Dict with all calculated metrics
    """
    profit_per_unit = calculate_profit_per_unit(buy_price, sell_price, skills)
    total_profit = profit_per_unit * volume
    break_even = calculate_break_even(buy_price, 1, 0.0, skills)
    margin_pct = calculate_margin_percent(buy_price, sell_price, skills)
    
    total_cost = buy_price * volume
    broker_fee = calculate_broker_fee(sell_price, volume, skills)
    sales_tax = calculate_sales_tax(sell_price, volume, skills)
    total_fees = broker_fee + sales_tax
    
    days_to_sell = volume / daily_volume if daily_volume > 0 else float('inf')
    
    return {
        "buy_price": buy_price,
        "sell_price": sell_price,
        "volume": volume,
        "profit_per_unit": profit_per_unit,
        "total_profit": total_profit,
        "break_even": break_even,
        "margin_percent": margin_pct,
        "total_cost": total_cost,
        "broker_fee": broker_fee,
        "sales_tax": sales_tax,
        "total_fees": total_fees,
        "days_to_sell": days_to_sell,
        "fee_rate_broker": get_broker_fee_rate(skills),
        "fee_rate_tax": get_sales_tax_rate(skills),
    }


# =============================================================================
# CROSS-HUB ARBITRAGE
# =============================================================================

def calculate_arbitrage_profit(
    buy_price: float,
    sell_price: float,
    quantity: int,
    buy_skills: TradingSkills,
    sell_skills: TradingSkills,
    buy_is_instant: bool = True,
    hauling_cost_per_unit: float = 0.0,
    collateral_percent: float = 0.0,
) -> dict:
    """
    Calculate profit for cross-hub arbitrage (buy in one hub, sell in another).
    
    Args:
        buy_price: Price to buy at source hub
        sell_price: Price to sell at destination hub
        quantity: Number of units
        buy_skills: TradingSkills with source hub standings (for buy broker fee)
        sell_skills: TradingSkills with destination hub standings (for sell broker fee)
        buy_is_instant: If True, buying from sell orders (no broker fee).
                        If False, placing buy order (broker fee applies).
        hauling_cost_per_unit: Courier/fuel cost per unit
        collateral_percent: Courier collateral as percent of cargo value (e.g., 2.0 = 2%)
    
    Returns: Dict with all cost/profit metrics
    """
    # Buy side costs
    buy_cost = buy_price * quantity
    
    if buy_is_instant:
        buy_broker_fee = 0.0  # Instant buy from sell order = no fee
    else:
        buy_broker_fee = calculate_broker_fee(buy_price, quantity, buy_skills)
    
    # Hauling costs
    hauling_cost = hauling_cost_per_unit * quantity
    collateral_cost = (buy_cost * collateral_percent / 100.0) if collateral_percent > 0 else 0.0
    total_hauling = hauling_cost + collateral_cost
    
    # Sell side costs (destination hub)
    sell_broker_fee = calculate_broker_fee(sell_price, quantity, sell_skills)
    sales_tax = calculate_sales_tax(sell_price, quantity, sell_skills)  # Tax uses Accounting skill only
    
    # Totals
    total_fees = buy_broker_fee + sell_broker_fee + sales_tax + total_hauling
    revenue = sell_price * quantity
    total_cost = buy_cost + total_fees
    
    net_profit = revenue - total_cost
    profit_per_unit = net_profit / quantity if quantity > 0 else 0.0
    margin_percent = (net_profit / buy_cost * 100.0) if buy_cost > 0 else 0.0
    
    # Break-even sell price at destination
    # Need: sell_price * qty - sell_broker - tax = buy_cost + buy_broker + hauling
    # sell_price * qty * (1 - broker_rate - tax_rate) = total_source_cost
    source_cost = buy_cost + buy_broker_fee + total_hauling
    broker_rate = get_broker_fee_rate(sell_skills) / 100.0
    tax_rate = get_sales_tax_rate(sell_skills) / 100.0
    net_rate = 1.0 - broker_rate - tax_rate
    
    if net_rate > 0 and quantity > 0:
        break_even_sell = source_cost / (quantity * net_rate)
    else:
        break_even_sell = float('inf')
    
    return {
        "buy_price": buy_price,
        "sell_price": sell_price,
        "quantity": quantity,
        "buy_cost": buy_cost,
        "buy_broker_fee": buy_broker_fee,
        "hauling_cost": hauling_cost,
        "collateral_cost": collateral_cost,
        "total_hauling": total_hauling,
        "sell_broker_fee": sell_broker_fee,
        "sales_tax": sales_tax,
        "total_fees": total_fees,
        "revenue": revenue,
        "total_cost": total_cost,
        "net_profit": net_profit,
        "profit_per_unit": profit_per_unit,
        "margin_percent": margin_percent,
        "break_even_sell": break_even_sell,
        "buy_broker_rate": get_broker_fee_rate(buy_skills),
        "sell_broker_rate": get_broker_fee_rate(sell_skills),
        "tax_rate": get_sales_tax_rate(sell_skills),
    }


# =============================================================================
# UTILITY
# =============================================================================

def format_isk(value: float, short: bool = False) -> str:
    """Format ISK value for display."""
    if short:
        if abs(value) >= 1_000_000_000:
            return f"{value / 1_000_000_000:.1f}B"
        elif abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        elif abs(value) >= 1_000:
            return f"{value / 1_000:.0f}K"
    return f"{value:,.0f}"


def get_skill_summary(skills: Optional[TradingSkills] = None) -> str:
    """Get human-readable summary of effective rates."""
    if skills is None:
        skills = DEFAULT_SKILLS
    
    broker = get_broker_fee_rate(skills)
    tax = get_sales_tax_rate(skills)
    total = broker + tax
    
    return f"Broker: {broker:.2f}% | Tax: {tax:.2f}% | Total: {total:.2f}%"


# =============================================================================
# CACHED SKILLS PERSISTENCE
# =============================================================================

import json
from pathlib import Path
from datetime import datetime

def _get_skills_cache_path() -> Path:
    """Get path to cached skills JSON file."""
    from core.sound_manager import get_data_dir
    return get_data_dir() / "character_skills.json"


def save_cached_skills(
    seller_skills: TradingSkills,
    seller_standings: dict[str, tuple[float, float]],
    seller_name: str = "",
    buyer_skills: Optional[TradingSkills] = None,
    buyer_standings: Optional[dict[str, tuple[float, float]]] = None,
    buyer_name: str = "",
) -> bool:
    """
    Save skills and standings to JSON for use by Stock Market.
    
    Args:
        seller_skills: TradingSkills for seller (skill levels only, standings ignored)
        seller_standings: Dict of hub_key -> (corp_standing, faction_standing)
        seller_name: Character name
        buyer_skills: TradingSkills for buyer (optional)
        buyer_standings: Dict of hub_key -> (corp_standing, faction_standing)
        buyer_name: Buyer character name
    
    Returns:
        True if saved successfully
    """
    data = {
        "seller": {
            "character_name": seller_name,
            "broker_relations": seller_skills.broker_relations,
            "accounting": seller_skills.accounting,
            "advanced_broker_relations": seller_skills.advanced_broker_relations,
            "standings": {
                hub: {"corp": corp, "faction": faction}
                for hub, (corp, faction) in seller_standings.items()
            },
            "updated_at": datetime.utcnow().isoformat() + "Z"
        }
    }
    
    if buyer_skills and buyer_standings:
        data["buyer"] = {
            "character_name": buyer_name,
            "broker_relations": buyer_skills.broker_relations,
            "accounting": buyer_skills.accounting,
            "advanced_broker_relations": buyer_skills.advanced_broker_relations,
            "standings": {
                hub: {"corp": corp, "faction": faction}
                for hub, (corp, faction) in buyer_standings.items()
            },
            "updated_at": datetime.utcnow().isoformat() + "Z"
        }
    
    try:
        cache_path = _get_skills_cache_path()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[Skills] Saved cached skills to {cache_path}")
        return True
    except Exception as e:
        print(f"[Skills] Error saving cached skills: {e}")
        return False


def load_cached_skills(hub_key: str, slot: str = "seller") -> TradingSkills:
    """
    Load cached skills for a specific hub.
    
    Args:
        hub_key: Hub key (e.g., 'amarr', 'jita')
        slot: "seller" or "buyer"
    
    Returns:
        TradingSkills with hub-specific standings, or DEFAULT_SKILLS if no cache
    """
    cache_path = _get_skills_cache_path()
    
    if not cache_path.exists():
        return DEFAULT_SKILLS
    
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
        
        char_data = data.get(slot)
        if not char_data:
            return DEFAULT_SKILLS
        
        # Get standings for this hub
        standings = char_data.get("standings", {}).get(hub_key, {})
        corp_standing = standings.get("corp", 0.0)
        faction_standing = standings.get("faction", 0.0)
        
        return TradingSkills(
            broker_relations=char_data.get("broker_relations", 0),
            accounting=char_data.get("accounting", 0),
            advanced_broker_relations=char_data.get("advanced_broker_relations", 0),
            station_standing=corp_standing,
            faction_standing=faction_standing,
        )
    except Exception as e:
        print(f"[Skills] Error loading cached skills: {e}")
        return DEFAULT_SKILLS


def get_cached_skills_summary() -> Optional[dict]:
    """
    Get summary info about cached skills (for display).
    
    Returns:
        Dict with character names and update times, or None if no cache
    """
    cache_path = _get_skills_cache_path()
    
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
        
        result = {}
        for slot in ["seller", "buyer"]:
            if slot in data:
                result[slot] = {
                    "name": data[slot].get("character_name", "Unknown"),
                    "updated_at": data[slot].get("updated_at", ""),
                    "broker_relations": data[slot].get("broker_relations", 0),
                    "accounting": data[slot].get("accounting", 0),
                }
        return result if result else None
    except Exception:
        return None


# =============================================================================
# TESTING / VALIDATION
# =============================================================================

if __name__ == "__main__":
    # Quick sanity checks
    print("=== Fee Rate Tests ===")
    print(f"No skills: {get_skill_summary(DEFAULT_SKILLS)}")
    print(f"Max skills: {get_skill_summary(MAX_SKILLS)}")
    
    # Test with specific skill levels (your current rates: 1.48% + 3.37% = 4.85%)
    # Work backwards: 3.37% tax means Accounting 5 (8% * 0.45 = 3.6%, close)
    # Actually 3.37% means: 8% * (1 - 0.11*x) = 3.37% -> x = 5.3, so maybe Accounting 4-5
    # 1.48% broker means: 3% - 0.3*5 = 1.5%, close to 1.48% with some standing
    
    test_skills = TradingSkills(
        broker_relations=5,
        accounting=5,
        station_standing=0.67,  # Tweak to hit 1.48%
    )
    print(f"Test skills (BR5/Acc5/0.67 standing): {get_skill_summary(test_skills)}")
    
    print("\n=== Profit Calculation Test ===")
    # Buy at 1,000,000, sell at 1,200,000
    buy = 1_000_000
    sell = 1_200_000
    
    result = evaluate_deal(buy, sell, 10, daily_volume=5, skills=test_skills)
    print(f"Buy: {format_isk(buy)} | Sell: {format_isk(sell)}")
    print(f"Profit/unit: {format_isk(result['profit_per_unit'])}")
    print(f"Total profit (10 units): {format_isk(result['total_profit'])}")
    print(f"Break-even: {format_isk(result['break_even'])}")
    print(f"Margin: {result['margin_percent']:.1f}%")
    print(f"Days to sell: {result['days_to_sell']:.1f}")
