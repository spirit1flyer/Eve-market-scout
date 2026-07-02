"""Configuration constants for EVE Market Scout.

Note: Fee calculations now live in calculate.py and are skill-dependent.
The values below are kept for reference/documentation only.
"""

# =============================================================================
# APP VERSION - Update this single line to change version everywhere
# =============================================================================
APP_VERSION = "3.2 live"

# Region IDs
JITA_REGION_ID = 10000002  # The Forge
AMARR_REGION_ID = 10000043  # Domain

# System IDs
AMARR_SYSTEM_ID = 30002187  # Amarr solar system
JITA_SYSTEM_ID = 30000142   # Jita solar system

# Station IDs
JITA_STATION_ID = 60003760  # Jita IV - Moon 4 - Caldari Navy Assembly Plant
AMARR_STATION_ID = 60008494  # Amarr VIII (Oris) - Emperor Family Academy

# =============================================================================
# TRADE HUBS - Configuration for multi-hub support
# =============================================================================
TRADE_HUBS = {
    "amarr": {
        "name": "Amarr",
        "station_id": 60008494,      # Amarr VIII (Oris) - Emperor Family Academy
        "region_id": 10000043,       # Domain
        "system_id": 30002187,       # Amarr solar system
        "corp_id": 1000086,          # Emperor Family (verified against ESI standings dump 2026-05)
        "faction_id": 500003,        # Amarr Empire
        "enabled": True,
    },
    "jita": {
        "name": "Jita",
        "station_id": 60003760,      # Jita IV - Moon 4 - Caldari Navy Assembly Plant
        "region_id": 10000002,       # The Forge
        "system_id": 30000142,       # Jita solar system
        "corp_id": 1000035,          # Caldari Navy
        "faction_id": 500001,        # Caldari State
        "enabled": True,
    },
    "dodixie": {
        "name": "Dodixie",
        "station_id": 60011866,      # Dodixie IX - Moon 20 - Federation Navy Assembly Plant
        "region_id": 10000032,       # Sinq Laison
        "system_id": 30002659,       # Dodixie solar system
        "corp_id": 1000120,          # Federation Navy
        "faction_id": 500004,        # Gallente Federation
        "enabled": True,
    },
    "hek": {
        "name": "Hek",
        "station_id": 60005686,      # Hek VIII - Moon 12 - Boundless Creation Factory
        "region_id": 10000042,       # Metropolis
        "system_id": 30002053,       # Hek solar system
        "corp_id": 1000102,          # Boundless Creation
        "faction_id": 500002,        # Minmatar Republic
        "enabled": True,
    },
    "rens": {
        "name": "Rens",
        "station_id": 60004588,      # Rens VI - Moon 8 - Brutor Tribe Treasury
        "region_id": 10000030,       # Heimatar
        "system_id": 30002510,       # Rens solar system
        "corp_id": 1000049,          # Brutor Tribe
        "faction_id": 500002,        # Minmatar Republic
        "enabled": True,
    },
}

# Default hub
DEFAULT_HUB = "amarr"


def get_hub_config(hub_key: str) -> dict:
    """Get configuration for a specific hub."""
    return TRADE_HUBS.get(hub_key, TRADE_HUBS[DEFAULT_HUB])


def get_enabled_hubs() -> list[tuple[str, str]]:
    """Get list of (key, display_name) for enabled hubs."""
    result = []
    for key, config in TRADE_HUBS.items():
        if config["enabled"]:
            result.append((key, config["name"]))
    return result


def station_id_to_hub_key(station_id: int) -> str | None:
    """Reverse-lookup a station_id to a hub_key.

    Covers built-in TRADE_HUBS and any custom stations already loaded into it.
    Returns None for player structures or otherwise unknown station_ids.
    """
    for key, cfg in TRADE_HUBS.items():
        if cfg.get("station_id") == station_id:
            return key
    return None


def register_custom_station(station_dict: dict):
    """Merge a custom station entry into TRADE_HUBS at runtime.

    Called by custom_stations.py at startup and by AddStationDialog when
    the user adds a new station.  No-op if the key is already present.
    """
    key = station_dict.get("hub_key")
    if key and key not in TRADE_HUBS:
        TRADE_HUBS[key] = {**station_dict, "enabled": True, "custom": True}


# Filtering thresholds (defaults - can be overridden in the GUI)
MIN_PROFIT_PER_UNIT = 1_000   # Minimum profit per item in ISK
MIN_TOTAL_PROFIT = 200_000    # Minimum total profit (profit Ãƒâ€” volume)
MIN_MARGIN_PERCENT = 0        # Minimum net margin % (0 = disabled, 10 = require 10%+ margin)
SCAM_THRESHOLD = 0.05         # 5% - if Amarr price exceeds Jita by this much, likely scam

# Spread thresholds for deal detection
MAX_SPREAD_PERCENT_SINGLE = 1.0   # Max 1% spread for single high-value items
MAX_SPREAD_PERCENT_VOLUME = 5.0   # Allow up to 5% spread for volume deals

# Jump and security settings
MAX_JUMPS_FROM_AMARR = 10  # Only consider systems within this many jumps
MIN_SECURITY_STATUS = 0.45  # High-sec only (0.5+ rounds to high, 0.45 is safe threshold)

# API settings
ESI_BASE_URL = "https://esi.evetech.net/latest"
MAX_CONCURRENT_REQUESTS = 20
REQUEST_TIMEOUT = 60  # total per-request wall-clock; doubled from 30 so a
                      # single transfer can still finish on a throttled link.
                      # On timeouts, ESICl._fetch_pages_staged re-pulls just
                      # the failed pages at progressively lower concurrency.

# User-Agent sent on every ESI request. CCP's best-practices doc says apps
# SHOULD identify themselves; anonymous traffic is what gets banned first
# when CCP investigates abuse. Format: "app-name/version (contact)".
# Update the contact if you want CCP to reach a different inbox/handle.
ESI_USER_AGENT = f"EVE-Market-Scout/{APP_VERSION} (spirit1flyer@yahoo.com; Discord: spirit1flyer)"

# When the X-ESI-Error-Limit-Remain header drops to or below this number,
# we pre-emptively pause outgoing requests until the window resets. Hitting
# the limit (a true 420) gets you tossed for the rest of the window AND
# flagged for ban consideration on repeat offenses; staying ~10 away is cheap
# insurance against a misbehaving endpoint draining the budget on us.
ERROR_LIMIT_PAUSE_THRESHOLD = 10

# Auto-refresh settings
AUTO_REFRESH_ENABLED = True
AUTO_REFRESH_INTERVAL = 60  # seconds between refreshes
SOUND_ALERTS_ENABLED = True

# Volume filter (can be overridden in GUI)
MIN_DAILY_VOLUME = 5  # Minimum avg daily volume to show deal (0 = disabled)


# =============================================================================
# LEGACY FEE CONSTANTS (for reference only - actual math in calculate.py)
# =============================================================================
# These were hardcoded before skills were implemented.
# Keeping them here for documentation of what they were.
#
# BROKER_FEE_PERCENT = 1.48   # With Broker Relations 5 + some standing
# SALES_TAX_PERCENT = 3.37    # With Accounting 5
# TOTAL_FEES_PERCENT = 4.85   # Combined
#
# Base rates (no skills):
#   Broker Fee: 3.0%
#   Sales Tax: 8.0%
#   Total: 11.0%
#
# With max skills (BR5, Acc5, no standing):
#   Broker Fee: 1.5%
#   Sales Tax: 3.6%
#   Total: 5.1%
