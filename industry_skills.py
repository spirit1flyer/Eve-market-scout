"""Per-character industry skill pull (Phase 2.2 of the Industry tab).

Pulls the industry-relevant skills for each roster character and caches them
keyed by character_id. These skills affect TIME only (build time, research time)
— never material cost or job cost (see PLAN_industry_tab.md S1) — so they feed the
Phase 4 build-time / research-time math, not the Phase 1 cost engine.

Auth comes from `IndustryRoster.get_auth_headers(character_id)`; the roster's
scope set already includes `esi-skills.read_skills.v1`. Parse math mirrors
`esi_skills.ESISkills` but is keyed per character_id rather than seller/buyer.
"""

import requests
from datetime import datetime, timedelta
from typing import Optional, Dict

BASE_URL = "https://esi.evetech.net/latest"

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
    def __init__(self, raw_skills: Dict[int, int]):
        self.raw_skills = raw_skills
        self.fetched_at = datetime.now()
        self.expires_at = self.fetched_at + timedelta(hours=1)

    @property
    def is_expired(self) -> bool:
        return datetime.now() >= self.expires_at


class IndustrySkills:
    """Fetches + caches industry skills per roster character_id."""

    def __init__(self, roster):
        """roster: an IndustryRoster instance (provides get_auth_headers)."""
        self.roster = roster
        self._cache: Dict[int, _SkillCache] = {}

    def fetch(self, character_id: int, force_refresh: bool = False) -> Optional[Dict[int, int]]:
        """Pull skills for one character. Returns raw {skill_id: level} or None."""
        cache = self._cache.get(character_id)
        if not force_refresh and cache and not cache.is_expired:
            return cache.raw_skills

        headers = self.roster.get_auth_headers(character_id)
        if not headers:
            print(f"[IndustrySkills] Not authenticated for character {character_id}")
            return None

        try:
            resp = requests.get(
                f"{BASE_URL}/characters/{character_id}/skills/",
                headers=headers, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            print(f"[IndustrySkills] request error for {character_id}: {e}")
            return None

        raw_skills = {}
        for skill in data.get("skills", []):
            sid = skill.get("skill_id")
            raw_skills[sid] = skill.get("trained_skill_level", 0)

        self._cache[character_id] = _SkillCache(raw_skills)
        print(f"[IndustrySkills] Fetched {len(raw_skills)} skills for character {character_id}")
        return raw_skills

    def get_levels(self, character_id: int) -> Dict[str, int]:
        """Return the named industry skill levels for a character.

        Fetches if not cached. Missing/unauthenticated → all zeros.
        """
        cache = self._cache.get(character_id)
        if not cache or cache.is_expired:
            self.fetch(character_id)
            cache = self._cache.get(character_id)

        if not cache:
            return {name: 0 for name in INDUSTRY_SKILL_IDS}

        return {
            name: cache.raw_skills.get(sid, 0)
            for name, sid in INDUSTRY_SKILL_IDS.items()
        }

    def get_level(self, character_id: int, skill_name: str) -> int:
        """Level of one named skill (0 if unknown/not trained)."""
        return self.get_levels(character_id).get(skill_name.lower(), 0)

    def get_cache_status(self, character_id: int) -> tuple:
        """(can_refresh, seconds_remaining)."""
        cache = self._cache.get(character_id)
        if not cache or cache.is_expired:
            return (True, 0)
        remaining = (cache.expires_at - datetime.now()).total_seconds()
        return (False, max(0, int(remaining)))

    def clear(self, character_id: int = None):
        if character_id is None:
            self._cache.clear()
        else:
            self._cache.pop(character_id, None)
