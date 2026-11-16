"""Per-character industry standings pull (Phase 2.3 of the Industry tab).

Kept for the future: standings affect broker fees + reprocessing only, NOT
industry job cost (see PLAN_industry_tab.md S1/S6), so this has ZERO effect on
T1 math. It is pulled and displayable now so the data is on hand if a later
activity needs it ("rather have it").

Auth via `IndustryRoster.get_auth_headers(character_id)`; the roster scope set
includes `esi-characters.read_standings.v1`. Connections/Diplomacy skill-modifier
math is lifted from `esi_skills.ESIStandings`; an optional `IndustrySkills`
fetcher supplies those levels (same /skills/ pull), else base standings are used.
"""

import requests
from datetime import datetime, timezone
from typing import Optional, Dict

from core.config import get_esi_headers
from core.esi_rate_limit import esi_request
from core.esi_character_cache import RosterAuth, character_snapshot, usable_until

BASE_URL = "https://esi.evetech.net/latest"


class _StandingsCache:
    def __init__(self, standings: dict, snapshot):
        self.standings = standings
        self.fetched_at = snapshot["acquired_at"]
        self.expires_at = usable_until(snapshot)

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class IndustryStandings:
    """Fetches + caches standings per roster character_id (display-only for now)."""

    def __init__(self, roster, skills: 'IndustrySkills' = None, cache=None):
        """roster: IndustryRoster; skills: optional IndustrySkills for modifiers."""
        self.roster = roster
        self.skills = skills
        self._snapshot_cache = cache
        self._cache: Dict[int, _StandingsCache] = {}
        self._names: Dict[int, str] = {}   # id -> faction/corp/agent name

    def _modifier(self, base: float, connections: int, diplomacy: int) -> float:
        """Apply Connections/Diplomacy: move 4%/lvl toward the cap (10 / 0)."""
        if base > 0 and connections > 0:
            return base + (10.0 - base) * 0.04 * connections
        if base < 0 and diplomacy > 0:
            return base + (10.0 + base) * 0.04 * diplomacy
        return base

    def fetch(self, character_id: int, force_refresh: bool = False) -> Optional[dict]:
        """Pull standings for one character. Returns categorized dict or None.

        Shape: {'agents': {id: standing}, 'npc_corps': {...}, 'factions': {...}}
        with Connections/Diplomacy applied when a skills fetcher is present.
        """
        try:
            snapshot = character_snapshot(RosterAuth(self.roster, character_id), "standings",
                "industry", fetch=True, force=force_refresh, cache=self._snapshot_cache,
                trigger="industry_standings")
            standings = self._derive(character_id, snapshot)
        except Exception as exc:
            self._cache.pop(character_id, None)
            print(f"[IndustryStandings] request error: {type(exc).__name__}")
            return None
        print(f"[IndustryStandings] Loaded {len(standings['factions'])} factions / "
              f"{len(standings['npc_corps'])} corps for character {character_id}")
        return standings

    def _derive(self, character_id, snapshot):
        connections = diplomacy = 0
        if self.skills:
            connections = self.skills.get_level(character_id, "connections", allow_stale=True)
            diplomacy = self.skills.get_level(character_id, "diplomacy", allow_stale=True)

        standings = {"agents": {}, "npc_corps": {}, "factions": {}}
        for entry in snapshot["payload"]:
            from_id = entry.get("from_id")
            from_type = entry.get("from_type")
            eff = self._modifier(entry.get("standing", 0.0), connections, diplomacy)
            if from_type == "agent":
                standings["agents"][from_id] = eff
            elif from_type == "npc_corp":
                standings["npc_corps"][from_id] = eff
            elif from_type == "faction":
                standings["factions"][from_id] = eff

        self._cache[character_id] = _StandingsCache(standings, snapshot)
        return standings

    def get(self, character_id: int) -> dict:
        """Categorized cached standings; never initiates a network request."""
        try:
            snapshot = character_snapshot(RosterAuth(self.roster, character_id), "standings",
                "industry", cache=self._snapshot_cache)
            if snapshot is not None:
                return self._derive(character_id, snapshot)
        except Exception as exc:
            print(f"[IndustryStandings] cache unavailable: {type(exc).__name__}")
        self._cache.pop(character_id, None)
        return {"agents": {}, "npc_corps": {}, "factions": {}}

    def resolve_names(self, ids) -> Dict[int, str]:
        """Resolve faction/corp/agent ids to names via public POST
        /universe/names/ (no auth). Cached; call from a worker thread."""
        from core.esi_lookup_cache import get_lookup_cache
        self._names.update(get_lookup_cache().names(ids))
        return self._names

    def name_for(self, id_: int) -> Optional[str]:
        return self._names.get(int(id_)) if id_ is not None else None

    def get_cache_status(self, character_id: int, force_refresh=False) -> tuple[bool, int]:
        """Return (can_fetch, seconds_remaining) for manual UI diagnostics."""
        self.get(character_id)
        cache = self._cache.get(character_id)
        if not cache:
            return True, 0
        if force_refresh:
            snapshot = character_snapshot(RosterAuth(self.roster, character_id), "standings",
                "industry", cache=self._snapshot_cache)
            if snapshot is None:
                return True, 0
            eligible = snapshot["expires"]
        else:
            eligible = cache.expires_at
        remaining = (eligible - datetime.now(timezone.utc)).total_seconds()
        return remaining <= 0, max(0, int(remaining))

    def clear(self, character_id: int = None):
        if character_id is None:
            self._cache.clear()
        else:
            self._cache.pop(character_id, None)
