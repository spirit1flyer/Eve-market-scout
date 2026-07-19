"""Trade-specific calculations for EVE Market Scout.

Pure functions that work with TrackedTrade objects. Skills are always passed explicitly.
This separates trade math from the TradeTracker class and GUI code.
"""

from datetime import datetime, timedelta
from typing import List, Dict, Optional

from core.calculate import (
    TradingSkills, DEFAULT_SKILLS,
    calculate_broker_fee, calculate_sales_tax, get_sales_tax_rate
)


def calculate_trade_fees(trade, skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate total fees for a trade using given skills.
    
    Includes: listing broker fee, relist fees, sales tax (actual or estimated).
    
    Args:
        trade: TrackedTrade object
        skills: Character's trading skills
    
    Returns: Total fees in ISK
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    total_fees = 0.0
    
    # Listing broker fee (use recorded if available, else estimate)
    if trade.list_broker_fee > 0:
        total_fees += trade.list_broker_fee
    elif trade.list_price > 0 and trade.buy_quantity > 0:
        total_fees += calculate_broker_fee(trade.list_price, trade.buy_quantity, skills)
    
    # Relist fees (already recorded)
    total_fees += trade.relist_fees
    
    # Sales tax (use recorded if sold, else estimate)
    if trade.status == "sold" and trade.sales_tax > 0:
        total_fees += trade.sales_tax
    else:
        sell_price = trade.current_price if trade.current_price > 0 else trade.list_price
        if sell_price > 0 and trade.buy_quantity > 0:
            total_fees += calculate_sales_tax(sell_price, trade.buy_quantity, skills)
    
    return total_fees


def calculate_trade_break_even(trade, skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate break-even price including all fees.
    
    Formula: total_cost / (quantity * (1 - sales_tax_rate))
    
    Args:
        trade: TrackedTrade object
        skills: Character's trading skills
    
    Returns: Break-even price per unit
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    if trade.buy_quantity == 0 or trade.buy_price == 0:
        return 0.0
    
    # Cost basis = buy cost + listing fee + relist fees
    buy_cost = trade.buy_price * trade.buy_quantity
    
    listing_fee = 0.0
    if trade.list_broker_fee > 0:
        listing_fee = trade.list_broker_fee
    elif trade.list_price > 0:
        listing_fee = calculate_broker_fee(trade.list_price, trade.buy_quantity, skills)
    
    total_cost = buy_cost + listing_fee + trade.relist_fees
    
    # Break-even: price where revenue - tax = total_cost
    tax_rate = get_sales_tax_rate(skills) / 100.0
    
    return total_cost / (trade.buy_quantity * (1 - tax_rate))


def calculate_projected_profit(trade, sell_price: float, 
                                skills: Optional[TradingSkills] = None) -> float:
    """
    Calculate what profit would be if sold at given price.
    
    Args:
        trade: TrackedTrade object
        sell_price: Hypothetical sell price per unit
        skills: Character's trading skills
    
    Returns: Projected profit in ISK
    """
    if skills is None:
        skills = DEFAULT_SKILLS
    
    if trade.buy_quantity == 0:
        return 0.0
    
    revenue = sell_price * trade.buy_quantity
    tax_rate = get_sales_tax_rate(skills) / 100.0
    sales_tax = revenue * tax_rate
    
    return revenue - sales_tax - trade.cost_basis


def get_margin_to_break_even(trade, skills: Optional[TradingSkills] = None) -> float:
    """
    How much above break-even the current price is.
    
    Args:
        trade: TrackedTrade object
        skills: Character's trading skills
    
    Returns: Price margin (positive = above break-even)
    """
    if trade.current_price == 0:
        return 0.0
    
    break_even = calculate_trade_break_even(trade, skills)
    return trade.current_price - break_even


def get_undercuts_remaining(trade, skills: Optional[TradingSkills] = None) -> int:
    """
    Estimated 0.01 ISK undercuts before hitting break-even.
    
    Args:
        trade: TrackedTrade object
        skills: Character's trading skills
    
    Returns: Number of 0.01 ISK undercuts possible
    """
    margin = get_margin_to_break_even(trade, skills)
    if margin <= 0:
        return 0
    return int(margin / 0.01)


def get_profit_by_period(sold_trades: List, days: int) -> float:
    """
    Sum profit from trades sold within the last N days.
    
    Args:
        sold_trades: List of TrackedTrade objects with status='sold'
        days: Number of days to look back
    
    Returns: Total profit in ISK
    """
    if not sold_trades:
        return 0.0
    
    cutoff = datetime.now() - timedelta(days=days)
    total = 0.0
    
    for trade in sold_trades:
        if not trade.sold_at:
            continue
        try:
            sold_time = datetime.fromisoformat(trade.sold_at)
            if sold_time >= cutoff:
                total += trade.actual_profit
        except (ValueError, TypeError):
            continue
    
    return total


def get_profit_trends(sold_trades: List) -> Dict[str, float]:
    """
    Get profit totals for day, week, month, and year.
    
    Args:
        sold_trades: List of TrackedTrade objects with status='sold'
    
    Returns: Dict with keys 'day', 'week', 'month', 'year' and profit values
    """
    return {
        "day": get_profit_by_period(sold_trades, 1),
        "week": get_profit_by_period(sold_trades, 7),
        "month": get_profit_by_period(sold_trades, 30),
        "year": get_profit_by_period(sold_trades, 365),
    }
