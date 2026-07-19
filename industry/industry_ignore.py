"""Shared ignore list for the Industry tab (Top Profit + Owned BPO/BPC).

Both lists answer "what's worth building" over the same universe of products, so
"hide this, I never want to see it" should be one shared, persisted set rather
than a per-panel toggle. The set is keyed by **product type_id** (the thing the
blueprint manufactures), so ignoring an item in either view hides it in both.

It also owns the automatic name-based skip: EVE seeds a handful of junk/event
products whose names start with "Expired " (e.g. "Expired Azdaja Redoubt
Filament") — they have nominal recipes but are never worth building and only
clutter the ranked list. `is_auto_hidden(name)` filters those unconditionally;
they are separate from the user's manual ignore set.

Persisted to `industry_ignored.json` in the shared AppData dir. Mirrors the
swallow-and-log JSON conventions of `contracts_lists.ExcludeList`.
"""

import json
import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from core.sound_manager import get_data_dir

logger = logging.getLogger(__name__)

IGNORE_FILENAME = "industry_ignored.json"

# Product-name prefixes that are auto-hidden from both Industry lists regardless
# of the manual ignore set. Lower-cased; matched as a prefix on the trimmed name.
AUTO_HIDE_PREFIXES = ("expired ",)

_SINGLETON: Optional["IgnoreList"] = None
_SINGLETON_LOCK = threading.Lock()


def is_auto_hidden(name: Optional[str]) -> bool:
    """True for products that should never appear (e.g. 'Expired …' filaments)."""
    if not name:
        return False
    low = name.strip().lower()
    return any(low.startswith(p) for p in AUTO_HIDE_PREFIXES)


class IgnoreList:
    """User-curated set of ignored product type_ids (with cached display names).

    Shared by the Top Profit list and the Owned BPO/BPC panel. The cached name
    is display-only (the manage dialog shows it); membership is decided by
    type_id so a name change in the SDE never orphans an entry.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (get_data_dir() / IGNORE_FILENAME)
        self._lock = threading.Lock()
        self._items: dict[int, str] = {}  # type_id -> cached name
        self._load()

    @classmethod
    def singleton(cls) -> "IgnoreList":
        global _SINGLETON
        if _SINGLETON is None:
            with _SINGLETON_LOCK:
                if _SINGLETON is None:
                    _SINGLETON = cls()
        return _SINGLETON

    # ----------------------------------------------------------------- io
    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._items = {int(k): str(v) for k, v in data.items()}
        except Exception:
            logger.exception("[IndustryIgnore] load failed from %s", self.path)
            self._items = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({str(k): v for k, v in self._items.items()},
                           indent=2),
                encoding="utf-8")
        except Exception:
            logger.exception("[IndustryIgnore] save failed to %s", self.path)

    # ----------------------------------------------------------------- api
    def contains(self, type_id: int) -> bool:
        return int(type_id) in self._items

    def add(self, type_id: int, name: str = "") -> None:
        with self._lock:
            self._items[int(type_id)] = name or str(type_id)
            self._save()

    def remove(self, type_id: int) -> None:
        with self._lock:
            if int(type_id) in self._items:
                del self._items[int(type_id)]
                self._save()

    def all(self) -> List[Tuple[int, str]]:
        """[(type_id, name)] sorted by name for the manage dialog."""
        return sorted(self._items.items(), key=lambda kv: kv[1].lower())

    def count(self) -> int:
        return len(self._items)
