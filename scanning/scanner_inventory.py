"""Scanner inventory tracker for EVE Market Scout.

Per-(type_id, hub) inventory model with FIFO lot-based cost basis.
Replaces the per-deal tracking in trade_tracker.py with running inventory:
buys add lots, sales draw down lots oldest-first, restocks merge into the
same entry. Realized profit per sale is exact (sale_price - lot.buy_price).

This module is data-only: file I/O, dataclasses, manager. ESI sync lives in
scanner_inventory_sync.py.
"""

import json
import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict, field

from core.sound_manager import get_data_dir


def _inventory_file(hub_key: str) -> str:
    return str(get_data_dir() / f"scanner_inventory_{hub_key}.json")


@dataclass
class BuyLot:
    """A single buy transaction's contribution to inventory."""
    transaction_id: int
    buy_price: float
    qty_total: int
    qty_remaining: int  # Decreases as FIFO sales consume the lot
    bought_at: str  # ISO timestamp


@dataclass
class SaleRecord:
    """A single sell transaction with its FIFO-matched cost basis."""
    transaction_id: int
    sell_price: float
    quantity: int
    cost_basis: float  # Sum of (consumed_qty * lot.buy_price) across consumed lots
    sales_tax: float
    sold_at: str


@dataclass
class ActiveListing:
    """A live sell order for this item."""
    order_id: int
    list_price: float
    current_price: float
    qty_listed: int  # volume_total at time of listing
    qty_remaining: int  # volume_remain (live)
    listed_at: str
    broker_fee: float = 0
    relist_count: int = 0
    relist_fees: float = 0
    ignore_underbid: bool = False


@dataclass
class RelistRecord:
    """A price modification on an active listing."""
    order_id: int
    old_price: float
    new_price: float
    fee: float
    timestamp: str


@dataclass
class InventoryEntry:
    """Per-item running inventory.

    Identity: (type_id, hub_key) -- hub_key is implicit per-file.
    """
    type_id: int
    type_name: str

    # Running totals (dedup'd by transaction_id)
    quantity_in: int = 0  # Sum of all buy_lots qty_total
    quantity_out: int = 0  # Sum of all sales quantity
    total_buy_cost: float = 0  # Sum of all buy_price * qty_total (excl broker fees)
    total_revenue: float = 0  # Sum of sales sell_price * quantity (gross)
    total_sales_tax: float = 0
    total_listing_fees: float = 0  # All broker fees (initial + relists) across all listings ever
    total_realized_profit: float = 0  # Sum of (sale.sell_price - sale.cost_basis) - sale.sales_tax across sales

    # FIFO lots (oldest first)
    buy_lots: List[BuyLot] = field(default_factory=list)

    # Sale ledger (dedup source)
    sales: List[SaleRecord] = field(default_factory=list)

    # Active sell orders (multiple allowed for the same item)
    active_listings: List[ActiveListing] = field(default_factory=list)

    # Recently retired listings: order_id -> "fulfilled" | "expired" | "cancelled"
    # Used so we don't re-process an order that vanished
    retired_listings: Dict[str, str] = field(default_factory=dict)

    # Relist history
    relists: List[RelistRecord] = field(default_factory=list)

    # Metadata
    flagged_at: Optional[str] = None  # First time this item was added
    last_activity_at: Optional[str] = None
    notes: str = ""
    # Persistent "ignore underbid warnings for this item" flag.
    # Mirrors the per-listing ignore_underbid on ActiveListing but at item scope.
    ignore_underbid: bool = False

    # Projected from scanner (for vs-actual comparison)
    projected_buy_price: float = 0
    projected_sell_price: float = 0
    projected_profit_per_unit: float = 0

    # === Derived properties ===

    @property
    def quantity_held(self) -> int:
        """Items currently owned (in hangar + currently listed).

        Clamped at 0: an orphan sale (its buy aged out of the ESI window)
        pushes quantity_out above quantity_in; that imbalance stays visible
        on the raw counters, but held stock can't be negative.
        """
        return max(0, self.quantity_in - self.quantity_out)

    @property
    def quantity_listed(self) -> int:
        """Items currently in active sell orders."""
        return sum(a.qty_remaining for a in self.active_listings)

    @property
    def quantity_in_hangar(self) -> int:
        """Items owned but not currently on sale."""
        return max(0, self.quantity_held - self.quantity_listed)

    @property
    def average_buy_price(self) -> float:
        """Weighted average cost across all lots ever bought."""
        if self.quantity_in <= 0:
            return 0
        return self.total_buy_cost / self.quantity_in

    @property
    def remaining_cost_basis(self) -> float:
        """Cost basis of unsold inventory (sum of lot.buy_price * qty_remaining)."""
        return sum(lot.buy_price * lot.qty_remaining for lot in self.buy_lots)

    @property
    def average_sell_price(self) -> float:
        if self.quantity_out <= 0:
            return 0
        return self.total_revenue / self.quantity_out

    @property
    def is_active(self) -> bool:
        """Has held inventory or active listings."""
        return self.quantity_held > 0 or len(self.active_listings) > 0


