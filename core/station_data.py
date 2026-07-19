"""ESI data loader for region/system/station cascading dropdowns.

Fetches region → system → station data lazily.  Region list and system lists
are cached to disk so subsequent dialog opens are instant.  Station lists are
fetched fresh per-system (fast — only a handful of stations per system).
"""

import json
import asyncio
from core.config import ESI_BASE_URL

_CACHE_FILE = "station_data_cache.json"
_KSPACE_MAX_REGION_ID = 11_000_000  # wormhole regions start above this

# Module-level in-memory cache (lives for the whole app session)
_regions: list[tuple[int, str]] = []
_systems: dict[int, list[tuple[int, str]]] = {}  # region_id -> [(system_id, name)]


# ---------------------------------------------------------------------------
# Disk cache helpers

def _data_path():
    from core.sound_manager import get_data_dir
    return get_data_dir() / _CACHE_FILE


def _load_disk() -> dict:
    p = _data_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_disk(data: dict):
    try:
        _data_path().write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        print(f"[StationData] cache write error: {e}")


# ---------------------------------------------------------------------------
# ESI helpers

async def _batch_names(client, ids: list[int]) -> dict[int, str]:
    """Resolve a list of ESI IDs to names via /universe/names/.

    Works for any category (regions, solar_systems, stations, …).
    """
    result: dict[int, str] = {}
    if not ids:
        return result
    BATCH = 500
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        try:
            async with client.semaphore:
                url = f"{ESI_BASE_URL}/universe/names/"
                async with client.session.post(url, json=batch) as resp:
                    if resp.status == 200:
                        for item in await resp.json():
                            result[item["id"]] = item["name"]
        except Exception as e:
            print(f"[StationData] batch names error: {e}")
    return result


# ---------------------------------------------------------------------------
# Public API

async def fetch_regions(client) -> list[tuple[int, str]]:
    """Return sorted (region_id, region_name) list for all K-space regions."""
    global _regions
    if _regions:
        return _regions

    disk = _load_disk()
    if disk.get("regions"):
        _regions = [tuple(r) for r in disk["regions"]]
        return _regions

    all_ids: list[int] = await client._get("/universe/regions/")
    kspace_ids = [rid for rid in all_ids if rid < _KSPACE_MAX_REGION_ID]
    names = await _batch_names(client, kspace_ids)

    _regions = sorted(
        [(rid, names.get(rid, f"Region {rid}")) for rid in kspace_ids],
        key=lambda x: x[1],
    )
    disk["regions"] = _regions
    _save_disk(disk)
    return _regions


async def fetch_systems_in_region(client, region_id: int) -> list[tuple[int, str]]:
    """Return sorted (system_id, system_name) list for a region, cached per region."""
    global _systems
    if region_id in _systems:
        return _systems[region_id]

    disk = _load_disk()
    key = str(region_id)
    if disk.get("systems", {}).get(key):
        _systems[region_id] = [tuple(s) for s in disk["systems"][key]]
        return _systems[region_id]

    region_data = await client._get(f"/universe/regions/{region_id}/")
    const_ids = region_data.get("constellations", [])

    const_tasks = [client._get(f"/universe/constellations/{cid}/") for cid in const_ids]
    const_results = await asyncio.gather(*const_tasks, return_exceptions=True)

    system_ids: list[int] = []
    for r in const_results:
        if isinstance(r, dict):
            system_ids.extend(r.get("systems", []))

    names = await _batch_names(client, system_ids)
    pairs = sorted(
        [(sid, names.get(sid, f"System {sid}")) for sid in system_ids],
        key=lambda x: x[1],
    )
    _systems[region_id] = pairs

    if "systems" not in disk:
        disk["systems"] = {}
    disk["systems"][key] = pairs
    _save_disk(disk)
    return pairs


async def fetch_stations_in_system(client, system_id: int, region_id: int) -> list[dict]:
    """Fetch NPC station dicts for a system (not cached — only a few per system)."""
    system_data = await client._get(f"/universe/systems/{system_id}/")
    station_ids: list[int] = system_data.get("stations", [])
    if not station_ids:
        return []

    tasks = [client._get(f"/universe/stations/{sid}/") for sid in station_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    stations = []
    for r in results:
        if isinstance(r, dict) and "station_id" in r:
            stations.append({
                "station_id": r["station_id"],
                "name": r.get("name", f"Station {r['station_id']}"),
                "system_id": system_id,
                "region_id": region_id,
                "corp_id": r.get("owner"),
            })
    return sorted(stations, key=lambda x: x["name"])
