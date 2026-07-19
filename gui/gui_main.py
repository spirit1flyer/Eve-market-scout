"""Main GUI window for EVE Market Scout - orchestrates all components."""

import tkinter as tk
from tkinter import ttk

from scanning.scanner_common import Deal
from core.config import (
    AUTO_REFRESH_ENABLED, SOUND_ALERTS_ENABLED,
    get_hub_config, DEFAULT_HUB, APP_VERSION
)

# Import tab managers
from gui.gui_deals import DealsTabManager
from gui.gui_filters import FilterManager
from gui.watchlist.gui_watchlist import WatchlistTabManager
from gui.gui_npc_orders import NPCOrdersTabManager
from gui.tracking.gui_tracking import TrackingTabManager
from gui.gui_crosshub import CrossHubDisplayManager
from gui.gui_demand_tab import DemandTabManager
from gui.gui_contracts import ContractsTabManager
from gui.gui_reprocess import ReprocessTabManager

# Optional local-only module: present on some machines, intentionally not in
# the repo. If it can't load for ANY reason its tab is shown greyed out and
# nothing else changes - the import must never crash startup.
try:
    from drug_local.drug_gui import DrugTabManager
except ModuleNotFoundError:
    # Expected on public checkouts: module simply isn't there. Grey out quietly.
    DrugTabManager = None
except Exception as _opt_err:
    # Module present but failed to load (e.g. missing data file, bad JSON).
    # Still grey out, but say why so a broken local build is diagnosable.
    print(f"[OptionalTab] optional tab present but failed to load, greying out: {_opt_err}")
    DrugTabManager = None
from gui.stockmarket.gui_stockmarket import StockMarketTab
from gui.industry.gui_industry import IndustryTabManager
from gui.gui_main_controls import MainControlsMixin
from gui.gui_main_scan import MainScanMixin

from typing import Callable


