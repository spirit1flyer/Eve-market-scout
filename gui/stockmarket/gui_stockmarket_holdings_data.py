"""Data classes and persistence for Stock Market Holdings.

Handles HoldingEntry dataclass and HoldingsManager with ESI transaction tracking.
"""

import json
from datetime import datetime
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List
from pathlib import Path

from core.config import get_hub_config
from core.sound_manager import get_data_dir


# Holdings file per hub
HOLDINGS_FILE_PREFIX = "stock_holdings_"


@dataclass
class HoldingEntry:
    """A single holding entry with ESI transaction tracking."""
    type_id: int
    type_name: str
    
    # Position data
    quantity_held: int = 0
    average_cost: float = 0.0
    
    # Order tracking (from ESI or manual)
    active_buy_orders: int = 0
    active_sell_orders: int = 0
    
    # Manual tracking
    is_watched: bool = False  # Manually pinned to watch
    notes: str = ""
    
    # Timestamps
    date_added: str = ""
    last_updated: str = ""
    
    # ESI Transaction tracking - avoid double-processing
    processed_buy_ids: List[int] = field(default_factory=list)
    processed_sell_ids: List[int] = field(default_factory=list)
    
    # Cumulative stats
    total_bought: int = 0
    total_sold: int = 0
    total_buy_cost: float = 0.0
    total_sell_revenue: float = 0.0
    realized_profit: float = 0.0


