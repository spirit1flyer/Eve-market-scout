"""Profit & Loss tracking for Stock Market holdings.

Tracks all fees and revenue for accurate P&L calculation:
- Broker fees on buy/sell order placement
- Modification fees when order prices change
- Sales tax on completed sales

Integrates with ESI refresh cycle to detect order modifications.
"""

import json
from datetime import datetime, timezone
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List
from pathlib import Path

from core.sound_manager import get_data_dir
from core.calculate import (
    TradingSkills, load_cached_skills,
    calculate_broker_fee, calculate_sales_tax, calculate_relist_fee,
    get_broker_fee_rate, get_sales_tax_rate
)


PNL_FILE_PREFIX = "stock_pnl_"


@dataclass
class OrderSnapshot:
    """Snapshot of an order for detecting price changes."""
    order_id: int
    type_id: int
    price: float
    volume_remain: int
    is_buy_order: bool
    last_seen: str = ""


@dataclass
class PnLEntry:
    """P&L tracking for a single item type."""
    type_id: int
    type_name: str

    # Fee totals (accumulated)
    buy_broker_fees: float = 0.0      # Fees on buy order placements
    sell_broker_fees: float = 0.0     # Fees on sell order placements
    modification_fees: float = 0.0    # Relist/modification fees
    sales_tax_paid: float = 0.0       # Tax on completed sales

    # Transaction totals
    total_bought_value: float = 0.0   # Total ISK spent on buys (before fees)
    total_sold_value: float = 0.0     # Total ISK received from sales (before tax)
    total_bought_qty: int = 0
    total_sold_qty: int = 0

    # Escrow committed in currently-active buy orders. Recomputed each refresh
    # (not accumulated) — represents ISK locked but not yet realized.
    escrow_committed: float = 0.0

    # Tracking
    processed_buy_order_ids: List[int] = field(default_factory=list)
    processed_sell_order_ids: List[int] = field(default_factory=list)
    processed_transaction_ids: List[int] = field(default_factory=list)

    # Timestamps
    first_activity: str = ""
    last_activity: str = ""

    @property
    def total_fees(self) -> float:
        """Sum of all fees paid."""
        return (self.buy_broker_fees + self.sell_broker_fees +
                self.modification_fees + self.sales_tax_paid)

    @property
    def realized_pnl(self) -> float:
        """Realized profit/loss (sold value - cost basis of sold - fees on sold)."""
        if self.total_sold_qty == 0:
            return 0.0
        # Approximate cost basis using average
        if self.total_bought_qty > 0:
            avg_cost = self.total_bought_value / self.total_bought_qty
            cost_of_sold = avg_cost * self.total_sold_qty
        else:
            cost_of_sold = 0.0
        return self.total_sold_value - cost_of_sold - self.sales_tax_paid

    @property
    def total_invested(self) -> float:
        """Total ISK tied up: realized buys + escrow on open buy orders.

        Buy broker fees are NOT included here — they're accounted for in total_fees.
        Including them here would double-count when computing P&L as
        sold - invested - fees.
        """
        return self.total_bought_value + self.escrow_committed

    @property
    def realized_pnl_simple(self) -> float:
        """Per-item realized P&L: sold - realized cost basis - all fees.

        Excludes escrow (those buys aren't realized yet).
        """
        return self.total_sold_value - self.total_bought_value - self.total_fees


