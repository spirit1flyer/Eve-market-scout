"""Persistence for user-added custom NPC stations.

Loaded early in main.py so TRADE_HUBS is fully populated before any GUI widget
reads get_enabled_hubs() or get_hub_config().
"""

import json
from pathlib import Path

_FILE = "custom_stations.json"

# Populated once on first call to _data_path() to avoid importing sound_manager
# at module-load time (it triggers logging setup).
_data_path_cache: Path | None = None


def _data_path() -> Path:
    global _data_path_cache
    if _data_path_cache is None:
        from core.sound_manager import get_data_dir
        _data_path_cache = get_data_dir() / _FILE
    return _data_path_cache


def get_custom_hub_key(station_id: int) -> str:
    return f"custom_{station_id}"


def is_custom_hub(hub_key: str) -> bool:
    return hub_key.startswith("custom_")


def load_custom_stations() -> list[dict]:
    """Return the persisted list of custom station dicts."""
    path = _data_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_custom_stations(stations: list[dict]):
    path = _data_path()
    path.write_text(json.dumps(stations, indent=2), encoding="utf-8")


def add_custom_station(
    station_dict: dict,
    in_stock_market: bool = False,
    station_type: str = "npc",
) -> str:
    """Persist a new custom station and register it in TRADE_HUBS.

    `station_type` is "npc" or "structure". Structures route their order
    fetches through the authenticated `/markets/structures/{id}/` endpoint
    instead of the public regional one.

    Returns the hub_key.
    """
    from core.config import register_custom_station

    hub_key = get_custom_hub_key(station_dict["station_id"])
    entry = {
        **station_dict,
        "hub_key": hub_key,
        "in_stock_market": in_stock_market,
        "type": station_type,
    }

    stations = load_custom_stations()
    if not any(s["hub_key"] == hub_key for s in stations):
        stations.append(entry)
        save_custom_stations(stations)

    register_custom_station(entry)
    return hub_key


def remove_custom_station(hub_key: str):
    """Remove a custom station entirely from disk and TRADE_HUBS."""
    from core.config import TRADE_HUBS
    stations = [s for s in load_custom_stations() if s["hub_key"] != hub_key]
    save_custom_stations(stations)
    TRADE_HUBS.pop(hub_key, None)


def update_station_in_stockmarket(hub_key: str, in_stock_market: bool):
    """Flip the in_stock_market flag without removing the station from the scanner."""
    from core.config import TRADE_HUBS
    stations = load_custom_stations()
    for s in stations:
        if s["hub_key"] == hub_key:
            s["in_stock_market"] = in_stock_market
            break
    save_custom_stations(stations)
    if hub_key in TRADE_HUBS:
        TRADE_HUBS[hub_key]["in_stock_market"] = in_stock_market


def _bootstrap():
    """Register all persisted custom stations into TRADE_HUBS at import time.

    Backfills `type="npc"` on legacy entries written before structures were
    supported, so downstream code can branch on `cfg["type"]` unconditionally.
    """
    from core.config import register_custom_station
    for entry in load_custom_stations():
        if "type" not in entry:
            entry["type"] = "npc"
        register_custom_station(entry)


_bootstrap()
