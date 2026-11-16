"""Offline safety checks run inside the IDE-launched app (Ctrl+F12).

Uses synthetic identities, an injected transport/clock and temporary files.
Never reads live tokens/caches or calls ESI. No global transport is patched.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

from core.esi_private_cache import (
    PrivateWalletCache, PrivateSnapshotError, RESOURCE_PATHS, private_context,
)
from esi.esi_wallet import ESIWallet


class _Auth:
    client_id = "offline-cache-check"

    def __init__(self, character=101, owner="synthetic-owner", scopes=None):
        self.owner = owner
        self.scopes = scopes or ["synthetic.wallet", "synthetic.orders"]
        self.rotate(character)

    def rotate(self, character=None, nonce=1):
        character = character or self.char.character_id
        claims = {"sub": f"CHARACTER:EVE:{character}", "owner": self.owner,
                  "scp": self.scopes, "azp": self.client_id, "nonce": nonce}
        encoded = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        self.char = SimpleNamespace(character_id=character, access_token=f"offline.{encoded}.offline")

    def get_character(self, slot="seller"):
        return self.char

    @property
    def is_authenticated(self):
        return bool(self.char.access_token)

    def get_auth_headers(self, slot="seller"):
        return {"Authorization": "synthetic-only"}


class _Server:
    def __init__(self):
        self.time = datetime.now(timezone.utc).replace(microsecond=0)
        self.calls = []
        self.version = 1
        self.pages = 1
        self.fail_page = None
        self.mixed = False
        self.invalid = False
        self.after_response = None
        self.empty_orders = False
        self.block = None

    def now(self):
        return self.time

    def advance(self, seconds=1201):
        self.time += timedelta(seconds=seconds)

    def request(self, method, url, headers, params, timeout):
        resource = next(key for key, suffix in RESOURCE_PATHS.items()
                        if url.endswith("/" + suffix))
        page = params.get("page", 1)
        self.calls.append((resource, page, params.get("from_id")))
        if self.block is not None:
            entered, release = self.block
            entered.set()
            if not release.wait(5):
                raise RuntimeError("offline transport release timed out")
        if self.fail_page == page:
            self.fail_page = None
            raise OSError("synthetic page failure")
        identity = self.version * 100 + page + (params.get("from_id") or 0)
        date = self.time.isoformat()
        if resource == "balance":
            payload = float(self.version)
        elif resource == "transactions":
            payload = [{"transaction_id": identity, "date": date, "type_id": 34,
                        "quantity": 1, "unit_price": 2.0, "is_buy": False,
                        "location_id": 60000001, "journal_ref_id": identity}]
        elif resource == "journal":
            payload = [{"id": identity, "date": date, "ref_type": "brokers_fee",
                        "amount": -1.0, "balance": 1.0, "description": "synthetic"}]
        else:
            payload = [] if self.empty_orders else [{
                "order_id": identity, "type_id": 34, "is_buy_order": False,
                "price": 2.0, "volume_total": 2, "volume_remain": 1,
                "issued": "invalid" if self.invalid else date,
                "duration": 90, "location_id": 60000001}]
        paginated = resource in ("orders", "journal", "order_history")
        response_headers = {
            "Expires": format_datetime(self.time + timedelta(seconds=60 if resource == "balance" else 1200), usegmt=True),
            "Last-Modified": f"synthetic-{self.version}" + ("-mixed" if self.mixed and page == 2 else ""),
            "ETag": f"synthetic-{identity}", "X-Pages": str(self.pages if paginated else 1)}
        if self.after_response:
            callback, self.after_response = self.after_response, None
            callback()
        return SimpleNamespace(headers=response_headers, json=lambda: payload,
                               raise_for_status=lambda: None)


class _Store(PrivateWalletCache):
    def __init__(self, directory, server):
        super().__init__(directory, request=server.request, now=server.now)
        self.fail_save = False
        self.waiting = threading.Event()

    def _event(self, context, resource, action, trigger, entry=None, **fields):
        if action == "WAIT":
            self.waiting.set()

    def _write_atomic(self, path, saved):
        if self.fail_save and saved.get("complete") is True:
            raise OSError("synthetic replacement failure")
        super()._write_atomic(path, saved)


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


def _rejected(callback):
    try:
        callback()
    except Exception:
        return
    raise AssertionError("invalid acquisition was accepted")


def _reuse_and_restart(directory):
    server, auth = _Server(), _Auth()
    server.version = 0  # A zero balance is successful data, not a cache miss.
    store = _Store(directory, server)
    context = private_context(auth)
    original = {resource: store.get(auth, context, resource) for resource in RESOURCE_PATHS}
    _require(len(server.calls) == 5, "initial resources not independent")
    server.advance(30)
    restored = _Store(directory, server)
    for resource, entry in original.items():
        reused = restored.get(auth, context, resource)
        _require(reused["payload"] == entry["payload"] and reused["acquired_at"] == entry["acquired_at"],
                 "restart lost original payload/time")
    _require(len(server.calls) == 5, "restart/reuse made a request")
    server.advance(31)
    for resource in RESOURCE_PATHS:
        restored.get(auth, context, resource)
    _require(len(server.calls) == 6 and server.calls[-1][0] == "balance", "independent expiry broken")


def _isolation(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    context = private_context(auth)
    store.get(auth, context, "orders")
    auth.scopes.reverse()
    auth.rotate(nonce=2)
    _require(private_context(auth) == context, "normal token rotation changed partition")
    store.get(auth, private_context(auth), "orders")
    for change in (lambda: setattr(auth, "owner", "another-owner"),
                   lambda: auth.scopes.append("another-scope"),
                   lambda: setattr(auth, "client_id", "another-client")):
        change()
        auth.rotate(nonce=3)
        _require(private_context(auth).partition != context.partition, "authorization contexts overlap")
        context = private_context(auth)
        store.get(auth, context, "orders")
    auth.rotate(character=202)
    _require(private_context(auth).partition != context.partition, "characters overlap")
    store.get(auth, private_context(auth), "orders")
    _require(len(server.calls) == 5, "isolation or rotation reuse failed")


def _concurrent(directory):
    server, auth = _Server(), _Auth()
    entered, release = threading.Event(), threading.Event()
    server.block = (entered, release)
    store = _Store(directory, server)
    context = private_context(auth)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.get, auth, context, "orders")
        try:
            _require(entered.wait(3), "first caller did not acquire")
            second = pool.submit(store.get, auth, context, "orders")
            _require(store.waiting.wait(3), "second caller did not wait")
        finally:
            release.set()
        _require(first.result(timeout=3)["payload"] == second.result(timeout=3)["payload"], "callers disagree")
    _require(len(server.calls) == 1, "concurrent duplicate acquisition")


def _page_failure(directory):
    server, auth = _Server(), _Auth()
    server.pages = 2
    store = _Store(directory, server)
    context = private_context(auth)
    original = store.get(auth, context, "orders")
    path = store._path("orders", store._identity(context, "orders", None))
    old_bytes = path.read_bytes()
    server.advance()
    server.version, server.fail_page = 2, 2
    _rejected(lambda: store.get(auth, context, "orders"))
    _require(store.peek(context, "orders") is original and path.read_bytes() == old_bytes,
             "page failure replaced complete snapshot")
    before = len(server.calls)
    accepted = _Store(directory, server).get(auth, context, "orders")
    _require(len(server.calls) == before + 1 and server.calls[-1][1] == 2,
             "retry refetched unexpired page after restart")
    _require(len(accepted["payload"]) == 2 and accepted["acquired_at"] > original["acquired_at"],
             "retry did not accept complete new snapshot")


def _mixed_pages(directory):
    server, auth = _Server(), _Auth()
    server.pages = 2
    store, context = _Store(directory, server), private_context(auth)
    original = store.get(auth, context, "journal")
    server.advance()
    server.version, server.mixed = 2, True
    _rejected(lambda: store.get(auth, context, "journal"))
    before = len(server.calls)
    _rejected(lambda: store.get(auth, context, "journal"))
    _require(len(server.calls) == before, "mixed retry refetched unexpired pages")
    _require(store.peek(context, "journal") is original, "mixed pages replaced good snapshot")


def _save_failure(directory):
    server, auth = _Server(), _Auth()
    store, context = _Store(directory, server), private_context(auth)
    original = store.get(auth, context, "orders")
    path = store._path("orders", store._identity(context, "orders", None))
    old_bytes = path.read_bytes()
    server.advance()
    server.version, store.fail_save = 2, True
    accepted = store.get(auth, context, "orders")
    _require(not accepted["durable"] and path.read_bytes() == old_bytes,
             "failed save destroyed previous file or claimed durability")
    wallet = ESIWallet(auth, cache=store, diagnostics=lambda *args, **kwargs: None,
                       log=lambda *args: None)
    _require(not wallet.refresh_all() and not wallet.resource_durable["orders"],
             "wallet claimed durable completion after failed saves")
    count = len(server.calls)
    store.fail_save = False
    retried = store.get(auth, context, "orders")
    _require(retried["durable"] and len(server.calls) == count, "save retry reacquired data")
    restored = _Store(directory, server).get(auth, context, "orders")
    _require(restored["acquired_at"] == accepted["acquired_at"] != original["acquired_at"], "save retry reset age")


def _wallet_adapter(directory):
    unauthenticated = ESIWallet(None, diagnostics=lambda *args, **kwargs: None,
                               log=lambda *args: None)
    _require(not unauthenticated.refresh_all() and unauthenticated.fetch_balance() == 0
             and unauthenticated.get_seconds_until_refresh() == 0,
             "unauthenticated replay wallet compatibility broken")
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    wallet = ESIWallet(auth, cache=store, diagnostics=lambda *args, **kwargs: None,
                       log=lambda *args: None)
    _require(wallet.refresh_all(), "wallet bundle did not accept durable snapshots")
    original, success_time = wallet.transactions, wallet.resource_last_success["transactions"]
    cursor = wallet.fetch_transactions(from_id=999)
    _require(cursor != original and wallet.transactions is original
             and wallet.resource_last_success["transactions"] == success_time, "cursor poisoned base snapshot")
    original_history = wallet.order_history
    history_time = wallet.resource_last_success["order_history"]
    server.pages = 2
    suffix = wallet.fetch_order_history(page=2)
    _require(suffix != original_history and wallet.order_history is original_history
             and wallet.resource_last_success["order_history"] == history_time,
             "suffix page poisoned base history snapshot")
    server.pages = 1
    original_orders = wallet.orders
    server.advance()
    server.invalid = True
    _require(not wallet.refresh_all() and wallet.orders is original_orders,
             "invalid order parsing replaced good view or claimed success")
    server.invalid = False
    auth.rotate(character=202)
    wallet.get_seconds_until_refresh()
    _require(not wallet.orders and not wallet.transactions, "account switch leaked prior character view")
    server.empty_orders = True
    _require(wallet.refresh_all() and wallet.orders == [], "valid empty orders were rejected")
    before = len(server.calls)
    auth.char.access_token = None
    _require(not wallet.refresh_all() and not wallet.transactions
             and not wallet.resource_last_success and len(server.calls) == before,
             "logout retained private view or made a request")


def _authorization_race(directory):
    server, auth = _Server(), _Auth()
    store, context = _Store(directory, server), private_context(auth)
    server.after_response = lambda: auth.rotate(character=202)
    _rejected(lambda: store.get(auth, context, "orders"))
    _require(store.peek(context, "orders") is None, "changed authorization accepted old response")


def run_checks():
    """Return pass/fail; each check prints a concise, payload-free result."""
    from tools.wallet_accounting_checks import (
        inventory_cache_lifecycle, inventory_delayed_journal, inventory_partial_refresh,
    )
    from tools.character_cache_checks import (
        character_sharing_restart, character_context_isolation, character_expiry_force,
        character_validation_failures, character_concurrency, character_local_views,
        reputation_retention, reputation_refresh_policy,
    )
    from tools.route_cache_checks import (
        structure_sharing_restart, structure_authorization, structure_page_restart,
        structure_validation, structure_save_recovery, structure_concurrency,
        underbid_route_cache, underbid_structure_routing, public_metadata_cache, structure_observation_reuse,
    )
    from tools.acquisition_batch_checks import (
        regional_page_resume, regional_page_validation, regional_atomic_persistence,
        blueprint_roundtrip, blueprint_database_failure, blueprint_context_concurrency,
        lookup_isolation_restart, lookup_concurrent, contract_singleflight,
        contract_failure_validation, metadata_route_sharing,
    )
    from tools.policy_batch_checks import (
        history_restart_and_merge, history_failures_and_save_recovery,
        history_partial_and_hourly_policy, history_concurrent_and_empty,
        history_cache_control_age, jita_reference_and_active_orders,
        stock_age_and_hub_independence, order_force_singleflight,
        sso_shared_cooldown_restart, sso_failed_grant_isolation,
        sso_date_and_concurrent_throttle, distribution_notice,
    )
    from tools.esi_audit_fix_checks import (
        contract_queued_cooldown, contract_cancelled_cooldown,
    )
    checks = (_reuse_and_restart, _isolation, _concurrent, _page_failure,
              _mixed_pages, _save_failure, _wallet_adapter, _authorization_race,
              inventory_cache_lifecycle, inventory_delayed_journal, inventory_partial_refresh,
              character_sharing_restart, character_context_isolation, character_expiry_force,
              character_validation_failures, character_concurrency, character_local_views,
              reputation_retention, reputation_refresh_policy,
              structure_sharing_restart, structure_authorization, structure_page_restart,
              structure_validation, structure_save_recovery, structure_concurrency,
              underbid_route_cache, underbid_structure_routing, public_metadata_cache, structure_observation_reuse,
              regional_page_resume, regional_page_validation, regional_atomic_persistence,
              blueprint_roundtrip, blueprint_database_failure, blueprint_context_concurrency,
              lookup_isolation_restart, lookup_concurrent, contract_singleflight,
              contract_failure_validation, metadata_route_sharing,
              history_restart_and_merge, history_failures_and_save_recovery,
              history_partial_and_hourly_policy, history_concurrent_and_empty,
              history_cache_control_age, jita_reference_and_active_orders,
              stock_age_and_hub_independence, order_force_singleflight,
              sso_shared_cooldown_restart, sso_failed_grant_isolation,
              sso_date_and_concurrent_throttle, distribution_notice,
              contract_queued_cooldown, contract_cancelled_cooldown)
    passed = 0
    for check in checks:
        try:
            with tempfile.TemporaryDirectory(prefix="eve-private-cache-check-") as directory:
                check(Path(directory))
            passed += 1
            print(f"[ESI-CacheCheck] check={check.__name__.lstrip('_')} result=PASS")
        except Exception as exc:
            print(f"[ESI-CacheCheck] check={check.__name__.lstrip('_')} result=FAIL "
                  f"error_type={type(exc).__name__} detail={exc}")
    print(f"[ESI-CacheCheck] passed={passed} total={len(checks)} network=none data=synthetic")
    return passed == len(checks)
