"""Synthetic structure/direct-route acceptance scenarios for Ctrl+F12.

Injected transport, clock, identities and temporary files only. No live auth,
global cache/transport patches, requests, or production diagnostic counters.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import threading
from types import SimpleNamespace

import requests

from core.esi_character_cache import RosterAuth
from core.esi_private_cache import private_context
from core.esi_route_cache import RouteSnapshotCache, route_snapshot
from esi.esi_structures import fetch_structure_info, fetch_structure_orders, StructureAccessError
from industry.industry_blueprints import fetch_structure_meta
from history.structure_history import StructureHistoryDB
from gui.tracking.gui_tracking_market import fetch_underbid_orders_sync
from tools.character_cache_checks import _Roster
from tools.private_cache_checks import _Auth, _require, _rejected


STRUCTURE = 1000000000001


class _Server:
    def __init__(self):
        self.time = datetime.now(timezone.utc).replace(microsecond=0)
        self.calls = []
        self.pages = 1
        self.version = 1
        self.fail_page = None
        self.validator = True
        self.mixed = False
        self.changed_count = False
        self.invalid = False
        self.empty = False
        self.block = None
        self.after_response = None

    def now(self):
        return self.time

    def request(self, method, url, headers, params, timeout):
        _require("resource_id" not in params, "path identity leaked into wire query")
        parts = url.rstrip("/").split("/")
        resource = ("structure_orders" if "/markets/structures/" in url else
                    "structure_info" if "/universe/structures/" in url else
                    "type_orders" if "/markets/" in url else
                    {"stations": "station_info", "systems": "system_info",
                     "constellations": "constellation_info"}[parts[-2]])
        identity = int(parts[-2] if resource == "type_orders" else parts[-1])
        page = params.get("page", 1)
        self.calls.append((resource, identity, page, params.get("type_id")))
        if self.block:
            entered, release = self.block
            entered.set()
            _require(release.wait(5), "synthetic blocked request timed out")
        status = 404 if self.fail_page == page else 200
        if resource in ("structure_orders", "type_orders"):
            payload = [] if self.empty else [{
                "order_id": self.version * 100 + page, "type_id": params.get("type_id", 34),
                "location_id": identity if resource == "structure_orders" else 60003760,
                "is_buy_order": False, "price": float("nan") if self.invalid else 2.0,
                "volume_remain": 1, "volume_total": 2, "duration": 90,
                "issued": self.time.isoformat()}]
        else:
            payload = {"name": "Synthetic metadata", "solar_system_id": 30000142,
                       "system_id": 30000142, "type_id": 35832, "owner_id": 1000001,
                       "station_id": identity, "owner": 1000001,
                       "constellation_id": 20000020, "region_id": 10000002}
        count = self.pages if resource in ("structure_orders", "type_orders") else 1
        response_headers = {
            "Expires": format_datetime(self.time + timedelta(seconds=300 + page), usegmt=True),
            "X-Pages": str(count + (1 if self.changed_count and page == 2 else 0)),
            "ETag": f"synthetic-{self.version}-{page}",
        }
        if self.validator:
            response_headers["Last-Modified"] = f"synthetic-{self.version}" + (
                "-mixed" if self.mixed and page == 2 else "")
        response = SimpleNamespace(status_code=status, headers=response_headers,
                                   json=lambda: payload)

        def raise_for_status():
            if status >= 400:
                raise requests.HTTPError("synthetic failed page", response=response)
        response.raise_for_status = raise_for_status
        if self.after_response:
            callback, self.after_response = self.after_response, None
            callback()
        return response


class _Store(RouteSnapshotCache):
    def __init__(self, directory, server):
        super().__init__(directory, request=server.request, now=server.now)
        self.fail_save = False
        self.waiting = threading.Event()

    def _event(self, context, resource, action, trigger, entry=None, **fields):
        if action == "WAIT":
            self.waiting.set()

    def _write_atomic(self, path, saved):
        if self.fail_save and saved.get("complete") is True:
            raise OSError("synthetic atomic replacement failure")
        super()._write_atomic(path, saved)


def _book(store, auth):
    return route_snapshot("structure_orders", STRUCTURE, auth=auth, cache=store)


def structure_sharing_restart(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    original = _book(store, auth)
    browse, expires, _ = fetch_structure_orders(STRUCTURE, auth, slot="buyer", cache=store)
    _require(browse == original["payload"] and expires == original["expires"], "Browse did not share")
    info = fetch_structure_info(STRUCTURE, auth, cache=store)
    industry = fetch_structure_meta(STRUCTURE, RosterAuth(_Roster(auth), 101), cache=store)
    _require(industry["name"] == info.name and len(server.calls) == 2, "metadata callers did not share")
    browse.clear()
    _require(_book(store, auth)["payload"], "consumer mutation changed saved book")
    server.time += timedelta(seconds=30)
    restored = _book(_Store(directory, server), auth)
    _require(restored["payload"] == original["payload"] and len(server.calls) == 2,
             "restart reacquired or lost book")
    _require(all(restored[key] == original[key] for key in ("acquired_at", "expires")), "restart reset ages")


def structure_authorization(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    _book(store, auth)
    for change in (lambda: setattr(auth, "owner", "second-owner"),
                   lambda: auth.scopes.append("another-scope"),
                   lambda: setattr(auth, "client_id", "second-app"),
                   lambda: auth.rotate(character=202)):
        count = len(server.calls)
        change()
        auth.rotate()
        _book(store, auth)
        _require(len(server.calls) == count + 1, "private authorization contexts shared")
    count = len(server.calls)
    auth.char.access_token = None
    _rejected(lambda: fetch_structure_orders(STRUCTURE, auth, cache=store))
    _require(len(server.calls) == count, "logout issued request")
    auth.rotate(character=303)
    context = private_context(auth)
    server.after_response = lambda: auth.rotate(character=404)
    _rejected(lambda: _book(store, auth))
    _require(store.peek(context, "structure_orders", {"resource_id": STRUCTURE}) is None,
             "authorization changed during request but response was accepted")


def structure_page_restart(directory):
    server, auth = _Server(), _Auth()
    server.pages, server.fail_page = 2, 2
    store = _Store(directory, server)
    try:
        fetch_structure_orders(STRUCTURE, auth, cache=store)
    except StructureAccessError as exc:
        _require(exc.status == 404, "permission/page status lost")
    else:
        raise AssertionError("later-page 404 accepted partial book")
    _require(store.peek(private_context(auth), "structure_orders", {"resource_id": STRUCTURE}) is None,
             "partial pages became a full snapshot")
    server.fail_page = None
    book = _book(_Store(directory, server), auth)
    _require(len(book["payload"]) == 2 and [row[2] for row in server.calls] == [1, 2, 2],
             "restart repeated successful unexpired page or omitted failed page")
    server.time += timedelta(seconds=301)
    _book(_Store(directory, server), auth)
    _require(len(server.calls) == 3, "book reacquired before all page expiries")


def structure_validation(directory):
    for case in ("validator", "mixed", "changed_count", "invalid"):
        server, auth = _Server(), _Auth()
        store = _Store(directory / case, server)
        original = _book(store, auth)
        server.time += timedelta(seconds=400)
        server.version, server.pages = 2, 2
        setattr(server, case, False if case == "validator" else True)
        _rejected(lambda: _book(store, auth))
        retained = store.peek(private_context(auth), "structure_orders", {"resource_id": STRUCTURE})
        _require(retained["payload"] == original["payload"] and
                 retained["acquired_at"] == original["acquired_at"], "invalid book replaced last good snapshot")
    server, auth = _Server(), _Auth()
    server.empty = True
    _require(_book(_Store(directory / "empty", server), auth)["payload"] == [], "valid empty book rejected")


def structure_save_recovery(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    original = _book(store, auth)
    server.time += timedelta(seconds=400)
    server.version, store.fail_save = 2, True
    accepted = _book(store, auth)
    _require(not accepted["durable"], "failed disk save claimed durability")
    restored = route_snapshot("structure_orders", STRUCTURE, auth=auth,
                              cache=_Store(directory, server), fetch=False)
    _require(restored["payload"] == original["payload"], "failed replacement destroyed previous disk book")
    before = len(server.calls)
    store.fail_save = False
    saved = _book(store, auth)
    _require(saved["durable"] and saved["acquired_at"] == accepted["acquired_at"] and
             len(server.calls) == before, "save retry reacquired/reset data")


def structure_concurrency(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    entered, release = threading.Event(), threading.Event()
    server.block = entered, release
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_book, store, auth)
        try:
            _require(entered.wait(3), "first request did not start")
            second = pool.submit(_book, store, auth)
            _require(store.waiting.wait(3), "second caller did not wait")
        finally:
            release.set()
        _require(first.result()["payload"] == second.result()["payload"] and len(server.calls) == 1,
                 "concurrent consumers acquired duplicate books")


def underbid_route_cache(directory):
    for restart in (False, True):
        server = _Server()
        case_directory = directory / ("restart" if restart else "memory")
        store = _Store(case_directory, server)

        def fetch(cache, type_id=34, *, acquire=True):
            return route_snapshot("type_orders", 10000002, cache=cache, fetch=acquire,
                                  params={"type_id": type_id, "order_type": "sell"})

        original = fetch(store)
        fetch(store)
        restored = fetch(_Store(case_directory, server))
        _require(len(server.calls) == 1 and restored["acquired_at"] == original["acquired_at"],
                 "underbid repeat/restart reacquired before expiry")
        fetch(store, 35)
        _require(len(server.calls) == 2, "different item incorrectly reused filtered response")
        server.time += timedelta(seconds=400)
        server.version, server.pages, server.fail_page = 2, 2, 2
        try:
            fetch(store)
        except requests.HTTPError as exc:
            _require(exc.response.status_code == 404, "filtered-page failure lost HTTP status")
        else:
            raise AssertionError("failed filtered page was accepted")
        retained = fetch(_Store(case_directory, server) if restart else store, acquire=False)
        _require(retained["payload"] == original["payload"]
                 and retained["acquired_at"] == original["acquired_at"],
                 "failed filtered page replaced the last complete snapshot or reset its age")
        server.fail_page = None
        recovered_store = _Store(case_directory, server) if restart else store
        fetched = fetch(recovered_store)
        _require(len(fetched["payload"]) == 2 and [row[2] for row in server.calls[-3:]] == [1, 2, 2],
                 "filtered order retry accepted partial book or repeated page 1")
        calls = len(server.calls)
        _require(fetch(recovered_store)["payload"] == fetched["payload"] and len(server.calls) == calls,
                 "recovered filtered snapshot was not reused")


def underbid_structure_routing(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    config = {"type": "structure", "station_id": STRUCTURE, "region_id": 11000004}

    class RegionalCache:
        def get_cached_orders(self, *args, **kwargs):
            raise AssertionError("private structure consulted public regional cache")

        def get_cache_timing(self, *args, **kwargs):
            raise AssertionError("private structure consulted public regional cache")

    def fetch(cache, identity=auth, hub=config, regional=None):
        return fetch_underbid_orders_sync(hub, [34], regional or RegionalCache(),
            auth=identity, route_cache=cache, log=lambda line: None)

    browse, _expires, metadata = fetch_structure_orders(STRUCTURE, auth, cache=store)
    _require(fetch(store) == browse and len(server.calls) == 1,
             "Tracking did not share Browse's structure book")
    restored = _Store(directory, server)
    _require(fetch(restored) == browse and len(server.calls) == 1,
             "Tracking restart reacquired an unexpired structure book")
    snapshot = _book(restored, auth)
    _require(snapshot["acquired_at"] == metadata["acquired_at"], "Tracking reset acquisition age")
    _rejected(lambda: fetch(restored, identity=None))
    _require(len(server.calls) == 1, "missing auth caused a public fallback")

    auth.rotate(character=202)
    fetch(restored)
    _require(len(server.calls) == 2, "Tracking leaked another character's structure book")
    server.time += timedelta(seconds=400)
    server.pages, server.fail_page = 2, 2
    _rejected(lambda: fetch(restored))
    _require(all(call[0] == "structure_orders" for call in server.calls),
             "private acquisition failure fell back to a public route")

    server.pages, server.fail_page = 1, None
    npc = {"station_id": 60003760, "region_id": 10000002}
    public = fetch(restored, hub=npc, regional=SimpleNamespace(get_cached_orders=lambda *a, **k: None))
    _require(public and server.calls[-1][0] == "type_orders", "NPC per-type fallback changed")
    count = len(server.calls)
    regional = SimpleNamespace(get_cached_orders=lambda *a, **k: public,
                               get_cache_timing=lambda *a, **k: {"age_seconds": None, "reusable": False})
    logs = []
    _require(fetch_underbid_orders_sync(npc, [34], regional, route_cache=restored,
                                       log=logs.append) == public and len(server.calls) == count,
             "NPC shared regional snapshot was not reused")
    _require(len(logs) == 1 and "age=unknown" in logs[0],
             "unavailable display age changed reuse or was not logged as unknown")


def public_metadata_cache(directory):
    server = _Server()
    store = _Store(directory, server)
    routes = (("station_info", 60003760), ("system_info", 30000142),
              ("constellation_info", 20000020))
    originals = [route_snapshot(resource, identity, cache=store) for resource, identity in routes]
    restored = _Store(directory, server)
    for (resource, identity), original in zip(routes, originals):
        snapshot = route_snapshot(resource, identity, cache=restored)
        _require(snapshot["payload"] == original["payload"] and
                 snapshot["expires"] == original["expires"], "metadata restore lost original data/expiry")
    _require(len(server.calls) == 3, "metadata repeated unexpired requests")


def structure_observation_reuse(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    original = _book(store, auth)
    history = StructureHistoryDB(directory / "synthetic_structure_history.db")
    try:
        history.record_snapshot(STRUCTURE, original["payload"], original["acquired_at"])
        later = original["acquired_at"] + timedelta(seconds=400)
        filled = [{**original["payload"][0], "volume_remain": 0}]
        history.record_snapshot(STRUCTURE, filled, later)
        before = [tuple(row) for row in history._conn().execute("SELECT * FROM structure_daily")]
        history.record_snapshot(STRUCTURE, filled, later)
        after = [tuple(row) for row in history._conn().execute("SELECT * FROM structure_daily")]
        _require(before and before == after, "reused observation duplicated history rollup")
        count = history._conn().execute("SELECT COUNT(*) FROM structure_snapshots").fetchone()[0]
        _require(count == 2, "reused observation became a new snapshot")
    finally:
        history._conn().close()
