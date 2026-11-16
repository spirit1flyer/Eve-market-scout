"""ESI wallet orchestration and trade analysis helpers."""

from datetime import datetime
from typing import Dict, List, Optional
from esi.esi_auth import ESIAuth
from esi.esi_wallet_models import Transaction, JournalEntry, MarketOrder
from esi.esi_wallet_fetch import WalletFetchMixin
from core.esi_diagnostics import decision_event


class ESIWallet(WalletFetchMixin):
    """Fetch wallet data through shared durable private snapshots."""

    def __init__(self, auth: ESIAuth, cache=None, diagnostics=decision_event, log=print):
        self.auth = auth
        self._wallet_cache = cache
        self._wallet_diagnostic = diagnostics
        self._wallet_log = log
        
        # Cached data
        self.balance: float = 0
        self.transactions: List[Transaction] = []
        self.journal: List[JournalEntry] = []
        self.orders: List[MarketOrder] = []
        self.order_history: List[MarketOrder] = []
        
        self.last_update: Optional[datetime] = None
        
        # ESI cache expiry tracking - we track orders specifically
        # since that's what matters for trade sync (not balance which is ~60s)
        self.orders_cache_expires: Optional[datetime] = None
        self.resource_cache_expires: Dict[str, datetime] = {}
        self.resource_last_success: Dict[str, datetime] = {}
        self.resource_generations: Dict[str, int] = {}
        self.resource_durable: Dict[str, bool] = {}
        if self.auth is not None and self.auth.is_authenticated:
            try:
                self._ensure_wallet_context()
            except Exception as exc:
                self._wallet_diagnostic("trade_bundle", "RESTORE_FAILED", "startup",
                               reason="context_unavailable", status=type(exc).__name__)
                self._clear_wallet_context()

    def refresh_all(self, progress_callback=None, trigger: str = "manual") -> bool:
        """Refresh only due wallet resources, then reuse all cached results."""
        if self.auth is None or not self.auth.is_authenticated:
            self._clear_wallet_context()
            return False

        try:
            bundle_context = self._ensure_wallet_context()
            resources = (
                ("balance", "balance", self.fetch_balance, lambda: 1),
                ("transactions", "transactions", self.fetch_transactions,
                 lambda: len(self.transactions)),
                ("journal", "journal", self.fetch_journal,
                 lambda: len(self.journal)),
                ("orders", "active orders", self.fetch_orders,
                 lambda: len(self.orders)),
                ("order_history", "order history", self.fetch_order_history,
                 lambda: len(self.order_history)),
            )
            success = True
            for index, (resource, label, fetcher, count) in enumerate(resources):
                progress = index * 20
                if progress_callback:
                    progress_callback(f"Loading {label}...", progress)
                prior_generation = self.resource_generations.get(resource, 0)
                fetch_error = None
                try:
                    fetcher()
                except Exception as e:
                    fetch_error = e
                    self._wallet_log(
                        f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                        "decision=FAILED phase=fetch "
                        f"error_type={type(e).__name__}"
                    )
                if self._ensure_wallet_context() != bundle_context:
                    self._wallet_diagnostic("trade_bundle", "FAILED", f"trade_{trigger}",
                                   reason="authorization_changed")
                    return False
                if self.resource_generations.get(resource, 0) == prior_generation:
                    success = False
                    if fetch_error is None:
                        self._wallet_log(
                            f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                            "decision=FAILED phase=validate reason=no_new_snapshot"
                        )
                else:
                    next_due = self._seconds_until_resource_due(resource)
                    durable = self.resource_durable.get(resource, False)
                    success = success and durable
                    self._wallet_diagnostic(resource, "READY", f"trade_{trigger}",
                                   status="durable" if durable else "memory_only_save_failed",
                                   items=count(), next_in=f"{next_due:.0f}s")

            if success:
                self.last_update = datetime.now()
            
            if progress_callback:
                progress_callback("Complete!" if success else "Partial failure", 100)
            
            return success
            
        except Exception as e:
            self._wallet_log(
                "[ESI-Timing] resource=trade_bundle "
                f"trigger={trigger} decision=FAILED phase=orchestration "
                f"error_type={type(e).__name__}"
            )
            return False

    # === Analysis helpers ===

    def get_broker_fee_for_order(self, order_id: int,
                                  issued: Optional[datetime] = None,
                                  tolerance_seconds: float = 5.0) -> float:
        """Find the placement broker fee for an order.

        ESI removed context_id from brokers_fee entries (as of 2026-05), so
        the old `entry.context_id == order_id` match no longer works. We
        match by date proximity to `order.issued` -- the broker fee is
        debited at the same instant the order is placed, so timestamps line
        up within ~1s.

        If `issued` is None, falls back to the legacy context_id match
        (still works for ancient journal entries that retain context_id).
        """
        # Legacy fallback: try context_id first if no date hint is given.
        if issued is None:
            matches = [
                e for e in self.journal
                if e.ref_type == "brokers_fee" and e.context_id == order_id
            ]
            if matches:
                matches.sort(key=lambda e: e.date)
                return abs(matches[0].amount)
            return 0

        # Date-proximity match.
        candidates = []
        for e in self.journal:
            if e.ref_type != "brokers_fee":
                continue
            delta = abs((e.date - issued).total_seconds())
            if delta <= tolerance_seconds:
                candidates.append((delta, e))
        if not candidates:
            print(f"[FeeDiag] get_broker_fee_for_order({order_id}) "
                  f"issued={issued.isoformat()}: NO brokers_fee within "
                  f"+/-{tolerance_seconds}s")
            return 0
        candidates.sort(key=lambda t: t[0])
        delta, best = candidates[0]
        print(f"[FeeDiag] get_broker_fee_for_order({order_id}) "
              f"issued={issued.isoformat()}: DATE match delta={delta:.2f}s "
              f"amount={abs(best.amount)} ({len(candidates)} candidate(s))")
        return abs(best.amount)

    def build_sales_tax_index(self) -> Dict[int, float]:
        """Return {transaction_id: sales_tax_amount} for all sales in the journal.

        ESI dropped context_id on transaction_tax entries (2026-05), so tax
        can't be matched to a transaction directly. Bridge: the
        market_transaction entry still carries context_id == transaction_id,
        and its transaction_tax entry is written immediately after it
        (entry_id + 1). Single shared implementation — the NPC Orders sales
        panel and the Stock Market P&L sync both key off this.
        """
        mt_eid_by_tx: Dict[int, int] = {}
        tax_by_entry_id: Dict[int, float] = {}
        for entry in self.journal:
            if entry.ref_type == "market_transaction" and entry.context_id is not None:
                mt_eid_by_tx[entry.context_id] = entry.entry_id
            elif entry.ref_type == "transaction_tax":
                tax_by_entry_id[entry.entry_id] = abs(entry.amount)

        result: Dict[int, float] = {}
        for tx_id, mt_eid in mt_eid_by_tx.items():
            tax = tax_by_entry_id.get(mt_eid + 1)
            if tax is not None:
                result[tx_id] = tax
        return result

    def get_sales_tax_for_transaction(self, journal_ref_id: int) -> float:
        """Find the sales tax for a sale via its journal reference.

        `journal_ref_id` (from the wallet transaction) is the entry_id of the
        sale's market_transaction journal entry; the matching transaction_tax
        entry lands at entry_id + 1 (see build_sales_tax_index). The old
        `entry_id == journal_ref_id` match compared the tax entry's own id to
        the market_transaction's id and could never hit — every caller got 0.
        Returns 0 when the tax entry isn't in the fetched journal (caller
        retries next sync / estimates past the journal age limit).
        """
        for entry in self.journal:
            if entry.ref_type == "transaction_tax" and entry.entry_id == journal_ref_id + 1:
                return abs(entry.amount)
        return 0

    def get_transactions_for_type(self, type_id: int, is_buy: Optional[bool] = None) -> List[Transaction]:
        """Get all transactions for a specific item type."""
        result = [t for t in self.transactions if t.type_id == type_id]
        if is_buy is not None:
            result = [t for t in result if t.is_buy == is_buy]
        return result

    def get_orders_for_type(self, type_id: int, is_buy: Optional[bool] = None) -> List[MarketOrder]:
        """Get all active orders for a specific item type."""
        result = [o for o in self.orders if o.type_id == type_id]
        if is_buy is not None:
            result = [o for o in result if o.is_buy_order == is_buy]
        return result

    def get_relist_fees_for_order(self, order_id: int) -> tuple[float, int]:
        """Find all modification (relist) fees for an order.

        Journal is stored newest-first. Sort matching entries oldest-first
        so the placement fee is index 0 and relist fees are index 1+.
        Returns (total_relist_fees, relist_count).
        """
        matches = [
            e for e in self.journal
            if e.ref_type == "brokers_fee" and e.context_id == order_id
        ]
        matches.sort(key=lambda e: e.date)
        # matches[0] is placement; matches[1:] are relists
        total = sum(abs(e.amount) for e in matches[1:])
        return (total, max(0, len(matches) - 1))