class MarketScoutGUI(MainControlsMixin, MainScanMixin):
    """Main GUI window for market scout."""

    def __init__(self, root: tk.Tk, scan_callback: Callable, get_client: Callable = None):
        """Initialize the main GUI.
        
        Args:
            root: The single Tk root window (created in main.py)
            scan_callback: Function to call for market scans
            get_client: Function to get ESI client
        """
        self.scan_callback = scan_callback
        self.get_client = get_client
        self.deals: list[Deal] = []
        self.previous_deal_ids: set[int] = set()
        self.auto_refresh_job = None
        self.auto_refresh_enabled = AUTO_REFRESH_ENABLED
        self.sound_enabled = SOUND_ALERTS_ENABLED
        self.is_scanning = False
        
        # Selected trading hubs - now separate for buy and sell
        self.buy_station = DEFAULT_HUB
        self.sell_station = DEFAULT_HUB
        
        # Legacy compatibility - selected_hub points to sell station
        self.selected_hub = DEFAULT_HUB
        
        # Flag to force Jita refresh on next scan
        self.force_jita_refresh = False
        
        # ESI sync timing (seconds until next cache refresh)
        from core.config import AUTO_REFRESH_INTERVAL
        self.next_refresh_seconds: int = AUTO_REFRESH_INTERVAL
        
        # Shared clipboard for copy/paste between watchlist and NPC orders
        self._shared_clipboard: list[dict] = []
        
        # Background import monitoring
        self._bg_import_poll_job = None

        # Use the provided root window (created in main.py)
        self.root = root
        
        # Show and configure the root window
        self.root.deiconify()  # Make visible (was withdrawn)
        self._update_window_title()
        screen_w = self.root.winfo_screenwidth()
        win_w = min(1550, screen_w - 20)
        self.root.geometry(f"{win_w}x760")
        self.root.minsize(1100, 550)

        self._setup_styles()
        
        # Initialize sort state before managers need it
        self.sort_state = {}
        
        # Initialize managers (they need root first)
        self.filter_manager = FilterManager(self.root)
        
        self._create_widgets()
        
        # Initialize deals tab manager after notebook exists
        self.deals_manager = DealsTabManager(
            self.notebook,
            self.filter_manager,
            self._set_status,
            self._get_column_title,
            self.sort_state,
            root=self.root,
            get_client=self.get_client,
            get_buy_station=lambda: self.buy_station,
            get_sell_station=lambda: self.sell_station
        )
        
        # Initialize crosshub display manager (uses same notebook, different display format)
        self.crosshub_display_manager = CrossHubDisplayManager(
            self.notebook,
            self._set_status,
            root=self.root,
            get_client=self.get_client,
        )

        # Demand / Restock tab — shares Buy/Sell station selectors with cross-hub
        # but lives in its own tab so cross-hub stays untouched. Populated only
        # by cross-hub scans (same-station mode leaves it empty).
        self.demand_tab_manager = DemandTabManager(
            self.notebook,
            set_status=self._set_status,
            root=self.root,
            get_buy_station=lambda: self.buy_station,
            get_sell_station=lambda: self.sell_station,
        )

        # Initialize NPC Orders tab (replaces History)
        self.npc_orders_manager = NPCOrdersTabManager(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status
        )
        
        # Initialize watchlist tab
        self.watchlist_manager = WatchlistTabManager(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status
        )
        
        # Wire up shared clipboard for copy/paste between tabs
        self.watchlist_manager.set_clipboard_functions(
            self._get_shared_clipboard,
            self._set_shared_clipboard
        )
        self.npc_orders_manager.set_clipboard_functions(
            self._get_shared_clipboard,
            self._set_shared_clipboard
        )
        
        # Connect deals manager to watchlist for "Add to Watchlist" context menu
        self.deals_manager.watchlist_manager = self.watchlist_manager
        
        # Initialize tracking tab (created last, accesses other managers)
        self.tracking_manager = TrackingTabManager(
            self.notebook,
            set_status=self._set_status,
            get_client=self.get_client
        )
        
        # Connect deals manager to tracking for "Track Trade" context menu
        self.deals_manager.tracking_manager = self.tracking_manager
        
        # Initialize stock market managers
        # Initialize Stock Market tab (after Tracking)
        self.stock_market_tab = StockMarketTab(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status,
        )

        # Sync the main control-bar "Stock Market" master switch to the tab's
        # persisted on/off state, and register a callback so the off-overlay's
        # "Turn On" button also keeps the checkbutton in sync.
        self.sm_master_var.set(self.stock_market_tab.is_enabled())
        self.stock_market_tab.set_enabled_changed_callback(self.sm_master_var.set)

        # Initialize Contracts tab (manual public-contract search). Self-
        # contained: owns its own ContractsDB/engine; no cross-wiring needed.
        self.contracts_manager = ContractsTabManager(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status,
            root=self.root,
        )

        # Initialize Reprocess-or-Sell tab. Self-contained: pure-calc engine +
        # local SDE; reuses tracking_manager's ESISkills/ESIStandings for the
        # Scrapmetal-level + reprocessing-tax auto-pull, and prices at the
        # current sell hub.
        self.reprocess_manager = ReprocessTabManager(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status,
            get_sell_station=lambda: self.sell_station,
            get_esi_skills=lambda: self.tracking_manager.esi_skills,
            get_esi_standings=lambda: self.tracking_manager.esi_standings,
            root=self.root,
        )

        # Optional local-only Boosters tab. Self-contained (own ESI fetcher,
        # own config/data files). If the module is missing on this machine,
        # show a greyed-out disabled placeholder tab instead.
        if DrugTabManager is not None:
            self.drug_manager = DrugTabManager(
                self.notebook,
                get_client=self.get_client,
                set_status=self._set_status,
                root=self.root,
            )
        else:
            self.drug_manager = None
            placeholder = ttk.Frame(self.notebook)
            self.notebook.add(placeholder, text="Boosters", state="disabled")

        # Industry tab (committed/public): T1 manufacturing buy-vs-build.
        # Self-contained — own market-data layer over the shared ESI client.
        self.industry_manager = IndustryTabManager(
            self.notebook,
            get_client=self.get_client,
            set_status=self._set_status,
            root=self.root,
        )

        # Connect deals manager to stock market for context menu
        self.deals_manager.stock_market_tab = self.stock_market_tab
        
        # Connect watchlist manager to stock market for context menu
        self.watchlist_manager.stock_market_tab = self.stock_market_tab
        
        # Connect tracking manager to stock market for context menu
        self.tracking_manager.stock_market_tab = self.stock_market_tab

        # Connect demand tab to watchlist + stock-market for its context menu.
        # Note: the demand tab does NOT consume the top filter bar — it has its
        # own inline category toggles so users don't have to guess which row of
        # checkboxes drives which tab.
        self.demand_tab_manager.watchlist_manager = self.watchlist_manager
        self.demand_tab_manager.stock_market_tab = self.stock_market_tab
        
        # Wire up stock market holdings sync from ESI orders
        self.tracking_manager._setup_stock_market_sync()
        
        # Wire up character change callback so main GUI updates when login/logout happens
        self.tracking_manager._on_characters_changed = self._update_character_display
        
        # Connect crosshub display manager to tracking and filter managers
        self.crosshub_display_manager.tracking_manager = self.tracking_manager
        self.crosshub_display_manager.filter_manager = self.filter_manager
        
        # Give filter manager access to skills for calculations
        self.filter_manager.set_skills_getter(self.tracking_manager.get_skills)
        
        # Wire up est_sell_pct callback to refresh deals display without re-scanning
        self.filter_manager.set_est_sell_pct_callback(self.deals_manager.refresh_display)
        
        # Give watchlist manager access to skills
        self.watchlist_manager.set_skills_getter(self.tracking_manager.get_skills)
        
        # Give NPC orders manager access to skills (for Add dialog max-buy calc)
        self.npc_orders_manager.set_skills_getter(self.tracking_manager.get_skills)

        # Wire NPC Orders' rep-aware max-buy calc: origin (current sell hub
        # system) for the ≤6-jump filter, and live ESIStandings for looking
        # up rep at whichever buyer-station the calc lands on.
        self.npc_orders_manager.set_origin_system_getter(
            lambda: get_hub_config(self.sell_station).get("system_id")
        )
        self.npc_orders_manager.set_esi_standings_getter(
            lambda: self.tracking_manager.esi_standings
        )

        # Set initial region for watchlist
        hub_config = get_hub_config(self.sell_station)
        self.watchlist_manager.set_region_id(hub_config["region_id"])
        self.npc_orders_manager.set_region_id(hub_config["region_id"])

        # Wire NPC Orders sales tracker to tracking_manager's wallet refresh.
        # Sales ledger is per-character: point at the seller's file on startup;
        # _update_character_display re-points it whenever auth changes.
        self.tracking_manager.set_npc_orders_wallet_hook(
            self.npc_orders_manager.on_wallet_refresh
        )
        if self.tracking_manager.auth.is_authenticated:
            self.npc_orders_manager.set_seller_character(
                self.tracking_manager.auth.seller_name
            )

        # Update character display
        self._update_character_display()
        
        # Start background import status polling
        self._start_bg_import_polling()

    def _setup_styles(self):
        """Configure ttk styles."""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

    def _get_shared_clipboard(self) -> list[dict]:
        """Get items from shared clipboard."""
        return self._shared_clipboard.copy()

    def _set_shared_clipboard(self, items: list[dict]):
        """Set items to shared clipboard."""
        self._shared_clipboard = items.copy()

    def _update_window_title(self):
        """Update window title with current hub(s)."""
        if self.buy_station == self.sell_station:
            hub_config = get_hub_config(self.sell_station)
            self.root.title(f"EVE Market Scout v{APP_VERSION} - {hub_config['name']} Station Trading")
        else:
            buy_config = get_hub_config(self.buy_station)
            sell_config = get_hub_config(self.sell_station)
            self.root.title(f"EVE Market Scout v{APP_VERSION} - {buy_config['name']} -> {sell_config['name']} Arbitrage")

    def _get_column_title(self, col: str) -> str:
        """Get display title for a column."""
        # Dynamic ceiling header based on est_sell_pct
        try:
            est_pct = float(self.filter_manager.est_sell_pct_var.get())
            discount = 100 - est_pct
            pct_display = f"-{discount:.0f}%"
        except (ValueError, TypeError):
            pct_display = ""
        
        titles = {
            "name": "Item Name",
            "system": "System",
            "buy_price": "Buy At",
            "buy_order": "Buy Order",
            "ceiling": f"Ceiling {pct_display}",
            "break_even": "Break Even",
            "unit_profit": "Profit/Unit",
            "et_flip": "ET Flip",
            "volume": "Volume",
            "raw_volume": "Available",
            "total_profit": "Total Profit",
            "avg_7d": "7d Avg",
            "avg_30d": "30d Avg",
            "vol_30d": "30d/Day",
            "vol_7d": "7d/Day",
        }
        return titles.get(col, col)

    def _set_status(self, text: str):
        """Update status label text."""
        self.status_label.configure(text=text)

    def _create_widgets(self):
        """Create all GUI widgets."""
        self._create_control_bar()  # From MainControlsMixin
        self._create_filter_bar()
        self._create_notebook()
        self._create_info_bar()

    def _create_filter_bar(self):
        """Create filter controls bar."""
        self.filter_manager.create_widgets(self.root)

    def _create_notebook(self):
        """Create tabbed notebook for deals categories."""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

    def _create_info_bar(self):
        """Create bottom info bar."""
        info_frame = ttk.Frame(self.root, padding=5)
        info_frame.pack(fill=tk.X)

        self.count_label = ttk.Label(
            info_frame,
            text="Deals: 0",
            font=("Segoe UI", 9)
        )
        self.count_label.pack(side=tk.LEFT, padx=10)
        
        # Background import status label (hidden until import running)
        self.bg_import_label = ttk.Label(
            info_frame,
            text="",
            font=("Segoe UI", 9),
            foreground="blue"
        )
        self.bg_import_label.pack(side=tk.LEFT, padx=10)

        # Progress bar
        self.progress = ttk.Progressbar(
            info_frame,
            mode="determinate",
            length=200
        )
        self.progress.pack(side=tk.RIGHT, padx=10)

    # =========================================================================
    # BACKGROUND IMPORT MONITORING
    # =========================================================================

    def _start_bg_import_polling(self):
        """Start polling for background import status."""
        self._poll_bg_import_status()
    
    def _poll_bg_import_status(self):
        """Poll background import status and update UI."""
        try:
            from gui.gui_migration import get_background_import_status
            
            status = get_background_import_status()
            
            if status.get('restart_required', False):
                # Import finished, restart needed
                self.bg_import_label.configure(
                    text="Full history ready - restart app to activate",
                    foreground="orange"
                )
                # Keep polling less frequently in case user restarts
                self._bg_import_poll_job = self.root.after(30000, self._poll_bg_import_status)
                return
            elif status['running']:
                # Show progress
                if status['total'] > 0:
                    pct = int((status['current'] / status['total']) * 100)
                    self.bg_import_label.configure(
                        text=f"Building full history: {pct}% ({status['current']}/{status['total']} files)"
                    )
                else:
                    self.bg_import_label.configure(text=status['status'])
            elif status['complete']:
                # Import finished (shouldn't hit this with new flow, but keep for safety)
                self.bg_import_label.configure(
                    text="Full history ready - restart app to activate",
                    foreground="orange"
                )
                # Don't poll anymore
                return
            else:
                # Not running, clear label
                self.bg_import_label.configure(text="")
                # Check again in 30 seconds in case it starts
                self._bg_import_poll_job = self.root.after(30000, self._poll_bg_import_status)
                return
                
        except ImportError:
            # gui_migration not available, stop polling
            return
        except Exception as e:
            print(f"[BgImport] Poll error: {e}")
        
        # Poll again in 2 seconds while running
        self._bg_import_poll_job = self.root.after(2000, self._poll_bg_import_status)

    # =========================================================================
    # STATION SELECTION
    # =========================================================================

    def _on_buy_station_changed(self, event):
        """Handle buy station selection change."""
        from core.config import get_enabled_hubs
        selected_name = self.buy_station_var.get()
        hub_choices = get_enabled_hubs()
        for key, name in hub_choices:
            if name == selected_name:
                self.buy_station = key
                break
        self._update_mode_indicator()
        self._update_window_title()
        self._update_filter_hub()
        self._set_status(f"Buy station: {selected_name} - scan to update")

    def _on_sell_station_changed(self, event):
        """Handle sell station selection change."""
        from core.config import get_enabled_hubs
        selected_name = self.sell_station_var.get()
        hub_choices = get_enabled_hubs()
        for key, name in hub_choices:
            if name == selected_name:
                self.sell_station = key
                self.selected_hub = key  # Legacy compatibility
                break
        self._update_mode_indicator()
        self._update_window_title()
        self._update_filter_hub()
        
        # Update watchlist region
        hub_config = get_hub_config(self.sell_station)
        if self.watchlist_manager:
            self.watchlist_manager.set_region_id(hub_config["region_id"])
        if hasattr(self, 'npc_orders_manager') and self.npc_orders_manager:
            self.npc_orders_manager.set_region_id(hub_config["region_id"])
        
        # Update tracking manager hub for underbid monitoring
        if hasattr(self, 'tracking_manager') and self.tracking_manager:
            self.tracking_manager.set_hub(self.sell_station)
        
        self._set_status(f"Sell station: {selected_name} - scan to update")

    def _update_filter_hub(self):
        """Update filter_manager.selected_hub based on mode.
        
        Cross-hub mode: use buy station (where items are picked up)
        Same-station mode: use the station
        """
        if self.is_crosshub_mode():
            self.filter_manager.set_selected_hub(self.buy_station)
        else:
            self.filter_manager.set_selected_hub(self.sell_station)

    def _update_mode_indicator(self):
        """Update the mode label based on station selection."""
        if self.buy_station == self.sell_station:
            self.mode_label.configure(text="[Same Station]", foreground="gray")
            # Reset crosshub tree configuration flag so normal display works
            if hasattr(self, '_crosshub_trees_configured'):
                delattr(self, '_crosshub_trees_configured')
                # Undo the cross-hub takeover of the deals trees — otherwise
                # its double-click/right-click/header handlers stay bound and
                # resolve rows against the stale CrossHubDeal list.
                self.deals_manager.restore_tree_bindings()
        else:
            self.mode_label.configure(text="[Cross-Hub]", foreground="blue")

    def is_crosshub_mode(self) -> bool:
        """Check if we're in cross-hub arbitrage mode."""
        return self.buy_station != self.sell_station

    # =========================================================================
    # CHARACTER MANAGEMENT
    # =========================================================================

    def _update_character_display(self):
        """Update the character name labels from auth."""
        if not hasattr(self, 'tracking_manager') or not self.tracking_manager:
            return
        
        auth = self.tracking_manager.auth
        
        # Seller (primary)
        if auth.is_authenticated:
            seller_name = auth.seller_name if hasattr(auth, 'seller_name') else auth.character_name
            self.seller_label.configure(text=f"Seller: {seller_name}")
            if hasattr(self, 'npc_orders_manager') and self.npc_orders_manager:
                self.npc_orders_manager.set_seller_character(seller_name)
        else:
            self.seller_label.configure(text="Seller: (not logged in)")
            if hasattr(self, 'npc_orders_manager') and self.npc_orders_manager:
                self.npc_orders_manager.set_seller_character("unknown")
        
        # Buyer (secondary) - only relevant for cross-hub
        if hasattr(auth, 'has_buyer') and auth.has_buyer:
            self.buyer_label.configure(text=f"Buyer: {auth.buyer_name}")
        else:
            self.buyer_label.configure(text="Buyer: (same)")

    def run(self):
        """Start the GUI main loop."""
        self.root.mainloop()
