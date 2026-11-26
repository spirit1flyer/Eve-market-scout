"""Synthetic Industry pricing checks for the IDE's existing Ctrl+F12 runner.

Every database, clock, settings file, engine and refresh lock is fixture-local.
No live clients, production caches, credentials or global pricing store used.
"""

import asyncio
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from core.config import JITA_REGION_ID, JITA_STATION_ID
from contracts.contracts_db import ContractsDB
from contracts.contracts_engine import ContractsEngine
from contracts.contracts_scheduler import ITEMS_PER_CYCLE
from industry.industry_contract_prices import BlueprintPriceStore, DAY, read_cached_offers
from industry.industry_contract_refresh import refresh_blueprint_contracts
from industry.industry_blueprint_pricing import BpcPricing
from industry.industry_engine import IndustryCalculator, Recipe, SellInfo, FacilityParams, SellFees


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _store(directory):
    clock = [2_000_000_000.0]
    return BlueprintPriceStore(directory / "prices.db", clock=lambda: clock[0]), clock


def _offer(cid=1, price=50, me=0, runs=10, **extra):
    row = dict(contract_id=cid, blueprint_id=1001, is_copy=1, price=price,
               quantity=1, runs=runs, me=me, te=20, location_id=JITA_STATION_ID,
               region_id=JITA_REGION_ID, date_expired=_iso(2_000_000_000 + 400 * DAY))
    row.update(extra)
    return row


def _pricing(directory, store):
    return BpcPricing(store, settings_file=directory / "legacy.json")


class _Data:
    price = 10

    def recipe(self, tid):
        return Recipe(2, [(34, 100)]) if tid == 100 else None

    def input_price(self, tid):
        return self.price

    def adjusted_price(self, tid):
        return 0

    def sell_info(self, tid):
        return SellInfo(avg_7d=2000, volume=10, history_days=30)


def _calc():
    return IndustryCalculator(_Data(), buildable=lambda tid: False)


def _build(pricing, calc=None, batch=3, has_bpo=False):
    return pricing.cost_build(calc or _calc(), FacilityParams(facility_tax_pct=0),
                              SellFees(), 100, 1001, has_bpo,
                              dict(me=10, te=18, batch=batch), pricing.view())


def blueprint_override_restart(directory):
    store, clock = _store(directory)
    legacy = directory / "legacy.json"
    legacy.write_text(json.dumps({"bpc_prices": {"1001": {"price": 700, "runs": 10}},
                                  "unrelated": "preserved"}), encoding="utf-8")
    pricing = _pricing(directory, store)
    _require(pricing.resolve(1001)["price"] == 700, "legacy override lost")
    store.save_snapshot([_offer()], clock[0], complete=True)
    _require(pricing.resolve(1001)["source"] == "write-in", "refresh overwrote override")
    pricing.set_writein(1001, 300, 5, me=7, te=14)
    restored = _pricing(directory, BlueprintPriceStore(store.path, clock=lambda: clock[0]))
    _require(restored.resolve(1001)["me"] == 7, "override research did not persist")
    restored.set_writein(1001, 350, 5)
    _require(restored.resolve(1001)["me"] == 7 and restored.resolve(1001)["te"] == 14,
             "legacy price-only editor cleared saved research")
    restored.set_writein(1001, 0, 0)
    restored = _pricing(directory, BlueprintPriceStore(store.path, clock=lambda: clock[0]))
    _require(restored.get_writein(1001) is None, "cleared legacy override resurrected")
    restored.set_writein(1001, 9000, None, is_copy=False)
    original = restored.resolve(1001, is_copy=False)
    _require(original["price"] == 9000 and original["per_run"] == 0,
             "original override treated as a copy charge")
    _require(json.loads(legacy.read_text())["unrelated"] == "preserved", "legacy settings rewritten")


def blueprint_material_aware(directory):
    store, clock = _store(directory)
    pricing = _pricing(directory, store)
    store.save_snapshot([_offer(1, 50, me=0), _offer(2, 500, me=10)], clock[0], True)
    result = _build(pricing)
    _require(result["_blueprint"]["contract_id"] == 2, "ignored material savings")
    _require(result["me"] == 10 and result["te"] == 20, "winning ME/TE not applied")
    _require(result["blueprint_cost"] == 150 and result["total_build"] == 2850
             and result["unit_cost"] == 475, "per-run/batch/output allocation incorrect")
    pricing.set_writein(1001, 100, 10, me=0, te=12)
    result = _build(pricing)
    _require(result["me"] == 0 and result["te"] == 12
             and result["total_build"] == 3030, "manual override did not dominate")


