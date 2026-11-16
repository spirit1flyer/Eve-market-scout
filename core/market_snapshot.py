"""Complete regional books assembled from durable, expiry-aware pages."""

import asyncio

from core.esi_public_cache import _utc
from core.esi_route_cache import validate_route_payload
from core.market_pages import get_regional_pages
from core.esi_cache_io import async_guard


class ESIPaginationChangedError(RuntimeError):
    """Pages cannot establish one complete ESI representation."""


async def regional_page(client, store, region, page):
    async with async_guard(store._lock(("acquire", region, page))):
        return await _regional_page_locked(client, store, region, page)


async def _regional_page_locked(client, store, region, page):
    row = await asyncio.to_thread(store.peek_page, region, page)
    if row is not None:
        print(f"[RegionalPages] decision=REUSE region={region} page={page} expires={row['expires']}")
        return row
    payload, headers = await client._get_with_headers(f"/markets/{region}/orders/", {"page": page})
    try:
        return await asyncio.to_thread(store.accept_page, region, page, payload, headers)
    except (ValueError, KeyError, TypeError) as exc:
        print(
            f"[RegionalPages] decision=REJECT region={region} page={page} "
            f"error_type={type(exc).__name__} details={str(exc)!r}"
        )
        raise ESIPaginationChangedError(f"invalid_page={page} error_type={type(exc).__name__}") from exc


async def fetch_market_snapshot(client, region_id):
    store = getattr(client, "_regional_pages", None) or get_regional_pages()
    first = await regional_page(client, store, region_id, 1)
    total = int(first["headers"].get("x-pages", "1"))
    rows = {1: first}
    if total > 1:
        results, _validators, stale = await client._fetch_pages_staged(
            f"/markets/{region_id}/orders/", list(range(2, total + 1)),
            page_loader=lambda page: regional_page(client, store, region_id, page))
        if stale or set(results) != set(range(2, total + 1)):
            raise ESIPaginationChangedError("missing_or_vanished_pages")
        rows.update(results)
    validator = first["headers"].get("last-modified")
    if total > 1 and not validator:
        raise ESIPaginationChangedError("missing_multi_page_validator")
    orders = []
    metadata = []
    for page, row in sorted(rows.items()):
        headers = row["headers"]
        if (int(headers.get("x-pages", "1")) != total
                or headers.get("last-modified") != validator):
            raise ESIPaginationChangedError(f"representation_changed_page={page}")
        orders.extend(row["payload"])
        metadata.append({"page": page, "total_pages": total, "expires": row["expires"],
                         "acquired_at": row["acquired_at"], "last_modified": validator,
                         "etag": headers.get("etag")})
    try:
        validate_route_payload("type_orders", orders)
    except (ValueError, KeyError, TypeError) as exc:
        raise ESIPaginationChangedError("invalid_full_book") from exc
    return orders, max(_utc(row["expires"]) for row in rows.values()), total, {
        "etag": first["headers"].get("etag"), "last_modified": validator,
        "complete": True, "source": "esi_region_orders", "page_metadata": metadata,
        "acquired_at": max(_utc(row["acquired_at"]) for row in rows.values()),
    }
