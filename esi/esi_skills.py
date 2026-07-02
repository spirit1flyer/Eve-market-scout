"""ESI Skills fetcher for EVE Market Scout.

Fetches character skills from ESI and extracts trading-relevant skills.
Supports separate skill lookups for seller and buyer characters.
Requires scope: esi-skills.read_skills.v1
"""

import requests
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime, timedelta

from core.calculate import TradingSkills

BASE_URL = "https://esi.evetech.net/latest"

# Trading skill type IDs (verified against SDE 2026-05)
SKILL_IDS = {
    "accounting": 16622,
    "broker_relations": 3446,
    "advanced_broker_relations": 16597,  # formerly named "Margin Trading"; renamed by CCP
    # Other useful ones for future
    "trade": 3443,
    "retail": 3444,
    "wholesale": 16596,
    "tycoon": 18580,
    "daytrading": 16595,
    "marketing": 16598,
    "procurement": 16594,
    "visibility": 3447,
    # Social skills that modify standings
    "connections": 3359,      # +4% per level to positive NPC standings
    "diplomacy": 3357,        # +4% per level to negative NPC standings
    # Reprocessing — Scrapmetal Processing gives +2%/level on junk (non-ore)
    # reprocessing yield. Used by the Reprocess-or-Sell tab.
    "scrapmetal_processing": 12196,
}

# Reverse lookup
SKILL_NAMES = {v: k for k, v in SKILL_IDS.items()}


@dataclass
class SkillCache:
    """Cached skill data with expiry."""
    skills: TradingSkills
    raw_skills: Dict[int, int]  # type_id -> trained_level
    fetched_at: datetime
    expires_at: datetime
    
    @property
    def is_expired(self) -> bool:
        return datetime.now() >= self.expires_at