def blueprint_fallback_capacity(directory):
    store, clock = _store(directory)
    pricing = _pricing(directory, store)
    store.save_snapshot([_offer(1, 50, me=None), _offer(2, 500, me=10),
                         _offer(3, 1, runs=None)], clock[0], True)
    result = _build(pricing)
    _require(result["_blueprint"]["contract_id"] == 1
             and result["_blueprint"]["comparison"] == "ISK/run", "missing ME fallback incorrect")
    result = _build(pricing, batch=11)
    _require(result["patient_profit"] is None, "insufficient capacity produced a profit")
    pricing.set_writein(1001, 20, 2)
    result = _build(pricing)
    _require(result["_blueprint"]["source"] == "write-in"
             and result["patient_profit"] is None, "insufficient override silently bypassed")
    pricing.set_writein(1001, 0, 0)
    store.save_snapshot([_offer(4, 2000, is_copy=0, quantity=2, runs=-1)], clock[0], True)
    result = _build(pricing, has_bpo=True)
    _require(result["_blueprint"]["price"] == 1000 and result["blueprint_cost"] == 0,
             "stacked originals priced incorrectly or charged to manufacturing")


def blueprint_history_retention(directory):
    store, clock = _store(directory)
    _require(store.retention() == 30, "default retention changed")
    first = store.save_snapshot([_offer()], clock[0], True)
    clock[0] += DAY
    store.save_snapshot([], clock[0], True)
    _require(not store.offers(), "vanished offer resurrected in latest estimates")
    with store.connect() as conn:
        _require(conn.execute("SELECT COUNT(*) FROM offers WHERE snapshot_id=?", (first,)).fetchone()[0] == 1,
                 "vanished offer history lost")
    store.set_retention(None)
    clock[0] += 100 * DAY
    store.prune()
    with store.connect() as conn:
        _require(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 2, "forever pruned history")
    try:
        store.set_retention(6)
    except ValueError:
        pass
    else:
        raise AssertionError("retention accepted fewer than seven days")
    store.set_retention(7)
    with store.connect() as conn:
        _require(conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1, "retention cleanup failed")
    store.save_snapshot([_offer(date_expired=_iso(clock[0] - 1))], clock[0], True)
    _require(not store.offers(), "expired offer used")


@contextmanager
def _contracts(directory):
    db = ContractsDB(directory / "contracts.db")
    expiry = _iso(2_000_000_000 + 400 * DAY)
    for cid in range(1, 7):
        db.upsert_list_rows(JITA_REGION_ID, [dict(
            contract_id=cid, type="auction" if cid == 4 else "item_exchange", price=50,
            date_expired=expiry, start_location_id=60000000 if cid == 5 else JITA_STATION_ID)])
        item = dict(record_id=1, type_id=1001, quantity=1, runs=10,
                    material_efficiency=10, time_efficiency=20, is_blueprint_copy=True,
                    is_included=True)
        items = [item]
        if cid in (2, 3):
            items.append(dict(item, record_id=2, type_id=1002, is_included=cid == 2))
        if cid == 6:
            item["type_id"] = 888  # A non-blueprint item.
        db.store_items_batch({cid: items})
    db.set_scope_freshness(f"region:{JITA_REGION_ID}", JITA_REGION_ID,
                           expires=expiry, etag="fixture", last_status=200)
    try:
        yield db
    finally:
        db._conn().close()


def blueprint_contract_scope(directory):
    with _contracts(directory) as db:
        rows, checked = read_cached_offers(db, {1001, 1002})
        _require([r["contract_id"] for r in rows] == [1],
                 "mixed bundle, requested goods, auction, wrong station or non-blueprint accepted")
        _require(checked is not None and rows[0]["me"] == 10, "research/source time lost")
        _require(not read_cached_offers(db, {1001, 1002}, {1})[0], "excluded contract priced")


