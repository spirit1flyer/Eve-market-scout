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
from datetime import datetime, timedelta
from typing import Optional, Dict

from core.config import ESI_USER_AGENT

BASE_URL = "https://esi.evetech.net/latest"


class _StandingsCache:
    def __init__(self, standings: dict, base: dict = None):
        self.standings = standings   # Connections/Diplomacy-modified (display)
        self.base = base or standings  # raw ESI values (fee math needs BASE)
        self.fetched_at = datetime.now()
        self.expires_at = self.fetched_at + timedelta(hours=1)

    @property
    def is_expired(self) -> bool:
        return datetime.now() >= self.expires_at


class IndustryStandings:
    """Fetches + caches standings per roster character_id (display-only for now)."""

    def __init__(self, roster, skills: 'IndustrySkills' = None):
        """roster: IndustryRoster; skills: optional IndustrySkills for modifiers."""
        self.roster = roster
        self.skills = skills
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
        cache = self._cache.get(character_id)
        if not force_refresh and cache and not cache.is_expired:
            return cache.standings

        headers = self.roster.get_auth_headers(character_id)
        if not headers:
            print(f"[IndustryStandings] Not authenticated for character {character_id}")
            return None

        connections = diplomacy = 0
        if self.skills:
            connections = self.skills.get_level(character_id, "connections")
            diplomacy = self.skills.get_level(character_id, "diplomacy")

        try:
            resp = requests.get(
                f"{BASE_URL}/characters/{character_id}/standings/",
                headers=headers, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[IndustryStandings] request error for {character_id}: {e}")
            return None

        # Both flavors are cached: the Connections/Diplomacy-modified values
        # for display, and the BASE values for fee math (the in-game broker
        # fee ignores social skills — verified 2026-07-03).
        standings = {"agents": {}, "npc_corps": {}, "factions": {}}
        base = {"agents": {}, "npc_corps": {}, "factions": {}}
        for entry in data:
            from_id = entry.get("from_id")
            from_type = entry.get("from_type")
            raw = entry.get("standing", 0.0)
            eff = self._modifier(raw, connections, diplomacy)
            key = {"agent": "agents", "npc_corp": "npc_corps",
                   "faction": "factions"}.get(from_type)
            if key:
                standings[key][from_id] = eff
                base[key][from_id] = raw

        self._cache[character_id] = _StandingsCache(standings, base)
        print(f"[IndustryStandings] Fetched {len(standings['factions'])} factions / "
              f"{len(standings['npc_corps'])} corps for character {character_id}")
        return standings

    def get(self, character_id: int) -> dict:
        """Categorized standings (fetches if needed; empty on failure)."""
        cache = self._cache.get(character_id)
        if not cache or cache.is_expired:
            self.fetch(character_id)
            cache = self._cache.get(character_id)
        if not cache:
            return {"agents": {}, "npc_corps": {}, "factions": {}}
        return cache.standings

    def get_base(self, character_id: int, allow_fetch: bool = True) -> Optional[dict]:
        """BASE (unmodified) standings for fee math. Returns None when nothing
        is cached and fetching is disallowed (UI thread) or fails, so the
        caller can fall back instead of silently reading zeros."""
        cache = self._cache.get(character_id)
        if not cache or cache.is_expired:
            if not allow_fetch:
                return None
            self.fetch(character_id)
            cache = self._cache.get(character_id)
        return cache.base if cache else None

    def resolve_names(self, ids) -> Dict[int, str]:
        """Resolve faction/corp/agent ids to names via public POST
        /universe/names/ (no auth). Cached; call from a worker thread."""
        todo = list({int(i) for i in ids if i and int(i) not in self._names})
        for start in range(0, len(todo), 1000):  # ESI caps at 1000 ids/call
            chunk = todo[start:start + 1000]
            if not chunk:
                continue
            try:
                resp = requests.post(f"{BASE_URL}/universe/names/",
                                     json=chunk,
                                     headers={"User-Agent": ESI_USER_AGENT},
                                     timeout=30)
                resp.raise_for_status()
                for entry in resp.json():
                    self._names[entry["id"]] = entry["name"]
            except requests.RequestException as e:
                print(f"[IndustryStandings] name resolve error: {e}")
        return self._names

    def name_for(self, id_: int) -> Optional[str]:
        return self._names.get(int(id_)) if id_ is not None else None

    def clear(self, character_id: int = None):
        if character_id is None:
            self._cache.clear()
        else:
            self._cache.pop(character_id, None)