class HoldingsManager:
    """Manages holdings data for a hub with ESI transaction sync."""
    
    def __init__(self, hub_key: str):
        self.hub_key = hub_key
        self.hub_config = get_hub_config(hub_key)
        self.region_id = self.hub_config["region_id"]
        self.station_id = self.hub_config["station_id"]
        
        self.filepath = get_data_dir() / f"{HOLDINGS_FILE_PREFIX}{hub_key}.json"
        self.holdings: Dict[int, HoldingEntry] = {}
        
        self._load()
    
    def _load(self):
        """Load holdings from disk."""
        if not self.filepath.exists():
            return
        
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            for type_id_str, entry_data in data.get("holdings", {}).items():
                type_id = int(type_id_str)
                # Handle missing fields for backwards compatibility
                if "processed_buy_ids" not in entry_data:
                    entry_data["processed_buy_ids"] = []
                if "processed_sell_ids" not in entry_data:
                    entry_data["processed_sell_ids"] = []
                if "total_bought" not in entry_data:
                    entry_data["total_bought"] = 0
                if "total_sold" not in entry_data:
                    entry_data["total_sold"] = 0
                if "total_buy_cost" not in entry_data:
                    entry_data["total_buy_cost"] = 0.0
                if "total_sell_revenue" not in entry_data:
                    entry_data["total_sell_revenue"] = 0.0
                if "realized_profit" not in entry_data:
                    entry_data["realized_profit"] = 0.0
                
                self.holdings[type_id] = HoldingEntry(**entry_data)
        except Exception as e:
            print(f"[Holdings] Error loading: {e}")
    
    def _save(self):
        """Save holdings to disk."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        try:
            data = {
                "holdings": {
                    str(tid): asdict(entry)
                    for tid, entry in self.holdings.items()
                },
                "last_saved": datetime.now().isoformat(),
            }

            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Holdings] Error saving: {e}")
        _dur = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] HoldingsManager._save dur={_dur*1000:.1f}ms holdings={len(self.holdings)}")
    
    def add_watched(self, type_id: int, type_name: str) -> HoldingEntry:
        """Add an item to watch list."""
        if type_id in self.holdings:
            self.holdings[type_id].is_watched = True
            self.holdings[type_id].last_updated = datetime.now().isoformat()
        else:
            now = datetime.now().isoformat()
            self.holdings[type_id] = HoldingEntry(
                type_id=type_id,
                type_name=type_name,
                is_watched=True,
                date_added=now,
                last_updated=now,
            )
        
        self._save()
        return self.holdings[type_id]
    
    def add_inventory(
        self,
        type_id: int,
        type_name: str,
        quantity: int,
        avg_cost: float,
        transaction_id: Optional[int] = None
    ) -> HoldingEntry:
        """Add or update inventory for an item (buy transaction).
        
        Args:
            type_id: Item type ID
            type_name: Item name
            quantity: Quantity bought
            avg_cost: Price per unit
            transaction_id: ESI transaction ID to track (avoids double-processing)
        
        Returns:
            Updated HoldingEntry
        """
        now = datetime.now().isoformat()
        
        if type_id in self.holdings:
            entry = self.holdings[type_id]
            
            # Check if already processed
            if transaction_id and transaction_id in entry.processed_buy_ids:
                return entry
            
            # Update with weighted average
            old_value = entry.quantity_held * entry.average_cost
            new_value = quantity * avg_cost
            total_qty = entry.quantity_held + quantity
            
            if total_qty > 0:
                entry.average_cost = (old_value + new_value) / total_qty
            entry.quantity_held = total_qty
            entry.last_updated = now
            
            # Track cumulative stats
            entry.total_bought += quantity
            entry.total_buy_cost += quantity * avg_cost
            
            # Mark transaction as processed
            if transaction_id:
                entry.processed_buy_ids.append(transaction_id)
        else:
            self.holdings[type_id] = HoldingEntry(
                type_id=type_id,
                type_name=type_name,
                quantity_held=quantity,
                average_cost=avg_cost,
                date_added=now,
                last_updated=now,
                total_bought=quantity,
                total_buy_cost=quantity * avg_cost,
                processed_buy_ids=[transaction_id] if transaction_id else [],
            )
        
        self._save()
        return self.holdings[type_id]
    
    def record_sale(
        self,
        type_id: int,
        quantity: int,
        price_per_unit: float,
        transaction_id: Optional[int] = None
    ) -> Optional[HoldingEntry]:
        """Record a sale transaction.
        
        Args:
            type_id: Item type ID
            quantity: Quantity sold
            price_per_unit: Sale price per unit
            transaction_id: ESI transaction ID to track (avoids double-processing)
        
        Returns:
            Updated HoldingEntry or None if item not in holdings
        """
        if type_id not in self.holdings:
            return None
        
        entry = self.holdings[type_id]
        
        # Check if already processed
        if transaction_id and transaction_id in entry.processed_sell_ids:
            return entry
        
        now = datetime.now().isoformat()
        
        # Calculate profit for this sale
        revenue = quantity * price_per_unit
        cost_basis = quantity * entry.average_cost
        profit = revenue - cost_basis
        
        # Update entry
        entry.quantity_held = max(0, entry.quantity_held - quantity)
        entry.total_sold += quantity
        entry.total_sell_revenue += revenue
        entry.realized_profit += profit
        entry.last_updated = now
        
        # Mark transaction as processed
        if transaction_id:
            entry.processed_sell_ids.append(transaction_id)
        
        self._save()
        return entry
    
    def update_from_orders(
        self,
        type_id: int,
        type_name: str,
        buy_orders: int = 0,
        sell_orders: int = 0
    ):
        """Update order counts for an item (from ESI sync).
        
        Only updates items already in holdings - does NOT auto-add.
        """
        if type_id not in self.holdings:
            return  # Don't auto-add from ESI
        
        now = datetime.now().isoformat()
        entry = self.holdings[type_id]
        entry.active_buy_orders = buy_orders
        entry.active_sell_orders = sell_orders
        entry.last_updated = now
        
        self._save()
    
    def remove(self, type_id: int) -> bool:
        """Remove an item from holdings."""
        if type_id in self.holdings:
            del self.holdings[type_id]
            self._save()
            return True
        return False
    
    def get_all(self) -> List[HoldingEntry]:
        """Get all holdings."""
        return list(self.holdings.values())
    
    def has_item(self, type_id: int) -> bool:
        """Check if item is in holdings."""
        return type_id in self.holdings
    
    def get_item(self, type_id: int) -> Optional[HoldingEntry]:
        """Get a specific holding."""
        return self.holdings.get(type_id)
    
    def get_type_ids(self) -> List[int]:
        """Get all type IDs in holdings."""
        return list(self.holdings.keys())
    
    def set_average_cost(self, type_id: int, new_cost: float) -> bool:
        """Manually set average cost for an item."""
        if type_id not in self.holdings:
            return False
        
        entry = self.holdings[type_id]
        entry.average_cost = new_cost
        entry.last_updated = datetime.now().isoformat()
        self._save()
        return True
    
    def set_quantity(self, type_id: int, new_qty: int) -> bool:
        """Manually set quantity for an item."""
        if type_id not in self.holdings:
            return False
        
        entry = self.holdings[type_id]
        entry.quantity_held = max(0, new_qty)
        entry.last_updated = datetime.now().isoformat()
        self._save()
        return True
    
    def clear_transaction_history(self, type_id: int) -> bool:
        """Clear processed transaction IDs (for re-syncing)."""
        if type_id not in self.holdings:
            return False
        
        entry = self.holdings[type_id]
        entry.processed_buy_ids = []
        entry.processed_sell_ids = []
        self._save()
        return True
