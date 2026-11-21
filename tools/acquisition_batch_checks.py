"""Offline combined acquisition checks for the normal IDE Ctrl+F12 runner."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import json
import threading
from types import SimpleNamespace

import aiohttp
import requests

from core.api import ESIClient
from core.esi_get import ESIGetMixin
from core.esi_lookup_cache import LookupCache
from core.esi_private_cache import PrivateSnapshotError
from core.esi_request_cache import PublicRequestCache
from core.esi_route_cache import RouteSnapshotCache, route_snapshot, validate_route_payload
from core.market_pages import RegionalPageStore
from core.market_snapshot import fetch_market_snapshot
from core.order_cache import OrderCacheStore
from industry.industry_blueprint_cache import PrivateBlueprintCache
from industry.industry_blueprints import IndustryBlueprintsDB, BlueprintPuller
from tools.character_cache_checks import _Roster
from tools.private_cache_checks import _Auth, _require, _rejected


class _Clock:
    def __init__(self):
        self.time = datetime.now(timezone.utc).replace(microsecond=0)

    def now(self):
        return self.time

    def headers(self, seconds=300, page_count=1):
        return {"Expires": format_datetime(self.time + timedelta(seconds=seconds), usegmt=True),
                "X-Pages": str(page_count), "Last-Modified": "synthetic-one", "ETag": "synthetic-etag"}


def _order(page=1):
    return {"order_id": page, "type_id": 34, "location_id": 60003760,
            "is_buy_order": False, "price": 2.0, "volume_remain": 1,
            "volume_total": 2, "duration": 90, "issued": "2026-01-01T00:00:00+00:00"}


class _RegionalClient:
    _fetch_pages_staged = ESIClient._fetch_pages_staged
    get_market_orders = ESIClient.get_market_orders

    def __init__(self, directory, clock, calls=None):
        self._regional_pages = RegionalPageStore(directory, now=clock.now)
        self.clock, self.calls = clock, calls if calls is not None else []
        self.fail = None
        self.invalid = None
        self.zero_duration = False
        self.halted = False

    def _check_halted(self):
        _require(not self.halted, "synthetic client was halted")

    def _set_halted(self, endpoint):
        self.halted = True

    def _apply_earliest_expires(self, expires):
        self.expires = expires

    async def _get_with_headers(self, endpoint, params):
        page = params["page"]
        self.calls.append(page)
        if page == self.fail:
            raise aiohttp.ClientResponseError(SimpleNamespace(real_url="synthetic"), (), status=404)
        headers = self.clock.headers(300 + page, 3)
        if self.invalid == "validator":
            headers.pop("Last-Modified")
        if self.invalid == "mixed" and page == 2:
            headers["Last-Modified"] = "different"
        if self.invalid == "count" and page == 2:
            headers["X-Pages"] = "4"
        payload = [_order(1 if self.invalid == "duplicate" else page)]
        if self.zero_duration and page == 2:
            payload[0]["duration"] = 0
        return payload, headers


def regional_zero_duration(directory):
    clock = _Clock()
    client = _RegionalClient(directory, clock)
    client.zero_duration = True
    orders, expiry, count, metadata = asyncio.run(fetch_market_snapshot(client, 10000043))
    _require(len(orders) == 3 and count == 3 and metadata["complete"] is True,
             "zero-duration row rejected or truncated the regional snapshot")
    _require(orders[1]["duration"] == 0 and [row["order_id"] for row in orders] == [1, 2, 3],
             "zero-duration row was modified or other orders were lost")
    restarted = _RegionalClient(directory, clock)
    restored = asyncio.run(fetch_market_snapshot(restarted, 10000043))
    _require(restored == (orders, expiry, count, metadata) and not restarted.calls,
             "restart lost zero-duration data/metadata or refetched accepted pages")


def order_duration_validation(_directory):
    for resource in ("type_orders", "structure_orders"):
        zero, normal = {**_order(), "duration": 0}, _order(2)
        validate_route_payload(resource, [zero, normal])
        _require(zero["duration"] == 0 and normal["duration"] == 90,
                 "order validation modified the original durations")
        for value in (-1, True, "0", 0.0, None):
            _rejected(lambda: validate_route_payload(resource, [{**zero, "duration": value}]))
        for field in ("order_id", "type_id", "location_id", "volume_total"):
            _rejected(lambda: validate_route_payload(resource, [{**zero, field: 0}]))
        for changes in ({"price": float("nan")}, {"volume_remain": 3}, {"is_buy_order": 1}):
            _rejected(lambda: validate_route_payload(resource, [{**zero, **changes}]))
        _rejected(lambda: validate_route_payload(resource, [zero, zero]))


def regional_page_resume(directory):
    clock = _Clock()
    client = _RegionalClient(directory, clock)
    client.fail = 2
    _rejected(lambda: asyncio.run(fetch_market_snapshot(client, 10000043)))
    _require(client.calls.count(1) == 1 and client.calls.count(3) == 1, "successful pages lost on failure")
    clock.time += timedelta(seconds=10)
    restarted = _RegionalClient(directory, clock, client.calls)
    orders, expiry, count, metadata = asyncio.run(fetch_market_snapshot(restarted, 10000043))
    _require(len(orders) == 3 and count == 3 and restarted.calls.count(1) == 1
             and restarted.calls.count(3) == 1 and restarted.calls.count(2) == 2,
             "restart refetched successful unexpired regional pages")
    _require(expiry == clock.time + timedelta(seconds=302), "regional expiry ignored a later page")
    before = len(restarted.calls)
    asyncio.run(fetch_market_snapshot(restarted, 10000043))
    _require(len(restarted.calls) == before, "manual repeat refetched pages before expiry")
    _require(metadata["page_metadata"][0]["acquired_at"] != metadata["acquired_at"].isoformat(),
             "recovered page acquisition time was reset")


def regional_page_validation(directory):
    for case in ("validator", "mixed", "count", "duplicate"):
        clock = _Clock()
        client = _RegionalClient(directory / case, clock)
        client.invalid = case
        _rejected(lambda: asyncio.run(fetch_market_snapshot(client, 10000043)))
        before = len(client.calls)
        _rejected(lambda: asyncio.run(fetch_market_snapshot(client, 10000043)))
        _require(len(client.calls) == before, "invalid representation retried successful pages before expiry")


class _Orders(OrderCacheStore):
    def __init__(self, directory):
        self.directory = directory
        self._order_cache = {}
        self._inflight_locks = {}
        self._inflight_locks_meta = threading.Lock()
        self.fail_write = False

    def _diagnostic(self, *args, **kwargs):
        pass

    def _disk_cache_path(self, region):
        return self.directory / f"region_{region}.json.gz"

    def _write_snapshot(self, path, payload):
        if self.fail_write:
            raise OSError("synthetic disk replacement failure")
        super()._write_snapshot(path, payload)


def regional_atomic_persistence(directory):
    store = _Orders(directory)
    now = datetime.now(timezone.utc)
    store.cache_orders_for_region(10000043, [_order()], now + timedelta(seconds=900),
                                  acquired_at=now-timedelta(seconds=400), last_modified="synthetic")
    path = store._disk_cache_path(10000043)
    before = path.read_bytes()
    store.fail_write = True
    store.cache_orders_for_region(10000043, [_order(2)], now + timedelta(seconds=900))
    _require(path.read_bytes() == before and not store._order_cache[10000043]["durable"],
             "failed regional save destroyed old disk or claimed durability")
    store.fail_write = False
    cached = store.get_cached_orders(10000043)
    _require(cached[0]["order_id"] == 2 and store._order_cache[10000043]["durable"],
             "regional memory/save retry lost accepted book")
    client = _RegionalClient(directory / "pages", _Clock())
    client.order_cache = store
    _require(asyncio.run(client.get_market_orders(10000043, force_refresh=True)) == cached
             and not client.calls, "forced regional refresh bypassed ESI expiry")
    restored = _Orders(directory)
    restored._load_region_disk_cache(10000043)
    _require(restored._order_cache[10000043]["timestamp"] == store._order_cache[10000043]["timestamp"],
             "regional disk restore reset acquisition age")


class _BlueprintServer(_Clock):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.pages = 1
        self.bad = False
        self.empty = False
        self.after_response = None

    def request(self, method, url, headers, params, timeout):
        page = params.get("page", 1)
        self.calls.append(page)
        rows = [] if self.empty else [{"item_id": page, "type_id": 100,
            "location_id": 60003760, "location_flag": "Hangar", "quantity": -2,
            "runs": 10, "material_efficiency": 0, "time_efficiency": 0}]
        if self.bad:
            rows[0]["material_efficiency"] = "invalid"
        if self.after_response:
            callback, self.after_response = self.after_response, None
            callback()
        return SimpleNamespace(status_code=200, headers=self.headers(600, self.pages),
                               json=lambda: rows, raise_for_status=lambda: None)


class _BlueprintStore(PrivateBlueprintCache):
    def _event(self, *args, **kwargs):
        pass


def blueprint_roundtrip(directory):
    server, auth = _BlueprintServer(), _Auth()
    store = _BlueprintStore(directory, request=server.request, now=server.now)
    db = IndustryBlueprintsDB(directory / "blueprints.db")
    try:
        puller = BlueprintPuller(_Roster(auth), db, store)
        _require(puller.pull(101)[0], "blueprint initial pull failed")
        original = db.get_pull_meta(101)
        server.time += timedelta(seconds=700)
        restored = _BlueprintStore(directory, request=server.request, now=server.now)
        _require(BlueprintPuller(_Roster(auth), db, restored).pull(101)[0]
                 and server.calls == [1] and db.get_pull_meta(101) == original,
                 "blueprint restart reset age or ignored existing one-hour preference")
        server.time += timedelta(seconds=3000)
        server.empty = True
        _require(puller.pull(101)[0] and db.get_blueprints(101) == [], "valid empty blueprints were treated as failure")
    finally:
        db._conn().close()


def blueprint_database_failure(directory):
    server, auth = _BlueprintServer(), _Auth()
    store = _BlueprintStore(directory, request=server.request, now=server.now)
    db = IndustryBlueprintsDB(directory / "blueprints.db")
    try:
        puller = BlueprintPuller(_Roster(auth), db, store)
        _require(puller.pull(101)[0], "initial blueprint seed failed")
        original = db.get_blueprints(101), db.get_pull_meta(101)
        server.time += timedelta(seconds=3601)
        server.bad = True
        _require(not puller.pull(101)[0] and (db.get_blueprints(101), db.get_pull_meta(101)) == original,
                 "invalid blueprint rows overwrote database")
        server.bad = False
        db._conn().execute("CREATE TRIGGER deny_bp BEFORE INSERT ON blueprints BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END")
        _require(not puller.pull(101)[0] and (db.get_blueprints(101), db.get_pull_meta(101)) == original,
                 "database failure claimed success or failed to roll back deletion")
        _require(puller.get_cache_status(101)[0], "blueprint DB failure blocked persistence retry in UI")
        before = len(server.calls)
        db._conn().execute("DROP TRIGGER deny_bp")
        _require(puller.pull(101)[0] and len(server.calls) == before, "database retry reacquired an unexpired raw snapshot")
    finally:
        db._conn().close()


def blueprint_context_concurrency(directory):
    server, auth = _BlueprintServer(), _Auth()
    store = _BlueprintStore(directory, request=server.request, now=server.now)
    db = IndustryBlueprintsDB(directory / "blueprints.db")
    puller = BlueprintPuller(_Roster(auth), db, store)
    def pull():
        try:
            return puller.pull(101)
        finally:
            db._conn().close()
            db._local.conn = None
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(pull) for _ in range(2)]
            _require(all(future.result()[0] for future in futures) and server.calls == [1],
                     "simultaneous blueprint pulls duplicated network or database work")
        auth.owner = "new-owner"
        auth.rotate()
        _require(puller.get_cache_status(101)[0] and puller.pull(101)[0] and len(server.calls) == 2,
                 "blueprint context change inherited previous refresh window")
        original = db.get_blueprints(101), db.get_pull_meta(101)
        server.time += timedelta(seconds=3601)
        server.after_response = lambda: auth.rotate(character=202)
        _require(not puller.pull(101)[0] and (db.get_blueprints(101), db.get_pull_meta(101)) == original,
                 "blueprint authorization race committed data")
    finally:
        db._conn().close()


class _LookupServer(_Clock):
    def __init__(self):
        super().__init__()
        self.calls = []

    def request(self, method, url, json, headers, timeout):
        kind = "names" if url.endswith("names/") else "ids"
        self.calls.append((kind, tuple(json)))
        bad = 999 if kind == "names" else "invalid"
        status = 404 if bad in json else 200
        body = ([{"id": value, "name": f"Name {value}"} for value in json]
                if kind == "names" else {"inventory_types": [{"id": 34, "name": value} for value in json]})
        return SimpleNamespace(status_code=status, headers={}, json=lambda: body)


def lookup_isolation_restart(directory):
    server = _LookupServer()
    store = LookupCache(directory, request=server.request, now=server.now)
    _require(store.names([1, 999, 2]) == {1: "Name 1", 2: "Name 2"}, "one bad ID poisoned valid names")
    before = len(server.calls)
    restored = LookupCache(directory, request=server.request, now=server.now)
    _require(restored.names([1, 2, 999]) == {1: "Name 1", 2: "Name 2"} and len(server.calls) == before,
             "positive/negative name restart cache missed")
    resolved = store.ids(["Tritanium", "invalid"])
    _require("tritanium" in resolved and "invalid" not in resolved, "name/ID failure isolation failed")
    before = len(server.calls)
    store.ids(["TRITANIUM", "invalid"])
    _require(len(server.calls) == before, "ID cache normalization or negative reuse failed")
    server.time += timedelta(seconds=301)
    restored.names([1, 2, 999])
    _require(server.calls[-1] == ("names", (999,)), "negative expiry refetched good names")


def lookup_concurrent(directory):
    server = _LookupServer()
    store = LookupCache(directory, request=server.request, now=server.now)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.names, [1, 2]) for _ in range(2)]
        _require(all(future.result() == {1: "Name 1", 2: "Name 2"} for future in futures)
                 and len(server.calls) == 1, "overlapping name consumers duplicated POST")


def contract_singleflight(directory):
    clock = _Clock()
    store = PublicRequestCache(directory, now=clock.now)
    calls = []
    async def producer():
        calls.append(1)
        await asyncio.sleep(0)
        return 200, clock.headers(120), [{"contract_id": 1}]
    def fetch(cache):
        return asyncio.run(cache.get_reply("/contracts/public/10000043/", {"page": 1}, producer))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(fetch, store) for _ in range(2)]
        _require(all(future.result()[2] == [{"contract_id": 1}] for future in futures) and len(calls) == 1,
                 "contract callers on different loops duplicated requests")
    _require(fetch(PublicRequestCache(directory, now=clock.now))[0] == 200 and len(calls) == 1,
             "contract page restart ignored unexpired persisted response")


def contract_failure_validation(directory):
    clock = _Clock()
    store = PublicRequestCache(directory, now=clock.now)
    calls = []
    async def request(status, body):
        calls.append(status)
        return status, clock.headers(120), body
    def fetch(status, body):
        return asyncio.run(store.get_reply("/contracts/public/1/", {}, lambda: request(status, body), "synthetic-etag"))
    fetch(200, [{"contract_id": 1}])
    clock.time += timedelta(seconds=121)
    _require(fetch(304, None)[2] == [{"contract_id": 1}], "conditional refresh lost full contract payload")
    original = next(iter(store.entries.values()))["acquired_at"]
    clock.time += timedelta(seconds=121)
    _require(fetch(200, {"malformed": True})[0] == 502, "malformed contract page accepted")
    before = len(calls)
    _require(fetch(200, [])[0] == 502 and len(calls) == before,
             "failed contract reply was reported as good or immediately refetched")
    row = next(iter(store.entries.values()))
    _require(row["acquired_at"] == original and row["body"] == [{"contract_id": 1}],
             "contract failure destroyed last good payload or reset success age")
    conditional = PublicRequestCache(directory / "conditional", now=clock.now)
    asyncio.run(conditional.get_reply("/contracts/public/2/", {}, lambda: request(304, None), "one"))
    before = len(calls)
    other = asyncio.run(conditional.get_reply("/contracts/public/2/", {}, lambda: request(200, []), "two"))
    _require(other[0] == 503 and len(calls) == before, "different conditional caller received false 304 or early request")


def metadata_route_sharing(directory):
    clock = _Clock()
    calls = []
    def request(method, url, headers, params, timeout):
        calls.append(url)
        status = 404 if "/30099999/" in url else 200
        if "/route/" in url:
            payload = [30000142, 30002187]
        elif url.endswith("/search/"):
            payload = {"structure": [1000000000001]}
        elif url.endswith("/regions/"):
            payload = [10000002]
        elif "/stargates/" in url:
            payload = {"name": "Gate", "system_id": 30000142, "destination": {"system_id": 30002187}}
        else:
            payload = {"name": "Synthetic"}
        response = SimpleNamespace(status_code=status, headers=clock.headers(), json=lambda: payload)
        def raise_for_status():
            if status == 404:
                raise requests.HTTPError("synthetic route not found", response=response)
        response.raise_for_status = raise_for_status
        return response
    class Store(RouteSnapshotCache):
        def _event(self, *args, **kwargs):
            pass
    class Client(ESIGetMixin):
        def _check_halted(self):
            pass
    store = Store(directory, request=request, now=clock.now)
    client = Client()
    client._public_route_cache = store
    for endpoint in ("/universe/regions/", "/universe/regions/10000002/",
                     "/universe/types/34/", "/universe/stargates/50000001/"):
        asyncio.run(client._get(endpoint))
        asyncio.run(client._get(endpoint))
    route = route_snapshot("route", 30000142, params={"destination": 30002187}, cache=store)
    route_snapshot("route", 30000142, params={"destination": 30002187},
                   cache=Store(directory, request=request, now=clock.now))
    _require(len(calls) == 5 and len(route["payload"]) - 1 == 1, "metadata/route reuse or endpoint dispatch failed")
    def missing_route(cache, expected):
        try:
            route_snapshot("route", 30000142, params={"destination": 30099999}, cache=cache)
        except expected as exc:
            if isinstance(exc, requests.HTTPError):
                _require(exc.response.status_code == 404, "missing route returned a different HTTP error")
            else:
                _require(str(exc) == "cached_public_not_found", "missing route returned a different cache error")
        else:
            raise AssertionError("missing route was accepted")
    missing_route(store, requests.HTTPError)
    missing_route(store, PrivateSnapshotError)
    missing_route(Store(directory, request=request, now=clock.now), PrivateSnapshotError)
    _require(len(calls) == 6, "public route negative cache was not reused across restart")
    auth = _Auth()
    for _ in range(2):
        route_snapshot("structure_search", 101, auth=auth,
            params={"categories": "structure", "search": "Synthetic", "strict": "false"}, cache=store)
    _require(len(calls) == 7, "private structure search repeated its unexpired response")
