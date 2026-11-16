"""Per-character industry skill pull (Phase 2.2 of the Industry tab).

Pulls the industry-relevant skills for each roster character and caches them
keyed by character_id. These skills affect TIME only (build time, research time)
— never material cost or job cost (see PLAN_industry_tab.md S1) — so they feed the
Phase 4 build-time / research-time math, not the Phase 1 cost engine.

Auth comes from `IndustryRoster.get_auth_headers(character_id)`; the roster's
scope set already includes `esi-skills.read_skills.v1`. Parse math mirrors
`esi_skills.ESISkills` but is keyed per character_id rather than seller/buyer.
"""

from datetime import datetime, timezone
from typing import Optional, Dict
from core.esi_character_cache import RosterAuth, character_snapshot, trained_levels, usable_until

# Industry-relevant skill type IDs (verified against SDE).
INDUSTRY_SKILL_IDS = {
    "industry": 3380,            # 4%/lvl manufacturing time
    "advanced_industry": 3388,   # 3%/lvl manufacturing + research time
    "research": 3403,            # 5%/lvl TE research time
    "metallurgy": 3409,          # 5%/lvl ME research time
    # Social skills (standings modifiers) — same /skills/ pull, used by
    # industry_standings.py so it needn't add another endpoint.
    "connections": 3359,         # +4%/lvl positive NPC standings
    "diplomacy": 3357,           # +4%/lvl negative NPC standings
}


class _SkillCache:
    def __init__(self, snapshot):
        self.raw_skills = trained_levels(snapshot)
        self.fetched_at = snapshot["acquired_at"]
        self.expires_at = usable_until(snapshot)

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at


class IndustrySkills:
    """Fetches + caches industry skills per roster character_id."""

    def __init__(self, roster, cache=None):
        """roster: an IndustryRoster instance (provides get_auth_headers)."""
        self.roster = roster
        self._snapshot_cache = cache
        self._cache: Dict[int, _SkillCache] = {}

    def _read_cache(self, character_id, allow_stale=False):
        try:
            snapshot = character_snapshot(RosterAuth(self.roster, character_id), "skills",
                "industry", cache=self._snapshot_cache, allow_stale=allow_stale)
            if snapshot is not None:
                cache = _SkillCache(snapshot)
                self._cache[character_id] = cache
                return cache
        except Exception as exc:
            print(f"[IndustrySkills] cache unavailable: {type(exc).__name__}")
        self._cache.pop(character_id, None)
        return None

    def fetch(self, character_id: int, force_refresh: bool = False) -> Optional[Dict[int, int]]:
        """Pull skills for one character. Returns raw {skill_id: level} or None."""
        try:
            snapshot = character_snapshot(RosterAuth(self.roster, character_id), "skills",
                "industry", fetch=True, force=force_refresh, cache=self._snapshot_cache,
                trigger="industry_skills")
            cached = _SkillCache(snapshot)
        except Exception as exc:
            self._cache.pop(character_id, None)
            print(f"[IndustrySkills] request error: {type(exc).__name__}")
            return None
        self._cache[character_id] = cached
        print(f"[IndustrySkills] Loaded {len(cached.raw_skills)} skills for character {character_id}")
        return cached.raw_skills

    def get_levels(self, character_id: int, *, allow_stale=False) -> Dict[str, int]:
        """Return the named industry skill levels for a character.

        Cache-only: missing/expired data returns all zeros. Network acquisition
        is deliberately limited to an explicit Characters > Skills action.
        """
        cache = self._read_cache(character_id, allow_stale=allow_stale)
        if not cache or (cache.is_expired and not allow_stale):
            return {name: 0 for name in INDUSTRY_SKILL_IDS}

        return {
            name: cache.raw_skills.get(sid, 0)
            for name, sid in INDUSTRY_SKILL_IDS.items()
        }

    def get_level(self, character_id: int, skill_name: str, *, allow_stale=False) -> int:
        """Level of one named skill (0 if unknown/not trained)."""
        return self.get_levels(character_id, allow_stale=allow_stale).get(skill_name.lower(), 0)

    def peek_levels(self, character_id: int) -> Optional[Dict[str, int]]:
        """Named skill levels from cache ONLY — never triggers a network fetch.

        Returns None if nothing is cached (or it expired) so a UI-thread caller
        can decide to render a placeholder + warm the cache in the background
        instead of blocking on ESI. Mirrors `get_levels`' naming.
        """
        cache = self._read_cache(character_id)
        if not cache or cache.is_expired:
            return None
        return {name: cache.raw_skills.get(sid, 0)
                for name, sid in INDUSTRY_SKILL_IDS.items()}

    def get_cache_status(self, character_id: int) -> tuple:
        """(can_refresh, seconds_remaining)."""
        cache = self._read_cache(character_id)
        if not cache or cache.is_expired:
            return (True, 0)
        remaining = (cache.expires_at - datetime.now(timezone.utc)).total_seconds()
        return (False, max(0, int(remaining)))

    def clear(self, character_id: int = None):
        if character_id is None:
            self._cache.clear()
        else:
            self._cache.pop(character_id, None)
