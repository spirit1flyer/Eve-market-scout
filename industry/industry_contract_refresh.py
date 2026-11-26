"""Daily Jita IV-4 blueprint feed, run by the existing Contracts scheduler."""

import threading

from core.config import JITA_REGION_ID, JITA_STATION_ID
from contracts.contracts_engine import BACKGROUND_ITEMS_PER_CYCLE
from industry.industry_contract_prices import get_blueprint_price_store, read_cached_offers
from sde.sde_industry import get_sde_industry_db

_refresh_lock = threading.Lock()


def manufacturing_blueprint_ids(sde=None):
    sde = sde or get_sde_industry_db()
    return {bid for tid in sde.get_all_manufacturable_items()
            if (bid := sde.get_blueprint_for_item(tid)) is not None}


async def refresh_blueprint_contracts(engine, store=None, blueprint_ids=None, guard=None):
    store = store or get_blueprint_price_store()
    guard = _refresh_lock if guard is None else guard
    if not store.due() or not guard.acquire(blocking=False):
        return False
    try:
        if not store.due():
            return False
        ids = blueprint_ids if blueprint_ids is not None else manufacturing_blueprint_ids()
        if not ids:
            return False  # Missing SDE is not a successful zero-offer acquisition.
        excluded = getattr(engine, "_excludes", lambda: set())
        if store.latest() is None:
            rows, checked = read_cached_offers(engine.db, ids, excluded())
            if rows:
                store.save_snapshot(rows, checked, complete=False)
        fresh = engine.db.is_scope_fresh(f"region:{JITA_REGION_ID}")
        print("[BlueprintPrices] daily Jita refresh started; scope=Jita IV-4")
        # Existing engine/transport reuse shared regional lists, immutable item
        # contents, per-route locks, cooldowns and pacing. No separate fetch path.
        result = await engine.sync_region(
            JITA_REGION_ID, item_fetch_locations={JITA_STATION_ID},
            max_items=BACKGROUND_ITEMS_PER_CYCLE)
        list_ok = result.get("list_status") in (200, 304) or (
            result.get("list_status") is None and fresh)
        # The engine's progress count measures attempted work; transient fetch
        # or save failures can leave rows pending despite items_remaining=0.
        pending = engine.pending_station_count(JITA_REGION_ID, JITA_STATION_ID)
        if not result.get("ok") or not list_ok:
            raise RuntimeError(f"incomplete Jita refresh: {result}")
        if pending:
            # A capped pass is expected to leave work on a cold cache. Keep the
            # prior snapshot and daily-success clock; cached item batches survive
            # restart and the next hourly cycle resumes the remaining work.
            store.set_setting("retry_after", store.clock() + 3600)
            print(f"[BlueprintPrices] daily Jita refresh deferred; pending={pending} "
                  f"batch_limit={BACKGROUND_ITEMS_PER_CYCLE}; keeping saved prices")
            return False
        rows, checked = read_cached_offers(engine.db, ids, excluded())
        if checked is None:
            raise RuntimeError("Jita source has no successful validation time")
        store.save_snapshot(rows, checked, complete=True)
        store.set_setting("retry_after", 0)
        print(f"[BlueprintPrices] daily Jita refresh saved offers={len(rows)}")
        return True
    except Exception as exc:
        store.set_setting("retry_after", store.clock() + 3600)
        print(f"[BlueprintPrices] daily refresh failed; keeping saved prices: {exc}")
        return False
    finally:
        guard.release()