class PnLManager:
    """Manages P&L tracking for a hub with order modification detection."""
    
    def __init__(self, hub_key: str):
        self.hub_key = hub_key
        self.filepath = get_data_dir() / f"{PNL_FILE_PREFIX}{hub_key}.json"
        
        # P&L entries by type_id
        self.entries: Dict[int, PnLEntry] = {}
        
        # Cached order state for modification detection
        self.order_cache: Dict[int, OrderSnapshot] = {}  # order_id -> snapshot
        
        self._load()
    
    def _load(self):
        """Load P&L data from disk."""
        if not self.filepath.exists():
            return
        
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Load entries
            for type_id_str, entry_data in data.get("entries", {}).items():
                type_id = int(type_id_str)
                # Handle missing fields
                for list_field in ["processed_buy_order_ids", "processed_sell_order_ids", 
                                   "processed_transaction_ids"]:
                    if list_field not in entry_data:
                        entry_data[list_field] = []
                self.entries[type_id] = PnLEntry(**entry_data)
            
            # Load order cache
            for order_id_str, snap_data in data.get("order_cache", {}).items():
                order_id = int(order_id_str)
                self.order_cache[order_id] = OrderSnapshot(**snap_data)
                
            print(f"[PnL] Loaded {len(self.entries)} entries, {len(self.order_cache)} cached orders for {self.hub_key}")
        except Exception as e:
            print(f"[PnL] Error loading {self.hub_key}: {e}")
    
    def _save(self):
        """Save P&L data to disk."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        try:
            data = {
                "entries": {
                    str(tid): asdict(entry)
                    for tid, entry in self.entries.items()
                },
                "order_cache": {
                    str(oid): asdict(snap)
                    for oid, snap in self.order_cache.items()
                },
                "hub_key": self.hub_key,
                "last_saved": datetime.now().isoformat(),
            }

            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[PnL] Error saving {self.hub_key}: {e}")
        _dur = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] stock_pnl._save hub={self.hub_key} dur={_dur*1000:.1f}ms entries={len(self.entries)} orders={len(self.order_cache)}")
    
    def _get_or_create_entry(self, type_id: int, type_name: str) -> PnLEntry:
        """Get existing entry or create new one."""
        if type_id not in self.entries:
            now = datetime.now().isoformat()
            self.entries[type_id] = PnLEntry(
                type_id=type_id,
                type_name=type_name,
                first_activity=now,
                last_activity=now,
            )
        return self.entries[type_id]
    
    def _get_skills(self) -> TradingSkills:
        """Load cached skills for this hub."""
        return load_cached_skills(self.hub_key, slot="seller")

    # === Dedup helpers (caller short-circuits before doing journal lookup) ===

    def is_buy_order_processed(self, order_id: int, type_id: int) -> bool:
        entry = self.entries.get(type_id)
        return entry is not None and order_id in entry.processed_buy_order_ids

    def is_sell_order_processed(self, order_id: int, type_id: int) -> bool:
        entry = self.entries.get(type_id)
        return entry is not None and order_id in entry.processed_sell_order_ids

    def is_transaction_processed(self, transaction_id: int, type_id: int) -> bool:
        entry = self.entries.get(type_id)
        return entry is not None and transaction_id in entry.processed_transaction_ids

    # === Escrow (derived each refresh, not accumulated) ===

    def reset_escrow(self):
        """Zero out escrow_committed on every entry. Caller adds fresh values from current orders."""
        for entry in self.entries.values():
            entry.escrow_committed = 0.0

    def add_escrow(self, type_id: int, type_name: str, escrow: float):
        """Add escrow from a currently-active buy order to the type's entry."""
        if escrow <= 0:
            return
        entry = self._get_or_create_entry(type_id, type_name)
        entry.escrow_committed += escrow

    # === Order Placement Recording ===

    def record_buy_order(self, order_id: int, type_id: int, type_name: str,
                         price: float, quantity: int, fee_amount: float) -> float:
        """Record a buy order placement with an externally-supplied broker fee.

        Caller is responsible for looking up the actual fee in the wallet journal
        (or computing an estimate fallback). Idempotent: returns 0 if already processed.
        """
        entry = self._get_or_create_entry(type_id, type_name)

        if order_id in entry.processed_buy_order_ids:
            return 0.0

        entry.buy_broker_fees += fee_amount
        entry.processed_buy_order_ids.append(order_id)
        entry.last_activity = datetime.now().isoformat()

        # Cache the order for modification tracking
        self.order_cache[order_id] = OrderSnapshot(
            order_id=order_id,
            type_id=type_id,
            price=price,
            volume_remain=quantity,
            is_buy_order=True,
            last_seen=datetime.now().isoformat(),
        )

        self._save()
        print(f"[PnL] Buy order {order_id}: {type_name} - broker fee {fee_amount:,.0f} ISK")
        return fee_amount

    def record_sell_order(self, order_id: int, type_id: int, type_name: str,
                          price: float, quantity: int, fee_amount: float) -> float:
        """Record a sell order placement with an externally-supplied broker fee.

        Caller looks up the actual fee in the wallet journal (or falls back to estimate).
        Idempotent: returns 0 if already processed.
        """
        entry = self._get_or_create_entry(type_id, type_name)

        if order_id in entry.processed_sell_order_ids:
            return 0.0

        entry.sell_broker_fees += fee_amount
        entry.processed_sell_order_ids.append(order_id)
        entry.last_activity = datetime.now().isoformat()

        # Cache the order for modification tracking
        self.order_cache[order_id] = OrderSnapshot(
            order_id=order_id,
            type_id=type_id,
            price=price,
            volume_remain=quantity,
            is_buy_order=False,
            last_seen=datetime.now().isoformat(),
        )

        self._save()
        print(f"[PnL] Sell order {order_id}: {type_name} - broker fee {fee_amount:,.0f} ISK")
        return fee_amount
    
    # === Order Modification Detection ===

    def check_order_modifications(self, current_orders: List[dict],
                                  type_id_filter: Optional[set] = None,
                                  wallet=None,
                                  age_limit_days: int = 25) -> Dict[int, float]:
        """Compare current orders to cache, detect price changes, record mod fees.

        Fee source priority:
          1. wallet.get_broker_fee_for_order(order_id, issued=order.issued) — the
             order's `issued` updates on modification, so the journal entry at that
             timestamp is the mod fee.
          2. If journal entry not found and order is fresh (< age_limit_days):
             skip the cache update so the modification re-detects next refresh.
          3. If journal entry not found and order is past retention (>= age_limit_days):
             fall back to calculate_relist_fee skill estimate.

        Args:
            current_orders: List of order dicts from ESI (need keys: order_id, type_id,
                            price, volume_remain, is_buy_order, issued)
            type_id_filter: Optional set of type_ids to track (holdings)
            wallet: ESIWallet (optional; if None, falls straight to estimate)
            age_limit_days: After this many days, give up on journal and use estimate

        Returns:
            Dict of {order_id: modification_fee} for orders that changed
        """
        modification_fees: Dict[int, float] = {}
        now_iso = datetime.now().isoformat()
        now_utc = datetime.now(timezone.utc)
        skills_cache: Optional[TradingSkills] = None  # lazy-load only if we need fallback

        current_order_ids = set()

        for order in current_orders:
            order_id = order.get("order_id")
            if not order_id:
                continue

            current_order_ids.add(order_id)
            type_id = order.get("type_id")

            # Skip if not in our tracked holdings
            if type_id_filter and type_id not in type_id_filter:
                continue

            price = order.get("price", 0)
            volume = order.get("volume_remain", 0)
            is_buy = order.get("is_buy_order", False)
            issued = order.get("issued")  # tz-aware datetime, or None

            # Check if we have this order cached
            if order_id in self.order_cache:
                cached = self.order_cache[order_id]

                # Detect price change
                if abs(cached.price - price) > 0.001:
                    fee = 0.0
                    fee_source = ""

                    # Try journal first (date-proximity to order.issued)
                    if wallet is not None and issued is not None:
                        fee = wallet.get_broker_fee_for_order(order_id, issued=issued)
                        if fee > 0:
                            fee_source = "journal"

                    if fee <= 0:
                        # Journal entry not found. Decide retry vs estimate-fallback.
                        age_days = (now_utc - issued).days if issued is not None else 999
                        if age_days < age_limit_days:
                            # Skip this refresh — leave cached.price unchanged so we
                            # re-detect the same modification next refresh once the
                            # journal entry has appeared. Update volume + last_seen.
                            cached.volume_remain = volume
                            cached.last_seen = now_iso
                            print(f"[PnL] Order {order_id} mod detected ({cached.price:,.2f} -> "
                                  f"{price:,.2f}) but journal entry not visible yet (age {age_days}d) "
                                  f"— retrying next refresh")
                            continue

                        # Past journal retention; fall back to skill estimate
                        if skills_cache is None:
                            skills_cache = self._get_skills()
                        fee = calculate_relist_fee(cached.price, price, volume, skills_cache)
                        fee_source = "estimate"

                    if fee > 0:
                        modification_fees[order_id] = fee

                        if type_id in self.entries:
                            entry = self.entries[type_id]
                            entry.modification_fees += fee
                            entry.last_activity = now_iso
                            print(f"[PnL] Order {order_id} mod: {cached.price:,.2f} -> "
                                  f"{price:,.2f}, fee {fee:,.0f} ISK ({fee_source})")

                # Commit cache update (only reached if no retry-skip above)
                cached.price = price
                cached.volume_remain = volume
                cached.last_seen = now_iso
            else:
                # New order — cache it. Initial placement fee is recorded via
                # record_buy/sell_order in sync_orders_to_pnl.
                self.order_cache[order_id] = OrderSnapshot(
                    order_id=order_id,
                    type_id=type_id,
                    price=price,
                    volume_remain=volume,
                    is_buy_order=is_buy,
                    last_seen=now_iso,
                )

        # Clean up expired orders from cache
        expired = [oid for oid in self.order_cache if oid not in current_order_ids]
        for oid in expired:
            del self.order_cache[oid]

        if modification_fees:
            self._save()

        return modification_fees
    
    # === Transaction Recording ===
    
    def record_buy_fill(self, transaction_id: int, type_id: int, type_name: str,
                        quantity: int, price: float):
        """Record a buy transaction (order filled or instant buy)."""
        entry = self._get_or_create_entry(type_id, type_name)
        
        if transaction_id in entry.processed_transaction_ids:
            return
        
        entry.total_bought_value += quantity * price
        entry.total_bought_qty += quantity
        entry.processed_transaction_ids.append(transaction_id)
        entry.last_activity = datetime.now().isoformat()
        
        self._save()
    
    def record_sale(self, transaction_id: int, type_id: int, type_name: str,
                    quantity: int, price: float, tax_amount: float) -> float:
        """Record a sale transaction with an externally-supplied sales tax.

        Caller looks up the actual tax via wallet.get_sales_tax_for_transaction()
        (or falls back to estimate). Idempotent: returns 0 if already processed.
        """
        entry = self._get_or_create_entry(type_id, type_name)

        if transaction_id in entry.processed_transaction_ids:
            return 0.0

        entry.total_sold_value += quantity * price
        entry.total_sold_qty += quantity
        entry.sales_tax_paid += tax_amount
        entry.processed_transaction_ids.append(transaction_id)
        entry.last_activity = datetime.now().isoformat()

        self._save()
        print(f"[PnL] Sale: {quantity}x {type_name} @ {price:,.2f} - tax {tax_amount:,.0f} ISK")
        return tax_amount
    
    # === Summary Methods ===
    
    def get_entry(self, type_id: int) -> Optional[PnLEntry]:
        """Get P&L entry for a specific item."""
        return self.entries.get(type_id)
    
    def get_all_entries(self) -> List[PnLEntry]:
        """Get all P&L entries."""
        return list(self.entries.values())
    
    def get_summary(self) -> dict:
        """Get aggregate P&L summary for the hub.

        Returns:
            Dict with: realized_invested (cost basis of items owned), escrow_committed
            (ISK locked in open buy orders), total_invested (displayed = realized+escrow),
            total_sold, fee breakdown, total_fees, net_pnl (realized only).
        """
        realized_invested = 0.0
        escrow_committed = 0.0
        total_sold = 0.0
        total_buy_fees = 0.0
        total_sell_fees = 0.0
        total_mod_fees = 0.0
        total_tax = 0.0

        for entry in self.entries.values():
            realized_invested += entry.total_bought_value
            escrow_committed += entry.escrow_committed
            total_sold += entry.total_sold_value
            total_buy_fees += entry.buy_broker_fees
            total_sell_fees += entry.sell_broker_fees
            total_mod_fees += entry.modification_fees
            total_tax += entry.sales_tax_paid

        total_fees = total_buy_fees + total_sell_fees + total_mod_fees + total_tax

        # Display: invested = realized cost basis + escrow on open buy orders.
        # Math:    net_pnl uses realized only — escrow isn't yet inventory.
        total_invested = realized_invested + escrow_committed
        net_pnl = total_sold - realized_invested - total_fees

        return {
            "realized_invested": realized_invested,
            "escrow_committed": escrow_committed,
            "total_invested": total_invested,
            "total_sold": total_sold,
            "buy_broker_fees": total_buy_fees,
            "sell_broker_fees": total_sell_fees,
            "modification_fees": total_mod_fees,
            "sales_tax": total_tax,
            "total_fees": total_fees,
            "net_pnl": net_pnl,
            "item_count": len(self.entries),
        }
    
    def clear_entry(self, type_id: int) -> bool:
        """Remove P&L entry for an item."""
        if type_id in self.entries:
            del self.entries[type_id]
            self._save()
            return True
        return False
    
    def clear_all(self):
        """Clear all P&L data for this hub."""
        self.entries.clear()
        self.order_cache.clear()
        self._save()
        print(f"[PnL] Cleared all data for {self.hub_key}")
