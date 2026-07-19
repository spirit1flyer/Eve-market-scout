"""ESI sync logic for Trade Tracking - refresh, backfill, auto-refresh timers."""

import threading
import requests
from datetime import datetime
from typing import Optional, Callable, List, TYPE_CHECKING

from esi.esi_wallet import ESIWallet
from analytics.trade_tracker import TradeTracker
from core.calculate import format_isk
from core.config import TRADE_HUBS, ESI_USER_AGENT
from core.tk_queue import submit

if TYPE_CHECKING:
    from gui.tracking.gui_tracking import TrackingTabManager


def fetch_market_orders_sync(region_id: int, type_ids: Optional[List[int]] = None,
                             order_cache=None) -> List[dict]:
    """
    Fetch market orders synchronously for underbid checking.

    Prefers the scanner's shared region dump (complete order book, no
    duplicate download). On a cache miss, queries ESI per listed type_id
    (`?type_id=&order_type=sell`) instead of paging the whole region — the
    old whole-region path truncated at 20 pages, so hub regions (300+ pages)
    gave the underbid check a partial book that could miss undercuts.

    Args:
        region_id: Region ID to fetch orders from
        type_ids: type_ids of the user's listings (fallback path fetches
            only these; empty/None means nothing to check)
        order_cache: shared OrderCacheStore to peek before hitting ESI

    Returns:
        List of market order dicts
    """
    if order_cache is not None:
        peeked = order_cache.peek_cached_orders(region_id, max_age_seconds=300)
        if peeked is not None:
            orders, age = peeked
            print(f"[Underbid] Using shared order dump for region {region_id} "
                  f"(age: {age:.0f}s, {len(orders)} orders)")
            return orders

    if not type_ids:
        return []

    all_orders = []
    base_url = "https://esi.evetech.net/latest"

    for type_id in type_ids:
        try:
            page = 1
            while True:
                response = requests.get(
                    f"{base_url}/markets/{region_id}/orders/",
                    headers={"User-Agent": ESI_USER_AGENT},
                    params={"type_id": type_id, "order_type": "sell", "page": page},
                    timeout=30
                )
                if response.status_code == 404:
                    break  # Page vanished, stop
                response.raise_for_status()
                total_pages = int(response.headers.get("X-Pages", 1))
                all_orders.extend(response.json())
                if page >= total_pages:
                    break
                page += 1
        except Exception as e:
            print(f"Error fetching market orders for type {type_id}: {e}")

    return all_orders