class ESISkills:
    """Fetches and caches character skills from ESI."""
    
    def __init__(self, auth):
        """
        Args:
            auth: ESIAuth instance for authentication
        """
        self.auth = auth
        
        # Separate caches for seller and buyer
        self._seller_cache: Optional[SkillCache] = None
        self._buyer_cache: Optional[SkillCache] = None
        
        # Legacy compatibility
        self._cache: Optional[SkillCache] = None
        
        # How long to cache skills (they rarely change)
        self.cache_duration = timedelta(hours=1)
    
    def _get_cache(self, slot: str) -> Optional[SkillCache]:
        """Get cache for specified slot."""
        if slot == "buyer":
            return self._buyer_cache
        return self._seller_cache
    
    def _set_cache(self, slot: str, cache: SkillCache):
        """Set cache for specified slot."""
        if slot == "buyer":
            self._buyer_cache = cache
        else:
            self._seller_cache = cache
            self._cache = cache  # Legacy compatibility
    
    def _make_request(self, endpoint: str, slot: str = "seller") -> Optional[dict]:
        """Make authenticated ESI request."""
        headers = self.auth.get_auth_headers(slot)
        if not headers:
            print(f"ESISkills: Not authenticated for {slot}")
            return None
        
        url = f"{BASE_URL}{endpoint}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"ESISkills request error: {e}")
            return None
    
    def fetch_skills(self, force_refresh: bool = False, slot: str = "seller") -> Optional[TradingSkills]:
        """
        Fetch character skills and return TradingSkills object.
        
        Args:
            force_refresh: If True, bypass cache
            slot: "seller" or "buyer"
        
        Returns:
            TradingSkills object, or None if fetch failed
        """
        # Check cache
        cache = self._get_cache(slot)
        if not force_refresh and cache and not cache.is_expired:
            return cache.skills
        
        # Get character ID for this slot
        char = self.auth.get_character(slot)
        if not char or not char.character_id:
            print(f"ESISkills: No character ID for {slot}")
            return None
        
        char_id = char.character_id
        
        data = self._make_request(f"/characters/{char_id}/skills/", slot)
        if not data:
            return None
        
        # Parse skills
        raw_skills = {}
        skills_list = data.get("skills", [])
        
        for skill in skills_list:
            type_id = skill.get("skill_id")
            level = skill.get("trained_skill_level", 0)
            raw_skills[type_id] = level
        
        # Extract trading skills
        trading_skills = TradingSkills(
            broker_relations=raw_skills.get(SKILL_IDS["broker_relations"], 0),
            accounting=raw_skills.get(SKILL_IDS["accounting"], 0),
            advanced_broker_relations=raw_skills.get(SKILL_IDS["advanced_broker_relations"], 0),
        )
        
        # Cache it
        now = datetime.now()
        new_cache = SkillCache(
            skills=trading_skills,
            raw_skills=raw_skills,
            fetched_at=now,
            expires_at=now + self.cache_duration
        )
        self._set_cache(slot, new_cache)
        
        char_name = char.character_name or slot
        print(f"ESISkills: Fetched {len(skills_list)} skills for {char_name}")
        print(f"  Broker Relations: {trading_skills.broker_relations}")
        print(f"  Accounting: {trading_skills.accounting}")
        print(f"  Advanced Broker Relations: {trading_skills.advanced_broker_relations}")
        
        return trading_skills
    
    def get_skills(self, slot: str = "seller") -> TradingSkills:
        """
        Get trading skills for specified slot, fetching if needed.
        
        Returns cached skills if available, otherwise fetches.
        Returns DEFAULT_SKILLS if not authenticated or fetch fails.
        """
        cache = self._get_cache(slot)
        if cache and not cache.is_expired:
            return cache.skills
        
        result = self.fetch_skills(slot=slot)
        if result:
            return result
        
        # Fallback to defaults
        from core.calculate import DEFAULT_SKILLS
        return DEFAULT_SKILLS
    
    def get_seller_skills(self) -> TradingSkills:
        """Get trading skills for seller character."""
        return self.get_skills("seller")
    
    def get_buyer_skills(self) -> TradingSkills:
        """Get trading skills for buyer character."""
        return self.get_skills("buyer")
    
    def get_skill_level(self, skill_name: str, slot: str = "seller") -> int:
        """
        Get level of a specific skill by name.
        
        Args:
            skill_name: Skill name (e.g., "accounting", "broker_relations")
            slot: "seller" or "buyer"
        
        Returns:
            Trained level (0-5), or 0 if unknown/not trained
        """
        cache = self._get_cache(slot)
        if not cache:
            self.fetch_skills(slot=slot)
            cache = self._get_cache(slot)
        
        if not cache:
            return 0
        
        type_id = SKILL_IDS.get(skill_name.lower())
        if not type_id:
            return 0
        
        return cache.raw_skills.get(type_id, 0)
    
    def get_all_trading_skills(self, slot: str = "seller") -> Dict[str, int]:
        """
        Get all trading-related skill levels.
        
        Returns:
            Dict of skill_name -> level
        """
        cache = self._get_cache(slot)
        if not cache:
            self.fetch_skills(slot=slot)
            cache = self._get_cache(slot)
        
        if not cache:
            return {name: 0 for name in SKILL_IDS.keys()}
        
        return {
            name: cache.raw_skills.get(type_id, 0)
            for name, type_id in SKILL_IDS.items()
        }
    
    def get_cache_status(self, slot: str = "seller") -> tuple[bool, int]:
        """
        Check if cache is valid and how long until it expires.
        
        Args:
            slot: "seller" or "buyer"
            
        Returns:
            (can_refresh, seconds_remaining)
            - can_refresh: True if cache is expired or empty (OK to fetch)
            - seconds_remaining: Seconds until cache expires (0 if expired/empty)
        """
        cache = self._get_cache(slot)
        if not cache:
            return (True, 0)
        
        if cache.is_expired:
            return (True, 0)
        
        remaining = (cache.expires_at - datetime.now()).total_seconds()
        return (False, max(0, int(remaining)))
    
    def clear_cache(self, slot: str = None):
        """
        Clear the skill cache.
        
        Args:
            slot: "seller", "buyer", or None for all
        """
        if slot is None or slot == "seller":
            self._seller_cache = None
            self._cache = None
        if slot is None or slot == "buyer":
            self._buyer_cache = None


