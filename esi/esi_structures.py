"""Fetcher for player-owned structure markets (PRS).

Two endpoints, both authenticated:
  GET /universe/structures/{id}/    -> name, solar_system_id, type_id, owner_id
  GET /markets/structures/{id}/     -> paginated order list, region-less

Returns raw dicts matching ESI's regional order shape so existing scan code
can consume them with no translation layer.

Common failure modes:
  403 -> character has no docking/market access (or scope missing). Treated as
         "structure dark for this character" — caller should drop it.
  404 -> structure doesn't exist / was destroyed. Caller should prune from
         saved list.
  5xx -> CCP burp. One retry, then give up; caller falls back to stale cache.
"""

import requests
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Tuple

from core.config import get_esi_headers
from core.esi_rate_limit import esi_request
from esi.esi_auth import ESIAuth
from core.esi_private_cache import get_private_cache, private_context

BASE_URL = "https://esi.evetech.net/latest"


@dataclass
class StructureInfo:
    structure_id: int
    name: str
    solar_system_id: int
    type_id: int
    owner_id: Optional[int] = None


class StructureAccessError(Exception):
    """Raised when a structure is unreachable (403/404). Caller decides whether
    to prune from saved list (404) or just skip this cycle (403)."""

    def __init__(self, structure_id: int, status: int, message: str = ""):
        self.structure_id = structure_id
        self.status = status
        super().__init__(f"structure {structure_id}: HTTP {status} {message}")


class StructureSnapshotError(RuntimeError):
    """Raised rather than returning a partial or mixed-page order snapshot."""


def _parse_expires(response: requests.Response) -> Optional[datetime]:
    expires_str = response.headers.get("Expires")
    if not expires_str:
        return None
    try:
        return datetime.strptime(
            expires_str, "%a, %d %b %Y %H:%M:%S %Z"
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _request_with_retry(
    url: str, headers: dict, params: Optional[dict] = None
) -> requests.Response:
    """One retry on 5xx — covers transient CCP hiccups without masking real
    auth/permission failures."""
    last: Optional[requests.Response] = None
    for attempt in (1, 2):
        resp = esi_request("GET", url, headers=headers, params=params, timeout=30)
        if resp.status_code < 500:
            return resp
        last = resp
    return last  # type: ignore[return-value]


def fetch_structure_info(
    structure_id: int, auth: ESIAuth, slot: str = "seller", *, cache=None
) -> StructureInfo:
    """Resolve structure metadata. Raises StructureAccessError on 403/404."""
    data = _structure_snapshot("structure_info", structure_id, auth, slot, cache=cache)["payload"]
    return StructureInfo(structure_id, data["name"], data["solar_system_id"],
                         data["type_id"], data.get("owner_id"))


def _structure_snapshot(resource, structure_id, auth, slot, *, cache=None, fetch=True):
    from core.esi_route_cache import route_snapshot
    from core.esi_private_cache import PrivateSnapshotError
    try:
        return route_snapshot(resource, structure_id, auth=auth, slot=slot,
                              cache=cache, fetch=fetch, trigger="structure_lookup")
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (401, 403, 404):
            raise StructureAccessError(structure_id, status) from exc
        raise StructureSnapshotError(f"HTTP {status}; previous snapshot retained") from exc
    except PrivateSnapshotError as exc:
        if str(exc) in ("not_authenticated", "authorization_changed", "character_context_mismatch"):
            raise StructureAccessError(structure_id, 401, "authorization unavailable or changed") from exc
        raise StructureSnapshotError(str(exc)) from exc
    except (requests.RequestException, ValueError, KeyError, OSError) as exc:
        raise StructureSnapshotError(type(exc).__name__) from exc


_STRUCTURE_ID_FLOOR = 1_000_000_000_000


def is_structure_id(value: int) -> bool:
    """Player structures live above 1T; NPC station IDs are well below."""
    return value >= _STRUCTURE_ID_FLOOR


def resolve_region_for_system(system_id: int) -> int:
    """Walk system → constellation → region via public ESI (no auth)."""
    from core.esi_route_cache import route_snapshot
    system = route_snapshot("system_info", system_id, trigger="structure_region")["payload"]
    constellation = route_snapshot("constellation_info", system["constellation_id"],
                                   trigger="structure_region")["payload"]
    return constellation["region_id"]


def discover_accessible_structures(
    auth: ESIAuth, slot: str = "seller"
) -> list[StructureInfo]:
    """Enumerate player structures the character has active orders at.

    Returns a list of StructureInfo for every distinct location_id in the
    character's active orders that looks like a player structure (>= 1T).
    Unreachable structures (403/404 on resolve) are skipped silently — they
    may be ones the character has lost access to since placing the order.
    """
    char = auth.get_character(slot)
    if not char or not char.character_id or not char.access_token:
        raise StructureAccessError(0, 401, f"{slot} slot not authenticated")

    context = private_context(auth, slot)
    snapshot = get_private_cache().get(auth, context, "orders", trigger="structure_discovery")
    orders = snapshot["payload"]

    structure_ids = sorted({
        o["location_id"] for o in orders
        if o.get("location_id", 0) >= _STRUCTURE_ID_FLOOR
    })

    results: list[StructureInfo] = []
    for sid in structure_ids:
        try:
            results.append(fetch_structure_info(sid, auth, slot=slot))
        except StructureAccessError:
            continue
    return results


def search_structures_by_name(
    query: str, auth: ESIAuth, slot: str = "seller", strict: bool = False
) -> list[int]:
    """Find structure IDs by name fragment, scoped to what the character can see.

    ESI's character search returns structures the character has any relation to:
    docked at, has orders in, corp/alliance owned, or any public structure with
    market access. `strict=False` does case-insensitive substring matching.
    Returns an empty list when no matches; raises StructureAccessError on auth
    failure so the caller can prompt re-login.
    """
    from core.esi_route_cache import route_snapshot
    from core.esi_private_cache import PrivateSnapshotError
    char = auth.get_character(slot)
    if not char or not char.character_id:
        raise StructureAccessError(0, 401, "not authenticated")
    try:
        snapshot = route_snapshot("structure_search", char.character_id,
            auth=auth, slot=slot, params={"categories": "structure", "search": query,
                                       "strict": str(strict).lower()}, trigger="structure_search")
        return list(snapshot["payload"].get("structure", []))
    except PrivateSnapshotError as exc:
        raise StructureAccessError(0, 401, "authorization unavailable or changed") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        if status in (401, 403):
            raise StructureAccessError(0, status) from exc
        raise


def fetch_structure_orders(
    structure_id: int, auth: ESIAuth, slot: str = "seller", *, cache=None
) -> Tuple[list, Optional[datetime], dict]:
    """Fetch every page of a structure's market orders.

    Returns (orders, expires_utc, metadata). Order dicts match the regional
    /markets/{id}/orders/ shape so they slot straight into the existing
    scanner pipeline. Raises StructureAccessError on 403/404.
    """
    snapshot = _structure_snapshot("structure_orders", structure_id, auth, slot, cache=cache)
    first = snapshot["page_metadata"][0]
    return snapshot["payload"], snapshot["expires"], {
        "etag": first.get("etag"), "last_modified": first.get("last_modified"),
        "complete": True, "source": "esi_structure_orders",
        "acquired_at": snapshot["acquired_at"], "durable": snapshot["durable"],
    }