class ESISyncManager:
    """Handles ESI data sync, backfill, and auto-refresh timing."""
    
    def __init__(self, tracker: TradeTracker, set_status: Callable[[str], None],
                 get_client: Optional[Callable] = None):
        self.tracker = tracker
        self.set_status = set_status
        self.get_client = get_client  # () -> api.ESIClient (shared instance)
        self.wallet: Optional[ESIWallet] = None
        
        # Auto-refresh state
        self.auto_refresh_enabled = True
        self.auto_refresh_job = None
        self.is_refreshing = False
        
        # Hub for underbid checking (set by TrackingTabManager)
        self.hub_key: str = "amarr"
        
        # Underbid monitor reference (set externally)
        self.underbid_monitor = None
        
        # Stock market holdings sync callback (set externally)
        self.on_orders_synced: Optional[Callable[[List[dict], int], None]] = None
        
        # Holdings wallet transaction sync callback (set externally)
        self.on_wallet_synced: Optional[Callable[["ESIWallet"], None]] = None

        # P&L sync callback (set externally)
        self.on_pnl_synced: Optional[Callable[[List[dict], "ESIWallet"], None]] = None

        # Last fetched market orders (shared with TrackingTabManager)
        self.market_orders_cache: List[dict] = []
        
        # UI references (set by TrackingTabManager)
        self.frame = None
        self.refresh_btn = None
        self.countdown_label = None
        self.on_refresh_complete: Optional[Callable] = None
    
    def set_wallet(self, wallet: Optional[ESIWallet]):
        """Set the wallet instance for ESI calls."""
        self.wallet = wallet
    
    def set_underbid_monitor(self, monitor, hub_key: str):
        """Set the underbid monitor instance and hub."""
        self.underbid_monitor = monitor
        self.hub_key = hub_key
    
    def set_stock_market_callback(self, callback: Callable[[List[dict], int], None]):
        """Set callback for syncing orders to stock market holdings.
        
        Args:
            callback: Function that takes (orders_list, region_id)
        """
        self.on_orders_synced = callback
    
    def set_wallet_sync_callback(self, callback: Callable[["ESIWallet"], None]):
        """Set callback for syncing wallet transactions to holdings.

        Args:
            callback: Function that takes ESIWallet instance
        """
        self.on_wallet_synced = callback

    def set_pnl_sync_callback(self, callback: Callable[[List[dict], "ESIWallet"], None]):
        """Set callback for syncing orders/wallet to P&L tracking.

        Args:
            callback: Function that takes (char_orders_list, ESIWallet)
        """
        self.on_pnl_synced = callback

    def set_ui_refs(self, frame, refresh_btn, countdown_label, on_complete: Callable):
        """Set UI widget references for updates."""
        self.frame = frame
        self.refresh_btn = refresh_btn
        self.countdown_label = countdown_label
        self.on_refresh_complete = on_complete
    
    # === Auto-refresh timing ===
    
    def toggle_auto_refresh(self, enabled: bool):
        """Toggle auto-refresh on/off."""
        self.auto_refresh_enabled = enabled
        if enabled and self.wallet:
            self.schedule_auto_refresh()
        else:
            self.cancel_auto_refresh()
    
    def cancel_auto_refresh(self):
        """Cancel pending auto-refresh."""
        if self.auto_refresh_job and self.frame:
            self.frame.after_cancel(self.auto_refresh_job)
            self.auto_refresh_job = None
        if self.countdown_label:
            self.countdown_label.configure(text="")
    
    def schedule_auto_refresh(self):
        """Schedule the next auto-refresh based on ESI cache expiry."""
        self.cancel_auto_refresh()
        
        if not self.auto_refresh_enabled or not self.wallet or self.is_refreshing:
            return
        
        # Get seconds until cache expires
        wait_seconds = self.wallet.get_seconds_until_refresh()

        if wait_seconds <= 0:
            # No expiry yet known (bootstrap, first sync of the session). Use
            # a short wait so the first sync fires quickly and populates the
            # cache; subsequent schedules fall into the floor branch below.
            wait_seconds = 60
        else:
            # Floor at 20 minutes: matches the NPC Sales tracker's update
            # cadence and aligns with the wallet transactions endpoint's
            # 1-hour ESI cache (polling more often returns the same data).
            MIN_REFRESH_SECONDS = 1200
            if wait_seconds < MIN_REFRESH_SECONDS:
                wait_seconds = MIN_REFRESH_SECONDS

        # Start countdown
        self._start_countdown(int(wait_seconds))
    
    def _start_countdown(self, seconds_left: int):
        """Update countdown display and trigger refresh when done."""
        if not self.auto_refresh_enabled or self.is_refreshing:
            if self.countdown_label:
                self.countdown_label.configure(text="")
            return
        
        if seconds_left <= 0:
            if self.countdown_label:
                self.countdown_label.configure(text="Syncing...")
            self._auto_refresh()
        else:
            # Format time nicely
            if seconds_left >= 3600:
                time_str = f"{seconds_left // 3600}h {(seconds_left % 3600) // 60}m"
            elif seconds_left >= 60:
                time_str = f"{seconds_left // 60}m {seconds_left % 60}s"
            else:
                time_str = f"{seconds_left}s"
            
            if self.countdown_label:
                self.countdown_label.configure(text=f"ESI sync: {time_str}")
            if self.frame:
                self.auto_refresh_job = self.frame.after(
                    1000, lambda: self._start_countdown(seconds_left - 1)
                )
    
    def _auto_refresh(self):
        """Triggered by auto-refresh timer."""
        if self.auto_refresh_enabled and self.wallet and not self.is_refreshing:
            self.refresh_esi_data(is_auto=True)
    
    # === ESI refresh ===
    
    def refresh_esi_data(self, is_auto: bool = False):
        """Refresh wallet data from ESI and sync with tracked trades."""
        if not self.wallet or self.is_refreshing:
            return
        
        self.is_refreshing = True
        if self.refresh_btn:
            self.refresh_btn.configure(state="disabled")
        self.set_status("Refreshing ESI data...")
        
        # Clear expiry before refresh so we get fresh timing
        self.wallet.orders_cache_expires = None
        
        def do_refresh():
            success = self.wallet.refresh_all()
            
            # Debug: check what orders came back
            print(f"Orders fetched: {len(self.wallet.orders)}")
            for o in self.wallet.orders[:5]:
                print(f"  {o.type_id} buy={o.is_buy_order} price={o.price}")

            # Debug: check pending trades vs orders
            pending = [t for t in self.tracker.trades.values() if t.status == "pending"]
            print(f"Pending trades: {len(pending)}")
            for t in pending:
                print(f"  {t.type_name} (type_id={t.type_id})")
                matching_orders = [o for o in self.wallet.orders if o.type_id == t.type_id and not o.is_buy_order]
                print(f"    Matching sell orders: {len(matching_orders)}")
            
            # Debug: check transactions
            print(f"Transactions fetched: {len(self.wallet.transactions)}")
            for tx in self.wallet.transactions[:10]:
                print(f"  {tx.type_id} buy={tx.is_buy} qty={tx.quantity} price={tx.unit_price}")
            
            # Debug: check buy matching for pending trades
            print("Checking buy transaction matches:")
            for t in pending:
                matching_buys = [tx for tx in self.wallet.transactions if tx.type_id == t.type_id and tx.is_buy]
                print(f"  {t.type_name}: {len(matching_buys)} buy transaction(s)")
            
            # Fetch market orders so _on_esi_refresh can run the underbid check
            # against inventory listings (the underbid check itself moved out of
            # this thread when the UI swapped from TradeTracker to InventoryManager).
            if self.hub_key:
                hub_config = TRADE_HUBS.get(self.hub_key)
                if hub_config:
                    region_id = hub_config["region_id"]
                    listed_type_ids = sorted({
                        o.type_id for o in self.wallet.orders if not o.is_buy_order
                    }) if self.wallet else []
                    order_cache = self.get_client().order_cache if self.get_client else None
                    print(f"Fetching market orders ({self.hub_key})...")
                    self.market_orders_cache = fetch_market_orders_sync(
                        region_id, listed_type_ids, order_cache
                    )
                    print(f"  Got {len(self.market_orders_cache)} orders")
            
            def update():
                import time as _pt
                _pt0 = _pt.perf_counter()
                _step_sync_esi = 0.0
                _step_orders_synced = 0.0
                _step_wallet_synced = 0.0
                _step_pnl_synced = 0.0
                _step_refresh_complete = 0.0
                self.is_refreshing = False
                if self.refresh_btn:
                    self.refresh_btn.configure(state="normal")

                if success:
                    # Sync ESI data with tracked trades
                    _ts = _pt.perf_counter()
                    sync_results = self.sync_esi_to_trades()
                    _step_sync_esi = _pt.perf_counter() - _ts

                    # Sync orders to stock market holdings
                    if self.on_orders_synced and self.wallet and self.hub_key:
                        hub_config = TRADE_HUBS.get(self.hub_key)
                        if hub_config:
                            # Convert wallet orders to dict format for holdings sync
                            orders_for_holdings = [
                                {
                                    "type_id": o.type_id,
                                    "is_buy_order": o.is_buy_order,
                                    "price": o.price,
                                    "volume_remain": o.volume_remain,
                                }
                                for o in self.wallet.orders
                            ]
                            _ts = _pt.perf_counter()
                            self.on_orders_synced(orders_for_holdings, hub_config["region_id"])
                            _step_orders_synced = _pt.perf_counter() - _ts

                    # Sync wallet transactions to stock market holdings
                    if self.on_wallet_synced and self.wallet:
                        try:
                            _ts = _pt.perf_counter()
                            self.on_wallet_synced(self.wallet)
                            _step_wallet_synced = _pt.perf_counter() - _ts
                        except Exception as e:
                            print(f"[ESISync] Holdings wallet sync error: {e}")

                    # Sync orders + wallet to P&L tracking
                    if self.on_pnl_synced and self.wallet:
                        try:
                            char_orders_for_pnl = [
                                {
                                    "order_id": o.order_id,
                                    "type_id": o.type_id,
                                    "is_buy_order": o.is_buy_order,
                                    "price": o.price,
                                    "volume_remain": o.volume_remain,
                                    "issued": o.issued,
                                    "location_id": o.location_id,
                                    "escrow": o.escrow,
                                }
                                for o in self.wallet.orders
                            ]
                            _ts = _pt.perf_counter()
                            self.on_pnl_synced(char_orders_for_pnl, self.wallet)
                            _step_pnl_synced = _pt.perf_counter() - _ts
                        except Exception as e:
                            print(f"[ESISync] P&L sync error: {e}")

                    status_parts = [f"Balance: {format_isk(self.wallet.balance)}"]
                    if sync_results["buys_matched"]:
                        status_parts.append(f"{sync_results['buys_matched']} buy(s) matched")
                    if sync_results["listings_matched"]:
                        status_parts.append(f"{sync_results['listings_matched']} listing(s) matched")
                    if sync_results["relists_detected"]:
                        status_parts.append(f"{sync_results['relists_detected']} relist(s)")
                    if sync_results["sales_detected"]:
                        status_parts.append(f"{sync_results['sales_detected']} sale(s)")

                    self.set_status(" | ".join(status_parts))
                else:
                    self.set_status("ESI refresh failed")

                # Notify UI to refresh display
                if self.on_refresh_complete:
                    _ts = _pt.perf_counter()
                    self.on_refresh_complete()
                    _step_refresh_complete = _pt.perf_counter() - _ts

                # Schedule next auto-refresh
                if self.auto_refresh_enabled:
                    self.schedule_auto_refresh()

                _pt_total = _pt.perf_counter() - _pt0
                print(
                    f"[PerfTimer] ESISync.update_closure total={_pt_total*1000:.0f}ms "
                    f"sync_esi_to_trades={_step_sync_esi*1000:.0f}ms "
                    f"on_orders_synced={_step_orders_synced*1000:.0f}ms "
                    f"on_wallet_synced={_step_wallet_synced*1000:.0f}ms "
                    f"on_pnl_synced={_step_pnl_synced*1000:.0f}ms "
                    f"on_refresh_complete={_step_refresh_complete*1000:.0f}ms"
                )
            
            if self.frame:
                submit(update)
        
        threading.Thread(target=do_refresh, daemon=True).start()
    
    # === Trade sync logic ===
    
    def sync_esi_to_trades(self) -> dict:
        """
        Sync ESI wallet data to tracked trades.
        
        Flow: pending -> listed -> sold
        
        For PENDING trades, we look for:
        1. A buy transaction (you bought the item)
        2. A sell order (you listed it for sale)
        When BOTH are found, status becomes "listed"
        
        For LISTED trades, we look for:
        1. Price changes (relists)
        2. Order disappearing + sell transaction = sold
        
        Returns dict with counts of what was synced.
        """
        results = {
            "buys_matched": 0,
            "listings_matched": 0,
            "relists_detected": 0,
            "sales_detected": 0
        }
        
        if not self.wallet:
            return results
        
        # Get active trades (pending, listed)
        active_trades = self.tracker.get_active_trades()
        if not active_trades:
            return results
        
        # Build lookup by type_id for quick matching
        trades_by_type = {}
        for trade in active_trades:
            trades_by_type[trade.type_id] = trade
        
        # === PENDING TRADES: Look for buy transaction AND sell order ===
        for trade in active_trades:
            if trade.status != "pending":
                continue
            
            type_id = trade.type_id
            buy_found = trade.buy_price > 0  # Already have buy data?
            list_found = trade.sell_order_id is not None  # Already have listing?
            
            # Look for buy transaction
            if not buy_found:
                for transaction in self.wallet.transactions:
                    if not transaction.is_buy:
                        continue
                    if transaction.type_id != type_id:
                        continue
                    # Skip if already matched to another trade
                    if trade.buy_transaction_id == transaction.transaction_id:
                        continue
                    
                    # Found buy transaction
                    self.tracker.update_buy_info(
                        trade.trade_id,
                        price=transaction.unit_price,
                        quantity=transaction.quantity,
                        transaction_id=transaction.transaction_id
                    )
                    buy_found = True
                    results["buys_matched"] += 1
                    print(f"Matched buy: {trade.type_name} @ {transaction.unit_price} x{transaction.quantity}")
                    break
            
            # Look for sell order (active orders first)
            if not list_found:
                for order in self.wallet.orders:
                    if order.is_buy_order:
                        continue
                    if order.type_id != type_id:
                        continue
                    
                    # Found sell order - get broker fee
                    broker_fee = self.wallet.get_broker_fee_for_order(order.order_id)
                    
                    self.tracker.update_listing_info(
                        trade.trade_id,
                        order_id=order.order_id,
                        price=order.price,
                        broker_fee=broker_fee
                    )
                    list_found = True
                    results["listings_matched"] += 1
                    print(f"Matched listing: {trade.type_name} @ {order.price}")
                    break
            
            # If still no listing found, check order_history for fulfilled orders
            # This catches "listed and sold between syncs" scenario
            if not list_found and buy_found:
                from datetime import timezone, timedelta
                now = datetime.now(timezone.utc)
                
                # Debug: show what's in order_history for this type
                matching_history = [o for o in self.wallet.order_history 
                                   if o.type_id == type_id and not o.is_buy_order]
                if matching_history:
                    print(f"  Order history for {trade.type_name}:")
                    for o in matching_history:
                        age_hours = (now - o.issued).total_seconds() / 3600
                        print(f"    order_id={o.order_id} state={o.state} price={o.price} issued={age_hours:.1f}h ago")
                else:
                    print(f"  No order history found for {trade.type_name}")
                
                found_in_history = False
                for order in self.wallet.order_history:
                    if order.is_buy_order:
                        continue
                    if order.type_id != type_id:
                        continue
                    if order.state != "fulfilled":
                        print(f"    Skipping order {order.order_id}: state={order.state} (not fulfilled)")
                        continue
                    
                    # Check order is recent (within 7 days - issued date may be old)
                    order_age = now - order.issued
                    if order_age > timedelta(days=7):
                        print(f"    Skipping order {order.order_id}: issued {order_age.days}d ago (too old)")
                        continue
                    
                    found_in_history = True
                    
                    # Found fulfilled sell order - get broker fee
                    broker_fee = self.wallet.get_broker_fee_for_order(order.order_id)
                    
                    # Update listing info (this sets status to "listed" since we have buy data)
                    self.tracker.update_listing_info(
                        trade.trade_id,
                        order_id=order.order_id,
                        price=order.price,
                        broker_fee=broker_fee
                    )
                    list_found = True
                    results["listings_matched"] += 1
                    print(f"Matched fulfilled listing from history: {trade.type_name} @ {order.price}")
                    
                    # Now immediately look for the sale transaction since order is fulfilled
                    for transaction in self.wallet.transactions:
                        if transaction.is_buy:
                            continue
                        if transaction.type_id != type_id:
                            continue
                        
                        # Match by approximate price (within 1%)
                        price_diff = abs(transaction.unit_price - order.price) / order.price
                        if price_diff > 0.01:
                            continue
                        
                        # Found sale transaction - get sales tax
                        sales_tax = 0
                        for entry in self.wallet.journal:
                            if entry.ref_type == "transaction_tax":
                                if entry.context_id == transaction.transaction_id:
                                    sales_tax = abs(entry.amount)
                                    break
                                if entry.entry_id == transaction.journal_ref_id:
                                    sales_tax = abs(entry.amount)
                                    break
                        
                        revenue = transaction.quantity * transaction.unit_price
                        
                        self.tracker.record_sale(
                            trade.trade_id,
                            quantity=transaction.quantity,
                            revenue=revenue,
                            sales_tax=sales_tax
                        )
                        results["sales_detected"] += 1
                        print(f"Detected sale from history: {trade.type_name} x{transaction.quantity}")
                        break
                    
                    break  # Only match one fulfilled order
                
                # FALLBACK: If no order_history, check for sell transaction directly
                # This handles cases where ESI order_history doesn't include the order
                if not found_in_history and not list_found:
                    print(f"  Checking sell transactions directly for {trade.type_name}...")
                    for transaction in self.wallet.transactions:
                        if transaction.is_buy:
                            continue
                        if transaction.type_id != type_id:
                            continue
                        
                        # Check transaction is recent (within 7 days)
                        tx_age = now - transaction.date
                        if tx_age > timedelta(days=7):
                            continue
                        
                        print(f"  Found sell transaction: qty={transaction.quantity} price={transaction.unit_price}")
                        
                        # We have a sell but no order info - estimate broker fee from journal
                        # Look for brokers_fee entries around the same time
                        broker_fee = 0
                        for entry in self.wallet.journal:
                            if entry.ref_type == "brokers_fee":
                                # Check if it's around the right time and amount
                                entry_age = now - entry.date
                                if entry_age <= timedelta(days=7):
                                    # Rough match - broker fee should be ~1-2% of order value
                                    expected_fee_low = transaction.unit_price * transaction.quantity * 0.01
                                    expected_fee_high = transaction.unit_price * transaction.quantity * 0.03
                                    if expected_fee_low <= abs(entry.amount) <= expected_fee_high:
                                        broker_fee = abs(entry.amount)
                                        break
                        
                        # Create a synthetic listing (we don't have the order_id)
                        trade.list_price = transaction.unit_price
                        trade.current_price = transaction.unit_price
                        trade.list_broker_fee = broker_fee
                        trade.listed_at = transaction.date.isoformat()
                        trade.status = "listed"
                        
                        # Get sales tax
                        sales_tax = 0
                        for entry in self.wallet.journal:
                            if entry.ref_type == "transaction_tax":
                                if entry.context_id == transaction.transaction_id:
                                    sales_tax = abs(entry.amount)
                                    break
                                if entry.entry_id == transaction.journal_ref_id:
                                    sales_tax = abs(entry.amount)
                                    break
                        
                        revenue = transaction.quantity * transaction.unit_price
                        
                        self.tracker.record_sale(
                            trade.trade_id,
                            quantity=transaction.quantity,
                            revenue=revenue,
                            sales_tax=sales_tax
                        )
                        results["sales_detected"] += 1
                        print(f"Detected sale (no order history): {trade.type_name} x{transaction.quantity} @ {transaction.unit_price}")
                        self.tracker._save()
                        break
        
        # Refresh active trades after updates
        active_trades = self.tracker.get_active_trades()
        trades_by_type = {t.type_id: t for t in active_trades}
        
        # === LISTED TRADES: Check for relists and sales ===
        for trade in active_trades:
            if trade.status != "listed":
                continue
            
            # Check if order still exists
            current_order = None
            for o in self.wallet.orders:
                if o.order_id == trade.sell_order_id:
                    current_order = o
                    break
            
            if current_order:
                # Order exists - check for price changes (relists)
                if abs(current_order.price - trade.current_price) > 0.001:
                    # Price changed - find relist fee
                    relist_fees, relist_count = self.wallet.get_relist_fees_for_order(current_order.order_id)
                    
                    # Only record if there's a new relist we haven't tracked
                    if relist_count > trade.relist_count:
                        new_fee = relist_fees - trade.relist_fees
                        self.tracker.record_relist(
                            trade.trade_id,
                            new_price=current_order.price,
                            fee=new_fee if new_fee > 0 else 0
                        )
                        results["relists_detected"] += 1
                        print(f"Detected relist: {trade.type_name} @ {current_order.price}")
            else:
                # Order gone - check order_history to see what happened
                historical_order = None
                for o in self.wallet.order_history:
                    if o.order_id == trade.sell_order_id:
                        historical_order = o
                        break
                
                # If order was cancelled or expired, don't look for sale
                if historical_order and historical_order.state in ("cancelled", "expired"):
                    print(f"Order {trade.type_name} was {historical_order.state}, not sold")
                    continue
                
                # Order fulfilled or not in history (assume sold) - look for sell transaction
                for transaction in self.wallet.transactions:
                    if transaction.is_buy:
                        continue
                    if transaction.type_id != trade.type_id:
                        continue
                    
                    # Found sell transaction - get sales tax
                    sales_tax = 0
                    for entry in self.wallet.journal:
                        if entry.ref_type == "transaction_tax":
                            if entry.context_id == transaction.transaction_id:
                                sales_tax = abs(entry.amount)
                                break
                            if entry.entry_id == transaction.journal_ref_id:
                                sales_tax = abs(entry.amount)
                                break
                    
                    revenue = transaction.quantity * transaction.unit_price
                    
                    self.tracker.record_sale(
                        trade.trade_id,
                        quantity=transaction.quantity,
                        revenue=revenue,
                        sales_tax=sales_tax
                    )
                    results["sales_detected"] += 1
                    print(f"Detected sale: {trade.type_name} x{transaction.quantity}")
                    break
        
        return results
    
    def backfill_trade_from_esi(self, trade_id: str) -> dict:
        """
        Immediately after flagging, scan ESI data for existing matches.
        Handles: Buy->Track, Buy->List->Track, Buy->List->Sell->Track
        
        Returns dict with what was found for status message.
        """
        trade = self.tracker.trades.get(trade_id)
        if not trade or not self.wallet:
            return {"found": False}
        
        type_id = trade.type_id
        results = {"buy": False, "listing": False, "sale": False}
        
        # 1. Look for buy transaction
        for tx in self.wallet.transactions:
            if tx.is_buy and tx.type_id == type_id:
                # Check not already claimed by another trade
                existing = self.tracker.get_trade_by_transaction(tx.transaction_id)
                if existing and existing.trade_id != trade_id:
                    continue
                
                self.tracker.update_buy_info(
                    trade_id,
                    price=tx.unit_price,
                    quantity=tx.quantity,
                    transaction_id=tx.transaction_id
                )
                results["buy"] = True
                break
        
        # 2. Look for active sell order
        for order in self.wallet.orders:
            if order.is_buy_order:
                continue
            if order.type_id != type_id:
                continue
            
            # Check not already claimed
            existing = self.tracker.get_trade_by_order(order.order_id)
            if existing and existing.trade_id != trade_id:
                continue
            
            broker_fee = self.wallet.get_broker_fee_for_order(order.order_id)
            self.tracker.update_listing_info(
                trade_id,
                order_id=order.order_id,
                price=order.price,
                broker_fee=broker_fee
            )
            results["listing"] = True
            break
        
        # 3. If no active order, look for completed sale
        if not results["listing"] and results["buy"]:
            trade = self.tracker.trades.get(trade_id)  # Refresh
            for tx in self.wallet.transactions:
                if tx.is_buy:
                    continue
                if tx.type_id != type_id:
                    continue
                
                # Found a sale - record it
                sales_tax = 0
                for entry in self.wallet.journal:
                    if entry.ref_type == "transaction_tax":
                        if entry.context_id == tx.transaction_id or entry.entry_id == tx.journal_ref_id:
                            sales_tax = abs(entry.amount)
                            break
                
                revenue = tx.quantity * tx.unit_price
                self.tracker.record_sale(
                    trade_id,
                    quantity=tx.quantity,
                    revenue=revenue,
                    sales_tax=sales_tax
                )
                results["sale"] = True
                break
        
        results["found"] = any([results["buy"], results["listing"], results["sale"]])
        return results
