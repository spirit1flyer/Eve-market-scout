"""ESI Wallet and Order data fetching for EVE Market Scout."""

import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from esi.esi_auth import ESIAuth
from core.esi_rate_limit import esi_request

BASE_URL = "https://esi.evetech.net/latest"

_RESOURCE_FALLBACK_SECONDS = {
    "balance": 60,
    "transactions": 60 * 60,
    "journal": 60 * 60,
    "orders": 20 * 60,
    "order_history": 60 * 60,
}


@dataclass
class Transaction:
    """A wallet transaction (buy or sell)."""
    transaction_id: int
    date: datetime
    type_id: int
    type_name: str  # Populated later
    quantity: int
    unit_price: float
    is_buy: bool
    location_id: int
    journal_ref_id: int  # Links to journal for fees
    
    @property
    def total(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class JournalEntry:
    """A wallet journal entry (fees, taxes, etc)."""
    entry_id: int
    date: datetime
    ref_type: str  # 'brokers_fee', 'transaction_tax', 'market_escrow', etc.
    amount: float  # Negative = cost, Positive = income
    balance: float
    description: str
    context_id: Optional[int] = None  # Links to order_id or transaction_id
    context_type: Optional[str] = None


@dataclass 
class MarketOrder:
    """An active or historical market order."""
    order_id: int
    type_id: int
    type_name: str  # Populated later
    is_buy_order: bool
    price: float
    volume_total: int
    volume_remain: int
    issued: datetime
    duration: int  # Days
    location_id: int
    state: str = "active"  # active, cancelled, expired, fulfilled
    escrow: float = 0  # ISK in escrow for buy orders
    
    @property
    def volume_filled(self) -> int:
        return self.volume_total - self.volume_remain
    
    @property
    def is_complete(self) -> bool:
        return self.volume_remain == 0


# NOTE: TrackedTrade class moved to trade_tracker.py
# This file only handles ESI data fetching, not trade tracking logic


class ESIWallet:
    """Fetches wallet data from ESI."""

    def __init__(self, auth: ESIAuth):
        self.auth = auth
        
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

    def _parse_expires_header(self, response: requests.Response) -> Optional[datetime]:
        """Parse the Expires header from an ESI response."""
        expires_str = response.headers.get("Expires")
        if not expires_str:
            return None
        
        try:
            # ESI format: "Thu, 16 Jan 2025 14:05:22 GMT"
            return datetime.strptime(expires_str, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def get_seconds_until_refresh(self) -> float:
        """
        Get seconds until ESI orders cache expires.
        Returns 0 if no expiry known, or if already expired.
        Adds 1-second buffer to ensure fresh data.
        """
        if self.orders_cache_expires is None:
            return 0
        
        now = datetime.now(timezone.utc)
        delta = (self.orders_cache_expires - now).total_seconds()
        
        if delta <= 0:
            return 0
        
        # Add 1s buffer to ensure CCP servers have refreshed
        return delta + 1.0

    def _record_resource_success(self, resource: str,
                                 response: requests.Response):
        now = datetime.now(timezone.utc)
        expires = self._parse_expires_header(response)
        if expires is None:
            expires = now + timedelta(
                seconds=_RESOURCE_FALLBACK_SECONDS[resource]
            )
        self.resource_cache_expires[resource] = expires
        self.resource_last_success[resource] = now
        self.resource_generations[resource] = (
            self.resource_generations.get(resource, 0) + 1
        )
        if resource == "orders":
            self.orders_cache_expires = expires

    def _seconds_until_resource_due(self, resource: str) -> float:
        if resource not in self.resource_last_success:
            return 0.0
        expires = self.resource_cache_expires.get(resource)
        if expires is None:
            return 0.0
        return max(0.0, (expires - datetime.now(timezone.utc)).total_seconds())

    def _make_request(self, endpoint: str, params: dict = None
                      ) -> tuple[Optional[dict | list],
                                 Optional[requests.Response]]:
        """Make an authenticated request without advancing a cache clock."""
        headers = self.auth.get_auth_headers()
        if not headers:
            print("Not authenticated")
            return None, None

        url = f"{BASE_URL}{endpoint}"
        try:
            response = esi_request("GET", url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            return response.json(), response
        except (requests.RequestException, ValueError) as e:
            print(f"ESI request error: {e}")
            return None, None

    def fetch_balance(self) -> float:
        """Fetch current wallet balance."""
        char_id = self.auth.character_id
        if not char_id:
            return 0
            
        data, response = self._make_request(f"/characters/{char_id}/wallet/")
        if data is not None:
            try:
                balance = float(data)
            except (TypeError, ValueError):
                print("Wallet balance parse error")
                return self.balance
            self.balance = balance
            self._record_resource_success("balance", response)
        return self.balance

    def fetch_transactions(self, from_id: int = None) -> List[Transaction]:
        """
        Fetch wallet transactions.
        Returns newest first. Use from_id to paginate backwards.
        """
        char_id = self.auth.character_id
        if not char_id:
            return []

        params = {"datasource": "tranquility"}
        if from_id:
            params["from_id"] = from_id

        data, response = self._make_request(
            f"/characters/{char_id}/wallet/transactions/", params
        )

        if data is not None and not isinstance(data, list):
            print("Wallet transaction response was not a list")
            return self.transactions
        if data is not None:
            try:
                staged = [
                    Transaction(
                        transaction_id=item["transaction_id"],
                        date=datetime.fromisoformat(
                            item["date"].replace("Z", "+00:00")
                        ),
                        type_id=item["type_id"],
                        type_name="",
                        quantity=item["quantity"],
                        unit_price=item["unit_price"],
                        is_buy=item["is_buy"],
                        location_id=item["location_id"],
                        journal_ref_id=item["journal_ref_id"],
                    )
                    for item in data
                ]
            except (KeyError, TypeError, ValueError) as e:
                print(f"Wallet transaction parse error: {e}")
                return self.transactions
            # Sort newest first
            staged.sort(key=lambda x: x.date, reverse=True)
            self.transactions = staged
            self._record_resource_success("transactions", response)

        return self.transactions

    def fetch_journal(self, page: int = 1) -> List[JournalEntry]:
        """
        Fetch wallet journal entries.
        Includes broker fees, sales tax, market escrow, etc.

        On the first call (page=1) we walk every page returned by ESI's
        X-Pages header. Multi-page coverage is required because date-proximity
        matching for broker fees needs to reach entries that may not be on
        page 1 if the user trades heavily.
        """
        char_id = self.auth.character_id
        if not char_id:
            return []

        headers = self.auth.get_auth_headers()
        if not headers:
            return self.journal

        prior_journal = self.journal
        staged: List[JournalEntry] = []
        first_response = None
        first_validator = None
        total_pages = page

        for current_page in range(page, 10_000):
            try:
                response = esi_request(
                    "GET",
                    f"{BASE_URL}/characters/{char_id}/wallet/journal/",
                    headers=headers,
                    params={"datasource": "tranquility", "page": current_page},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, TypeError, ValueError) as e:
                print(f"[FeeDiag] journal page={current_page} fetch error: {e}")
                return prior_journal

            validator = response.headers.get("Last-Modified")
            if first_response is None:
                first_response = response
                first_validator = validator
                try:
                    total_pages = int(response.headers.get("X-Pages", "1"))
                except (TypeError, ValueError):
                    print("[FeeDiag] journal invalid X-Pages header")
                    return prior_journal
                print(f"[FeeDiag] journal X-Pages={total_pages} (will fetch all)")
            elif validator != first_validator:
                print(
                    "[ESI-Timing] resource=journal trigger=page_validation "
                    f"decision=REJECT reason=representation_changed "
                    f"page={current_page}"
                )
                return prior_journal

            if not isinstance(data, list):
                print("[FeeDiag] journal response was not a list")
                return prior_journal

            try:
                for entry in data:
                    staged.append(JournalEntry(
                        entry_id=entry["id"],
                        date=datetime.fromisoformat(
                            entry["date"].replace("Z", "+00:00")
                        ),
                        ref_type=entry.get("ref_type", ""),
                        amount=entry.get("amount", 0),
                        balance=entry.get("balance", 0),
                        description=entry.get("description", ""),
                        context_id=entry.get("context_id"),
                        context_type=entry.get("context_id_type"),
                    ))
            except (KeyError, TypeError, ValueError) as e:
                print(f"[FeeDiag] journal parse error: {e}")
                return prior_journal

            if current_page >= total_pages:
                break

        staged.sort(key=lambda x: x.date, reverse=True)
        self.journal = staged
        self._record_resource_success("journal", first_response)

        ref_counts = {}
        for entry in self.journal:
            ref_counts[entry.ref_type] = ref_counts.get(entry.ref_type, 0) + 1
        print(f"[FeeDiag] journal full fetch: {len(self.journal)} entries; "
              f"ref_type counts: {ref_counts}")
        return self.journal

    def fetch_orders(self) -> List[MarketOrder]:
        """Fetch active market orders."""
        char_id = self.auth.character_id
        if not char_id:
            print("fetch_orders: No character_id")
            return []

        data, response = self._make_request(f"/characters/{char_id}/orders/")

        if data is not None and not isinstance(data, list):
            print("Active orders response was not a list")
            return self.orders
        if data is not None:
            staged = []
            try:
                for o in data:
                    # ESI returns is_buy_order directly as a boolean
                    # Default to False (sell order) if not present
                    is_buy = o.get("is_buy_order", False)
                    
                    staged.append(MarketOrder(
                        order_id=o["order_id"],
                        type_id=o["type_id"],
                        type_name="",
                        is_buy_order=is_buy,
                        price=o["price"],
                        volume_total=o["volume_total"],
                        volume_remain=o["volume_remain"],
                        issued=datetime.fromisoformat(o["issued"].replace("Z", "+00:00")),
                        duration=o["duration"],
                        location_id=o["location_id"],
                        escrow=o.get("escrow", 0)
                    ))
            except (KeyError, TypeError, ValueError) as e:
                print(f"Order parse error: {e}")
                print(f"Order data: {o}")
                return self.orders
            self.orders = staged
            self._record_resource_success("orders", response)
        
        buy_count = len([o for o in self.orders if o.is_buy_order])
        sell_count = len([o for o in self.orders if not o.is_buy_order])
        print(
            "[ESI-Parse] resource=orders "
            f"items={len(self.orders)} buy={buy_count} sell={sell_count}"
        )
        return self.orders

    def fetch_order_history(self, page: int = 1) -> List[MarketOrder]:
        """Fetch a complete, validator-consistent historical order snapshot."""
        char_id = self.auth.character_id
        if not char_id:
            return []

        headers = self.auth.get_auth_headers()
        if not headers:
            return self.order_history

        prior_history = self.order_history
        staged: List[MarketOrder] = []
        first_response = None
        first_validator = None
        total_pages = page

        for current_page in range(page, 10_000):
            try:
                response = esi_request(
                    "GET",
                    f"{BASE_URL}/characters/{char_id}/orders/history/",
                    headers=headers,
                    params={"datasource": "tranquility", "page": current_page},
                    timeout=30,
                )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, TypeError, ValueError) as e:
                print(f"Order history page {current_page} fetch error: {e}")
                return prior_history

            validator = response.headers.get("Last-Modified")
            if first_response is None:
                first_response = response
                first_validator = validator
                try:
                    total_pages = int(response.headers.get("X-Pages", "1"))
                except (TypeError, ValueError):
                    print("Order history invalid X-Pages header")
                    return prior_history
            elif validator != first_validator:
                print(
                    "[ESI-Timing] resource=order_history "
                    "trigger=page_validation decision=REJECT "
                    f"reason=representation_changed page={current_page}"
                )
                return prior_history

            if not isinstance(data, list):
                print("Order history response was not a list")
                return prior_history

            for order in data:
                try:
                    staged.append(MarketOrder(
                        order_id=order["order_id"],
                        type_id=order["type_id"],
                        type_name="",
                        is_buy_order=order.get("is_buy_order", False),
                        price=order["price"],
                        volume_total=order["volume_total"],
                        volume_remain=order["volume_remain"],
                        issued=datetime.fromisoformat(
                            order["issued"].replace("Z", "+00:00")
                        ),
                        duration=order["duration"],
                        location_id=order["location_id"],
                        state=order.get("state", "expired"),
                    ))
                except (KeyError, TypeError, ValueError) as e:
                    print(f"Order history parse error: {e}")
                    print(f"Order data: {order}")
                    return prior_history

            if current_page >= total_pages:
                break

        self.order_history = staged
        self._record_resource_success("order_history", first_response)
        return self.order_history

    def refresh_all(self, progress_callback=None, trigger: str = "manual") -> bool:
        """Refresh only due wallet resources, then reuse all cached results."""
        if not self.auth.is_authenticated:
            return False

        try:
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
                next_in = self._seconds_until_resource_due(resource)
                progress = index * 20
                if next_in > 0:
                    print(
                        f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                        f"decision=REUSE reason=not_due next_in={next_in:.0f}s "
                        f"items={count()}"
                    )
                    if progress_callback:
                        progress_callback(f"Using cached {label}...", progress)
                    continue

                print(
                    f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                    "decision=FETCH reason=due"
                )
                if progress_callback:
                    progress_callback(f"Fetching {label}...", progress)
                prior_generation = self.resource_generations.get(resource, 0)
                fetch_error = None
                try:
                    fetcher()
                except Exception as e:
                    fetch_error = e
                    print(
                        f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                        "decision=FAILED phase=fetch "
                        f"error_type={type(e).__name__}"
                    )
                if self.resource_generations.get(resource, 0) == prior_generation:
                    success = False
                    if fetch_error is None:
                        print(
                            f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                            "decision=FAILED phase=validate reason=no_new_snapshot"
                        )
                else:
                    next_due = self._seconds_until_resource_due(resource)
                    print(
                        f"[ESI-Timing] resource={resource} trigger=trade_{trigger} "
                        f"decision=FETCHED next_in={next_due:.0f}s "
                        f"items={count()}"
                    )

            self.last_update = datetime.now()
            
            if progress_callback:
                progress_callback("Complete!", 100)
            
            return success
            
        except Exception as e:
            print(
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
