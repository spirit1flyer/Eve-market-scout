"""Synthetic cache-to-inventory acceptance checks for the in-app Ctrl+F12 run.

Exercises real wallet parsing, sync, FIFO and inventory serialization. Only
injected transports and temporary directories are used; no global paths,
credentials, live caches, Tk callbacks or production evidence are changed.
"""

import atexit
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from email.utils import format_datetime
from math import isclose

from esi.esi_wallet import ESIWallet
from scanning.scanner_inventory import InventoryManager
from scanning.scanner_inventory_sync import sync_inventory_from_wallet
from tools.private_cache_checks import _Auth, _Server, _Store, _require


TYPE_ID = 34
LOCATION = 60000001


class _ReplayInventory(InventoryManager):
    """Defer writes until explicit flush, avoiding Tk scheduling in the worker."""

    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        super().__init__("offline_wallet", data_dir=directory)
        atexit.unregister(self._atexit_flush)

    def _save(self):
        self._dirty = True
        self._dirty_count += 1


class _AccountingServer(_Server):
    def __init__(self):
        super().__init__()
        self.base = self.time - timedelta(days=2)
        self.failed_resources = set()
        self.lifetimes = {}
        self.payloads = {
            "balance": 10000.0,
            "transactions": [self.tx(1, 6, 100, 0), self.tx(2, 4, 150, 1),
                             self.tx(3, 4, 200, 4, buy=False)],
            "journal": [self.fee(101, 501, 20, 2), self.fee(102, 502, 10, 3),
                        *self.tax(200, 3, 8, 4)],
            "orders": [self.order(501, 7, 3, 2), self.order(502, 3, 3, 3)],
            "order_history": [],
        }

    def date(self, hours):
        return (self.base + timedelta(hours=hours)).isoformat()

    def tx(self, identity, quantity, price, hours, buy=True):
        return {"transaction_id": identity, "date": self.date(hours),
                "type_id": TYPE_ID, "quantity": quantity, "unit_price": price,
                "is_buy": buy, "location_id": LOCATION, "journal_ref_id": 0}

    def fee(self, identity, order, amount, hours):
        return {"id": identity, "date": self.date(hours), "ref_type": "brokers_fee",
                "amount": -amount, "balance": 0, "context_id": order,
                "context_id_type": "market_transaction"}

    def tax(self, identity, transaction, amount, hours):
        return [{"id": identity, "date": self.date(hours),
                 "ref_type": "market_transaction", "amount": 0, "balance": 0,
                 "context_id": transaction, "context_id_type": "transaction_id"},
                {"id": identity + 1, "date": self.date(hours),
                 "ref_type": "transaction_tax", "amount": -amount, "balance": 0}]

    def order(self, identity, total, remain, hours, state="active"):
        return {"order_id": identity, "type_id": TYPE_ID, "is_buy_order": False,
                "price": 200, "volume_total": total, "volume_remain": remain,
                "issued": self.date(hours), "duration": 90,
                "location_id": LOCATION, "state": state}

    def finish_orders(self):
        self.version += 1
        self.payloads["transactions"].append(self.tx(4, 3, 210, 5, buy=False))
        self.payloads["journal"].extend(self.tax(202, 4, 6.3, 5))
        self.payloads["orders"] = []
        self.payloads["order_history"] = [self.order(501, 7, 0, 2, "fulfilled"),
                                         self.order(502, 3, 3, 3, "cancelled")]

    def request(self, method, url, headers, params, timeout):
        response = super().request(method, url, headers, params, timeout)
        resource = self.calls[-1][0]
        if resource in self.failed_resources:
            raise OSError("synthetic linking-resource failure")
        lifetime = self.lifetimes.get(resource, 60 if resource == "balance" else 1200)
        response.headers["Expires"] = format_datetime(
            self.time + timedelta(seconds=lifetime), usegmt=True)
        payload = deepcopy(self.payloads[resource])
        response.json = lambda: payload
        return response


def _wallet(auth, store):
    return ESIWallet(auth, cache=store, diagnostics=lambda *args, **kwargs: None,
                     log=lambda *args: None)


def _state(inventory):
    # UI activity timestamps vary by replay; all accounting fields must agree.
    result = {identity: asdict(entry) for identity, entry in inventory.entries.items()}
    for entry in result.values():
        entry.pop("flagged_at")
        entry.pop("last_activity_at")
    return result


def _number(actual, expected, label):
    _require(isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-7),
             f"{label}: expected {expected}, got {actual}")


