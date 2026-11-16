"""Synthetic ESI audit regressions for the IDE's Ctrl+F12 runner.

Only fixture-local gates, sessions and temporary caches are used. No live
client, transport, credentials, cache or process-wide cooldown is touched.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from contracts.contracts_esi import _raw_request_network
from gui.tracking.gui_tracking_market import fetch_market_orders_sync
from tools.acquisition_batch_checks import _Orders, _order
from tools.route_cache_checks import _Server, _Store


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


class _Response:
    status = 200

    def __init__(self):
        self.headers = {"ETag": "synthetic-contract"}
        self.payload = [{"contract_id": 1}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []
        self.response = _Response()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class _Client:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(1)
        self.gate = asyncio.Event()
        self.gate.set()
        self.outer_passed = asyncio.Event()
        self.inner_entered = asyncio.Event()
        self.gate_calls = 0
        self.session = _Session()
        self.recorded = []

    def ensure_session(self):
        return self.session

    async def _await_rate_limit_pause(self):
        self.gate_calls += 1
        if self.gate_calls > 1:
            self.inner_entered.set()
        await self.gate.wait()
        if self.gate_calls == 1:
            self.outer_passed.set()

    def _record_response_headers(self, response, endpoint):
        self.recorded.append((response, endpoint))


@asynccontextmanager
async def _queued_during_cooldown():
    client = _Client()
    await client.semaphore.acquire()
    slot_held = True
    request = asyncio.create_task(_raw_request_network(
        client, "GET", "/contracts/public/1/", params={"page": 1},
        extra_headers={"If-None-Match": "synthetic-old"}))
    probe = None
    try:
        # The request signals its outer pass and then queues at the held slot
        # before yielding control back to this fixture.
        await asyncio.wait_for(client.outer_passed.wait(), timeout=3)
        _require(not request.done() and not client.session.calls,
                 "request did not queue behind the occupied semaphore")
        client.gate.clear()
        client.semaphore.release()
        slot_held = False
        probe = asyncio.create_task(client.inner_entered.wait())
        done, _ = await asyncio.wait(
            (request, probe), timeout=3, return_when=asyncio.FIRST_COMPLETED)
        # Without the inner check, the real request completes and fails here
        # immediately; the timeout is only a deadlock watchdog.
        _require(request not in done and not request.done(),
                 "queued contract dispatched during the new cooldown")
        _require(probe in done and client.inner_entered.is_set(),
                 "queued contract never reached its dispatch gate")
        _require(client.gate_calls == 2 and not client.gate.is_set()
                 and not client.session.calls and not client.recorded,
                 "blocked dispatch sent a request or recorded a response")
        yield client, request
    finally:
        if slot_held:
            client.semaphore.release()
        tasks = [task for task in (request, probe) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def contract_queued_cooldown(directory):
    async def scenario():
        async with _queued_during_cooldown() as (client, request):
            client.gate.set()
            result = await asyncio.wait_for(request, timeout=3)
            response = client.session.response
            _require(result == (200, response.headers, response.payload),
                     "gate release changed successful status, headers or payload")
            _require(len(client.session.calls) == 1,
                     "gate release did not send exactly one request")
            method, url, kwargs = client.session.calls[0]
            _require(method == "GET" and url.endswith("/contracts/public/1/")
                     and kwargs == {"params": {"page": 1}, "json": None,
                                    "headers": {"If-None-Match": "synthetic-old"}},
                     "dispatch changed request arguments or conditional headers")
            _require(client.recorded == [(response, "/contracts/public/1/")],
                     "successful response was not recorded exactly once")

    asyncio.run(scenario())


def contract_cancelled_cooldown(directory):
    async def scenario():
        async with _queued_during_cooldown() as (client, request):
            request.cancel()
            try:
                await request
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cooldown cancellation did not propagate")
            _require(not client.session.calls and not client.recorded,
                     "cancelled contract dispatched or recorded a response")

            async def acquire_slot():
                async with client.semaphore:
                    return True

            follower = asyncio.create_task(acquire_slot())
            try:
                _require(await asyncio.wait_for(follower, timeout=3),
                         "cancelled contract retained its semaphore slot")
            finally:
                if not follower.done():
                    follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)

    asyncio.run(scenario())


class _UnderbidServer(_Server):
    def __init__(self, *, allow_acquisition=False):
        super().__init__()
        self.allow_acquisition = allow_acquisition
        self.attempts = []

    def request(self, method, url, headers, params, timeout):
        self.attempts.append((method, url, dict(params)))
        _require(self.allow_acquisition, "regional reuse attempted route acquisition")
        _require(params.get("order_type") == "sell", "fallback changed order side")
        return super().request(method, url, headers, params, timeout)


def _regional_times(store, region):
    entry = store.get_snapshot_entry(region)
    return None if entry is None else (entry["timestamp"], entry["expires"])


def underbid_regional_expiry_reuse(directory):
    region = 10000002
    now = datetime.now(timezone.utc)
    acquired = now - timedelta(seconds=301)
    expiry = acquired + timedelta(seconds=600)
    rows = [_order(), {**_order(2), "is_buy_order": True},
            {**_order(3), "type_id": 35}, {**_order(4), "location_id": 60008494}]
    original = deepcopy(rows)
    orders = _Orders(directory / "populated")
    orders.cache_orders_for_region(region, rows, expiry, acquired_at=acquired)
    server = _UnderbidServer()
    routes = _Store(directory / "routes", server)
    logs = []

    def fetch(store):
        result = fetch_market_orders_sync(region, [34], store,
                                         route_cache=routes, log=logs.append)
        _require(result == original and not server.attempts,
                 "unexpired older regional book was filtered, changed or reacquired")
        _require(_regional_times(store, region) == (acquired, expiry),
                 "regional read reset acquisition or expiry")
        return result

    _require(fetch(orders) is rows and rows == original,
             "Tracking replaced or mutated shared regional rows")
    restored = _Orders(directory / "populated")
    restored._load_region_disk_cache(region)
    _require(fetch(restored) is restored.get_snapshot_entry(region)["orders"],
             "Tracking did not return the restored full regional book")
    _require(all("stage=underbid_snapshot decision=REUSE" in line
                 and "source=shared_region_cache" in line
                 and f"region={region}" in line and "orders=4" in line
                 for line in logs) and len(logs) == 2,
             "regional reuse diagnostic changed")

    empty = _Orders(directory / "empty")
    empty.cache_orders_for_region(region, [], expiry, acquired_at=acquired)
    _require(fetch_market_orders_sync(region, [34], empty,
                 route_cache=routes, log=lambda line: None) == [] and not server.attempts,
             "valid empty unexpired regional book triggered acquisition")
    _require(_regional_times(empty, region) == (acquired, expiry),
             "empty regional read reset acquisition or expiry")


def underbid_regional_fallback(directory):
    region = 10000002
    now = datetime.now(timezone.utc)
    cases = (("expired", 60, -30, True, [34], True),
             ("missing", None, None, True, [34], True),
             ("incomplete", 60, 600, False, [34], True),
             ("no_expiry_recent", 60, None, True, [34], False),
             ("no_expiry_old", 600, None, True, [34], True),
             ("no_types", None, None, True, [], False),
             ("duplicate_types", None, None, True, [35, 34, 35, 34], True))
    for name, age, expires_in, complete, types, fallback in cases:
        case = directory / name
        orders = _Orders(case / "regional")
        regional_rows = [_order(7)]
        if age is not None:
            expiry = None if expires_in is None else now + timedelta(seconds=expires_in)
            orders.cache_orders_for_region(region, regional_rows, expiry,
                acquired_at=now - timedelta(seconds=age), complete=complete)
        before = deepcopy(orders.get_snapshot_entry(region))
        server = _UnderbidServer(allow_acquisition=fallback)
        routes = _Store(case / "routes", server)
        expected_types = sorted(set(types)) if fallback else []
        expected = ([{**_order(101), "type_id": type_id, "issued": server.time.isoformat()}
                     for type_id in expected_types] if fallback else
                    regional_rows if age is not None else [])
        for _ in range(2):
            result = fetch_market_orders_sync(region, types, orders,
                                              route_cache=routes, log=lambda line: None)
            _require(result == expected, f"{name}: wrong regional/fallback result")
            _require(len(server.attempts) == len(expected_types)
                     and [call[3] for call in server.calls] == expected_types,
                     f"{name}: wrong unique/sorted acquisition or route-cache reuse")
            _require(all(call[:3] == ("type_orders", region, 1) for call in server.calls),
                     f"{name}: fallback used a different route")
            _require(orders.get_snapshot_entry(region) == before,
                     f"{name}: reading regional data changed its rows or metadata")
