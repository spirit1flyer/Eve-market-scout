"""Persistent station -> owner-corp / faction / system lookup.

Used by the NPC Orders max-buy calc to figure out which corp owns the
station of a buy order, so the user's standings against that corp/faction
can be plugged into the sales-tax formula.

Resolution order:
  1. Built-in TRADE_HUBS (no ESI call needed; corp_id + faction_id known).
  2. ESI `/universe/stations/{station_id}/` -> owner (corp_id), system_id,
     race_id. race_id is mapped to faction_id via RACE_TO_FACTION (ESI's
     `/corporations/{id}/` no longer returns faction_id for NPC corps, so
     using the station's race_id is the only reliable path).
  3. Player structures (station_id >= 1e12): returned as None -- NPC sales
     tax doesn't apply the same way; caller should fall back to default.

Cache lives in `station_info_cache.json` in the data dir. Persisted across
sessions because station ownership effectively never changes.
"""

import json
import os
from typing import Optional, TYPE_CHECKING

from core.sound_manager import get_data_dir

if TYPE_CHECKING:
    import aiohttp


PLAYER_STRUCTURE_ID_THRESHOLD = 1_000_000_000_000

# Station race_id -> empire faction_id. Covers the four major NPC empires
# that own trade hubs. Other race_ids (e.g. Jove) map to None; calc falls
# back to corp-standing-only behavior, which is fine for niche cases.
RACE_TO_FACTION = {
    1: 500001,  # Caldari State
    2: 500002,  # Minmatar Republic
    4: 500003,  # Amarr Empire
    8: 500004,  # Gallente Federation
}


def _cache_path() -> str:
    return str(get_data_dir() / "station_info_cache.json")


class StationLookup:
    """Singleton-ish station_id -> info cache."""

    _instance: "Optional[StationLookup]" = None

    @classmethod
    def singleton(cls) -> "StationLookup":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        # station_id -> {"corp_id", "faction_id", "system_id", "name"}
        self._data: dict[int, dict] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        path = _cache_path()
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for sid_str, info in raw.get("stations", {}).items():
                    self._data[int(sid_str)] = info
            except Exception as e:
                print(f"[StationLookup] load error: {e}")
        self._loaded = True

    def _save(self):
        path = _cache_path()
        try:
            payload = {
                "stations": {str(sid): info for sid, info in self._data.items()},
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[StationLookup] save error: {e}")

    def lookup(self, station_id: int) -> Optional[dict]:
        """Synchronous cached lookup. Returns None if not yet resolved."""
        self._load()
        # Built-in hubs first -- avoids ever hitting ESI for the common case.
        from core.config import TRADE_HUBS
        for cfg in TRADE_HUBS.values():
            if cfg.get("station_id") == station_id:
                return {
                    "corp_id": cfg.get("corp_id"),
                    "faction_id": cfg.get("faction_id"),
                    "system_id": cfg.get("system_id"),
                    "name": cfg.get("name"),
                }
        if station_id >= PLAYER_STRUCTURE_ID_THRESHOLD:
            return None
        return self._data.get(station_id)

    async def fetch(self, session: "aiohttp.ClientSession",
                    station_id: int) -> Optional[dict]:
        """Resolve via cache or ESI. Returns None for player structures or on
        error. Idempotent: subsequent calls return the cached entry.

        Self-heals cached entries that were written by the old corp->faction
        lookup path (now always-null because ESI dropped that field for NPC
        corps) by re-fetching the station and deriving faction from race_id.
        """
        cached = self.lookup(station_id)
        if cached is not None:
            if (cached.get("faction_id") is None
                    and station_id < PLAYER_STRUCTURE_ID_THRESHOLD):
                fresh = await self._fetch_station_info(session, station_id)
                if fresh and fresh.get("faction_id") is not None:
                    cached["faction_id"] = fresh["faction_id"]
                    self._data[station_id] = cached
                    self._save()
            return cached
        if station_id >= PLAYER_STRUCTURE_ID_THRESHOLD:
            return None

        info = await self._fetch_station_info(session, station_id)
        if info is None:
            return None
        self._data[station_id] = info
        self._save()
        return info

    async def _fetch_station_info(self, session: "aiohttp.ClientSession",
                                  station_id: int) -> Optional[dict]:
        """ESI /universe/stations/{id}/ -> info dict. Derives faction_id from
        race_id since /corporations/{id}/ no longer returns it for NPC corps.
        """
        url = f"https://esi.evetech.net/latest/universe/stations/{station_id}/"
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                sta = await resp.json()
        except Exception as e:
            print(f"[StationLookup] station fetch error {station_id}: {e}")
            return None

        return {
            "corp_id": sta.get("owner"),
            "faction_id": RACE_TO_FACTION.get(sta.get("race_id")),
            "system_id": sta.get("system_id"),
            "name": sta.get("name"),
        }