def _expect(inventory, *, sold=4, tax=8, fees=30, finished=False):
    entry = inventory.get(TYPE_ID)
    _require(entry is not None, "tracked item disappeared")
    _require((entry.quantity_in, entry.quantity_out, entry.quantity_held) ==
             (10, sold, 10 - sold), "buy/sale/held quantities differ")
    _require([(lot.transaction_id, lot.qty_remaining) for lot in entry.buy_lots] ==
             ([(1, 0), (2, 3)] if sold == 7 else [(1, 2), (2, 4)]), "FIFO lots differ")
    _require([(sale.transaction_id, sale.cost_basis) for sale in entry.sales] ==
             ([(3, 400), (4, 350)] if sold == 7 else [(3, 400)]),
             "sale-specific FIFO cost basis differs")
    _number(entry.total_buy_cost, 1200, "buy spend")
    cost = 750 if sold == 7 else 400
    _number(entry.remaining_cost_basis, 1200 - cost, "remaining basis")
    revenue = 1430 if sold == 7 else 800
    _number(entry.total_revenue, revenue, "revenue")
    _number(entry.total_sales_tax, tax, "tax")
    _number(entry.total_listing_fees, fees, "listing fees")
    _number(entry.total_realized_profit, revenue - cost - tax, "realized profit")
    _number(inventory.get_summary()["total_realized_profit_net"],
            revenue - cost - tax - fees, "profit after fees")
    expected_listings = [] if finished else [(501, 3), (502, 3)]
    _require([(listing.order_id, listing.qty_remaining) for listing in entry.active_listings]
             == expected_listings, "partial-fill listings differ")
    _require(entry.retired_listings == ({"501": "fulfilled", "502": "cancelled"}
                                       if finished else {}), "final order states differ")


def _compare_modes(directory, server, auth, store, **expected):
    """Replay each view, repeat sync, flush/reload inventory, and compare."""
    original = _wallet(auth, store)
    ok = original.refresh_all()
    _require(ok == (not server.failed_resources), "bundle completion hid a failed resource")
    original_times = dict(original.resource_last_success)
    original_expiries = dict(original.resource_cache_expires)
    before = len(server.calls)
    states = []
    for mode in ("fetched", "reused", "restored"):
        cache = _Store(store.directory, server) if mode == "restored" else store
        wallet = original if mode == "fetched" else _wallet(auth, cache)
        if mode != "fetched":
            _require(wallet.refresh_all() == ok, "reuse/restore changed completion status")
        _require(wallet.resource_last_success == original_times and
                 wallet.resource_cache_expires == original_expiries,
                 "reuse/restore changed acquisition or expiry clocks")
        path = directory / mode
        inventory = _ReplayInventory(path)
        inventory.get_or_create(TYPE_ID, "Synthetic item")
        sync_inventory_from_wallet(inventory, wallet)
        _expect(inventory, **expected)
        state = _state(inventory)
        sync_inventory_from_wallet(inventory, wallet)
        _require(_state(inventory) == state, "repeat sync double-counted accounting")
        inventory.flush()
        restored = _ReplayInventory(path)
        _require(_state(restored) == state, "inventory serialization changed accounting")
        sync_inventory_from_wallet(restored, wallet)
        _expect(restored, **expected)
        _require(_state(restored) == state, "inventory restart double-counted accounting")
        restored.flush()
        states.append(state)
    _require(states[0] == states[1] == states[2], "fetched/reused/restored accounting differs")
    _require(all(call[0] in server.failed_resources for call in server.calls[before:]),
             "reuse/restore reacquired an unexpired accepted resource")


def inventory_cache_lifecycle(directory):
    server, auth = _AccountingServer(), _Auth()
    store = _Store(directory / "cache", server)
    _compare_modes(directory / "inventory", server, auth, store)
    server.advance()
    server.finish_orders()
    _compare_modes(directory / "inventory", server, auth, store,
                   sold=7, tax=14.3, finished=True)


def inventory_delayed_journal(directory):
    server, auth = _AccountingServer(), _Auth()
    journal, server.payloads["journal"] = server.payloads["journal"], []
    server.lifetimes["journal"] = 120
    store = _Store(directory / "cache", server)
    _compare_modes(directory / "inventory", server, auth, store, tax=0, fees=0)
    server.advance(121)
    server.version += 1
    server.payloads["journal"] = journal
    before = len(server.calls)
    _compare_modes(directory / "inventory", server, auth, store)
    _require({call[0] for call in server.calls[before:]} == {"balance", "journal"},
             "delayed journal refresh changed independent resource clocks")


def inventory_partial_refresh(directory):
    server, auth = _AccountingServer(), _Auth()
    store = _Store(directory / "cache", server)
    _compare_modes(directory / "inventory", server, auth, store)
    server.advance()
    server.finish_orders()
    server.failed_resources.add("journal")
    _compare_modes(directory / "inventory", server, auth, store,
                   sold=7, tax=8, finished=True)
    server.failed_resources.clear()
    before = len(server.calls)
    _compare_modes(directory / "inventory", server, auth, store,
                   sold=7, tax=14.3, finished=True)
    _require([call[0] for call in server.calls[before:]] == ["journal"],
             "failure recovery reacquired successful resources")