class InventoryManager:
    """Manages scanner inventory entries for a single hub with persistence."""

    def __init__(self, hub_key: str = "amarr"):
        self.hub_key = hub_key
        self.entries: Dict[int, InventoryEntry] = {}  # type_id -> entry
        # Write-coalescing: mutators mark _dirty + schedule one idle flush
        # instead of writing per-call (was 14+ full-JSON rewrites per ESI cycle).
        self._dirty = False
        self._save_scheduled = False
        self._dirty_count = 0  # number of _save() calls since last actual write
        self._load()
        import atexit
        atexit.register(self._atexit_flush)

    def _atexit_flush(self):
        print(f"[InventoryCoalesce] hub={self.hub_key} atexit: dirty={self._dirty} pending_marks={self._dirty_count}")
        self.flush()

    def set_hub(self, hub_key: str):
        """Switch to a different hub. Saves current, loads new."""
        if hub_key == self.hub_key:
            return
        # Flush synchronously: hub_key changes the destination file path,
        # so any pending writes must hit the *current* file first.
        print(f"[InventoryCoalesce] set_hub: {self.hub_key} -> {hub_key} (flushing first, dirty={self._dirty})")
        self.flush()
        self.hub_key = hub_key
        self.entries = {}
        self._load()

    # === Persistence ===

    def _load(self):
        path = _inventory_file(self.hub_key)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            for entry_data in data.get("entries", []):
                entry = self._deserialize_entry(entry_data)
                self.entries[entry.type_id] = entry
        except (json.JSONDecodeError, IOError, TypeError, KeyError) as e:
            print(f"[ScannerInventory] Error loading {path}: {e}")

    def _save(self):
        """Mark dirty and schedule one coalesced flush on next idle tick.

        Repeated callers within one mainloop iteration collapse to a single
        actual disk write. Falls back to immediate write if tk_queue has no
        root yet (e.g. during module import).
        """
        self._dirty = True
        self._dirty_count += 1
        if self._save_scheduled:
            return
        self._save_scheduled = True
        try:
            from core.tk_queue import schedule_idle
            schedule_idle(self._flush_save)
            print(f"[InventoryCoalesce] hub={self.hub_key} mark: dirty, idle flush scheduled")
        except Exception as e:
            # No tk root available; do the write now so data isn't lost.
            print(f"[InventoryCoalesce] hub={self.hub_key} mark: schedule_idle failed ({e}), flushing inline")
            self._flush_save()

    def _flush_save(self):
        """Idle-callback flush. Skips the write if nothing changed."""
        coalesced = self._dirty_count
        self._save_scheduled = False
        if not self._dirty:
            print(f"[InventoryCoalesce] hub={self.hub_key} flush: idle fired but not dirty (coalesced={coalesced})")
            self._dirty_count = 0
            return
        print(f"[InventoryCoalesce] hub={self.hub_key} flush: writing (coalesced={coalesced})")
        self._do_save_now()
        self._dirty = False
        self._dirty_count = 0

    def flush(self):
        """Synchronous immediate flush. Use before destination changes
        (set_hub) and at shutdown (atexit).
        """
        if not self._dirty:
            return
        coalesced = self._dirty_count
        print(f"[InventoryCoalesce] hub={self.hub_key} flush: explicit write (coalesced={coalesced})")
        self._do_save_now()
        self._dirty = False
        self._dirty_count = 0

    def _do_save_now(self):
        import time as _pt
        _pt0 = _pt.perf_counter()
        path = _inventory_file(self.hub_key)
        try:
            data = {
                "hub": self.hub_key,
                "entries": [asdict(e) for e in self.entries.values()],
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"[ScannerInventory] Error saving {path}: {e}")
        _dur = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] scanner_inventory._save hub={self.hub_key} dur={_dur*1000:.1f}ms entries={len(self.entries)}")

    def _deserialize_entry(self, data: dict) -> InventoryEntry:
        """Rehydrate an entry from JSON, including nested dataclasses."""
        buy_lots = [BuyLot(**lot) for lot in data.pop("buy_lots", [])]
        sales = [SaleRecord(**s) for s in data.pop("sales", [])]
        active_listings = [ActiveListing(**a) for a in data.pop("active_listings", [])]
        relists = [RelistRecord(**r) for r in data.pop("relists", [])]
        entry = InventoryEntry(**data)
        entry.buy_lots = buy_lots
        entry.sales = sales
        entry.active_listings = active_listings
        entry.relists = relists
        return entry

    def save(self):
        """Public save (called after batched updates by sync layer)."""
        self._save()

    # === Lookup ===

    def get(self, type_id: int) -> Optional[InventoryEntry]:
        return self.entries.get(type_id)

    def get_or_create(self, type_id: int, type_name: str) -> InventoryEntry:
        entry = self.entries.get(type_id)
        if entry is None:
            entry = InventoryEntry(
                type_id=type_id,
                type_name=type_name,
                flagged_at=datetime.now().isoformat(),
            )
            self.entries[type_id] = entry
        elif type_name and not entry.type_name:
            entry.type_name = type_name
        return entry

    def all_entries(self) -> List[InventoryEntry]:
        return list(self.entries.values())

    def active_entries(self) -> List[InventoryEntry]:
        return [e for e in self.entries.values() if e.is_active]

    # === Mutations ===

    def flag_from_scanner(self, type_id: int, type_name: str,
                          projected_buy: float = 0, projected_sell: float = 0,
                          projected_profit_per_unit: float = 0) -> InventoryEntry:
        """Add or update an entry from a scanner deal flag."""
        entry = self.get_or_create(type_id, type_name)
        # Always update projections to the most recent flag
        entry.projected_buy_price = projected_buy
        entry.projected_sell_price = projected_sell
        entry.projected_profit_per_unit = projected_profit_per_unit
        entry.last_activity_at = datetime.now().isoformat()
        self._save()
        return entry

    def record_buy(self, type_id: int, type_name: str, transaction_id: int,
                   buy_price: float, quantity: int,
                   bought_at: Optional[str] = None) -> Tuple[InventoryEntry, bool]:
        """Record a buy transaction. Dedup'd by transaction_id.

        Returns (entry, was_new) - was_new=False if this transaction was already recorded.
        """
        entry = self.get_or_create(type_id, type_name)

        # Dedup
        for lot in entry.buy_lots:
            if lot.transaction_id == transaction_id:
                return entry, False

        ts = bought_at or datetime.now().isoformat()
        lot = BuyLot(
            transaction_id=transaction_id,
            buy_price=buy_price,
            qty_total=quantity,
            qty_remaining=quantity,
            bought_at=ts,
        )
        # Insert in chronological order (oldest first) so FIFO consumption is straightforward
        entry.buy_lots.append(lot)
        entry.buy_lots.sort(key=lambda l: l.bought_at)

        entry.quantity_in += quantity
        entry.total_buy_cost += buy_price * quantity
        entry.last_activity_at = ts
        self._save()
        return entry, True

    def record_sale(self, type_id: int, type_name: str, transaction_id: int,
                    sell_price: float, quantity: int, sales_tax: float = 0,
                    sold_at: Optional[str] = None) -> Tuple[Optional[InventoryEntry], bool]:
        """Record a sell transaction with FIFO lot consumption.

        Returns (entry, was_new). was_new=False if already recorded.
        An orphan sale (no inventory for this type) gets a minimal entry
        created; unmatched units carry cost basis 0.
        """
        entry = self.entries.get(type_id)
        if entry is None:
            # No buys ever recorded for this type -- orphan sale, can't FIFO-match.
            # Create a minimal entry so the sale isn't silently lost.
            entry = self.get_or_create(type_id, type_name)

        # Dedup
        for s in entry.sales:
            if s.transaction_id == transaction_id:
                return entry, False

        ts = sold_at or datetime.now().isoformat()

        # FIFO consume from buy_lots
        cost_basis = self._consume_lots_fifo(entry, quantity)

        sale = SaleRecord(
            transaction_id=transaction_id,
            sell_price=sell_price,
            quantity=quantity,
            cost_basis=cost_basis,
            sales_tax=sales_tax,
            sold_at=ts,
        )
        entry.sales.append(sale)
        entry.sales.sort(key=lambda s: s.sold_at)

        entry.quantity_out += quantity
        entry.total_revenue += sell_price * quantity
        entry.total_sales_tax += sales_tax

        # Realized profit for this sale: revenue - cost_basis - sales_tax
        # (listing fees are tracked separately and subtracted at the entry level)
        entry.total_realized_profit += (sell_price * quantity) - cost_basis - sales_tax
        entry.last_activity_at = ts
        self._save()
        return entry, True

    def _consume_lots_fifo(self, entry: InventoryEntry, quantity: int) -> float:
        """Draw `quantity` units from the oldest-available buy lots.

        Returns total cost basis of consumed units.
        If lots are insufficient (orphan sale), missing units are treated as
        cost-basis 0 (i.e. pure profit) -- this preserves accounting integrity
        when a sale is found before its buy.
        """
        remaining = quantity
        cost_basis = 0.0
        for lot in entry.buy_lots:
            if remaining <= 0:
                break
            if lot.qty_remaining <= 0:
                continue
            take = min(lot.qty_remaining, remaining)
            cost_basis += take * lot.buy_price
            lot.qty_remaining -= take
            remaining -= take
        # If remaining > 0, we have an orphan sale; cost_basis stays at what we matched.
        return cost_basis

    def add_or_update_listing(self, type_id: int, type_name: str,
                              order_id: int, list_price: float,
                              qty_listed: int, qty_remaining: int,
                              listed_at: str, broker_fee: float = 0
                              ) -> Tuple[InventoryEntry, ActiveListing, bool]:
        """Register a new active listing or update an existing one's volume_remain/price.

        Returns (entry, listing, was_new).
        """
        entry = self.get_or_create(type_id, type_name)

        # If we previously retired this order, don't resurrect it
        if str(order_id) in entry.retired_listings:
            # Find or create a stub for return value
            for a in entry.active_listings:
                if a.order_id == order_id:
                    return entry, a, False
            # Should not happen, but return a transient stub
            stub = ActiveListing(
                order_id=order_id, list_price=list_price, current_price=list_price,
                qty_listed=qty_listed, qty_remaining=qty_remaining,
                listed_at=listed_at, broker_fee=broker_fee
            )
            return entry, stub, False

        for listing in entry.active_listings:
            if listing.order_id == order_id:
                # Update volume_remain (price changes are handled via record_relist)
                listing.qty_remaining = qty_remaining
                self._save()
                return entry, listing, False

        listing = ActiveListing(
            order_id=order_id,
            list_price=list_price,
            current_price=list_price,
            qty_listed=qty_listed,
            qty_remaining=qty_remaining,
            listed_at=listed_at,
            broker_fee=broker_fee,
        )
        entry.active_listings.append(listing)
        entry.total_listing_fees += broker_fee
        entry.last_activity_at = listed_at
        self._save()
        return entry, listing, True

    def record_relist(self, type_id: int, order_id: int,
                      new_price: float, fee: float) -> Optional[ActiveListing]:
        entry = self.entries.get(type_id)
        if entry is None:
            return None
        for listing in entry.active_listings:
            if listing.order_id == order_id:
                old_price = listing.current_price
                listing.current_price = new_price
                listing.relist_count += 1
                listing.relist_fees += fee
                listing.ignore_underbid = False
                ts = datetime.now().isoformat()
                entry.relists.append(RelistRecord(
                    order_id=order_id,
                    old_price=old_price,
                    new_price=new_price,
                    fee=fee,
                    timestamp=ts,
                ))
                entry.total_listing_fees += fee
                entry.last_activity_at = ts
                self._save()
                return listing
        return None

    def record_orphan_listing(self, type_id: int, order_id: int,
                              broker_fee: float, state: str = "fulfilled"
                              ) -> Optional[InventoryEntry]:
        """Record a listing whose lifecycle finished before sync observed it.

        When a listing is placed and fulfilled entirely between two syncs we
        never get to capture its broker fee via the active-orders path. The
        sync layer calls this with either the actual fee (looked up in the
        journal) or an estimated fee (sale_price * rate) so total_listing_fees
        stays accurate. Marks the order as retired so re-processing is a no-op.
        """
        entry = self.entries.get(type_id)
        if entry is None:
            return None
        if str(order_id) in entry.retired_listings:
            return entry
        entry.total_listing_fees += broker_fee
        entry.retired_listings[str(order_id)] = state
        entry.last_activity_at = datetime.now().isoformat()
        self._save()
        return entry

    def retire_listing(self, type_id: int, order_id: int, reason: str) -> Optional[InventoryEntry]:
        """Remove an order from active_listings and remember its fate.

        reason: "fulfilled" | "expired" | "cancelled"
        For "expired"/"cancelled" the items effectively return to in-hangar inventory
        (no quantity_out change). For "fulfilled" the qty_out is already accounted
        for via the sale transactions.
        """
        entry = self.entries.get(type_id)
        if entry is None:
            return None
        entry.active_listings = [a for a in entry.active_listings if a.order_id != order_id]
        entry.retired_listings[str(order_id)] = reason
        entry.last_activity_at = datetime.now().isoformat()
        self._save()
        return entry

    def set_ignore_underbid_for_type(self, type_id: int, ignore: bool) -> bool:
        """Set item-level ignore_underbid flag (persists)."""
        entry = self.entries.get(type_id)
        if entry is None:
            return False
        entry.ignore_underbid = ignore
        self._save()
        return True

    def set_ignore_underbid(self, type_id: int, order_id: int, ignore: bool) -> bool:
        entry = self.entries.get(type_id)
        if entry is None:
            return False
        for listing in entry.active_listings:
            if listing.order_id == order_id:
                listing.ignore_underbid = ignore
                self._save()
                return True
        return False

    def delete_entry(self, type_id: int) -> bool:
        if type_id in self.entries:
            del self.entries[type_id]
            self._save()
            return True
        return False

    # === Summary ===

    def get_summary(self) -> dict:
        entries = list(self.entries.values())
        active = [e for e in entries if e.is_active]
        total_realized = sum(e.total_realized_profit for e in entries)
        total_fees = sum(e.total_listing_fees for e in entries)
        # Cash-flow net: realized profit minus every listing/relist fee ever paid.
        realized_after_fees = total_realized - total_fees

        return {
            "total_items": len(entries),
            "active_items": len(active),
            "total_quantity_in": sum(e.quantity_in for e in entries),
            "total_quantity_out": sum(e.quantity_out for e in entries),
            "total_quantity_held": sum(e.quantity_held for e in entries),
            "total_quantity_listed": sum(e.quantity_listed for e in entries),
            "total_revenue": sum(e.total_revenue for e in entries),
            "total_buy_cost": sum(e.total_buy_cost for e in entries),
            "total_sales_tax": sum(e.total_sales_tax for e in entries),
            "total_listing_fees": total_fees,
            "total_realized_profit_gross": total_realized,
            "total_realized_profit_net": realized_after_fees,
            "remaining_cost_basis": sum(e.remaining_cost_basis for e in entries),
        }
