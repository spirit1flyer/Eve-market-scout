"""Shared automatic item exclusions.

These filters are for items that should not enter recommendation/calculation
pipelines at all. They are deliberately separate from user-managed ignore
lists: automatic exclusions cannot go stale as CCP adds new type IDs.
"""

import threading
from collections.abc import Iterable
from typing import Any, Optional


EXPIRED_NAME_PREFIX = "expired "
_expired_status_cache: dict[int, bool] = {}
_expired_status_lock = threading.Lock()


def is_expired_item_name(name: Optional[str]) -> bool:
    """Return True when CCP's display name marks an item as expired."""
    return bool(
        name
        and name.strip().casefold().startswith(EXPIRED_NAME_PREFIX)
    )


def get_expired_type_ids(
    type_ids: Iterable[int],
    *,
    sde=None,
) -> set[int]:
    """Resolve automatic expired exclusions for a collection of type IDs.

    The local SDE is authoritative for the display names. Missing/unavailable
    SDE data fails open so an unresolved item is never hidden accidentally.
    """
    unique_ids = {int(type_id) for type_id in type_ids}
    if not unique_ids:
        return set()

    with _expired_status_lock:
        expired_ids = {
            type_id for type_id in unique_ids
            if _expired_status_cache.get(type_id) is True
        }
        unresolved_ids = {
            type_id for type_id in unique_ids
            if type_id not in _expired_status_cache
        }
    if not unresolved_ids:
        return expired_ids

    if sde is None:
        try:
            from sde.sde_manager import get_sde_manager
            sde = get_sde_manager()
        except Exception:
            return expired_ids

    try:
        if not sde.is_available():
            return expired_ids
        names = sde.get_type_names_bulk(list(unresolved_ids))
    except Exception:
        return expired_ids

    resolved_status = {
        type_id: is_expired_item_name(name)
        for type_id, name in names.items()
    }
    with _expired_status_lock:
        _expired_status_cache.update(resolved_status)
    expired_ids.update(
        type_id for type_id, is_expired in resolved_status.items()
        if is_expired
    )
    return expired_ids


def is_expired_type_id(type_id: int, *, sde=None) -> bool:
    """Single-type convenience wrapper used by ignore-list fallbacks."""
    return int(type_id) in get_expired_type_ids([type_id], sde=sde)


def filter_expired_orders(
    orders: Iterable[dict[str, Any]],
    *,
    sde=None,
    context: str = "market",
) -> list[dict[str, Any]]:
    """Drop expired named items from an order collection before processing."""
    order_list = orders if isinstance(orders, list) else list(orders)
    expired_ids = get_expired_type_ids(
        (order.get("type_id") for order in order_list if order.get("type_id")),
        sde=sde,
    )
    if not expired_ids:
        return order_list

    filtered = [
        order for order in order_list
        if int(order.get("type_id", 0)) not in expired_ids
    ]
    print(
        f"[AutoIgnore] context={context} expired_types={len(expired_ids)} "
        f"orders_removed={len(order_list) - len(filtered)}"
    )
    return filtered


def filter_expired_profiles(profiles: Iterable[Any], *, sde=None) -> list[Any]:
    """Drop expired named items from objects carrying a ``type_id`` field."""
    profile_list = list(profiles)
    expired_ids = get_expired_type_ids(
        (profile.type_id for profile in profile_list),
        sde=sde,
    )
    if not expired_ids:
        return profile_list
    return [p for p in profile_list if int(p.type_id) not in expired_ids]
