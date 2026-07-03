"""Auto-tracked sales tracker for the NPC Orders tab.

Walks `wallet.transactions` after each ESI refresh, picks out sell-side rows
whose type_id is in the NPC Orders list, attaches the matching
`transaction_tax` amount from the journal at ingest time, and persists the
result per-character (`npc_sales_<character>.json`) so the ledger rolls
forward beyond the ESI journal window.

Display layer (the panel + treeview) lives in gui_npc_orders.py.
"""

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Dict, TYPE_CHECKING

from core.sound_manager import get_data_dir

if TYPE_CHECKING:
    from esi.esi_wallet import ESIWallet


SCHEMA_VERSION = 2  # v2 adds the "buys" array; v1 files load fine (empty buys).


def _slug(name: str) -> str:
    """Make a character name safe to use as a filename component."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", (name or "unknown").strip())
    return cleaned or "unknown"


def _sales_file(character_name: str) -> str:
    return str(get_data_dir() / f"npc_sales_{_slug(character_name)}.json")


@dataclass
class SaleRecord:
    transaction_id: int
    type_id: int
    type_name: str
    quantity: int
    unit_price: float
    gross: float
    sales_tax: float
    location_id: int
    date: str  # ISO 8601


@dataclass
class BuyRecord:
    transaction_id: int
    type_id: int
    type_name: str
    quantity: int
    unit_price: float
    total: float
    location_id: int
    date: str  # ISO 8601


class NPCSalesTracker:
    """Persistent, dedup'd ledger of sell-side transactions for NPC Orders items.

    One file per logged-in seller character. ingest() is idempotent -- already
    seen transaction_ids are skipped, so calling on every ESI refresh is safe.
    """

    def __init__(self, character_name: str):
        self.character_name = character_name or "unknown"
        self.sales: Dict[int, SaleRecord] = {}
        self.buys: Dict[int, BuyRecord] = {}
        # ISO-8601 cutoff. Transactions dated before this are skipped on
        # ingest -- used to truly "start fresh" after a reset, since the ESI
        # transactions endpoint exposes ~30 days of history that would
        # otherwise re-populate immediately.
        self.since_cutoff: Optional[str] = None
        self._load()

    def set_character(self, character_name: str):
        """Switch to a different character's ledger file."""
        new_name = character_name or "unknown"
        if new_name == self.character_name:
            return
        self.character_name = new_name
        self.sales = {}
        self.buys = {}
        self.since_cutoff = None
        self._load()

    def reset(self):
        """Wipe in-memory state and set a cutoff at "now" so any transactions
        already in the ESI window do not re-populate. The cutoff is persisted
        as part of the ledger, so it survives restarts.
        """
        from datetime import datetime, timezone
        self.sales = {}
        self.buys = {}
        self.since_cutoff = datetime.now(timezone.utc).isoformat()
        self.save()
        print(f"[NPCSales] reset: cutoff = {self.since_cutoff}")

    def _path(self) -> str:
        return _sales_file(self.character_name)

    def _load(self):
        path = self._path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[NPCSales] load error ({path}): {e}")
            return

        self.since_cutoff = data.get("since_cutoff") or None

        for rec in data.get("sales", []):
            try:
                s = SaleRecord(**rec)
            except TypeError:
                # Schema drift -- ignore unknown fields rather than crash.
                continue
            self.sales[s.transaction_id] = s

        for rec in data.get("buys", []):
            try:
                b = BuyRecord(**rec)
            except TypeError:
                continue
            self.buys[b.transaction_id] = b

    def save(self):
        import time as _pt
        _pt0 = _pt.perf_counter()
        path = self._path()
        try:
            payload = {
                "schema_version": SCHEMA_VERSION,
                "character_name": self.character_name,
                "since_cutoff": self.since_cutoff,
                "sales": [asdict(s) for s in self.sales.values()],
                "buys": [asdict(b) for b in self.buys.values()],
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[NPCSales] save error ({path}): {e}")
        _dur = _pt.perf_counter() - _pt0
        print(f"[PerfTimer] NPCSalesTracker.save char={self.character_name} dur={_dur*1000:.1f}ms sales={len(self.sales)} buys={len(self.buys)}")

    def ingest(self, wallet: "ESIWallet", tracked_type_ids: set) -> tuple[int, int]:
        """Pick up new buys + sells for tracked items.

        `tracked_type_ids` is the current NPC Orders list (caller passes it in
        so this module stays free of GUI coupling). Returns (new_sales, new_buys).
        """
        import time as _pt
        _pt0 = _pt.perf_counter()
        if wallet is None or not tracked_type_ids:
            print(f"[NPCSalesDiag] ingest skipped: wallet={wallet is not None}, "
                  f"tracked_count={len(tracked_type_ids) if tracked_type_ids else 0}")
            return (0, 0)

        _ts = _pt.perf_counter()
        tax_by_tx = _build_tax_index(wallet)
        _step_tax_index = _pt.perf_counter() - _ts

        # Parse cutoff once; tx.date is a datetime so we compare ISO strings
        # against the cutoff string (ISO-8601 sorts lexicographically).
        cutoff = self.since_cutoff or ""

        # Diagnostic counters so we can tell whether the hook is firing but
        # data is being filtered out (rather than silently no-op'ing).
        considered = 0
        filtered_type = 0
        filtered_cutoff = 0
        already_seen = 0

        new_sales = 0
        new_buys = 0
        for tx in wallet.transactions:
            considered += 1
            if tx.type_id not in tracked_type_ids:
                filtered_type += 1
                continue
            date_iso = tx.date.isoformat() if hasattr(tx.date, "isoformat") else str(tx.date)
            if cutoff and date_iso < cutoff:
                filtered_cutoff += 1
                continue

            if tx.is_buy:
                if tx.transaction_id in self.buys:
                    already_seen += 1
                    continue
                self.buys[tx.transaction_id] = BuyRecord(
                    transaction_id=tx.transaction_id,
                    type_id=tx.type_id,
                    type_name=tx.type_name or "",
                    quantity=tx.quantity,
                    unit_price=tx.unit_price,
                    total=tx.quantity * tx.unit_price,
                    location_id=tx.location_id,
                    date=date_iso,
                )
                new_buys += 1
            else:
                if tx.transaction_id in self.sales:
                    already_seen += 1
                    continue
                tax = tax_by_tx.get(tx.transaction_id, 0.0)
                self.sales[tx.transaction_id] = SaleRecord(
                    transaction_id=tx.transaction_id,
                    type_id=tx.type_id,
                    type_name=tx.type_name or "",
                    quantity=tx.quantity,
                    unit_price=tx.unit_price,
                    gross=tx.quantity * tx.unit_price,
                    sales_tax=tax,
                    location_id=tx.location_id,
                    date=date_iso,
                )
                new_sales += 1

        # Always print a one-line summary so we can see hook activity even
        # when nothing new is ingested.
        print(f"[NPCSalesDiag] {self.character_name}: "
              f"considered={considered}, "
              f"filtered_by_type_id={filtered_type}, "
              f"filtered_by_cutoff={filtered_cutoff}, "
              f"already_seen={already_seen}, "
              f"new_sales={new_sales}, new_buys={new_buys}, "
              f"tracked_items={len(tracked_type_ids)}, "
              f"cutoff={cutoff or 'none'}")

        if new_sales or new_buys:
            self.save()
            print(f"[NPCSales] {self.character_name}: "
                  f"ingested {new_sales} sale(s), {new_buys} buy(s)")

        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] NPCSalesTracker.ingest char={self.character_name} "
            f"total={_pt_total*1000:.0f}ms considered={considered} "
            f"new_sales={new_sales} new_buys={new_buys} "
            f"tax_index_build={_step_tax_index*1000:.0f}ms "
            f"wallet_tx={len(wallet.transactions)} journal={len(wallet.journal)}"
        )
        return (new_sales, new_buys)

    def compute_cost_basis(self, tracked_type_ids: set) -> dict:
        """FIFO-match buys to sells per type_id; return aggregate cost stats.

        Walks each item's buys and sells in chronological order, consuming
        the oldest buy lots first. Returns a dict:
          {
            "matched_cost":     ISK across all FIFO-matched sales,
            "matched_qty":      qty whose cost was matched to a buy lot,
            "unmatched_qty":    qty sold without a matching buy lot
                                (tracking gap -- inflates apparent profit),
          }
        """
        from datetime import datetime

        def _parse(d: str):
            try:
                return datetime.fromisoformat(d)
            except (ValueError, TypeError):
                return None

        matched_cost = 0.0
        matched_qty = 0
        unmatched_qty = 0

        # Per-type buckets
        buys_by_type: dict = {}
        sales_by_type: dict = {}
        for b in self.buys.values():
            if b.type_id not in tracked_type_ids:
                continue
            buys_by_type.setdefault(b.type_id, []).append(b)
        for s in self.sales.values():
            if s.type_id not in tracked_type_ids:
                continue
            sales_by_type.setdefault(s.type_id, []).append(s)

        for type_id, sales in sales_by_type.items():
            sales_sorted = sorted(sales, key=lambda x: _parse(x.date) or datetime.min)
            lots = sorted(buys_by_type.get(type_id, []),
                          key=lambda x: _parse(x.date) or datetime.min)
            # Mutable lot remaining-qty so we can draw down across sales.
            remaining = [[lot.quantity, lot.unit_price] for lot in lots]
            lot_idx = 0
            for sale in sales_sorted:
                need = sale.quantity
                while need > 0 and lot_idx < len(remaining):
                    available = remaining[lot_idx][0]
                    if available <= 0:
                        lot_idx += 1
                        continue
                    take = min(available, need)
                    matched_cost += take * remaining[lot_idx][1]
                    matched_qty += take
                    remaining[lot_idx][0] -= take
                    need -= take
                if need > 0:
                    unmatched_qty += need

        return {
            "matched_cost": matched_cost,
            "matched_qty": matched_qty,
            "unmatched_qty": unmatched_qty,
        }


def _build_tax_index(wallet: "ESIWallet") -> Dict[int, float]:
    """Return {transaction_id: sales_tax_amount}.

    Thin wrapper: the market_transaction → transaction_tax (entry_id + 1)
    bridge now lives in ESIWallet.build_sales_tax_index so the Stock Market
    P&L sync shares the exact same implementation.
    """
    return wallet.build_sales_tax_index()
