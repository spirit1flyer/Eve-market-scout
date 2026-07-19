"""Stock Market tab for EVE Market Scout - long-term value investing tracker.

Main coordinator that creates sub-tabs for each trading hub.
Each hub has Discovery (bulk scanning) and Holdings (position tracking) sub-tabs.
"""

import tkinter as tk
from tkinter import ttk
import asyncio
import threading
from datetime import datetime, timezone
from typing import Optional, Callable, List, Dict, TYPE_CHECKING

from core.tk_queue import submit
from core.config import TRADE_HUBS, get_hub_config
from analytics.historical_profiles import ProfileManager
from gui.stockmarket.gui_stockmarket_settings import load_settings, save_settings
from gui.stockmarket.gui_stockmarket_hub import StockMarketHubPanel
from gui.stockmarket.gui_stockmarket_actions import StockMarketActionsMixin
from gui.stockmarket.gui_stockmarket_overlay import StockMarketOverlayMixin
from gui.stockmarket.gui_stockmarket_burst import StockMarketBurstMixin
from gui.stockmarket.gui_stockmarket_coldstart import StockMarketColdStartMixin, PhaseState
from analytics.stockmarket_filters import get_hub_burst_tracker

if TYPE_CHECKING:
    from core.api import ESIClient
    from history.archive_downloader import ArchiveDownloader
    from esi.esi_wallet import ESIWallet


