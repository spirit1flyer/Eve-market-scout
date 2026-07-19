"""Trade Tracking tab for EVE Market Scout - ESI integration and profit tracking."""

import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from esi.esi_auth import ESIAuth
from esi.esi_wallet import ESIWallet
from esi.esi_skills import ESISkills, ESIStandings
from analytics.trade_tracker import TradeTracker
from scanning.scanner_inventory import InventoryManager
from scanning.scanner_inventory_sync import sync_inventory_from_wallet
from core.calculate import (
    TradingSkills, DEFAULT_SKILLS, format_isk, get_skill_summary,
    load_cached_skills
)

# Import sync manager
from gui.tracking.gui_tracking_sync import ESISyncManager

# Import panel components
from gui.tracking.gui_tracking_panels import SummaryPanel, StandingsBar
from gui.gui_tree_utils import sort_treeview

# Import underbid monitoring
from scanning.underbid_monitor import UnderbidMonitor
from core.tk_queue import submit


# Column configuration for inventory treeview (Step 3 swap from per-deal to per-item).
TRADE_COLUMNS = ("item", "status", "held", "listed", "avg_buy", "list_price", "profit", "fees")
TRADE_COL_TITLES = {
    "item": "Item",
    "status": "Status",
    "held": "Held",
    "listed": "Listed",
    "avg_buy": "Avg Buy",
    "list_price": "List Price",
    "profit": "Profit",
    "fees": "Total Fees"
}
TRADE_NUMERIC_COLS = {"held", "listed", "avg_buy", "list_price", "profit", "fees"}


