"""Persistent jump-count cache for system-to-system route lookups.

Used by the NPC Orders max-buy calc to constrain buy-order candidates to
within N jumps of a chosen origin. ESI `/route/{origin}/{destination}/`
returns the route as an array of system_ids; jump count == len(route) - 1.

Cache lives in `jump_cache.json` in the data dir. Keyed by an "origin -> dest"
string so the JSON file stays readable. Origin is fixed within a session
(the scanner's selected sell hub system), but we persist across origins
because the user might switch hubs and benefit from prior lookups.

This module is GUI-free: callers handle UI feedback. Use `get_jumps_async`
inside an aiohttp session you control; `JumpCache.lookup` is a sync helper
that returns a cached value or None.
"""

import json
import os
from typing import Optional, TYPE_CHECKING

from core.sound_manager import get_data_dir

if TYPE_CHECKING:
    import aiohttp


def _cache_path() -> str:
    return str(get_data_dir() / "jump_cache.json")


class JumpCache:
    """Singleton-ish cache. Instantiate once; the data is loaded lazily."""

    _instance: "Optional[JumpCache]" = None

    @classmethod
    def singleton(cls) -> "JumpCache":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # Two-level dict: {origin_system_id: {dest_system_id: jumps}}
        self._data: dict[int, dict[int, int]] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        path = _cache_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # JSON keys are strings; convert back to ints.
                for origin_str, dests in raw.items():
                    origin = int(origin_str)
                    self._data[origin] = {int(d): int(j) for d, j in dests.items()}
            except Exception as e:
                print(f"[JumpCache] load error: {e}")
        self._loaded = True

    def _save(self):
        path = _cache_path()
        try:
            # JSON can't have int keys at the top level either; stringify both.
            payload = {
                str(origin): {str(d): j for d, j in dests.items()}
                for origin, dests in self._data.items()
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[JumpCache] save error: {e}")

    def lookup(self, origin: int, dest: int) -> Optional[int]:
        """Cached jump count or None."""
        self._load()
        if origin == dest:
            return 0
        return self._data.get(origin, {}).get(dest)

    def put(self, origin: int, dest: int, jumps: int):
        self._load()
        self._data.setdefault(origin, {})[dest] = jumps
        self._save()

    async def fetch(self, session: "aiohttp.ClientSession",
                    origin: int, dest: int) -> Optional[int]:
        """Look up jumps via ESI `/route/{origin}/{dest}/`. Caches on success.

        Returns None on any failure (no route, network error, etc.).
        """
        cached = self.lookup(origin, dest)
        if cached is not None:
            return cached
        url = f"https://esi.evetech.net/latest/route/{origin}/{dest}/"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                route = await resp.json()
                if not isinstance(route, list) or not route:
                    return None
                jumps = max(0, len(route) - 1)
        except Exception as e:
            print(f"[JumpCache] fetch error {origin}->{dest}: {e}")
            return None
        self.put(origin, dest, jumps)
        return jumps
