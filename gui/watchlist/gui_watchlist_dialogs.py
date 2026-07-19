"""Dialogs for watchlist management - Add, Bulk Add, Search Match, Edit.

This module provides the main entry point and re-exports all dialog classes
for backward compatibility. The actual implementations are split across:
- gui_watchlist_add.py: AddItemDialog
- gui_watchlist_bulk.py: BulkAddDialog  
- gui_watchlist_search.py: SearchMatchDialog, EditItemDialog
"""

from typing import Optional
from dataclasses import dataclass, field, fields

# Re-export all dialog classes for backward compatibility
from gui.watchlist.gui_watchlist_add import AddItemDialog
from gui.watchlist.gui_watchlist_bulk import BulkAddDialog
from gui.watchlist.gui_watchlist_search import SearchMatchDialog, EditItemDialog


@dataclass
class WatchlistItem:
    """Represents an item being watched with alert conditions."""
    type_id: int
    name: str
    # Alert conditions (None = disabled)
    price_under: Optional[float] = None      # Alert if sell price drops below this
    price_over: Optional[float] = None       # Alert if sell price rises above this
    notes: str = ""                          # Personal notes
    categories: list[str] = field(default_factory=list)  # User-defined category tags (multi)

    # Current market data (populated during scan)
    current_price: Optional[float] = None
    current_qty: Optional[int] = None        # Quantity available at current_price
    last_updated: Optional[str] = None


def watchlist_item_from_dict(data: dict) -> WatchlistItem:
    """Build a WatchlistItem from persisted JSON, dropping unknown keys
    (e.g. the removed margin_over/current_margin fields in old files)."""
    known = {f.name for f in fields(WatchlistItem)}
    return WatchlistItem(**{k: v for k, v in data.items() if k in known})


# Expose all public classes
__all__ = [
    'WatchlistItem',
    'watchlist_item_from_dict',
    'AddItemDialog',
    'BulkAddDialog',
    'SearchMatchDialog',
    'EditItemDialog',
]