class TrackingTabManager:
    """Manages the Trade Tracking tab with ESI integration."""

    def __init__(self, notebook: ttk.Notebook, set_status: Callable[[str], None],
                 get_client: Callable = None):
        self.notebook = notebook
        self.set_status = set_status
        self.get_client = get_client
        
        # Selected hub (can be changed by gui_main)
        self.selected_hub = "amarr"
        
        # Hub overrides for cross-hub mode (set by gui_main)
        self._sell_hub_override: str = None
        self._buy_hub_override: str = None
        
        # Callback for when characters change (set by gui_main)
        self._on_characters_changed: Callable = None
        
        # ESI components
        self.auth = ESIAuth.singleton()
        self.wallet: Optional[ESIWallet] = None
        self.esi_skills: Optional[ESISkills] = None
        self.esi_standings: Optional[ESIStandings] = None
        self.skills: TradingSkills = DEFAULT_SKILLS
        self.buyer_skills: TradingSkills = DEFAULT_SKILLS  # For cross-hub
        
        # Trade tracker (with skills for accurate fee calcs)
        self.tracker = TradeTracker(self.skills)

        # Scanner inventory (per-hub FIFO tracker -- Step 3 UI now reads this).
        self.inventory = InventoryManager(self.selected_hub)

        # One-time backfill: legacy TradeTracker pending/listed/sold trades
        # predate the inventory system. Flag any missing type_ids so they
        # appear in the new view and pick up ESI data on next sync.
        self._backfill_inventory_from_tracker()

        # Sync manager
        self.sync_manager = ESISyncManager(self.tracker, set_status,
                                           get_client=get_client)

        # Underbid monitoring -- now keyed by type_id, fed by inventory listings
        # in _on_esi_refresh. Seed ignored set from persisted entry flags.
        self.underbid_monitor = UnderbidMonitor()
        self.underbid_monitor.seed_ignored_from_inventory(self.inventory.all_entries())
        
        # Stock market tab reference (set by gui_main after creation)
        self.stock_market_tab = None
        self.market_orders_cache: list[dict] = []  # Cached market orders for underbid checks
        
        # Sort state tracking
        self.sort_state: dict[str, bool] = {}
        
        if self.auth.is_authenticated:
            self.wallet = ESIWallet(self.auth)
            self.esi_skills = ESISkills(self.auth)
            self.esi_standings = ESIStandings(self.auth, self.esi_skills)
            self.sync_manager.set_wallet(self.wallet)
            # Load cached skills from JSON (no ESI call - skills rarely change)
            cached = load_cached_skills(self.selected_hub, "seller")
            if cached is not DEFAULT_SKILLS:
                self.skills = cached
                self.tracker.set_skills(self.skills)
            if self.auth.has_buyer:
                cached_buyer = load_cached_skills(self.selected_hub, "buyer")
                if cached_buyer is not DEFAULT_SKILLS:
                    self.buyer_skills = cached_buyer
        
        self._create_tab()
        
        # Wire up sync manager UI refs
        self.sync_manager.set_ui_refs(
            self.frame,
            self.refresh_btn,
            self.countdown_label,
            self._on_esi_refresh
        )
        
        # Wire up underbid monitor to sync manager
        self.sync_manager.set_underbid_monitor(self.underbid_monitor, self.selected_hub)
        
        # Wire up stock market holdings sync (callback set after stock_market_tab assigned)
        # This gets called by gui_main after creation
        
        # Start auto-refresh if authenticated
        if self.auth.is_authenticated and self.auto_refresh_var.get():
            self.sync_manager.schedule_auto_refresh()
    
    def _setup_stock_market_sync(self):
        """Wire up stock market holdings sync after stock_market_tab is assigned."""
        if self.stock_market_tab:
            # Sync active orders (for order count display)
            self.sync_manager.set_stock_market_callback(
                self.stock_market_tab.sync_orders_to_holdings
            )
            # Sync wallet transactions (for buy/sell tracking)
            self.sync_manager.set_wallet_sync_callback(
                self.stock_market_tab.sync_wallet_to_holdings
            )
            # Sync orders + wallet to P&L tracking (broker fees, sales tax, mods)
            self.sync_manager.set_pnl_sync_callback(
                self.stock_market_tab.sync_orders_to_pnl
            )

    def refresh_all_character_data(self, sell_hub: str = None, buy_hub: str = None):
        """Refresh skills and standings for both characters.
        
        Now delegates to _refresh_skills_async to avoid blocking main thread.
        
        Args:
            sell_hub: Hub key for seller standings (defaults to selected_hub)
            buy_hub: Hub key for buyer standings (defaults to sell_hub for same-station)
        """
        # Store hub overrides for the async method to use
        if sell_hub:
            self._sell_hub_override = sell_hub
        if buy_hub:
            self._buy_hub_override = buy_hub
        
        # Delegate to async version
        self._refresh_skills_async()
    
    def _save_cached_skills(self):
        """Save skills + standings for all hubs to JSON for Stock Market use."""
        if not self.esi_standings:
            return
        
        from core.calculate import save_cached_skills
        
        # Collect standings for all 5 hubs
        hub_keys = ["amarr", "jita", "dodixie", "hek", "rens"]
        
        # base=True: these standings feed broker fee math, which uses raw
        # standings (Connections/Diplomacy don't reduce fees in-game).
        seller_standings = {}
        for hub in hub_keys:
            corp, faction = self.esi_standings.get_standings_for_hub(
                hub, slot="seller", base=True)
            seller_standings[hub] = (corp, faction)

        buyer_standings = None
        if self.auth.has_buyer:
            buyer_standings = {}
            for hub in hub_keys:
                corp, faction = self.esi_standings.get_standings_for_hub(
                    hub, slot="buyer", base=True)
                buyer_standings[hub] = (corp, faction)
        
        save_cached_skills(
            seller_skills=self.skills,
            seller_standings=seller_standings,
            seller_name=self.auth.seller_name if hasattr(self.auth, 'seller_name') else "",
            buyer_skills=self.buyer_skills if self.auth.has_buyer else None,
            buyer_standings=buyer_standings,
            buyer_name=self.auth.buyer_name if hasattr(self.auth, 'buyer_name') else "",
        )

    def _create_tab(self):
        """Create the tracking tab."""
        self.frame = ttk.Frame(self.notebook)
        self.notebook.add(self.frame, text="Tracking")
        
        # Top bar - auth status and controls
        self._create_auth_bar()
        
        # Standings bar
        self.standings_bar = StandingsBar(
            self.frame,
            on_standings_changed=self._on_standings_changed,
            on_fees_changed=self._on_fees_changed
        )
        
        # Main content - split into left (summary) and right (trades list)
        self.content_frame = ttk.Frame(self.frame)
        self.content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.summary_panel = SummaryPanel(self.content_frame)
        self._create_trades_panel()
        
        # Initial display update
        self._update_auth_display()
        self.standings_bar.update(self.skills)
        self._refresh_display()

    def _create_auth_bar(self):
        """Create authentication status bar with dual character support."""
        auth_frame = ttk.Frame(self.frame, padding=5)
        auth_frame.pack(fill=tk.X)
        
        # === Row 1: Seller (Primary) Character ===
        seller_row = ttk.Frame(auth_frame)
        seller_row.pack(fill=tk.X, pady=(0, 2))
        
        ttk.Label(seller_row, text="Seller:", font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
        
        self.seller_status = ttk.Label(
            seller_row, text="Not logged in",
            font=("Segoe UI", 9), width=25, anchor=tk.W
        )
        self.seller_status.pack(side=tk.LEFT, padx=5)
        
        self.seller_logout_btn = ttk.Button(
            seller_row, text="Logout",
            command=lambda: self._logout("seller"), width=7
        )
        self.seller_logout_btn.pack(side=tk.LEFT, padx=2)
        
        self.seller_login_btn = ttk.Button(
            seller_row, text="Login",
            command=lambda: self._show_login_dialog("seller"), width=7
        )
        self.seller_login_btn.pack(side=tk.LEFT, padx=2)
        
        # Trade Refresh button (wallet, orders, transactions)
        self.refresh_btn = ttk.Button(
            seller_row, text="Trade Refresh",
            command=lambda: self.sync_manager.refresh_esi_data(), width=14
        )
        self.refresh_btn.pack(side=tk.LEFT, padx=5)
        
        # Refresh Skills button (manual, not on ESI cycle)
        self.skills_btn = ttk.Button(
            seller_row, text="Refresh Skills",
            command=self._refresh_skills_async, width=12
        )
        self.skills_btn.pack(side=tk.LEFT, padx=2)
        
        # Auto-refresh toggle
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.auto_refresh_cb = ttk.Checkbutton(
            seller_row, text="Auto-sync",
            variable=self.auto_refresh_var,
            command=self._toggle_auto_refresh
        )
        self.auto_refresh_cb.pack(side=tk.LEFT, padx=5)
        
        # Countdown label for next refresh
        self.countdown_label = ttk.Label(
            seller_row, text="",
            font=("Segoe UI", 9),
            foreground="gray"
        )
        self.countdown_label.pack(side=tk.LEFT, padx=10)
        
        # === Row 2: Buyer (Secondary) Character ===
        buyer_row = ttk.Frame(auth_frame)
        buyer_row.pack(fill=tk.X, pady=(2, 0))
        
        ttk.Label(buyer_row, text="Buyer:", font=("Segoe UI", 9, "bold"), width=6).pack(side=tk.LEFT)
        
        self.buyer_status = ttk.Label(
            buyer_row, text="(same as seller)",
            font=("Segoe UI", 9), width=25, anchor=tk.W,
            foreground="gray"
        )
        self.buyer_status.pack(side=tk.LEFT, padx=5)
        
        self.buyer_logout_btn = ttk.Button(
            buyer_row, text="Logout",
            command=lambda: self._logout("buyer"), width=7
        )
        self.buyer_logout_btn.pack(side=tk.LEFT, padx=2)
        
        self.buyer_login_btn = ttk.Button(
            buyer_row, text="Login",
            command=lambda: self._show_login_dialog("buyer"), width=7
        )
        self.buyer_login_btn.pack(side=tk.LEFT, padx=2)
        
        # Swap button
        self.swap_btn = ttk.Button(
            buyer_row, text="Swap",
            command=self._swap_characters, width=6
        )
        self.swap_btn.pack(side=tk.LEFT, padx=(10, 2))
        
        # Info label
        ttk.Label(
            buyer_row, 
            text="(Buyer only used for cross-hub arbitrage)",
            font=("Segoe UI", 8),
            foreground="gray"
        ).pack(side=tk.LEFT, padx=10)

    def _create_trades_panel(self):
        """Create right trades list panel."""
        trades_frame = ttk.LabelFrame(
            self.content_frame, text="Tracked Trades", padding=5
        )
        trades_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        
        # Filter buttons
        filter_frame = ttk.Frame(trades_frame)
        filter_frame.pack(fill=tk.X, pady=(0, 5))
        
        self.filter_var = tk.StringVar(value="active")
        filters = [
            ("Active", "active"),
            ("Pending", "pending"),
            ("Listed", "listed"),
            ("Sold", "sold"),
            ("All", "all"),
        ]
        for text, value in filters:
            ttk.Radiobutton(
                filter_frame, text=text, value=value,
                variable=self.filter_var, command=self._refresh_display
            ).pack(side=tk.LEFT, padx=5)
        
        # Trades treeview
        self.trades_tree = ttk.Treeview(
            trades_frame, columns=TRADE_COLUMNS, show="headings", height=15
        )
        
        # Configure columns with sort commands
        for col in TRADE_COLUMNS:
            self.trades_tree.heading(
                col, text=TRADE_COL_TITLES[col],
                command=lambda c=col: sort_treeview(
                    self.trades_tree, c, self.sort_state,
                    TRADE_COL_TITLES, TRADE_NUMERIC_COLS
                )
            )
        
        self.trades_tree.column("item", width=200)
        self.trades_tree.column("status", width=70, anchor=tk.CENTER)
        self.trades_tree.column("held", width=55, anchor=tk.E)
        self.trades_tree.column("listed", width=55, anchor=tk.E)
        self.trades_tree.column("avg_buy", width=90, anchor=tk.E)
        self.trades_tree.column("list_price", width=90, anchor=tk.E)
        self.trades_tree.column("profit", width=90, anchor=tk.E)
        self.trades_tree.column("fees", width=80, anchor=tk.E)
        
        # Scrollbar
        vsb = ttk.Scrollbar(trades_frame, orient=tk.VERTICAL, command=self.trades_tree.yview)
        self.trades_tree.configure(yscrollcommand=vsb.set)
        
        self.trades_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Tags for coloring
        self.trades_tree.tag_configure("profit", foreground="#006400")
        self.trades_tree.tag_configure("loss", foreground="#8B0000")
        self.trades_tree.tag_configure("active", foreground="#00008B")
        self.trades_tree.tag_configure("flagged", foreground="#555555")
        # Underbid warning - red background like a highlighter
        self.trades_tree.tag_configure("underbid", background="#FFCCCC")
        
        # Context menu
        self._create_context_menu(trades_frame)
        
        self.trades_tree.bind("<Button-3>", self._show_context_menu)
        self.trades_tree.bind("<Double-1>", self._show_trade_details)

    def _create_context_menu(self, parent):
        """Create right-click context menu for the inventory tree."""
        self.context_menu = tk.Menu(parent, tearoff=0)
        self.context_menu.add_command(label="View Details", command=lambda: self._show_trade_details(None))
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Ignore Underbid", command=self._ignore_underbid)
        self.context_menu.add_command(label="Add to Stock Market", command=self._add_to_stock_market)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Delete Entry", command=self._delete_trade)

    def _show_context_menu(self, event):
        """Show context menu at click position."""
        item = self.trades_tree.identify_row(event.y)
        if item:
            self.trades_tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _add_to_stock_market(self):
        """Add selected inventory entry's item to stock market portfolio."""
        entry = self._get_selected_entry()
        if not entry:
            return

        if self.stock_market_tab:
            from core.config import get_hub_config, DEFAULT_HUB
            hub_config = get_hub_config(self.selected_hub or DEFAULT_HUB)

            self.stock_market_tab.add_item_from_external(
                type_id=entry.type_id,
                region_id=hub_config["region_id"],
                station_id=hub_config["station_id"],
                type_name=entry.type_name
            )

    def _on_esi_refresh(self):
        """Called when ESI wallet data is refreshed.

        Order matters: sync inventory from the fresh wallet first (so listings
        reflect the latest volume_remain/price), then run the underbid check
        against the resulting active listings.
        """
        import time as _pt
        _pt0 = _pt.perf_counter()
        _step_inv_sync = 0.0
        _step_underbid = 0.0
        _step_refresh = 0.0
        try:
            from core.calculate import get_broker_fee_rate
            # get_broker_fee_rate returns a percentage (e.g. 1.48); convert to
            # decimal fraction for the orphan-fee estimator.
            rate = get_broker_fee_rate(self.skills) / 100.0
            _ts = _pt.perf_counter()
            results = sync_inventory_from_wallet(
                self.inventory, self.wallet, broker_fee_rate=rate
            )
            _step_inv_sync = _pt.perf_counter() - _ts
            if any(results.values()):
                print(f"[ScannerInventory] sync: {results}")
        except Exception as e:
            print(f"[ScannerInventory] sync error: {e}")

        _ts = _pt.perf_counter()
        self._run_underbid_check()
        _step_underbid = _pt.perf_counter() - _ts
        _ts = _pt.perf_counter()
        self._refresh_display()
        _step_refresh = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] _on_esi_refresh total={_pt_total*1000:.0f}ms "
            f"sync_inventory_from_wallet={_step_inv_sync*1000:.0f}ms "
            f"_run_underbid_check={_step_underbid*1000:.0f}ms "
            f"_refresh_display={_step_refresh*1000:.0f}ms"
        )

        # Push the fresh wallet into the NPC Orders sales tracker so it can
        # ingest any new sell-side transactions for items it watches.
        cb = getattr(self, "_npc_orders_wallet_hook", None)
        wallet_tx_count = len(self.wallet.transactions) if self.wallet else "no-wallet"
        print(f"[NPCSalesDiag] _on_esi_refresh: hook={'wired' if cb else 'MISSING'}, "
              f"wallet_transactions={wallet_tx_count}")
        if cb and self.wallet is not None:
            try:
                cb(self.wallet)
            except Exception as e:
                print(f"[NPCSales] hook error: {e}")

    def set_npc_orders_wallet_hook(self, callback):
        """Register a callback fired with the fresh wallet after each refresh.

        Used by NPCOrdersTabManager.on_wallet_refresh. Decoupled here so this
        module doesn't need to know about NPC Orders.
        """
        self._npc_orders_wallet_hook = callback

    def _run_underbid_check(self):
        """Run the underbid check against inventory listings.

        Uses the market_orders_cache populated by ESISyncManager.refresh_esi_data
        (fetched once per refresh, regardless of underbid usage).
        """
        market_orders = getattr(self.sync_manager, "market_orders_cache", None)
        if not market_orders:
            return

        # Pass the HIGHEST listing price per entry as your_price -- if that
        # isn't underbid, none of the entry's lower listings are either.
        listings = []
        for entry in self.inventory.active_entries():
            if not entry.active_listings:
                continue
            top_price = max(a.current_price for a in entry.active_listings)
            listings.append((entry.type_id, top_price))

        if not listings:
            return

        try:
            self.underbid_monitor.check_underbids(
                listings, market_orders, self.selected_hub
            )
        except Exception as e:
            print(f"[Underbid] check error: {e}")

    def _on_standings_changed(self, station: float, faction: float):
        """Called when manual standing override is changed."""
        self.skills = TradingSkills(
            broker_relations=self.skills.broker_relations,
            accounting=self.skills.accounting,
            advanced_broker_relations=self.skills.advanced_broker_relations,
            station_standing=station,
            faction_standing=faction,
            manual_broker_fee=self.skills.manual_broker_fee,
            manual_sales_tax=self.skills.manual_sales_tax
        )
        self.tracker.set_skills(self.skills)
        self.standings_bar.update(self.skills)
        self._refresh_display()

    def _on_fees_changed(self, broker_fee: float, sales_tax: float):
        """Called when manual fee override is changed."""
        self.skills = TradingSkills(
            broker_relations=self.skills.broker_relations,
            accounting=self.skills.accounting,
            advanced_broker_relations=self.skills.advanced_broker_relations,
            station_standing=self.skills.station_standing,
            faction_standing=self.skills.faction_standing,
            manual_broker_fee=broker_fee,
            manual_sales_tax=sales_tax
        )
        self.tracker.set_skills(self.skills)
        self.standings_bar.update(self.skills)
        self._refresh_display()

    def _refresh_skills_async(self):
        """Refresh skills and standings in background thread."""
        if not self.auth.is_authenticated:
            self.set_status("Not logged in")
            return
        
        if not self.esi_skills:
            self.set_status("ESI skills not initialized")
            return
        
        # Check cache status - show time remaining if not ready
        can_refresh, seconds_remaining = self.esi_skills.get_cache_status("seller")
        if not can_refresh and seconds_remaining > 0:
            mins = seconds_remaining // 60
            secs = seconds_remaining % 60
            self.set_status(f"Skills cache valid for {mins}m {secs}s")
            return
        
        # Disable button during refresh
        self.skills_btn.configure(state=tk.DISABLED)
        self.set_status("Refreshing skills/standings...")
        
        def do_fetch():
            """Background thread work."""
            sell_hub = self._sell_hub_override or self.selected_hub
            buy_hub = self._buy_hub_override or sell_hub

            # Fetch seller skills/standings
            seller_skills = None
            if self.auth.is_authenticated:
                fetched = self.esi_skills.fetch_skills(slot="seller", force_refresh=True)
                if fetched:
                    station_standing = 0.0
                    faction_standing = 0.0
                    if self.esi_standings:
                        # Force-refresh standings so in-memory cache doesn't shadow ESI.
                        # base=True: fee math wants raw standings, not Connections-adjusted.
                        self.esi_standings.fetch_standings(force_refresh=True, slot="seller")
                        corp, fac = self.esi_standings.get_standings_for_hub(
                            sell_hub, slot="seller", base=True)
                        station_standing = corp
                        faction_standing = fac
                    
                    seller_skills = TradingSkills(
                        broker_relations=fetched.broker_relations,
                        accounting=fetched.accounting,
                        advanced_broker_relations=fetched.advanced_broker_relations,
                        station_standing=station_standing,
                        faction_standing=faction_standing
                    )
            
            # Fetch buyer skills/standings if logged in
            buyer_skills = None
            if hasattr(self.auth, 'has_buyer') and self.auth.has_buyer:
                fetched = self.esi_skills.fetch_skills(slot="buyer", force_refresh=True)
                if fetched:
                    station_standing = 0.0
                    faction_standing = 0.0
                    if self.esi_standings:
                        # Force-refresh standings so in-memory cache doesn't shadow ESI.
                        # base=True: fee math wants raw standings, not Connections-adjusted.
                        self.esi_standings.fetch_standings(force_refresh=True, slot="buyer")
                        corp, fac = self.esi_standings.get_standings_for_hub(
                            buy_hub, slot="buyer", base=True)
                        station_standing = corp
                        faction_standing = fac
                    
                    buyer_skills = TradingSkills(
                        broker_relations=fetched.broker_relations,
                        accounting=fetched.accounting,
                        advanced_broker_relations=fetched.advanced_broker_relations,
                        station_standing=station_standing,
                        faction_standing=faction_standing
                    )
            
            # Schedule UI update on main thread
            def update_ui():
                if seller_skills:
                    self.skills = seller_skills
                    self.tracker.set_skills(self.skills)
                    print(f"Seller skills updated: BR={self.skills.broker_relations}, Acc={self.skills.accounting}")
                
                if buyer_skills:
                    self.buyer_skills = buyer_skills
                    print(f"Buyer skills updated: BR={self.buyer_skills.broker_relations}, Acc={self.buyer_skills.accounting}")
                
                # Update UI
                self.standings_bar.update(self.skills)
                self._save_cached_skills()
                self._refresh_display()
                
                # Re-enable button
                self.skills_btn.configure(state=tk.NORMAL)
                self.set_status("Skills/standings refreshed")
                
                # Notify main GUI
                if self._on_characters_changed:
                    self._on_characters_changed()
            
            submit(update_ui)
        
        threading.Thread(target=do_fetch, daemon=True).start()

    def _update_auth_display(self):
        """Update authentication status display for both characters."""
        # Seller status
        if self.auth.is_authenticated:
            seller_name = self.auth.seller_name if hasattr(self.auth, 'seller_name') else self.auth.character_name
            self.seller_status.configure(
                text=seller_name,
                foreground="green"
            )
            self.seller_login_btn.configure(state=tk.DISABLED)
            self.seller_logout_btn.configure(state=tk.NORMAL)
            self.refresh_btn.configure(state=tk.NORMAL)
        else:
            self.seller_status.configure(
                text="Not logged in",
                foreground="gray"
            )
            self.seller_login_btn.configure(state=tk.NORMAL)
            self.seller_logout_btn.configure(state=tk.DISABLED)
            self.refresh_btn.configure(state=tk.DISABLED)
        
        # Buyer status
        has_buyer = hasattr(self.auth, 'has_buyer') and self.auth.has_buyer
        if has_buyer:
            self.buyer_status.configure(
                text=self.auth.buyer_name,
                foreground="blue"
            )
            self.buyer_login_btn.configure(state=tk.DISABLED)
            self.buyer_logout_btn.configure(state=tk.NORMAL)
        else:
            self.buyer_status.configure(
                text="(not logged in)",
                foreground="gray"
            )
            self.buyer_login_btn.configure(state=tk.NORMAL)
            self.buyer_logout_btn.configure(state=tk.DISABLED)
        
        # Swap button - only enabled if BOTH characters are logged in
        can_swap = self.auth.is_authenticated and has_buyer
        self.swap_btn.configure(state=tk.NORMAL if can_swap else tk.DISABLED)

    def _refresh_display(self):
        """Refresh the inventory list and summary (Step 3: reads from inventory)."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        all_entries = self.inventory.all_entries()

        # Update summary panel from inventory.
        wallet_balance = self.wallet.balance if self.wallet else 0
        _ts = _pt.perf_counter()
        self.summary_panel.update_from_inventory(all_entries, wallet_balance)
        _step_summary = _pt.perf_counter() - _ts

        # Refresh treeview
        _ts = _pt.perf_counter()
        for item in self.trades_tree.get_children():
            self.trades_tree.delete(item)
        _step_tree_clear = _pt.perf_counter() - _ts

        filter_type = self.filter_var.get()
        if filter_type == "active":
            entries = [e for e in all_entries if e.is_active]
        elif filter_type == "listed":
            entries = [e for e in all_entries if e.active_listings]
        elif filter_type == "pending":
            # Pending = flagged or bought-but-not-listed (no active listings, no sales)
            entries = [e for e in all_entries
                       if not e.active_listings and e.quantity_out == 0]
        elif filter_type == "sold":
            # Sold = has sales but nothing currently held or listed
            entries = [e for e in all_entries
                       if e.quantity_out > 0 and not e.is_active]
        else:
            entries = all_entries

        # Sort by type_name for a stable default order.
        entries.sort(key=lambda e: e.type_name or "")
        _ts = _pt.perf_counter()
        for entry in entries:
            self._insert_inventory_entry(entry)
        _step_tree_insert = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] gui_tracking._refresh_display total={_pt_total*1000:.0f}ms "
            f"entries={len(entries)} "
            f"summary_panel={_step_summary*1000:.0f}ms "
            f"tree_clear={_step_tree_clear*1000:.0f}ms "
            f"tree_insert={_step_tree_insert*1000:.0f}ms"
        )

    def _insert_inventory_entry(self, entry):
        """Insert one InventoryEntry as a row."""
        # Status derivation
        if entry.active_listings:
            status = "Listed"
            tag = "active"
        elif entry.quantity_held > 0:
            status = "Held"
            tag = "flagged"
        elif entry.quantity_out > 0:
            status = "Sold Out"
            tag = "profit" if entry.total_realized_profit > 0 else "loss"
        else:
            status = "Flagged"
            tag = "flagged"

        # List price: highest current_price across active listings (or "-")
        if entry.active_listings:
            list_price = max(a.current_price for a in entry.active_listings)
            list_price_str = format_isk(list_price)
        else:
            list_price_str = "-"

        avg_buy_str = format_isk(entry.average_buy_price) if entry.quantity_in > 0 else "-"
        held_str = str(entry.quantity_held) if entry.quantity_held else "-"
        listed_str = str(entry.quantity_listed) if entry.quantity_listed else "-"

        # Net profit (cash-flow): realized profit minus ALL listing fees ever
        # paid for this item. Matches the Summary panel's Net Profit math.
        net_profit = entry.total_realized_profit - entry.total_listing_fees
        if entry.quantity_out > 0 or entry.total_listing_fees > 0:
            profit_str = format_isk(net_profit)
        else:
            profit_str = "-"

        total_fees = entry.total_listing_fees + entry.total_sales_tax
        fees_str = format_isk(total_fees) if total_fees else "-"

        tags = [tag]
        # Underbid tag overrides if any listing is underbid and not ignored
        if (entry.active_listings and not entry.ignore_underbid
                and self.underbid_monitor.is_underbid(entry.type_id)):
            tags.append("underbid")

        self.trades_tree.insert("", tk.END, iid=f"inv-{entry.type_id}", values=(
            entry.type_name,
            status,
            held_str,
            listed_str,
            avg_buy_str,
            list_price_str,
            profit_str,
            fees_str,
        ), tags=tuple(tags))

    def _get_selected_entry(self):
        """Return the selected InventoryEntry, or None."""
        selection = self.trades_tree.selection()
        if not selection:
            return None
        iid = selection[0]
        if not iid.startswith("inv-"):
            return None
        try:
            type_id = int(iid[4:])
        except ValueError:
            return None
        return self.inventory.get(type_id)

    # === Auth dialogs ===

    def _show_login_dialog(self, slot: str = "seller"):
        """Start ESI login flow for specified character slot."""
        # Disable the appropriate login button
        if slot == "buyer":
            self.buyer_login_btn.configure(state=tk.DISABLED)
        else:
            self.seller_login_btn.configure(state=tk.DISABLED)
        
        self.set_status(f"Check your browser to log in {slot}...")
        
        def on_complete(success: bool, message: str):
            def update():
                if success:
                    # Re-initialize ESI components if this is first login
                    if not self.esi_skills:
                        self.esi_skills = ESISkills(self.auth)
                    if not self.esi_standings:
                        self.esi_standings = ESIStandings(self.auth, self.esi_skills)
                    
                    # For seller, also set up wallet
                    if slot == "seller":
                        self.wallet = ESIWallet(self.auth)
                        self.sync_manager.set_wallet(self.wallet)
                    
                    # Update auth display first
                    self._update_auth_display()
                    
                    # Fetch skills async (non-blocking)
                    self._refresh_skills_async()
                    
                    self.set_status(f"{message} - fetching skills...")
                    
                    if slot == "seller":
                        if self.auto_refresh_var.get():
                            self.sync_manager.schedule_auto_refresh()
                else:
                    self.set_status(f"Login failed: {message}")
                    self._update_auth_display()
                
                # Notify main GUI
                if self._on_characters_changed:
                    self._on_characters_changed()
                
                # Re-enable login button
                if slot == "buyer":
                    self.buyer_login_btn.configure(state=tk.NORMAL)
                else:
                    self.seller_login_btn.configure(state=tk.NORMAL)
            
            submit(update)
        
        # Use the new slot parameter
        if hasattr(self.auth, 'start_auth_flow'):
            # Check if the method accepts slot parameter
            import inspect
            sig = inspect.signature(self.auth.start_auth_flow)
            if 'slot' in sig.parameters:
                self.auth.start_auth_flow(on_complete, slot=slot)
            else:
                # Old auth without slot support
                self.auth.start_auth_flow(on_complete)
        else:
            self.auth.start_auth_flow(on_complete)

    def _logout(self, slot: str = "seller"):
        """Log out specified character."""
        if hasattr(self.auth, 'logout'):
            # Check if logout accepts slot parameter
            import inspect
            sig = inspect.signature(self.auth.logout)
            if 'slot' in sig.parameters:
                self.auth.logout(slot=slot)
            else:
                # Old auth - logout clears everything
                self.auth.logout()
        
        if slot == "seller":
            self.wallet = None
            self.skills = DEFAULT_SKILLS
            self.tracker.set_skills(self.skills)
            self.sync_manager.set_wallet(None)
            self.sync_manager.cancel_auto_refresh()
            
            # If no buyer either, clear ESI components
            if not (hasattr(self.auth, 'has_buyer') and self.auth.has_buyer):
                self.esi_skills = None
                self.esi_standings = None
        else:
            self.buyer_skills = DEFAULT_SKILLS
        
        self._update_auth_display()
        
        # Notify main GUI
        if self._on_characters_changed:
            self._on_characters_changed()
        
        self.set_status(f"Logged out {slot}")

    def _swap_characters(self):
        """Swap seller and buyer characters."""
        if not hasattr(self.auth, 'swap_characters'):
            messagebox.showinfo("Swap", "Character swap requires updated ESI auth module")
            return
        
        if not self.auth.has_buyer:
            messagebox.showinfo("Swap", "No buyer character to swap with. Login a second character first.")
            return
        
        # Swap in auth (this swaps tokens)
        self.auth.swap_characters()
        
        # Swap our local skill objects
        self.skills, self.buyer_skills = self.buyer_skills, self.skills
        self.tracker.set_skills(self.skills)
        
        # Clear all caches to force fresh fetches
        if self.esi_skills:
            self.esi_skills.clear_cache()
        if self.esi_standings:
            self.esi_standings.clear_cache()
        
        # Clear any stale hub overrides from prior calls
        self._sell_hub_override = None
        self._buy_hub_override = None
        
        # Re-fetch both characters with fresh data
        self.refresh_all_character_data()
        
        # Refresh wallet/orders/transactions for the new seller
        # (skills fetch above is async; wallet refresh runs in parallel)
        if self.wallet:
            self.sync_manager.refresh_esi_data()
        
        self._update_auth_display()
        
        # Notify main GUI
        if self._on_characters_changed:
            self._on_characters_changed()
        
        self.set_status(f"Swapped: Seller={self.auth.seller_name}, Buyer={self.auth.buyer_name}")

    def _toggle_auto_refresh(self):
        """Toggle auto-refresh on/off."""
        self.sync_manager.toggle_auto_refresh(self.auto_refresh_var.get())

    def _delete_trade(self):
        """Delete the selected inventory entry."""
        entry = self._get_selected_entry()
        if not entry:
            return
        if messagebox.askyesno("Delete Entry", f"Delete inventory entry for {entry.type_name}?"):
            self.underbid_monitor.clear_type(entry.type_id)
            self.inventory.delete_entry(entry.type_id)
            self._refresh_display()

    def _ignore_underbid(self):
        """Suppress underbid warnings for the selected inventory entry."""
        entry = self._get_selected_entry()
        if not entry:
            return
        if not entry.active_listings:
            messagebox.showinfo(
                "Cannot Ignore",
                "This item has no active listings to mark as ignored."
            )
            return
        self.inventory.set_ignore_underbid_for_type(entry.type_id, True)
        self.underbid_monitor.ignore_underbid(entry.type_id)
        self.set_status(f"Ignoring underbid for {entry.type_name}")
        self._refresh_display()

    def _show_trade_details(self, event):
        """Show detailed inventory info on double-click."""
        entry = self._get_selected_entry()
        if not entry:
            return

        # Build a plain-text summary of the entry. Step 3 keeps this lightweight;
        # a richer dialog can come later.
        lines = [
            f"Item: {entry.type_name}  (type_id {entry.type_id})",
            "",
            f"Quantity in: {entry.quantity_in}",
            f"Quantity out: {entry.quantity_out}",
            f"Held: {entry.quantity_held}  (in hangar {entry.quantity_in_hangar}, listed {entry.quantity_listed})",
            "",
            f"Avg buy price: {format_isk(entry.average_buy_price)}",
            f"Avg sell price: {format_isk(entry.average_sell_price)}",
            f"Remaining cost basis: {format_isk(entry.remaining_cost_basis)}",
            "",
            f"Total revenue: {format_isk(entry.total_revenue)}",
            f"Total buy cost: {format_isk(entry.total_buy_cost)}",
            f"Total sales tax: {format_isk(entry.total_sales_tax)}",
            f"Total listing fees: {format_isk(entry.total_listing_fees)}",
            f"Realized profit (gross): {format_isk(entry.total_realized_profit)}",
            "",
            f"Active listings: {len(entry.active_listings)}",
            f"Buy lots: {len(entry.buy_lots)}",
            f"Sales: {len(entry.sales)}",
        ]
        messagebox.showinfo(f"Inventory: {entry.type_name}", "\n".join(lines))

    # === Public API for integration with deals tab ===

    def flag_deal(self, type_id: int, type_name: str,
                  buy_price: float, sell_price: float, profit_per_unit: float):
        """Flag a deal for tracking (called from deals tab context menu)."""
        trade = self.tracker.flag_for_buy(
            type_id=type_id,
            type_name=type_name,
            projected_buy=buy_price,
            projected_sell=sell_price,
            projected_profit=profit_per_unit
        )

        # Parallel: register in scanner inventory (Step 2 -- populated only;
        # not yet read by UI). Lot/listing data is filled by ESI sync.
        self.inventory.flag_from_scanner(
            type_id=type_id,
            type_name=type_name,
            projected_buy=buy_price,
            projected_sell=sell_price,
            projected_profit_per_unit=profit_per_unit,
        )
        
        # Backfill from ESI if wallet data available
        if self.wallet and self.wallet.transactions:
            results = self.sync_manager.backfill_trade_from_esi(trade.trade_id)
            if results["sale"]:
                self.set_status(f"Tracked (already sold): {type_name}")
            elif results["listing"]:
                self.set_status(f"Tracked (listed): {type_name}")
            elif results["buy"]:
                self.set_status(f"Tracked (bought, awaiting list): {type_name}")
            else:
                self.set_status(f"Tracking: {type_name}")
        else:
            self.set_status(f"Tracking: {type_name}")
        
        self._refresh_display()
        return trade

    def get_skills(self) -> TradingSkills:
        """Get current trading skills for use by other modules (e.g., scanner)."""
        return self.skills
    
    def get_buyer_skills(self) -> TradingSkills:
        """Get buyer character's trading skills for cross-hub calculations."""
        return self.buyer_skills
    
    def get_standings(self):
        """Get ESIStandings instance for hub-specific standings lookups."""
        return self.esi_standings
    
    def _backfill_inventory_from_tracker(self):
        """Copy legacy TradeTracker trades into InventoryManager (idempotent).

        For each TrackedTrade whose type_id has no InventoryEntry yet, call
        flag_from_scanner so the next ESI sync can populate buy lots and
        listings. Safe to call repeatedly -- flag_from_scanner only updates
        projections on an existing entry.
        """
        added = 0
        for trade in self.tracker.trades.values():
            if trade.type_id in self.inventory.entries:
                continue
            self.inventory.flag_from_scanner(
                type_id=trade.type_id,
                type_name=trade.type_name,
                projected_buy=trade.projected_buy_price or 0,
                projected_sell=trade.projected_sell_price or 0,
                projected_profit_per_unit=trade.projected_profit or 0,
            )
            added += 1
        if added:
            print(f"[ScannerInventory] backfilled {added} entries from TradeTracker")

    def set_hub(self, hub_key: str):
        """Update the selected hub for underbid monitoring."""
        self.selected_hub = hub_key
        self.sync_manager.set_underbid_monitor(self.underbid_monitor, hub_key)
        self.inventory.set_hub(hub_key)
        # Hub switch -> different inventory file loaded -> re-run backfill
        # against the new hub's inventory.
        self._backfill_inventory_from_tracker()
