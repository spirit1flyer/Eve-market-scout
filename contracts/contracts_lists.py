"""User-managed JSON lists for the Contracts tab (Steps 5 + 6).

Two stores, both JSON (the contract *data* lives in contracts.db; these are the
small user-curated lists, kept human-readable and editable like the app's other
`*.json` user files):

  - ExcludeList → `contract_excludes.json`. Contracts the user never wants to
    see again. Stored by contract_id + a cached name/title/date so the file is
    legible. Applied at the LIST stage (before items are fetched), so excluding
    also trims fetch load.

  - ContractWatchlist → one `contract_watchlist_<region_id>.json` per region
    (growable). Each entry is a saved search (type_id + optional station + an
    optional max "cost per" threshold). Matching is PASSIVE: the hourly contract
    pull refreshes the cached scopes, and `match_region` diffs each entry's
    current cache hits against the ids it has already surfaced — new hits are
    the alerts. No separate poller.

All diagnostics carry the greppable `[ContractDiag]` tag.
"""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.sound_manager import get_data_dir

EXCLUDES_FILENAME = "contract_excludes.json"
WATCHLIST_FILENAME_FMT = "contract_watchlist_{region_id}.json"


def _print(msg: str) -> None:
    print(f"[ContractDiag] {msg}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Exclusion list
# =============================================================================

class ExcludeList:
    """Disk-backed set of excluded contract_ids (+ cached display fields)."""

    _SINGLETON: Optional["ExcludeList"] = None
    _LOCK = threading.Lock()

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (get_data_dir() / EXCLUDES_FILENAME)
        self._contracts: dict[int, dict] = {}
        self._load()

    @classmethod
    def singleton(cls) -> "ExcludeList":
        if cls._SINGLETON is None:
            with cls._LOCK:
                if cls._SINGLETON is None:
                    cls._SINGLETON = cls()
        return cls._SINGLETON

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for cid, meta in (data.get("contracts") or {}).items():
                self._contracts[int(cid)] = meta
            _print(f"loaded {len(self._contracts)} excluded contracts")
        except Exception as e:
            _print(f"exclude load failed: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"contracts": {str(k): v for k, v in self._contracts.items()}}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            _print(f"exclude save failed: {e}")

    def ids(self) -> set[int]:
        return set(self._contracts.keys())

    def is_excluded(self, contract_id: int) -> bool:
        return int(contract_id) in self._contracts

    def add(self, contract_id: int, title: Optional[str] = None,
            item_name: Optional[str] = None) -> None:
        cid = int(contract_id)
        self._contracts[cid] = {
            "title": title,
            "item_name": item_name,
            "date_added": _now_iso(),
        }
        self._save()
        _print(f"excluded contract {cid} ({item_name or title or '?'})")

    def remove(self, contract_id: int) -> None:
        if int(contract_id) in self._contracts:
            del self._contracts[int(contract_id)]
            self._save()
            _print(f"un-excluded contract {contract_id}")

    def all(self) -> list[dict]:
        return [{"contract_id": cid, **meta}
                for cid, meta in self._contracts.items()]


# =============================================================================
# Per-region contract watchlist
# =============================================================================

class ContractWatchlist:
    """Saved-search store for one region, backed by a JSON file.

    Entry shape:
      {
        "type_id": int,
        "type_name": str,
        "station_id": int | None,   # None = whole region
        "max_price": float | None,  # alert only when price <= this (the
                                     # "cost per" threshold; per-unit compare
                                     # is the caller's job for homogeneous
                                     # bundles)
        "date_added": iso,
        "seen_ids": [contract_id, ...],  # already-surfaced; diff target
      }
    Keyed by (type_id, station_id) so the same item at two stations is two
    entries.
    """

    _instances: dict[int, "ContractWatchlist"] = {}
    _LOCK = threading.Lock()

    def __init__(self, region_id: int, path: Optional[Path] = None):
        self.region_id = int(region_id)
        self.path = path or (
            get_data_dir() / WATCHLIST_FILENAME_FMT.format(region_id=self.region_id)
        )
        self._entries: dict[str, dict] = {}
        self._load()

    @classmethod
    def for_region(cls, region_id: int) -> "ContractWatchlist":
        rid = int(region_id)
        if rid not in cls._instances:
            with cls._LOCK:
                if rid not in cls._instances:
                    cls._instances[rid] = cls(rid)
        return cls._instances[rid]

    @staticmethod
    def _key(type_id: int, station_id: Optional[int]) -> str:
        return f"{int(type_id)}@{int(station_id) if station_id else 0}"

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for entry in data.get("entries") or []:
                key = self._key(entry["type_id"], entry.get("station_id"))
                self._entries[key] = entry
            _print(f"region {self.region_id} watchlist loaded "
                   f"{len(self._entries)} entries")
        except Exception as e:
            _print(f"watchlist load failed for region {self.region_id}: {e}")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"region_id": self.region_id,
                       "entries": list(self._entries.values())}
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            _print(f"watchlist save failed for region {self.region_id}: {e}")

    def entries(self) -> list[dict]:
        return list(self._entries.values())

    def add(self, type_id: int, type_name: str,
            station_id: Optional[int] = None,
            max_price: Optional[float] = None) -> None:
        key = self._key(type_id, station_id)
        existing = self._entries.get(key, {})
        self._entries[key] = {
            "type_id": int(type_id),
            "type_name": type_name,
            "station_id": int(station_id) if station_id else None,
            "max_price": max_price,
            "date_added": existing.get("date_added") or _now_iso(),
            "seen_ids": existing.get("seen_ids", []),
        }
        self._save()
        _print(f"region {self.region_id} watchlist + {type_name} "
               f"(station={station_id}, max={max_price})")

    def update_price(self, type_id: int, station_id: Optional[int],
                     max_price: Optional[float]) -> None:
        key = self._key(type_id, station_id)
        if key in self._entries:
            self._entries[key]["max_price"] = max_price
            self._save()

    def remove(self, type_id: int, station_id: Optional[int]) -> None:
        key = self._key(type_id, station_id)
        if key in self._entries:
            del self._entries[key]
            self._save()
            _print(f"region {self.region_id} watchlist - type {type_id}")

    # --- passive matching ---------------------------------------------------

    def match_region(self, db) -> list[dict]:
        """Diff each entry's current cache hits against already-seen ids.

        Returns a list of NEW match dicts (one per newly-surfaced contract):
          {type_id, type_name, station_id, contract_id, ...list-row fields}
        Updates each entry's seen_ids and persists. Designed to be called after
        the hourly pull has refreshed this region's cache.
        """
        new_matches: list[dict] = []
        dirty = False
        for entry in self._entries.values():
            type_id = entry["type_id"]
            station_id = entry.get("station_id")
            hits = set(db.find_contracts_with_type(type_id, self.region_id,
                                                   station_id))
            # Apply the price threshold against the contract's list price.
            max_price = entry.get("max_price")
            seen = set(entry.get("seen_ids", []))
            fresh = hits - seen
            for cid in fresh:
                row = db.get_list_row(cid)
                if row is None:
                    continue
                if max_price is not None and (row.get("price") or 0) > max_price:
                    continue
                new_matches.append({
                    "type_id": type_id,
                    "type_name": entry.get("type_name"),
                    "station_id": station_id,
                    "contract_id": cid,
                    "price": row.get("price"),
                    "title": row.get("title"),
                    "date_expired": row.get("date_expired"),
                    "start_location_id": row.get("start_location_id"),
                    "issuer_id": row.get("issuer_id"),
                })
            # Track all current hits as seen (prune stale ids to the live set so
            # a re-listed contract_id — which can't happen, ids are unique — or a
            # pruned one doesn't bloat the file forever).
            if hits != seen:
                entry["seen_ids"] = sorted(hits)
                dirty = True
        if dirty:
            self._save()
        if new_matches:
            _print(f"region {self.region_id} watchlist matched "
                   f"{len(new_matches)} new contracts")
        return new_matches
