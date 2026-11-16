"""ESI Skills fetcher for EVE Market Scout.

Fetches character skills from ESI and extracts trading-relevant skills.
Supports separate skill lookups for seller and buyer characters.
Requires scope: esi-skills.read_skills.v1
"""

from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.calculate import TradingSkills
from core.esi_character_cache import character_snapshot, trained_levels, usable_until

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
        return datetime.now(timezone.utc) >= self.expires_at


class ESISkills:
    """Fetches and caches character skills from ESI."""
    
    def __init__(self, auth, cache=None):
        """
        Args:
            auth: ESIAuth instance for authentication
        """
        self.auth = auth
        self._snapshot_cache = cache
        
        # Separate caches for seller and buyer
        self._seller_cache: Optional[SkillCache] = None
        self._buyer_cache: Optional[SkillCache] = None
        
        # Legacy compatibility
        self._cache: Optional[SkillCache] = None
        
        # How long to cache skills (they rarely change)
        self.cache_duration = timedelta(hours=1)
    
    def _get_cache(self, slot: str, allow_stale=False) -> Optional[SkillCache]:
        """Restore the matching raw response without initiating acquisition."""
        try:
            snapshot = character_snapshot(self.auth, "skills", slot, cache=self._snapshot_cache,
                                          allow_stale=allow_stale)
            if snapshot is not None:
                return self._apply_snapshot(slot, snapshot)
            self._set_cache(slot, None)
            return None
        except Exception as exc:
            self._set_cache(slot, None)
            print(f"ESISkills: cache unavailable ({type(exc).__name__})")
            return None

    def _apply_snapshot(self, slot, snapshot):
        raw_skills = trained_levels(snapshot)
        cached = SkillCache(
            TradingSkills(broker_relations=raw_skills.get(SKILL_IDS["broker_relations"], 0),
                          accounting=raw_skills.get(SKILL_IDS["accounting"], 0),
                          advanced_broker_relations=raw_skills.get(SKILL_IDS["advanced_broker_relations"], 0)),
            raw_skills, snapshot["acquired_at"], usable_until(snapshot))
        self._set_cache(slot, cached)
        return cached
    
    def _set_cache(self, slot: str, cache: Optional[SkillCache]):
        """Set cache for specified slot."""
        if slot == "buyer":
            self._buyer_cache = cache
        else:
            self._seller_cache = cache
            self._cache = cache  # Legacy compatibility
    
    def fetch_skills(self, force_refresh: bool = False, slot: str = "seller") -> Optional[TradingSkills]:
        """
        Fetch character skills and return TradingSkills object.
        
        Args:
            force_refresh: Request an eligible update; never bypass ESI expiry
            slot: "seller" or "buyer"
        
        Returns:
            TradingSkills object, or None if fetch failed
        """
        try:
            snapshot = character_snapshot(self.auth, "skills", slot, fetch=True,
                force=force_refresh, cache=self._snapshot_cache, trigger="trading_skills")
            trading_skills = self._apply_snapshot(slot, snapshot).skills
        except Exception as exc:
            self._set_cache(slot, None)
            print(f"ESISkills request error: {type(exc).__name__}")
            return None
        print(f"ESISkills: Loaded {len(snapshot['payload']['skills'])} skills for {slot}")
        print(f"  Broker Relations: {trading_skills.broker_relations}")
        print(f"  Accounting: {trading_skills.accounting}")
        print(f"  Advanced Broker Relations: {trading_skills.advanced_broker_relations}")
        
        return trading_skills
    
    def get_skills(self, slot: str = "seller") -> TradingSkills:
        """Return cached trading skills without initiating an ESI request."""
        cache = self._get_cache(slot)
        if cache and not cache.is_expired:
            return cache.skills

        from core.calculate import DEFAULT_SKILLS
        return DEFAULT_SKILLS
    
    def get_seller_skills(self) -> TradingSkills:
        """Get trading skills for seller character."""
        return self.get_skills("seller")
    
    def get_buyer_skills(self) -> TradingSkills:
        """Get trading skills for buyer character."""
        return self.get_skills("buyer")
    
    def get_skill_level(self, skill_name: str, slot: str = "seller", *, allow_stale=False) -> int:
        """
        Get level of a specific skill by name.
        
        Args:
            skill_name: Skill name (e.g., "accounting", "broker_relations")
            slot: "seller" or "buyer"
        
        Returns:
            Trained level (0-5), or 0 if unknown/not trained
        """
        cache = self._get_cache(slot, allow_stale=allow_stale)
        if not cache or (cache.is_expired and not allow_stale):
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
        if not cache or cache.is_expired:
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
        
        remaining = (cache.expires_at - datetime.now(timezone.utc)).total_seconds()
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
    
    def __init__(self, auth, skills_fetcher: 'ESISkills' = None, cache=None):
        self.auth = auth
        self.skills_fetcher = skills_fetcher
        self._snapshot_cache = cache
        
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

    def _get_cached_standings(self, slot: str) -> Optional[dict]:
        """Derive from matching durable raw responses; never acquire on a read."""
        try:
            snapshot = character_snapshot(self.auth, "standings", slot, cache=self._snapshot_cache)
            if snapshot is not None:
                self._read_social_skills(slot)
                return self._categorize(snapshot["payload"], slot)
        except Exception as exc:
            print(f"ESIStandings: cache unavailable ({type(exc).__name__})")
        self.clear_cache(slot)
        return None

    def _read_social_skills(self, slot):
        connections = self.skills_fetcher.get_skill_level("connections", slot, allow_stale=True) if self.skills_fetcher else 0
        diplomacy = self.skills_fetcher.get_skill_level("diplomacy", slot, allow_stale=True) if self.skills_fetcher else 0
        if slot == "buyer":
            self._buyer_connections, self._buyer_diplomacy = connections, diplomacy
        else:
            self._seller_connections, self._seller_diplomacy = connections, diplomacy

    def _categorize(self, data, slot):
        result = {key: {} for key in ("agents", "npc_corps", "factions", "npc_corps_base", "factions_base")}
        for row in data:
            category = {"agent": "agents", "npc_corp": "npc_corps", "faction": "factions"}[row["from_type"]]
            result[category][row["from_id"]] = self._apply_skill_modifier(row["standing"], slot)
            if category != "agents":
                result[category + "_base"][row["from_id"]] = row["standing"]
        return result

    def _standings_snapshot(self, slot, force_refresh):
        try:
            # This acquisition remains inside the existing explicit refresh path.
            self._fetch_social_skills(slot)
            snapshot = character_snapshot(self.auth, "standings", slot, fetch=True,
                force=force_refresh, cache=self._snapshot_cache, trigger="trading_standings")
            self._read_social_skills(slot)
            return snapshot
        except Exception as exc:
            self.clear_cache(slot)
            print(f"ESIStandings request error: {type(exc).__name__}")
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
        snapshot = self._standings_snapshot(slot, force_refresh)
        if snapshot is None:
            return None
        data = snapshot["payload"]
        
        # Parse into categories and apply skill modifiers. The *_base dicts
        # keep the raw ESI standings (no Connections/Diplomacy) — broker fee
        # math needs those; display and reprocessing tax use the adjusted ones.
        standings = self._categorize(data, slot)
        
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
            from_type = entry.get('from_type')

            if from_type not in ('agent', 'npc_corp', 'faction'):
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
            self._buyer_cache_time = snapshot["acquired_at"]
        else:
            self._seller_standings_cache = standings
            self._seller_cache_time = snapshot["acquired_at"]
            # Legacy compatibility
            self._standings_cache = standings
            self._cache_time = snapshot["acquired_at"]
        
        connections = self._buyer_connections if slot == "buyer" else self._seller_connections
        print(f"ESIStandings: Loaded {len(standings['factions'])} faction standings, "
              f"{len(standings['npc_corps'])} corp standings for {slot} (Connections L{connections})")
        
        return standings
    
    def get_faction_standing(self, faction_id: int, slot: str = "seller",
                             base: bool = False) -> float:
        """Get standing with a specific faction.

        base=True returns the raw ESI standing (no Connections/Diplomacy) —
        required for broker fee math; the in-game fee ignores social skills.
        """
        standings = self._get_cached_standings(slot)
        if not standings:
            return 0.0
        key = 'factions_base' if base else 'factions'
        return standings.get(key, standings['factions']).get(faction_id, 0.0)

    def get_corp_standing(self, corp_id: int, slot: str = "seller",
                          base: bool = False) -> float:
        """Get standing with a specific NPC corp (base=True: see get_faction_standing)."""
        standings = self._get_cached_standings(slot)
        if not standings:
            return 0.0
        key = 'npc_corps_base' if base else 'npc_corps'
        return standings.get(key, standings['npc_corps']).get(corp_id, 0.0)
    
    def get_station_standings(self, station_id: int, slot: str = "seller",
                              base: bool = False) -> tuple[float, float]:
        """
        Get both corp and faction standings for a station.

        Returns: (station_standing, faction_standing)

        base=True returns raw ESI standings (no Connections/Diplomacy) —
        use that for broker fee math. Default (adjusted) is for display and
        reprocessing tax, which DO scale with social skills.

        Uses config.TRADE_HUBS for station -> corp/faction mappings.
        For stations not in config, returns (0.0, 0.0)
        """
        from core.config import TRADE_HUBS

        standings = self._get_cached_standings(slot)
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

        corp_key = 'npc_corps_base' if base else 'npc_corps'
        fac_key = 'factions_base' if base else 'factions'
        corps = standings.get(corp_key, standings['npc_corps'])
        facs = standings.get(fac_key, standings['factions'])
        corp_standing = corps.get(corp_id, 0.0) if corp_id else 0.0
        faction_standing = facs.get(faction_id, 0.0) if faction_id else 0.0

        return (corp_standing, faction_standing)

    def get_standings_for_hub(self, hub_key: str, slot: str = "seller",
                              base: bool = False) -> tuple[float, float]:
        """
        Get standings for a hub by its config key (e.g., 'amarr', 'jita').

        Returns: (corp_standing, faction_standing); base=True -> raw ESI
        standings for fee math (see get_station_standings).
        """
        from core.config import get_hub_config

        hub = get_hub_config(hub_key)
        return self.get_station_standings(hub["station_id"], slot, base=base)
    
    def get_amarr_standings(self, slot: str = "seller") -> tuple[float, float]:
        """
        Convenience method: Get standings for Amarr trading.
        
        Returns: (emperor_family_corp_standing, amarr_empire_faction_standing)
        """
        return self.get_standings_for_hub("amarr", slot)
    
    def get_cache_status(self, slot: str = "seller", force_refresh=False) -> tuple[bool, int]:
        """
        Check if cache is valid and how long until it expires.
        
        Args:
            slot: "seller" or "buyer"
            
        Returns:
            (can_refresh, seconds_remaining)
            - can_refresh: True if cache is expired or empty (OK to fetch)
            - seconds_remaining: Seconds until cache expires (0 if expired/empty)
        """
        try:
            snapshot = character_snapshot(self.auth, "standings", slot, cache=self._snapshot_cache)
        except Exception:
            snapshot = None
        if snapshot is None:
            return True, 0
        eligible = snapshot["expires"] if force_refresh else usable_until(snapshot)
        remaining = (eligible - datetime.now(timezone.utc)).total_seconds()
        return remaining <= 0, max(0, int(remaining))
    
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