def blueprint_daily_restart(directory):
    store, clock = _store(directory)
    with _contracts(directory) as db:
        class Engine:
            calls = 0
            status = 200
            pending = 0
            backlog = 0
            last_batch = 0
            def __init__(self):
                self.db = db
            async def sync_region(self, region_id, item_fetch_locations, max_items):
                _require(region_id == JITA_REGION_ID and item_fetch_locations == {JITA_STATION_ID},
                         "daily job broadened its scope")
                _require(max_items == ITEMS_PER_CYCLE, "daily job bypassed the background batch cap")
                self.calls += 1
                self.last_batch = min(self.backlog, max_items) if self.status == 200 else 0
                self.backlog -= self.last_batch
                return dict(ok=True, list_status=self.status, items_remaining=0)
            def pending_station_count(self, *args):
                return self.pending + self.backlog
        engine = Engine()
        def refresh(target):
            return asyncio.run(refresh_blueprint_contracts(engine, target, {1001}, threading.Lock()))
        _require(refresh(store), "initial daily refresh failed")
        restored = BlueprintPriceStore(store.path, clock=lambda: clock[0])
        _require(not refresh(restored) and engine.calls == 1, "restart repeated daily acquisition")
        good = restored.latest()
        clock[0] += DAY
        engine.status = 500
        _require(not refresh(restored) and restored.latest() == good, "failure replaced saved prices")
        clock[0] += 3601
        engine.status, engine.pending = 200, 1
        _require(not refresh(restored) and restored.latest() == good,
                 "attempted-but-unsaved items advanced daily clock")
        clock[0] += 3601
        engine.pending = 0
        engine.backlog = ITEMS_PER_CYCLE + 1
        _require(not refresh(restored) and restored.latest() == good
                 and engine.backlog == 1 and engine.last_batch == ITEMS_PER_CYCLE,
                 "capped backlog replaced saved prices or drained without a limit")
        restored = BlueprintPriceStore(store.path, clock=lambda: clock[0])
        calls = engine.calls
        _require(not refresh(restored) and engine.calls == calls,
                 "restart bypassed the deferred batch retry time")
        clock[0] += 3601
        _require(refresh(restored) and restored.latest()["id"] != good["id"], "retry never completed")
        _require(engine.last_batch == 1 and not engine.backlog and not restored.due(),
                 "resume did not finish the backlog and advance the daily clock")


def blueprint_snapshot_save_failure(directory):
    store, clock = _store(directory)
    store.save_snapshot([_offer()], clock[0], True)
    good = store.latest()
    store.set_override(1001, 200, 10)
    with store.connect() as conn:
        conn.execute("CREATE TRIGGER reject_snapshot BEFORE INSERT ON snapshots "
                     "BEGIN SELECT RAISE(ABORT,'synthetic write failure'); END")
    clock[0] += DAY
    try:
        store.save_snapshot([_offer(2)], clock[0], True)
    except sqlite3.Error:
        pass
    else:
        raise AssertionError("snapshot write failure was swallowed")
    _require(store.latest() == good and store.due() and len(store.overrides()) == 1,
             "failed snapshot lost prior state or advanced refresh clock")


def blueprint_contract_list_failure(directory):
    with _contracts(directory) as db:
        db.set_scope_freshness(f"region:{JITA_REGION_ID}", JITA_REGION_ID,
                               expires="2000-01-01T00:00:00Z", etag="old", last_status=200)
        old = db.get_scope_freshness(f"region:{JITA_REGION_ID}")
        db.upsert_list_rows = lambda *args: 0
        async def response(*args, **kwargs):
            return dict(status=200, contracts=[dict(contract_id=20)],
                        expires="2099-01-01T00:00:00Z", etag="new")
        engine = ContractsEngine(lambda: object(), db=db, fetch_region_list=response)
        result = asyncio.run(engine.sync_region(JITA_REGION_ID, item_fetch_locations=set()))
        _require(not result["ok"] and result["reason"] == "list_save_failed", "list save failure accepted")
        _require(db.get_scope_freshness(f"region:{JITA_REGION_ID}") == old
                 and 1 in db.get_contract_ids_for_region(JITA_REGION_ID),
                 "failed list save pruned cache or advanced freshness")
