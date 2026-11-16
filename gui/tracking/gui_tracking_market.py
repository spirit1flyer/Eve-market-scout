"""Synchronous, hub-aware market snapshots for Tracking's underbid checks."""

from core.esi_route_cache import route_snapshot


def fetch_market_orders_sync(region_id, type_ids=None, order_cache=None, *,
                             route_cache=None, log=print):
    """Use a full regional snapshot or exact cached per-type sell responses."""
    if order_cache is not None:
        orders = order_cache.get_cached_orders(region_id, max_age_seconds=300)
        if orders is not None:
            age = order_cache.get_cache_timing(region_id).get("age_seconds")
            age_display = f"{age:.0f}s" if age is not None else "unknown"
            log("[TradeSync] stage=underbid_snapshot decision=REUSE "
                f"source=shared_region_cache region={region_id} "
                f"age={age_display} orders={len(orders)}")
            return orders
    if not type_ids:
        log("[TradeSync] stage=underbid_snapshot decision=SKIP "
            f"region={region_id} reason=no_active_sell_types")
        return []

    all_orders = []
    failed_types = 0
    for type_id in sorted(set(type_ids)):
        try:
            snapshot = route_snapshot("type_orders", region_id,
                params={"type_id": int(type_id), "order_type": "sell"},
                cache=route_cache, trigger="underbid_fallback")
            all_orders.extend(snapshot["payload"])
        except Exception as exc:
            failed_types += 1
            log("[ESI-Timing] resource=regional_orders "
                "trigger=underbid_fallback decision=FAILED "
                f"region={region_id} type_id={type_id} error_type={type(exc).__name__}")
    log("[ESI-Timing] resource=regional_orders trigger=underbid_fallback "
        f"decision=COMPLETE status={'ok' if failed_types == 0 else 'partial_failure'} "
        f"region={region_id} types={len(set(type_ids))} failed_types={failed_types} "
        f"orders={len(all_orders)}")
    return all_orders


def fetch_underbid_orders_sync(hub_config, type_ids=None, order_cache=None, *,
                               auth=None, slot="seller", route_cache=None, log=print):
    """Private structures must never fall through to public regional orders.

    The wallet's authorization is used for the same structure snapshot shared
    by Browse and the scanner. Exceptions propagate so the caller clears its
    view and reports failure, rather than substituting an empty public book.
    """
    if hub_config.get("type") != "structure":
        return fetch_market_orders_sync(hub_config["region_id"], type_ids,
                                        order_cache, route_cache=route_cache, log=log)
    structure_id = hub_config["station_id"]
    if not type_ids:
        log("[TradeSync] stage=underbid_snapshot decision=SKIP "
            f"structure={structure_id} reason=no_active_sell_types")
        return []
    snapshot = route_snapshot("structure_orders", structure_id, auth=auth,
                              slot=slot, cache=route_cache, trigger="underbid_structure")
    log("[TradeSync] stage=underbid_snapshot decision=READY "
        f"source=shared_structure_snapshot structure={structure_id} "
        f"orders={len(snapshot['payload'])}")
    return snapshot["payload"]
