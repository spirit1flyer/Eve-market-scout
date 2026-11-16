"""Wallet adapter over shared exact-resource private snapshots."""

from datetime import datetime, timezone

from core.esi_private_cache import (
    RESOURCE_PATHS, get_private_cache, private_context, PrivateSnapshotError,
)
from esi.esi_wallet_models import parse_wallet_resource


class WalletFetchMixin:
    def _private_cache(self):
        return self._wallet_cache if self._wallet_cache is not None else get_private_cache()

    def _ensure_wallet_context(self):
        """Discard previous-character views before reading any other snapshot."""
        context = private_context(self.auth)
        if getattr(self, "_wallet_context", None) == context:
            return context
        self._wallet_context = context
        self.balance = 0
        self.transactions, self.journal, self.orders, self.order_history = [], [], [], []
        self.resource_cache_expires.clear()
        self.resource_last_success.clear()
        self.resource_generations.clear()
        self.resource_durable = {}
        self.orders_cache_expires = None
        self.last_update = None
        cache = self._private_cache()
        for resource in RESOURCE_PATHS:
            entry = cache.peek(context, resource)
            if entry is not None:
                self._apply_wallet_snapshot(context, resource, entry)
        return context

    def _apply_wallet_snapshot(self, context, resource, entry):
        if private_context(self.auth) != context:
            raise PrivateSnapshotError("authorization_changed")
        parsed = parse_wallet_resource(resource, entry["payload"])
        setattr(self, resource, parsed)
        self.resource_cache_expires[resource] = entry["expires"]
        self.resource_last_success[resource] = entry["acquired_at"]
        self.resource_durable[resource] = entry["durable"]
        self.resource_generations[resource] = self.resource_generations.get(resource, 0) + 1
        if resource == "orders":
            self.orders_cache_expires = entry["expires"]

    def _fetch_wallet_resource(self, resource, params=None):
        try:
            context = self._ensure_wallet_context()
            entry = self._private_cache().get(self.auth, context, resource, params,
                                            trigger="trade_direct")
            if params:
                # A cursor or suffix-page result is a different exact resource;
                # never replace the base snapshot or advance its refresh clock.
                if private_context(self.auth) != context:
                    raise PrivateSnapshotError("authorization_changed")
                return parse_wallet_resource(resource, entry["payload"])
            self._apply_wallet_snapshot(context, resource, entry)
            return getattr(self, resource)
        except Exception as exc:
            self._wallet_diagnostic(resource, "FAILED", "trade_direct", slot="seller",
                           reason="snapshot_not_applied", status=type(exc).__name__)
            # If login changed during acquisition, erase the former view before
            # any consumer can fall back to it. No payloads enter error output.
            try:
                self._ensure_wallet_context()
            except Exception:
                self._clear_wallet_context()
            return getattr(self, resource)

    def _clear_wallet_context(self):
        self._wallet_context = None
        self.balance = 0
        self.transactions, self.journal, self.orders, self.order_history = [], [], [], []
        self.resource_cache_expires.clear()
        self.resource_last_success.clear()
        self.resource_generations.clear()
        self.resource_durable = {}
        self.orders_cache_expires = None
        self.last_update = None

    def get_seconds_until_refresh(self) -> float:
        try:
            self._ensure_wallet_context()
        except PrivateSnapshotError:
            self._clear_wallet_context()
            return 0
        if self.orders_cache_expires is None:
            return 0
        delta = (self.orders_cache_expires - datetime.now(timezone.utc)).total_seconds()
        return delta + 1.0 if delta > 0 else 0

    def _seconds_until_resource_due(self, resource: str) -> float:
        self._ensure_wallet_context()
        expires = self.resource_cache_expires.get(resource)
        return max(0, (expires - datetime.now(timezone.utc)).total_seconds()) if expires else 0

    def fetch_balance(self):
        return self._fetch_wallet_resource("balance")

    def fetch_transactions(self, from_id=None):
        return self._fetch_wallet_resource("transactions", {"from_id": from_id} if from_id else None)

    def fetch_journal(self, page=1):
        result = self._fetch_wallet_resource("journal", {"page": page} if page != 1 else None)
        ref_counts = {}
        for entry in result:
            ref_counts[entry.ref_type] = ref_counts.get(entry.ref_type, 0) + 1
        self._wallet_log(f"[FeeDiag] journal full fetch: {len(result)} entries; "
              f"ref_type counts: {ref_counts}")
        return result

    def fetch_orders(self):
        result = self._fetch_wallet_resource("orders")
        buy_count = sum(order.is_buy_order for order in result)
        self._wallet_log(f"[ESI-Parse] resource=orders items={len(result)} "
              f"buy={buy_count} sell={len(result) - buy_count}")
        return result

    def fetch_order_history(self, page=1):
        return self._fetch_wallet_resource("order_history", {"page": page} if page != 1 else None)