class StockMarketTab(StockMarketActionsMixin, StockMarketOverlayMixin, StockMarketBurstMixin, StockMarketColdStartMixin):
    """Main Stock Market tab with sub-tabs per trading hub."""
    
    def __init__(
        self,
        notebook: ttk.Notebook,
        get_client: Optional[Callable[[], "ESIClient"]] = None,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.notebook = notebook
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        
        # Load settings
        self.settings = load_settings()

        # Optional callback fired when the master on/off flag flips, so an
        # external control (the main-window checkbutton) can mirror state when
        # the change originates here (e.g. the off-overlay "Turn On" button).
        # Registered via set_enabled_changed_callback() after construction.
        self._on_enabled_changed: Optional[Callable[[bool], None]] = None

        # Profile manager (shared across all hubs)
        self.profiles = ProfileManager(
            buy_percentile=self.settings.buy_percentile,
            sell_percentile=self.settings.sell_percentile,
            archive_path=self.settings.get_archive_path()
        )
        
        # Archive downloader
        from history.archive_downloader import ArchiveDownloader
        self.downloader = ArchiveDownloader(archive_path=self.settings.get_archive_path())
        self.profiles.archive_path = self.downloader.archive_path
        
        # Hub panels
        self.hub_panels: Dict[str, StockMarketHubPanel] = {}
        self._active_hub_key: Optional[str] = None
        # Last ESI-freshen timestamp per hub_key for holdings history.
        # Debounce repeated tab switches within _HOLDINGS_FRESHEN_MIN_INTERVAL.
        self._holdings_freshen_ts: Dict[str, datetime] = {}
        
        # Create main frame
        self.frame = ttk.Frame(notebook)
        notebook.add(self.frame, text="Stock Market")
        
        # Cold-start orchestrator state (read by locked overlay each tick).
        # Initialised before _create_widgets so the overlay can read it.
        self._init_cold_start()

        self._create_widgets()
        self.frame.after(50, self._restore_active_tab)
        # Defer status update to avoid blocking startup with slow DB queries
        self.frame.after(100, self._update_archive_status_safe)
        # Cold-start orchestrator now owns the entire startup flow
        # (ESI burst + MF + LI + panel refresh).  The legacy
        # _startup_refresh path stays in the file as a quick rollback
        # — re-enable it by un-commenting the after() call below if
        # the orchestrator regresses.
        # self.frame.after(500, self._startup_refresh)
        #
        # Master off-switch: when Stock Market is disabled we skip the
        # cold-start worker entirely (the whole point of the toggle is a
        # fast, work-free launch). The locked overlay shows the "off"
        # state instead; flipping the main-bar toggle on spawns the worker
        # live. See set_enabled.
        if self.settings.stock_market_enabled:
            self.frame.after(500, self._start_cold_start_worker)
        
        # Register callback so background import can trigger refresh
        # + material filter after profile building completes
        from history.background_import import set_profiles_ready_callback
        set_profiles_ready_callback(self._on_profiles_ready)
    
    def _create_widgets(self):
        """Create all widgets."""
        self._create_toolbar()
        self._create_hub_notebook()
        self._create_locked_overlay()
        
        # Start polling for lock state
        self._poll_lock_state()
    
    def _create_toolbar(self):
        """Create toolbar with action buttons."""
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        
        # NOTE: the master on/off switch lives on the MAIN scanner control bar
        # (gui_main_controls.py), not here — this toolbar is covered by the
        # locked overlay during cold-start load, so the switch has to sit
        # somewhere always-reachable to allow turning off mid-load. The buttons
        # below grey out when off via _set_dependent_controls_state.
        self.btn_add_item = ttk.Button(toolbar, text="Add Item", command=self._on_add_item)
        self.btn_add_item.pack(side=tk.LEFT, padx=2)
        self.btn_download_sde = ttk.Button(toolbar, text="Download SDE", command=self._on_download_sde)
        self.btn_download_sde.pack(side=tk.LEFT, padx=2)
        self.btn_refresh_prices = ttk.Button(toolbar, text="Refresh Prices", command=self._on_refresh_prices)
        self.btn_refresh_prices.pack(side=tk.LEFT, padx=2)
        self.btn_reset_profiles = ttk.Button(toolbar, text="Reset Profiles", command=self._on_reset_profiles)
        self.btn_reset_profiles.pack(side=tk.LEFT, padx=2)
        # Settings stays enabled while off so the toggle/config is reachable.
        ttk.Button(toolbar, text="Settings", command=self._on_settings).pack(side=tk.LEFT, padx=2)

        # Grey out dependent buttons if launched in the off state.
        self._set_dependent_controls_state(self.settings.stock_market_enabled)

        # Spacer
        ttk.Frame(toolbar).pack(side=tk.LEFT, expand=True)
        
        # SDE status
        self.sde_label = ttk.Label(toolbar, text="SDE: --", font=("Segoe UI", 8))
        self.sde_label.pack(side=tk.RIGHT, padx=5)
        
        # Percentile display
        self.percentile_label = ttk.Label(
            toolbar,
            text=f"P{self.settings.buy_percentile}/P{self.settings.sell_percentile}",
            font=("Segoe UI", 8),
            foreground="gray"
        )
        self.percentile_label.pack(side=tk.RIGHT, padx=5)
    
    def _create_hub_notebook(self):
        """Create notebook with sub-tabs for each hub."""
        self.hub_notebook = ttk.Notebook(self.frame)
        self.hub_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        self.hub_notebook.bind("<<NotebookTabChanged>>", self._on_hub_tab_changed)

        # Create a panel for each enabled hub.
        # Custom stations only appear here if in_stock_market is True.
        for hub_key, config in TRADE_HUBS.items():
            if not config.get("enabled", True):
                continue
            if config.get("custom") and not config.get("in_stock_market"):
                continue

            # Frame for this hub's tab
            hub_frame = ttk.Frame(self.hub_notebook)
            self.hub_notebook.add(hub_frame, text=config["name"])

            # Create the hub panel
            panel = StockMarketHubPanel(
                parent=hub_frame,
                hub_key=hub_key,
                settings=self.settings,
                profiles=self.profiles,
                get_client=self.get_client,
                set_status=self.set_status,
            )
            self.hub_panels[hub_key] = panel
    
    def _get_current_hub_panel(self) -> Optional[StockMarketHubPanel]:
        """Get the currently selected hub panel."""
        try:
            current_tab = self.hub_notebook.index(self.hub_notebook.select())
            hub_keys = list(self.hub_panels.keys())
            if current_tab < len(hub_keys):
                return self.hub_panels.get(hub_keys[current_tab])
        except Exception:
            pass
        return None

    def _get_current_hub_key(self) -> Optional[str]:
        """Get the currently selected hub key."""
        try:
            current_tab = self.hub_notebook.index(self.hub_notebook.select())
            hub_keys = list(self.hub_panels.keys())
            if current_tab < len(hub_keys):
                return hub_keys[current_tab]
        except Exception:
            pass
        return None

    def remove_hub_tab(self, hub_key: str):
        """Remove a custom station's tab from the stock market notebook."""
        if hub_key not in self.hub_panels:
            return
        hub_keys = list(self.hub_panels.keys())
        idx = hub_keys.index(hub_key)
        try:
            tab_ids = self.hub_notebook.tabs()
            if idx < len(tab_ids):
                self.hub_notebook.forget(tab_ids[idx])
        except Exception:
            pass
        del self.hub_panels[hub_key]

    def add_hub_tab(self, hub_key: str):
        """Dynamically add a stock market tab for a newly registered custom station."""
        if hub_key in self.hub_panels:
            return
        config = get_hub_config(hub_key)
        hub_frame = ttk.Frame(self.hub_notebook)
        self.hub_notebook.add(hub_frame, text=config["name"])
        panel = StockMarketHubPanel(
            parent=hub_frame,
            hub_key=hub_key,
            settings=self.settings,
            profiles=self.profiles,
            get_client=self.get_client,
            set_status=self.set_status,
        )
        self.hub_panels[hub_key] = panel
    
    def _restore_active_tab(self):
        """Select the last-active hub tab from saved settings."""
        saved = self.settings.active_hub_key
        if not saved:
            return
        hub_keys = list(self.hub_panels.keys())
        if saved in hub_keys:
            idx = hub_keys.index(saved)
            try:
                self.hub_notebook.select(idx)
                self._active_hub_key = saved
            except Exception:
                pass

    _HOLDINGS_FRESHEN_MIN_INTERVAL = 60  # seconds; rapid tab-switch debounce

    def _on_hub_tab_changed(self, event=None):
        if self._stock_market_off():
            return
        import time as _pt
        _pt0 = _pt.perf_counter()
        hub_key = self._get_current_hub_key()
        if not hub_key or hub_key == self._active_hub_key:
            return
        self._active_hub_key = hub_key
        self.settings.active_hub_key = hub_key
        _ts = _pt.perf_counter()
        save_settings(self.settings)
        _step_save_settings = _pt.perf_counter() - _ts
        panel = self.hub_panels.get(hub_key)
        if not panel:
            return
        client = self.get_client() if self.get_client else None
        _step_render_cache = 0.0
        if client:
            _ts = _pt.perf_counter()
            panel.render_from_cache(client.order_cache)
            _step_render_cache = _pt.perf_counter() - _ts
        _step_first_render = 0.0
        if not getattr(panel, "_has_rendered_once", False):
            _ts = _pt.perf_counter()
            panel.refresh_display_async()
            _step_first_render = _pt.perf_counter() - _ts
            panel._has_rendered_once = True
        # Close the 1-4 day SQLite lag on holdings by force-pulling ESI
        # history for tracked items in this region. Refreshes the holdings
        # panel once fresh data lands.
        _ts = _pt.perf_counter()
        self._freshen_holdings_for_hub(hub_key, panel, client)
        _step_freshen_spawn = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] _on_hub_tab_changed hub={hub_key} total={_pt_total*1000:.0f}ms "
            f"save_settings={_step_save_settings*1000:.0f}ms "
            f"render_from_cache={_step_render_cache*1000:.0f}ms "
            f"first_render_kick={_step_first_render*1000:.0f}ms "
            f"freshen_spawn={_step_freshen_spawn*1000:.0f}ms"
        )

    def _freshen_holdings_for_hub(self, hub_key, panel, client):
        if self._stock_market_off():
            return
        if not client:
            return
        holdings_panel = getattr(panel, "holdings_panel", None)
        if holdings_panel is None:
            return
        type_ids = [e.type_id for e in holdings_panel.holdings.get_all()]
        if not type_ids:
            return
        now = datetime.now(timezone.utc)
        last = self._holdings_freshen_ts.get(hub_key)
        if last is not None:
            age = (now - last).total_seconds()
            if age < self._HOLDINGS_FRESHEN_MIN_INTERVAL:
                return
        self._holdings_freshen_ts[hub_key] = now
        region_id = holdings_panel.region_id

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def do_freshen():
                    client.ensure_session()
                    client.reset_for_new_loop()
                    await client.freshen_history_for_items(region_id, type_ids)
                loop.run_until_complete(do_freshen())
                submit(holdings_panel._schedule_refresh)
            except Exception as e:
                print(f"[StockMarket-{hub_key}] holdings ESI freshen failed: {e}")
            finally:
                loop.close()

        threading.Thread(target=run, daemon=True).start()

    # =========================================================================
    # Status Updates
    # =========================================================================
    
    def _update_archive_status_safe(self):
        """Deferred SDE status update - runs after GUI loads."""
        from sde.sde_manager import get_sde_manager
        
        def update_in_background():
            # Get SDE info
            sde = get_sde_manager()
            if sde.is_available():
                info = sde.get_version_info()
                count = info.get("record_count", 0)
                sde_text = f"SDE: {count:,}"
            else:
                sde_text = "SDE: Not installed"
            
            # Update GUI from main thread via task queue
            submit(lambda: self.sde_label.configure(text=sde_text))
        
        # Show loading state immediately
        self.sde_label.configure(text="SDE: ...")
        
        # Run in background
        threading.Thread(target=update_in_background, daemon=True).start()
    
    def _update_sde_status(self):
        """Update SDE status label."""
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        if sde.is_available():
            info = sde.get_version_info()
            count = info.get("record_count", 0)
            self.sde_label.configure(text=f"SDE: {count:,}")
        else:
            self.sde_label.configure(text="SDE: Not installed")

    # =========================================================================
    # Master on/off switch
    # =========================================================================

    def _set_dependent_controls_state(self, enabled: bool):
        """Enable/disable the toolbar buttons that only make sense when on."""
        state = tk.NORMAL if enabled else tk.DISABLED
        for btn in (
            getattr(self, "btn_add_item", None),
            getattr(self, "btn_download_sde", None),
            getattr(self, "btn_refresh_prices", None),
            getattr(self, "btn_reset_profiles", None),
        ):
            if btn is not None:
                btn.configure(state=state)

    def set_enabled(self, enabled: bool):
        """Master switch entry point. Driven by the main-window checkbutton and
        the off-overlay 'Turn On' button. Persists the flag, greys/un-greys the
        dependent toolbar buttons, and either starts cold-start live (on) or
        shows the off overlay (off)."""
        enabled = bool(enabled)
        self.settings.stock_market_enabled = enabled
        save_settings(self.settings)
        self._set_dependent_controls_state(enabled)

        # Bump the cold-start token on every flip: this cancels any worker
        # still running (it bails at its next checkpoint) and gives a fresh
        # run its own token. See _cold_start_check_cancel.
        self._cold_start_token += 1

        if enabled:
            print("[StockMarket] Enabled — starting cold-start worker")
            # Reset phase state so the locked overlay re-locks and shows
            # build progress during the fresh cold-start (a prior run left
            # phase_state.done=True).
            self.phase_state = PhaseState()
            self._show_phase_progress_overlay(self.phase_state)  # immediate lock
            self._start_cold_start_worker()
        else:
            print("[StockMarket] Disabled — auto-processing paused")
            # No need to interrupt any in-flight worker; every auto entry
            # point is gated on settings.stock_market_enabled and will no-op
            # from here. Show the off overlay immediately; the lock poll
            # keeps it there.
            self._show_off_overlay()

        # Mirror state to any external control (main-window checkbutton) when
        # the change originated here (overlay button). Setting a BooleanVar
        # programmatically does not re-fire its checkbutton command, so no loop.
        if self._on_enabled_changed:
            try:
                self._on_enabled_changed(enabled)
            except Exception as e:
                print(f"[StockMarket] enabled-changed callback error: {e}")

    def is_enabled(self) -> bool:
        """Current master on/off state."""
        return not self._stock_market_off()

    def set_enabled_changed_callback(self, callback: Optional[Callable[[bool], None]]):
        """Register a callback invoked with the new state whenever the master
        switch is flipped from inside the tab (e.g. the overlay button)."""
        self._on_enabled_changed = callback

    def _stock_market_off(self) -> bool:
        """True when the master switch is off — gate for all auto entry points."""
        return not getattr(self.settings, "stock_market_enabled", True)

    # =========================================================================
    # External API
    # =========================================================================
    
    def refresh_current_hub_prices(self):
        """Public wrapper for external refresh triggers (scan complete, ESI sync)."""
        if self._stock_market_off():
            return
        self._on_refresh_prices()
    
    def add_item_from_external(self, type_id: int, region_id: int, station_id: int, type_name: str = ""):
        """Add item from external source to appropriate hub."""
        # Find the hub for this region
        for hub_key, config in TRADE_HUBS.items():
            if config["region_id"] == region_id:
                panel = self.hub_panels.get(hub_key)
                if panel:
                    panel.add_item(type_id, type_name)
                    # Switch to that hub's tab
                    for i, (hk, _) in enumerate([(k, c) for k, c in TRADE_HUBS.items() if c.get("enabled", True)]):
                        if hk == hub_key:
                            self.hub_notebook.select(i)
                            break
                return
    
    def update_from_local_orders(self, orders: List[dict], region_id: Optional[int] = None):
        """Update prices from scan results.

        Args:
            orders: List of market orders from scan
            region_id: Region the orders are from (if known)
        """
        if self._stock_market_off():
            return
        import time as _pt
        _pt0 = _pt.perf_counter()
        if not orders:
            print(f"[StockMarket] update_from_local_orders: No orders provided")
            return

        # Build price dict: type_id -> lowest sell price
        _ts = _pt.perf_counter()
        prices: Dict[int, float] = {}

        for order in orders:
            if order.get("is_buy_order"):
                continue

            type_id = order["type_id"]
            price = order["price"]

            if type_id not in prices or price < prices[type_id]:
                prices[type_id] = price
        _step_scan = _pt.perf_counter() - _ts

        if not prices:
            print(f"[StockMarket] update_from_local_orders: No sell orders found in {len(orders)} orders")
            return

        print(f"[StockMarket] update_from_local_orders: {len(prices)} live prices from region {region_id}")

        # If region specified, update that hub only
        _ts = _pt.perf_counter()
        _hubs_updated = 0
        if region_id:
            for hub_key, panel in self.hub_panels.items():
                config = get_hub_config(hub_key)
                if config["region_id"] == region_id:
                    print(f"[StockMarket] -> Updating {hub_key} hub")
                    panel.update_live_prices(prices)
                    _hubs_updated = 1
                    _step_apply = _pt.perf_counter() - _ts
                    _pt_total = _pt.perf_counter() - _pt0
                    print(
                        f"[PerfTimer] StockMarketTab.update_from_local_orders "
                        f"total={_pt_total*1000:.0f}ms orders={len(orders)} prices={len(prices)} hubs_updated={_hubs_updated} "
                        f"scan_orders={_step_scan*1000:.0f}ms apply_live_prices={_step_apply*1000:.0f}ms"
                    )
                    return

        # Otherwise update all hubs (legacy behavior)
        print(f"[StockMarket] -> Updating all hubs (no region specified)")
        for panel in self.hub_panels.values():
            panel.update_live_prices(prices)
            _hubs_updated += 1
        _step_apply = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] StockMarketTab.update_from_local_orders "
            f"total={_pt_total*1000:.0f}ms orders={len(orders)} prices={len(prices)} hubs_updated={_hubs_updated} "
            f"scan_orders={_step_scan*1000:.0f}ms apply_live_prices={_step_apply*1000:.0f}ms"
        )
    
    def sync_orders_to_holdings(self, orders: List[dict], region_id: int):
        """Sync ESI orders to holdings for appropriate hub."""
        if self._stock_market_off():
            return
        for hub_key, panel in self.hub_panels.items():
            config = get_hub_config(hub_key)
            if config["region_id"] == region_id:
                panel.sync_from_orders(orders)
                return
    
    def sync_wallet_to_holdings(self, wallet: "ESIWallet"):
        """Sync ESI wallet transactions to holdings for all hubs.
        
        Called on each ESI refresh cycle. Each hub panel checks transactions
        for items in its holdings and updates buy/sell records.
        
        Args:
            wallet: ESIWallet instance with fetched transactions
        """
        if self._stock_market_off():
            return
        if not wallet or not wallet.transactions:
            return
        
        total_buys = 0
        total_sales = 0
        
        for hub_key, panel in self.hub_panels.items():
            try:
                results = panel.sync_from_esi_wallet(wallet)
                total_buys += results.get("buys_synced", 0)
                total_sales += results.get("sales_synced", 0)
            except Exception as e:
                print(f"[StockMarket] Holdings sync error for {hub_key}: {e}")
        
        if total_buys > 0 or total_sales > 0:
            print(f"[StockMarket] Holdings synced: {total_buys} buys, {total_sales} sales")
    
    def sync_orders_to_pnl(self, char_orders: List[dict], wallet: "ESIWallet"):
        """Sync character orders to P&L tracking for fee calculation.

        Called on each ESI refresh cycle. Tracks:
        - New buy/sell order placements (broker fees) — sourced from wallet journal
        - Escrow committed in open buy orders — recomputed each refresh
        - Order price modifications (relist fees) — TODO: still skill-estimate
        - Completed sales (sales tax) — sourced from wallet journal

        Retry policy: if a journal entry isn't found for a fresh order/transaction,
        skip recording this refresh; try again next time. After JOURNAL_AGE_LIMIT_DAYS,
        fall back to a skill-based estimate (journal retention ~30 days).
        """
        if self._stock_market_off():
            return
        import time as _pt
        _pt0 = _pt.perf_counter()
        if not char_orders and not wallet:
            return

        from sde.sde_manager import get_sde_manager
        from core.calculate import (
            calculate_broker_fee, calculate_sales_tax, load_cached_skills,
        )
        sde = get_sde_manager()

        # Past this age, the journal entry has rolled off ESI retention (~30d).
        # Fall back to the skill-based estimate so the order doesn't retry forever.
        JOURNAL_AGE_LIMIT_DAYS = 25

        now = datetime.now(timezone.utc)

        for hub_key, panel in self.hub_panels.items():
            if not panel.pnl_panel:
                continue

            try:
                pnl = panel.pnl_panel.get_pnl_manager()
                config = get_hub_config(hub_key)
                station_id = config["station_id"]

                # Get holdings type_ids for this hub (only track items in holdings)
                holdings_type_ids = set()
                if panel.holdings_panel:
                    holdings_type_ids = set(panel.holdings_panel.holdings.get_type_ids())

                if not holdings_type_ids:
                    continue

                # Skills used for estimate-fallback only (loaded once per hub).
                skills = load_cached_skills(hub_key, slot="seller")

                # Filter dict-shaped orders to this hub's holdings (for mod check)
                hub_orders = [
                    o for o in char_orders
                    if o.get("type_id") in holdings_type_ids
                ]

                # Reset escrow for all entries; we'll re-add from current orders below.
                pnl.reset_escrow()

                # Check for order modifications (price changes). Uses journal-sourced
                # fees with the same retry/estimate-fallback policy as placement fees.
                if hub_orders:
                    mod_fees = pnl.check_order_modifications(
                        hub_orders, holdings_type_ids,
                        wallet=wallet, age_limit_days=JOURNAL_AGE_LIMIT_DAYS,
                    )
                    if mod_fees:
                        print(f"[PnL-{hub_key}] Recorded {len(mod_fees)} order modification(s)")

                # Process active orders: broker fees + escrow
                if wallet and wallet.orders:
                    for order in wallet.orders:
                        if order.type_id not in holdings_type_ids:
                            continue
                        if order.location_id != station_id:
                            continue

                        type_name = sde.get_type_name(order.type_id) or f"Type {order.type_id}"

                        # Escrow: accumulate per-type from currently-active buy orders
                        if order.is_buy_order and order.escrow > 0:
                            pnl.add_escrow(order.type_id, type_name, order.escrow)

                        # Broker fee: only if we haven't already recorded this order
                        already_processed = (
                            pnl.is_buy_order_processed(order.order_id, order.type_id)
                            if order.is_buy_order
                            else pnl.is_sell_order_processed(order.order_id, order.type_id)
                        )
                        if already_processed:
                            continue

                        # Look up actual broker fee from journal
                        fee = wallet.get_broker_fee_for_order(
                            order.order_id, issued=order.issued
                        )

                        if fee <= 0:
                            # Journal entry not found yet. If the order is older than
                            # the journal retention window, fall back to estimate;
                            # otherwise skip and retry next refresh.
                            age_days = (now - order.issued).days
                            if age_days < JOURNAL_AGE_LIMIT_DAYS:
                                continue
                            fee = calculate_broker_fee(order.price, order.volume_remain, skills)
                            print(f"[PnL-{hub_key}] Order {order.order_id} age={age_days}d — "
                                  f"journal entry gone, using estimate {fee:,.0f} ISK")

                        if order.is_buy_order:
                            pnl.record_buy_order(
                                order.order_id, order.type_id, type_name,
                                order.price, order.volume_remain, fee_amount=fee,
                            )
                        else:
                            pnl.record_sell_order(
                                order.order_id, order.type_id, type_name,
                                order.price, order.volume_remain, fee_amount=fee,
                            )

                # Process transactions: buys (cost basis only) and sales (with tax)
                if wallet and wallet.transactions:
                    for tx in wallet.transactions:
                        if tx.type_id not in holdings_type_ids:
                            continue
                        if tx.location_id != station_id:
                            continue
                        if pnl.is_transaction_processed(tx.transaction_id, tx.type_id):
                            continue

                        type_name = sde.get_type_name(tx.type_id) or f"Type {tx.type_id}"

                        if tx.is_buy:
                            # No fee on fills — broker fee (if any) was recorded at
                            # order placement; instant buys have no fee at all.
                            pnl.record_buy_fill(
                                tx.transaction_id, tx.type_id, type_name,
                                tx.quantity, tx.unit_price,
                            )
                        else:
                            # Look up actual sales tax from journal
                            tax = wallet.get_sales_tax_for_transaction(tx.journal_ref_id)

                            if tax <= 0:
                                age_days = (now - tx.date).days
                                if age_days < JOURNAL_AGE_LIMIT_DAYS:
                                    continue
                                tax = calculate_sales_tax(tx.unit_price, tx.quantity, skills)
                                print(f"[PnL-{hub_key}] Tx {tx.transaction_id} age={age_days}d — "
                                      f"journal entry gone, using estimate tax {tax:,.0f} ISK")

                            pnl.record_sale(
                                tx.transaction_id, tx.type_id, type_name,
                                tx.quantity, tx.unit_price, tax_amount=tax,
                            )

                # Refresh P&L display
                panel.pnl_panel.refresh_display()

            except Exception as e:
                print(f"[StockMarket] P&L sync error for {hub_key}: {e}")
                import traceback
                traceback.print_exc()
        _pt_total = _pt.perf_counter() - _pt0
        _hub_count = len(self.hub_panels)
        _order_count = len(char_orders) if char_orders else 0
        print(
            f"[PerfTimer] sync_orders_to_pnl total={_pt_total*1000:.0f}ms "
            f"hubs={_hub_count} char_orders={_order_count}"
        )

    def fetch_history_for_region(self, region_id: int):
        """Fetch market history for all profiled items in a region.
        
        Uses the same data flow as the scanner:
        bulk_history -> ESI supplement -> ESI API fallback
        
        This ensures Stock Market has trend data for all profiled items,
        not just items that were candidates in the scan.
        """
        if self._stock_market_off():
            return
        if not self.get_client:
            print("[StockMarket] No client available for history fetch")
            return
        
        client = self.get_client()
        if not client:
            print("[StockMarket] Client is None")
            return
        
        # Get all profiled type_ids for this region
        all_profiles = self.profiles.get_all_profiles()
        region_profiles = [p for p in all_profiles if p.region_id == region_id]
        
        if not region_profiles:
            print(f"[StockMarket] No profiles for region {region_id}")
            return
        
        type_ids = [p.type_id for p in region_profiles]
        print(f"[StockMarket] Fetching history for {len(type_ids)} profiled items in region {region_id}")
        
        # Run async fetch in thread
        def run_fetch():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def do_fetch():
                    client.ensure_session()
                    client.reset_for_new_loop()
                    return await client.get_market_history_bulk(region_id, type_ids, use_cache=True)
                
                result = loop.run_until_complete(do_fetch())
                
                # Count results
                has_data = sum(1 for h in result.values() if h)
                empty = sum(1 for h in result.values() if not h)
                print(f"[StockMarket] History fetch complete: {has_data} with data, {empty} empty")
                
                # Refresh display on main thread
                if hasattr(self, 'frame'):
                    submit(self.refresh_display)
                    
            except Exception as e:
                print(f"[StockMarket] History fetch error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                loop.close()
        
        thread = threading.Thread(target=run_fetch, daemon=True)
        thread.start()
    
    def reload_filters_from_cache(self):
        """Reload fee rates from cached skills JSON for all hub panels.
        
        Called after tracking tab completes ESI refresh.
        """
        for panel in self.hub_panels.values():
            panel.reload_filters_from_cache()
    
    def refresh_display(self):
        """Refresh all hub panels."""
        for panel in self.hub_panels.values():
            panel.refresh_display()
    
    def _refresh_display_async(self):
        """Refresh all hub panels asynchronously (non-blocking).
        
        Used for startup and deferred refreshes.
        """
        for panel in self.hub_panels.values():
            panel.refresh_display_async()
    
    def _startup_refresh(self):
        """Initial load: pull stale hubs then run material filter + LI.

        On first launch of the day any hub whose orders are older than 24h
        triggers the full sequence:
          1. Notebook-wide overlay shows while orders are pulled from ESI.
          2. Overlay hides; per-hub MF + LI phases run (with per-hub overlays).

        If all hubs are already fresh the overlay is skipped and MF + LI
        run directly (their own once-per-day trackers decide whether to
        compute or skip to refresh).
        """
        client = self.get_client() if self.get_client else None
        stale = self._get_stale_hubs(client) if client else []

        if stale:
            self._show_burst_overlay("Preparing...", 0, len(stale))

            def run_burst():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    async def do_burst():
                        client.ensure_session()
                        client.reset_for_new_loop()
                        total = len(stale)
                        tracker = get_hub_burst_tracker()
                        for i, (hub_key, region_id, hub_name) in enumerate(stale, 1):
                            submit(lambda n=hub_name, idx=i, t=total:
                                   self._update_burst_overlay(n, idx, t))
                            try:
                                print(f"[StockMarket] Startup pull: {hub_key}")
                                await client.get_market_orders(region_id)
                                tracker.mark_complete(hub_key)
                            except Exception as e:
                                print(f"[StockMarket] Startup pull failed "
                                      f"for {hub_key}: {e}")
                    loop.run_until_complete(do_burst())
                finally:
                    loop.close()
                submit(self._hide_burst_overlay)
                submit(self._apply_material_filter_all)

            threading.Thread(target=run_burst, daemon=True,
                             name="StartupBurst").start()
        else:
            self._apply_material_filter_all()

    def _on_profiles_ready(self):
        """Called when background import finishes building profiles."""
        print("[StockMarket] Profiles ready - running material filter")
        self._apply_material_filter_all()

    def _apply_material_filter_all(self):
        """Run material filter on every hub panel.

        Each panel checks its own once-per-day tracker.  If the filter
        already ran today for a hub, that hub falls back to a normal
        async refresh (reading existing cached results).
        """
        if self._stock_market_off():
            return
        print(f"[StockMarket] === _apply_material_filter_all: "
              f"{len(self.hub_panels)} hubs ===")
        for panel in self.hub_panels.values():
            panel.apply_material_filter()

    # =========================================================================
    # Burst overlay (notebook-wide, used during startup order pull)
    # =========================================================================

    def _show_burst_overlay(self, message: str, current: int, total: int):
        """Show notebook-wide overlay while pulling orders at startup."""
        if not hasattr(self, '_burst_overlay_frame'):
            self._burst_overlay_frame = ttk.Frame(self.frame)
            center = ttk.Frame(self._burst_overlay_frame)
            center.place(relx=0.5, rely=0.4, anchor=tk.CENTER)
            ttk.Label(
                center, text="Loading Market Data",
                font=("Segoe UI", 13, "bold")
            ).pack(pady=(0, 12))
            self._burst_status_var = tk.StringVar(value=message)
            ttk.Label(
                center, textvariable=self._burst_status_var,
                font=("Segoe UI", 10)
            ).pack(pady=(0, 8))
            self._burst_progress_var = tk.DoubleVar(value=0)
            self._burst_progress = ttk.Progressbar(
                center, variable=self._burst_progress_var,
                length=300, mode="determinate"
            )
            self._burst_progress.pack(pady=(0, 6))
            self._burst_detail_var = tk.StringVar(value="")
            ttk.Label(
                center, textvariable=self._burst_detail_var,
                font=("Segoe UI", 9), foreground="gray"
            ).pack()

        if total > 0:
            self._burst_progress.configure(maximum=total)
            self._burst_progress_var.set(0)
        self._burst_status_var.set(message)
        self._burst_detail_var.set(f"0 of {total} hubs")
        self._burst_overlay_frame.place(
            in_=self.frame, relx=0, rely=0, relwidth=1.0, relheight=1.0
        )
        self._burst_overlay_frame.lift()

    def _update_burst_overlay(self, hub_name: str, current: int, total: int):
        """Update burst overlay progress. Main thread only."""
        if not hasattr(self, '_burst_status_var'):
            return
        self._burst_status_var.set(f"Loading {hub_name}...")
        self._burst_progress_var.set(current - 1)
        self._burst_detail_var.set(f"Hub {current} of {total}")

    def _hide_burst_overlay(self):
        """Hide the startup burst overlay."""
        if hasattr(self, '_burst_overlay_frame'):
            self._burst_overlay_frame.place_forget()

    # =========================================================================
    # Stale-hub helper (shared by startup and post-scanner burst)
    # =========================================================================

    def _get_stale_hubs(self, client) -> list:
        """Return (hub_key, region_id, hub_name) for hubs with cache > 24h old."""
        now = datetime.now(timezone.utc)
        stale = []
        for hub_key, config in TRADE_HUBS.items():
            if not config.get("enabled", True):
                continue
            region_id = config["region_id"]
            entry = client.order_cache._order_cache.get(region_id)
            if entry and entry.get('timestamp'):
                age = (now - entry['timestamp']).total_seconds()
                if age < 86400:
                    print(f"[StockMarket] {hub_key}: cache fresh ({age/3600:.1f}h old)")
                    continue
                print(f"[StockMarket] {hub_key}: cache stale ({age/3600:.1f}h old) - will pull")
            else:
                print(f"[StockMarket] {hub_key}: no cache entry - will pull")
            stale.append((hub_key, region_id, config["name"]))
        return stale

    # =========================================================================
    # Daily hub burst (called after each scanner tick)
    # =========================================================================

    def _on_scanner_tick_complete(self):
        """Called by the scanner after each scan finishes."""
        if self._stock_market_off():
            return
        self._run_daily_hub_burst()
        hub_key = self._active_hub_key or self._get_current_hub_key()
        if hub_key:
            client = self.get_client() if self.get_client else None
            if client:
                panel = self.hub_panels.get(hub_key)
                if panel:
                    self._pull_active_region_if_stale(hub_key, panel, client)
