"""Underbid detection for scanner inventory items.

Monitors active sell listings and detects when your price is no longer the
lowest at the hub. Keyed by type_id -- the underbid check is fundamentally
per-item, and the inventory model is one entry per (type_id, hub).
"""

from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass

from core.config import TRADE_HUBS


@dataclass
class UnderbidInfo:
    """Info about an underbid item."""
    type_id: int
    your_price: float
    lowest_price: float
    undercut_by: float  # How much lower the competition is

    @property
    def undercut_percent(self) -> float:
        if self.your_price > 0:
            return ((self.your_price - self.lowest_price) / self.your_price) * 100
        return 0.0


class UnderbidMonitor:
    """Monitors inventory listings for underbids, keyed by type_id."""

    def __init__(self):
        # type_ids where underbid warnings are suppressed
        self.ignored_underbids: Set[int] = set()
        # Last known underbid state, keyed by type_id
        self.underbid_state: Dict[int, UnderbidInfo] = {}

    def ignore_underbid(self, type_id: int):
        """Suppress underbid warnings for this item."""
        self.ignored_underbids.add(type_id)
        self.underbid_state.pop(type_id, None)

    def clear_ignore(self, type_id: int):
        """Stop suppressing warnings (e.g. user relisted)."""
        self.ignored_underbids.discard(type_id)

    def is_ignored(self, type_id: int) -> bool:
        return type_id in self.ignored_underbids

    def is_underbid(self, type_id: int) -> bool:
        return type_id in self.underbid_state

    def get_underbid_info(self, type_id: int) -> Optional[UnderbidInfo]:
        return self.underbid_state.get(type_id)

    def clear_type(self, type_id: int):
        """Forget all state for this type (e.g. entry deleted)."""
        self.ignored_underbids.discard(type_id)
        self.underbid_state.pop(type_id, None)

    def seed_ignored_from_inventory(self, entries):
        """Populate ignored_underbids from persisted InventoryEntry flags.

        Call once after the inventory loads so the in-memory ignore set matches
        what the user set in past sessions.
        """
        for entry in entries:
            if getattr(entry, "ignore_underbid", False):
                self.ignored_underbids.add(entry.type_id)

    def check_underbids(
        self,
        listings: List[Tuple[int, float]],
        market_orders: List[dict],
        hub_key: str,
    ) -> Dict[int, UnderbidInfo]:
        """Check inventory listings for underbids.

        Args:
            listings: List of (type_id, your_price) pairs. For an entry with
                multiple active listings, pass the HIGHEST listing price -- if
                that's not underbid, none of the lower ones are either.
            market_orders: Current market orders for the hub region.
            hub_key: Hub key (e.g. 'amarr', 'jita').

        Returns:
            Dict of {type_id: UnderbidInfo} for items that are underbid.
        """
        hub_config = TRADE_HUBS.get(hub_key)
        if not hub_config:
            return {}

        station_id = hub_config["station_id"]

        # Lookup: type_id -> lowest sell price at this station
        lowest_sells: Dict[int, float] = {}
        for order in market_orders:
            if order.get("is_buy_order", False):
                continue
            if order.get("location_id") != station_id:
                continue
            type_id = order.get("type_id")
            price = order.get("price", 0)
            if type_id and price > 0:
                if type_id not in lowest_sells or price < lowest_sells[type_id]:
                    lowest_sells[type_id] = price

        underbids: Dict[int, UnderbidInfo] = {}
        for type_id, your_price in listings:
            if type_id in self.ignored_underbids:
                continue
            if your_price <= 0:
                continue
            lowest = lowest_sells.get(type_id)
            if lowest is None:
                self.underbid_state.pop(type_id, None)
                continue
            if lowest < (your_price - 0.001):
                info = UnderbidInfo(
                    type_id=type_id,
                    your_price=your_price,
                    lowest_price=lowest,
                    undercut_by=your_price - lowest,
                )
                underbids[type_id] = info
                self.underbid_state[type_id] = info
            else:
                self.underbid_state.pop(type_id, None)

        return underbids