# =============================================================================
# STANDINGS
# =============================================================================

class ESIStandings:
    """Fetches character standings for broker fee calculation.
    
    Requires scope: esi-characters.read_standings.v1
    
    Station standing formula uses the HIGHER of:
    - Direct corp standing
    - Faction standing (if no corp standing or corp standing is lower)
    
    IMPORTANT: ESI returns BASE standings. We must apply skill modifiers:
    - Connections: +4% per level to positive standings
    - Diplomacy: +4% per level to negative standings
    
    Station/corp/faction mappings are now driven by config.TRADE_HUBS.
    """
    
    def __init__(self, auth, skills_fetcher: 'ESISkills' = None):
        self.auth = auth
        self.skills_fetcher = skills_fetcher
        
        # Separate caches for seller and buyer
        self._seller_standings_cache = None
        self._seller_cache_time = None
        self._buyer_standings_cache = None
        self._buyer_cache_time = None
        
        # Legacy compatibility
        self._standings_cache = None
        self._cache_time = None
        
        self.cache_duration = timedelta(hours=1)
        
        # Cached skill levels for standing modifiers (per character)
        self._seller_connections = 0
        self._seller_diplomacy = 0
        self._buyer_connections = 0
        self._buyer_diplomacy = 0
    
    def set_skills_fetcher(self, skills_fetcher: 'ESISkills'):
        """Set the skills fetcher for standing modifiers."""
        self.skills_fetcher = skills_fetcher
    
    def _fetch_social_skills(self, slot: str = "seller"):
        """Fetch Connections and Diplomacy skill levels."""
        if not self.skills_fetcher:
            print("ESIStandings: No skills_fetcher available")
            return
        
        # Make sure skills are fetched first
        self.skills_fetcher.fetch_skills(slot=slot)
        
        connections = self.skills_fetcher.get_skill_level("connections", slot)
        diplomacy = self.skills_fetcher.get_skill_level("diplomacy", slot)
        
        if slot == "buyer":
            self._buyer_connections = connections
            self._buyer_diplomacy = diplomacy
        else:
            self._seller_connections = connections
            self._seller_diplomacy = diplomacy
        
        print(f"Social skills ({slot}): Connections={connections}, Diplomacy={diplomacy}")
    
    def _apply_skill_modifier(self, base_standing: float, slot: str = "seller") -> float:
        """
        Apply Connections/Diplomacy skill modifier to base standing.
        
        EVE Formula for Connections (positive standings):
            effective = base + ((10 - base) x 0.04 x level)
        
        EVE Formula for Diplomacy (negative standings):
            effective = base + ((base + 10) x 0.04 x level)
        
        This moves the standing 4% per level toward the cap (10 or -10).
        """
        if slot == "buyer":
            connections = self._buyer_connections
            diplomacy = self._buyer_diplomacy
        else:
            connections = self._seller_connections
            diplomacy = self._seller_diplomacy
        
        if base_standing > 0 and connections > 0:
            # Connections: move toward +10
            modifier = (10.0 - base_standing) * 0.04 * connections
            return base_standing + modifier
        elif base_standing < 0 and diplomacy > 0:
            # Diplomacy: move toward 0 (from negative)
            modifier = (10.0 + base_standing) * 0.04 * diplomacy
            return base_standing + modifier
        return base_standing
    
    def _make_request(self, endpoint: str, slot: str = "seller") -> Optional[dict]:
        """Make authenticated ESI request."""
        headers = self.auth.get_auth_headers(slot)
        if not headers:
            return None
        
        url = f"{BASE_URL}{endpoint}"
        try:
            response = requests.get(url, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            print(f"ESIStandings request error: {e}")
            return None
    
    def fetch_standings(self, force_refresh: bool = False, slot: str = "seller") -> Optional[dict]:
        """
        Fetch all character standings from ESI and apply skill modifiers.
        
        Returns dict with:
        - 'agents': {agent_id: effective_standing}
        - 'npc_corps': {corp_id: effective_standing}  
        - 'factions': {faction_id: effective_standing}
        
        Note: ESI returns BASE standings. This method applies Connections/Diplomacy
        skill modifiers to return EFFECTIVE standings.
        """
        # Check cache
        if slot == "buyer":
            cache = self._buyer_standings_cache
            cache_time = self._buyer_cache_time
        else:
            cache = self._seller_standings_cache
            cache_time = self._seller_cache_time
        
        if not force_refresh and cache and cache_time:
            if datetime.now() < cache_time + self.cache_duration:
                return cache
        
        char = self.auth.get_character(slot)
        if not char or not char.character_id:
            print(f"ESIStandings: No character ID for {slot}")
            return None
        
        char_id = char.character_id
        
        # Fetch social skill levels first
        self._fetch_social_skills(slot)
        
        data = self._make_request(f"/characters/{char_id}/standings/", slot)
        if not data:
            return None
        
        # Parse into categories and apply skill modifiers
        standings = {
            'agents': {},
            'npc_corps': {},
            'factions': {}
        }
        
        # Diagnostic: log raw response shape so we can verify ESI is returning
        # the expected corp standings (e.g., Emperor Family 1000125 for Amarr).
        from_type_counts = {}
        for entry in data:
            t = entry.get('from_type', 'unknown')
            from_type_counts[t] = from_type_counts.get(t, 0) + 1
        print(f"[StandingsDiag] /standings/ raw response: {len(data)} entries; "
              f"from_type counts: {from_type_counts}")

        unknown_types = set()
        for entry in data:
            from_id = entry.get('from_id')
            from_type = entry.get('from_type')
            base_standing = entry.get('standing', 0.0)

            # Apply Connections/Diplomacy modifier
            effective_standing = self._apply_skill_modifier(base_standing, slot)

            if from_type == 'agent':
                standings['agents'][from_id] = effective_standing
            elif from_type == 'npc_corp':
                standings['npc_corps'][from_id] = effective_standing
            elif from_type == 'faction':
                standings['factions'][from_id] = effective_standing
            else:
                unknown_types.add(from_type)

        if unknown_types:
            print(f"[StandingsDiag] Unrecognized from_type values: {unknown_types}")

        # Verify the hub-relevant corp IDs are present
        from core.config import TRADE_HUBS
        for hub_key, cfg in TRADE_HUBS.items():
            cid = cfg.get("corp_id")
            fid = cfg.get("faction_id")
            corp_val = standings['npc_corps'].get(cid)
            fac_val = standings['factions'].get(fid)
            print(f"[StandingsDiag] {hub_key}: corp_id={cid} -> "
                  f"{corp_val if corp_val is not None else 'MISSING'}, "
                  f"faction_id={fid} -> "
                  f"{fac_val if fac_val is not None else 'MISSING'}")
        
        # Store in appropriate cache
        if slot == "buyer":
            self._buyer_standings_cache = standings
            self._buyer_cache_time = datetime.now()
        else:
            self._seller_standings_cache = standings
            self._seller_cache_time = datetime.now()
            # Legacy compatibility
            self._standings_cache = standings
            self._cache_time = datetime.now()
        
        connections = self._buyer_connections if slot == "buyer" else self._seller_connections
        print(f"ESIStandings: Fetched {len(standings['factions'])} faction standings, "
              f"{len(standings['npc_corps'])} corp standings for {slot} (Connections L{connections})")
        
        return standings
    
    def get_faction_standing(self, faction_id: int, slot: str = "seller") -> float:
        """Get standing with a specific faction."""
        standings = self.fetch_standings(slot=slot)
        if not standings:
            return 0.0
        return standings['factions'].get(faction_id, 0.0)
    
    def get_corp_standing(self, corp_id: int, slot: str = "seller") -> float:
        """Get standing with a specific NPC corp."""
        standings = self.fetch_standings(slot=slot)
        if not standings:
            return 0.0
        return standings['npc_corps'].get(corp_id, 0.0)
    
    def get_station_standings(self, station_id: int, slot: str = "seller") -> tuple[float, float]:
        """
        Get both corp and faction standings for a station.
        
        Returns: (station_standing, faction_standing)
        
        Uses config.TRADE_HUBS for station -> corp/faction mappings.
        For stations not in config, returns (0.0, 0.0)
        """
        from core.config import TRADE_HUBS
        
        standings = self.fetch_standings(slot=slot)
        if not standings:
            return (0.0, 0.0)
        
        # Find the hub config for this station
        corp_id = None
        faction_id = None
        for hub_config in TRADE_HUBS.values():
            if hub_config["station_id"] == station_id:
                corp_id = hub_config["corp_id"]
                faction_id = hub_config["faction_id"]
                break
        
        corp_standing = standings['npc_corps'].get(corp_id, 0.0) if corp_id else 0.0
        faction_standing = standings['factions'].get(faction_id, 0.0) if faction_id else 0.0
        
        return (corp_standing, faction_standing)
    
    def get_standings_for_hub(self, hub_key: str, slot: str = "seller") -> tuple[float, float]:
        """
        Get standings for a hub by its config key (e.g., 'amarr', 'jita').
        
        Returns: (corp_standing, faction_standing)
        """
        from core.config import get_hub_config
        
        hub = get_hub_config(hub_key)
        return self.get_station_standings(hub["station_id"], slot)
    
    def get_amarr_standings(self, slot: str = "seller") -> tuple[float, float]:
        """
        Convenience method: Get standings for Amarr trading.
        
        Returns: (emperor_family_corp_standing, amarr_empire_faction_standing)
        """
        return self.get_standings_for_hub("amarr", slot)
    
    def get_cache_status(self, slot: str = "seller") -> tuple[bool, int]:
        """
        Check if cache is valid and how long until it expires.
        
        Args:
            slot: "seller" or "buyer"
            
        Returns:
            (can_refresh, seconds_remaining)
            - can_refresh: True if cache is expired or empty (OK to fetch)
            - seconds_remaining: Seconds until cache expires (0 if expired/empty)
        """
        if slot == "buyer":
            cache_time = self._buyer_cache_time
        else:
            cache_time = self._seller_cache_time
        
        if not cache_time:
            return (True, 0)
        
        expires_at = cache_time + self.cache_duration
        if datetime.now() >= expires_at:
            return (True, 0)
        
        remaining = (expires_at - datetime.now()).total_seconds()
        return (False, max(0, int(remaining)))
    
    def clear_cache(self, slot: str = None):
        """
        Clear standings cache.
        
        Args:
            slot: "seller", "buyer", or None for all
        """
        if slot is None or slot == "seller":
            self._seller_standings_cache = None
            self._seller_cache_time = None
            self._seller_connections = 0
            self._seller_diplomacy = 0
            # Legacy
            self._standings_cache = None
            self._cache_time = None
        if slot is None or slot == "buyer":
            self._buyer_standings_cache = None
            self._buyer_cache_time = None
            self._buyer_connections = 0
            self._buyer_diplomacy = 0


# =============================================================================
# REQUIRED SCOPE
# =============================================================================

REQUIRED_SKILL_SCOPE = "esi-skills.read_skills.v1"
REQUIRED_STANDINGS_SCOPE = "esi-characters.read_standings.v1"


def check_scope_in_auth(auth) -> bool:
    """Check if auth has the required scope for skills."""
    # This would need to check the token's scopes
    # For now, just try to fetch and see if it works
    return True
