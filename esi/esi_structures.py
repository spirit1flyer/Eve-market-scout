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

from core.config import ESI_USER_AGENT
from esi.esi_auth import ESIAuth

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
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code < 500:
            return resp
        last = resp
    return last  # type: ignore[return-value]


def fetch_structure_info(
    structure_id: int, auth: ESIAuth, slot: str = "seller"
) -> StructureInfo:
    """Resolve structure metadata. Raises StructureAccessError on 403/404."""
    headers = auth.get_auth_headers(slot)
    if not headers:
        raise StructureAccessError(structure_id, 401, "not authenticated")

    resp = _request_with_retry(
        f"{BASE_URL}/universe/structures/{structure_id}/", headers=headers
    )

    if resp.status_code in (403, 404):
        raise StructureAccessError(structure_id, resp.status_code, resp.text[:160])
    resp.raise_for_status()

    data = resp.json()
    return StructureInfo(
        structure_id=structure_id,
        name=data.get("name", f"Structure {structure_id}"),
        solar_system_id=data["solar_system_id"],
        type_id=data["type_id"],
        owner_id=data.get("owner_id"),
    )


_STRUCTURE_ID_FLOOR = 1_000_000_000_000


def is_structure_id(value: int) -> bool:
    """Player structures live above 1T; NPC station IDs are well below."""
    return value >= _STRUCTURE_ID_FLOOR


def resolve_region_for_system(system_id: int) -> int:
    """Walk system → constellation → region via public ESI (no auth)."""
    r1 = requests.get(
        f"{BASE_URL}/universe/systems/{system_id}/",
        headers={"User-Agent": ESI_USER_AGENT}, timeout=30
    )
    r1.raise_for_status()
    const_id = r1.json()["constellation_id"]

    r2 = requests.get(
        f"{BASE_URL}/universe/constellations/{const_id}/",
        headers={"User-Agent": ESI_USER_AGENT}, timeout=30
    )
    r2.raise_for_status()
    return r2.json()["region_id"]


def discover_accessible_structures(
    auth: ESIAuth, slot: str = "seller"
) -> list[StructureInfo]:
    """Enumerate player structures the character has active orders at.

    Returns a list of StructureInfo for every distinct location_id in the
    character's active orders that looks like a player structure (>= 1T).
    Unreachable structures (403/404 on resolve) are skipped silently — they
    may be ones the character has lost access to since placing the order.
    """
    headers = auth.get_auth_headers(slot)
    char = auth.get_character(slot)
    if not headers or not char or not char.character_id:
        raise StructureAccessError(0, 401, f"{slot} slot not authenticated")

    resp = _request_with_retry(
        f"{BASE_URL}/characters/{char.character_id}/orders/", headers=headers
    )
    resp.raise_for_status()
    orders = resp.json()

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
    headers = auth.get_auth_headers(slot)
    if not headers:
        raise StructureAccessError(0, 401, "not authenticated")

    char = auth.get_character(slot)
    if not char or not char.character_id:
        raise StructureAccessError(0, 401, "no character id for slot")

    resp = _request_with_retry(
        f"{BASE_URL}/characters/{char.character_id}/search/",
        headers=headers,
        params={"categories": "structure", "search": query, "strict": str(strict).lower()},
    )
    if resp.status_code in (401, 403):
        raise StructureAccessError(0, resp.status_code, resp.text[:160])
    resp.raise_for_status()

    data = resp.json()
    return list(data.get("structure", []))


def fetch_structure_orders(
    structure_id: int, auth: ESIAuth, slot: str = "seller"
) -> Tuple[list, Optional[datetime]]:
    """Fetch every page of a structure's market orders.

    Returns (orders, expires_utc). Order dicts match the regional /markets/{id}/
    orders/ shape so they slot straight into the existing scanner pipeline.
    Raises StructureAccessError on 403/404.
    """
    headers = auth.get_auth_headers(slot)
    if not headers:
        raise StructureAccessError(structure_id, 401, "not authenticated")

    url = f"{BASE_URL}/markets/structures/{structure_id}/"
    first = _request_with_retry(url, headers=headers, params={"page": 1})

    if first.status_code in (403, 404):
        raise StructureAccessError(structure_id, first.status_code, first.text[:160])
    first.raise_for_status()

    orders: list = list(first.json())
    expires = _parse_expires(first)
    total_pages = int(first.headers.get("X-Pages", "1"))

    for page in range(2, total_pages + 1):
        resp = _request_with_retry(url, headers=headers, params={"page": page})
        if resp.status_code >= 400:
            print(f"[ESI-Struct] page {page}/{total_pages} for {structure_id}: "
                  f"HTTP {resp.status_code} — partial results returned")
            break
        orders.extend(resp.json())

    return orders, expires
