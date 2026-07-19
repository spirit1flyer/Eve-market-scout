"""Trade tracking for EVE Market Scout - tracks flagged items from buy to sell.

Status flow:
- pending: Flagged from scanner, waiting to buy and list
- listed: Bought AND listed for sale (ESI detected both)
- sold: Sale complete
- cancelled: Manually cancelled
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, asdict, field

from core.calculate import TradingSkills, DEFAULT_SKILLS
from core.calculate_trades import (
    calculate_trade_break_even,
    calculate_projected_profit,
    get_margin_to_break_even as _get_margin_to_break_even,
    get_undercuts_remaining as _get_undercuts_remaining
)
from core.sound_manager import get_data_dir

# Trades file - use centralized data directory
TRADES_FILE = str(get_data_dir() / "tracked_trades.json")


@dataclass
class TrackedTrade:
    """A trade being tracked from flagging through sale completion.
    
    Status flow: pending -> listed -> sold
    """
    
    # Identity
    trade_id: str  # Unique ID (timestamp-based)
    type_id: int
    type_name: str
    
    # Status: 'pending', 'listed', 'sold', 'cancelled'
    status: str = "pending"
    
    # Original deal info (from scanner)
    projected_buy_price: float = 0
    projected_sell_price: float = 0  # ceiling_price from deal
    projected_profit: float = 0
    flagged_at: Optional[str] = None
    
    # Actual buy (from ESI transaction)
    buy_price: float = 0
    buy_quantity: int = 0
    buy_broker_fee: float = 0  # Usually 0 for instant buys
    buy_transaction_id: Optional[int] = None
    bought_at: Optional[str] = None
    
    # Sell order (from ESI orders)
    sell_order_id: Optional[int] = None
    list_price: float = 0  # Initial list price (actual, not projected)
    current_price: float = 0  # Current price after modifications
    list_broker_fee: float = 0
    listed_at: Optional[str] = None
    
    # Modifications
    relist_fees: float = 0
    relist_count: int = 0
    price_history: List[dict] = field(default_factory=list)  # [{price, fee, timestamp}]
    
    # Sale completion
    sell_quantity: int = 0
    sell_revenue: float = 0  # Gross revenue from sales
    sales_tax: float = 0
    sold_at: Optional[str] = None
    
    # Notes
    notes: str = ""
    
    # Underbid monitoring
    ignore_underbid: bool = False  # If True, don't warn about underbids
    
    # === Calculated properties (skill-independent) ===
    
    @property
    def total_buy_cost(self) -> float:
        """Total cost to acquire items (buy price + buy broker fee)."""
        return (self.buy_price * self.buy_quantity) + self.buy_broker_fee
    
    @property
    def total_fees(self) -> float:
        """All fees paid so far."""
        return self.buy_broker_fee + self.list_broker_fee + self.relist_fees + self.sales_tax
    
    @property
    def cost_basis(self) -> float:
        """Total cost basis before sale (buy + all selling fees except sales tax)."""
        return self.total_buy_cost + self.list_broker_fee + self.relist_fees
    
    @property
    def actual_profit(self) -> float:
        """Actual profit (only meaningful when sold)."""
        if self.status != "sold":
            return 0
        # Revenue minus all costs and fees
        return self.sell_revenue - self.sales_tax - self.cost_basis
    
    @property
    def profit_per_unit(self) -> float:
        """Actual profit per unit."""
        if self.sell_quantity == 0:
            return 0
        return self.actual_profit / self.sell_quantity
    
    @property
    def vs_projected(self) -> float:
        """Difference between actual and projected profit."""
        if self.status != "sold":
            return 0
        return self.actual_profit - (self.projected_profit * self.buy_quantity)


class TradeTracker:
    """Manages tracked trades with persistence.
    
    Holds skills context for accurate fee calculations.
    """

    def __init__(self, skills: Optional[TradingSkills] = None):
        """
        Args:
            skills: Character's trading skills for fee calculations
        """
        self.skills = skills or DEFAULT_SKILLS
        self.trades: Dict[str, TrackedTrade] = {}  # trade_id -> TrackedTrade
        self.type_index: Dict[int, List[str]] = {}  # type_id -> [trade_ids]
        self._load()

    def set_skills(self, skills: TradingSkills):
        """Update skills for fee calculations."""
        self.skills = skills

    # === Skill-aware calculations (delegate to calculate_trades.py) ===
    
    def get_break_even(self, trade: TrackedTrade) -> float:
        """Calculate current break-even price for a trade."""
        return calculate_trade_break_even(trade, self.skills)
    
    def get_margin_to_break_even(self, trade: TrackedTrade) -> float:
        """How much above break-even the current price is."""
        return _get_margin_to_break_even(trade, self.skills)
    
    def get_undercuts_remaining(self, trade: TrackedTrade) -> int:
        """Estimated 0.01 ISK undercuts before hitting break-even."""
        return _get_undercuts_remaining(trade, self.skills)
    
    def get_projected_profit_at_price(self, trade: TrackedTrade, sell_price: float) -> float:
        """Calculate what profit would be if sold at given price."""
        return calculate_projected_profit(trade, sell_price, self.skills)

    # === File I/O ===

    def _load(self):
        """Load trades from file."""
        if os.path.exists(TRADES_FILE):
            try:
                with open(TRADES_FILE, 'r') as f:
                    data = json.load(f)
                for trade_data in data.get("trades", []):
                    # Migration: convert old status names
                    if trade_data.get("status") == "flagged":
                        trade_data["status"] = "pending"
                    elif trade_data.get("status") == "bought":
                        # "bought" was a broken intermediate state
                        # If we have buy data but no list data, keep as pending
                        # If we have both, it should be listed
                        if trade_data.get("sell_order_id") or trade_data.get("list_price", 0) > 0:
                            trade_data["status"] = "listed"
                        else:
                            trade_data["status"] = "pending"
                    
                    trade = TrackedTrade(**trade_data)
                    self.trades[trade.trade_id] = trade
                    self._index_trade(trade)
            except (json.JSONDecodeError, IOError, TypeError) as e:
                print(f"Error loading trades: {e}")

    def _save(self):
        """Save trades to file."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        try:
            trades_list = [asdict(t) for t in self.trades.values()]
            with open(TRADES_FILE, 'w') as f:
                json.dump({"trades": trades_list}, f, indent=2)
        except IOError as e:
            print(f"Error saving trades: {e}")
        _dur = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] trade_tracker._save dur={_dur*1000:.1f}ms trades={len(self.trades)}")

    def _index_trade(self, trade: TrackedTrade):
        """Add trade to type index."""
        if trade.type_id not in self.type_index:
            self.type_index[trade.type_id] = []
        if trade.trade_id not in self.type_index[trade.type_id]:
            self.type_index[trade.type_id].append(trade.trade_id)

    def _generate_id(self) -> str:
        """Generate unique trade ID."""
        return datetime.now().strftime("%Y%m%d_%H%M%S_%f")

    # === Trade creation and updates ===

    def flag_for_buy(self, type_id: int, type_name: str,
                     projected_buy: float = 0, projected_sell: float = 0,
                     projected_profit: float = 0) -> TrackedTrade:
        """Flag an item for tracking (from deals tab)."""
        trade = TrackedTrade(
            trade_id=self._generate_id(),
            type_id=type_id,
            type_name=type_name,
            status="pending",
            projected_buy_price=projected_buy,
            projected_sell_price=projected_sell,
            projected_profit=projected_profit,
            flagged_at=datetime.now().isoformat()
        )
        
        self.trades[trade.trade_id] = trade
        self._index_trade(trade)
        self._save()
        return trade

    def record_full_trade(self, type_id: int, type_name: str,
                          buy_price: float, buy_quantity: int, buy_transaction_id: int,
                          sell_order_id: int, list_price: float, 
                          list_broker_fee: float = 0) -> TrackedTrade:
        """Record a complete buy+list in one call (for ESI backfill)."""
        now = datetime.now().isoformat()
        
        trade = TrackedTrade(
            trade_id=self._generate_id(),
            type_id=type_id,
            type_name=type_name,
            flagged_at=now
        )
        
        # Buy data
        trade.buy_price = buy_price
        trade.buy_quantity = buy_quantity
        trade.buy_transaction_id = buy_transaction_id
        trade.bought_at = now
        
        # List data
        trade.sell_order_id = sell_order_id
        trade.list_price = list_price
        trade.current_price = list_price
        trade.list_broker_fee = list_broker_fee
        trade.listed_at = now
        
        # Status
        trade.status = "listed"
        
        # Start price history
        trade.price_history = [{
            "price": list_price,
            "fee": list_broker_fee,
            "timestamp": now,
            "type": "initial"
        }]
        
        self.trades[trade.trade_id] = trade
        self._index_trade(trade)
        self._save()
        return trade

    def update_buy_info(self, trade_id: str, price: float, quantity: int, 
                        transaction_id: int = None) -> Optional[TrackedTrade]:
        """Update buy information for a trade (doesn't change status)."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.buy_price = price
        trade.buy_quantity = quantity
        trade.buy_transaction_id = transaction_id
        trade.bought_at = datetime.now().isoformat()
        
        # If we now have both buy AND listing data, mark as listed
        if trade.sell_order_id is not None and trade.list_price > 0:
            trade.status = "listed"
        
        self._save()
        return trade

    def update_listing_info(self, trade_id: str, order_id: int, price: float, 
                            broker_fee: float = 0) -> Optional[TrackedTrade]:
        """Update listing information and set status to listed if we have buy info."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.sell_order_id = order_id
        trade.list_price = price
        trade.current_price = price
        trade.list_broker_fee = broker_fee
        trade.listed_at = datetime.now().isoformat()
        
        # Start price history
        trade.price_history = [{
            "price": price,
            "fee": broker_fee,
            "timestamp": trade.listed_at,
            "type": "initial"
        }]
        
        # If we have buy data, we're fully listed now
        if trade.buy_price > 0 and trade.buy_quantity > 0:
            trade.status = "listed"
        
        self._save()
        return trade

    def record_relist(self, trade_id: str, new_price: float, 
                      fee: float) -> Optional[TrackedTrade]:
        """Record a price modification."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.current_price = new_price
        trade.relist_fees += fee
        trade.relist_count += 1
        
        # Reset ignore_underbid on price change
        trade.ignore_underbid = False
        
        trade.price_history.append({
            "price": new_price,
            "fee": fee,
            "timestamp": datetime.now().isoformat(),
            "type": "relist"
        })
        
        self._save()
        return trade

    def record_sale(self, trade_id: str, quantity: int, revenue: float,
                    sales_tax: float = 0) -> Optional[TrackedTrade]:
        """Record partial or complete sale."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.sell_quantity += quantity
        trade.sell_revenue += revenue
        trade.sales_tax += sales_tax
        
        # Check if fully sold
        if trade.sell_quantity >= trade.buy_quantity:
            trade.status = "sold"
            trade.sold_at = datetime.now().isoformat()
        
        self._save()
        return trade

    def cancel_trade(self, trade_id: str) -> Optional[TrackedTrade]:
        """Mark trade as cancelled."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.status = "cancelled"
        self._save()
        return trade

    def delete_trade(self, trade_id: str) -> bool:
        """Delete a trade entirely."""
        if trade_id in self.trades:
            trade = self.trades[trade_id]
            # Remove from index
            if trade.type_id in self.type_index:
                self.type_index[trade.type_id] = [
                    tid for tid in self.type_index[trade.type_id] 
                    if tid != trade_id
                ]
            del self.trades[trade_id]
            self._save()
            return True
        return False

    def set_ignore_underbid(self, trade_id: str, ignore: bool) -> Optional[TrackedTrade]:
        """Set or clear ignore_underbid flag for a trade."""
        trade = self.trades.get(trade_id)
        if not trade:
            return None
        
        trade.ignore_underbid = ignore
        self._save()
        return trade

    # === Queries ===

    def get_trade(self, trade_id: str) -> Optional[TrackedTrade]:
        """Get a specific trade."""
        return self.trades.get(trade_id)

    def get_trades_for_type(self, type_id: int) -> List[TrackedTrade]:
        """Get all trades for an item type."""
        trade_ids = self.type_index.get(type_id, [])
        return [self.trades[tid] for tid in trade_ids if tid in self.trades]

    def get_active_trades(self) -> List[TrackedTrade]:
        """Get trades that are in progress (pending or listed)."""
        return [t for t in self.trades.values() 
                if t.status in ("pending", "listed")]

    def get_by_status(self, status: str) -> List[TrackedTrade]:
        """Get trades by status."""
        return [t for t in self.trades.values() if t.status == status]

    def get_recent_trades(self, limit: int = 50) -> List[TrackedTrade]:
        """Get most recent trades."""
        trades = list(self.trades.values())
        trades.sort(key=lambda t: t.flagged_at or "", reverse=True)
        return trades[:limit]

    # === Summary stats ===

    def get_summary(self) -> dict:
        """Get summary statistics."""
        trades = list(self.trades.values())
        
        sold = [t for t in trades if t.status == "sold"]
        active = [t for t in trades if t.status in ("pending", "listed")]
        
        total_profit = sum(t.actual_profit for t in sold)
        total_projected = sum(t.projected_profit * t.buy_quantity for t in sold)
        total_fees = sum(t.total_fees for t in sold)
        
        # Active capital (money tied up in active trades)
        active_capital = sum(t.cost_basis for t in active if t.status == "listed")
        
        return {
            "total_trades": len(trades),
            "sold_count": len(sold),
            "active_count": len(active),
            "total_profit": total_profit,
            "total_projected": total_projected,
            "vs_projected": total_profit - total_projected,
            "total_fees_paid": total_fees,
            "active_capital": active_capital,
            "win_rate": len([t for t in sold if t.actual_profit > 0]) / len(sold) if sold else 0
        }

    def get_trade_by_order(self, order_id: int) -> Optional[TrackedTrade]:
        """Find trade by sell order ID."""
        for trade in self.trades.values():
            if trade.sell_order_id == order_id:
                return trade
        return None

    def get_trade_by_transaction(self, transaction_id: int) -> Optional[TrackedTrade]:
        """Find trade by buy transaction ID."""
        for trade in self.trades.values():
            if trade.buy_transaction_id == transaction_id:
                return trade
        return None

    def get_pending_trade_for_type(self, type_id: int) -> Optional[TrackedTrade]:
        """Get the pending trade for a type_id, if any."""
        trade_ids = self.type_index.get(type_id, [])
        for tid in trade_ids:
            trade = self.trades.get(tid)
            if trade and trade.status == "pending":
                return trade
        return None

    def get_active_trade_for_type(self, type_id: int) -> Optional[TrackedTrade]:
        """Get the active (pending or listed) trade for a type_id, if any."""
        trade_ids = self.type_index.get(type_id, [])
        for tid in trade_ids:
            trade = self.trades.get(tid)
            if trade and trade.status in ("pending", "listed"):
                return trade
        return None
