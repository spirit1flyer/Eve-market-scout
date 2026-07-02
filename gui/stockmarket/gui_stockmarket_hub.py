"""Stock Market hub panel for EVE Market Scout.

Each trading hub gets its own panel with:
- Low/Medium/High Risk tabs: Curated by trend analysis
- Holdings sub-tab: Items being actively tracked/traded
- P&L sub-tab: Profit and loss tracking
- Scrolling ticker at bottom
"""

import tkinter as tk
from tkinter import ttk, messagebox
import asyncio
import threading
from typing import Optional, Callable, List, Dict, TYPE_CHECKING

from core.tk_queue import submit

from core.config import TRADE_HUBS, get_hub_config
from analytics.historical_profiles import ProfileManager, YearlyStats
from gui.stockmarket.gui_stockmarket_ticker import ScrollingTicker
from gui.stockmarket.gui_stockmarket_risk import RiskCategoryPanel, format_isk
from gui.stockmarket.gui_stockmarket_hub_filters import HubFilterPhaseMixin
from gui.stockmarket.gui_stockmarket_hub_refresh import HubPanelRefreshMixin

if TYPE_CHECKING:
    from core.api import ESIClient
    from gui.stockmarket.gui_stockmarket_settings import StockMarketSettings
    from analytics.stockmarket_filters import StockMarketFilters


def _check_thread(context: str):
    """Debug helper - warn if not on main thread."""
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from {current.name}")
        import traceback
        traceback.print_stack(limit=8)


class StockMarketHubPanel(HubPanelRefreshMixin, HubFilterPhaseMixin):
    """Panel for a single trading hub's stock market functionality.
    
    Contains sub-tabs:
    - Low Risk: Stable trend items (green)
    - Medium Risk: Rising trend items (yellow)
    - High Risk: Falling trend items (red)
    - Holdings: Track active positions
    - P&L: Profit and loss tracking
    """
    
    def __init__(
        self,
        parent: ttk.Frame,
        hub_key: str,
        settings: "StockMarketSettings",
        profiles: ProfileManager,
        get_client: Optional[Callable[[], "ESIClient"]] = None,
        set_status: Optional[Callable[[str], None]] = None,
    ):
        self.parent = parent
        self.hub_key = hub_key
        self.hub_config = get_hub_config(hub_key)
        self.settings = settings
        self.profiles = profiles
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        
        # Filters with hub-specific fee calculation (from cached JSON)
        from analytics.stockmarket_filters import load_filters
        self.filters = load_filters()
        self.filters.load_from_cached_skills(hub_key)
        
        self.region_id = self.hub_config["region_id"]
        self.station_id = self.hub_config["station_id"]
        
        # Live prices cache (shared across sub-tabs)
        self.live_prices: Dict[int, float] = {}
        
        # Sub-panels
        self.holdings_panel = None
        self.pnl_panel = None
        self.risk_panels = {}  # "low", "medium", "high"
        
        # Ticker
        self.ticker = None
        
        # Create UI
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create panel widgets."""
        # Main container
        main_container = ttk.Frame(self.frame)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Refresh status header
        header = ttk.Frame(main_container)
        header.pack(fill=tk.X, padx=5, pady=(2, 0))
        self._last_refreshed_var = tk.StringVar(value="Updated: --")
        self._next_refresh_var = tk.StringVar(value="Next: --")
        ttk.Label(header, textvariable=self._last_refreshed_var,
                  font=("Segoe UI", 8), foreground="gray").pack(side=tk.LEFT)
        ttk.Label(header, textvariable=self._next_refresh_var,
                  font=("Segoe UI", 8), foreground="gray").pack(side=tk.LEFT, padx=(12, 0))

        # Create notebook for sub-tabs
        self.sub_notebook = ttk.Notebook(main_container)
        self.sub_notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # Risk category tabs
        for risk_level, tab_name in [("low", "Low Risk"), ("medium", "Med Risk"), ("high", "High Risk")]:
            risk_frame = ttk.Frame(self.sub_notebook)
            self.sub_notebook.add(risk_frame, text=tab_name)
            self._create_risk_tab(risk_frame, risk_level)
        
        # Holdings tab
        holdings_frame = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(holdings_frame, text="Holdings")
        self._create_holdings_tab(holdings_frame)
        
        # P&L tab
        pnl_frame = ttk.Frame(self.sub_notebook)
        self.sub_notebook.add(pnl_frame, text="P&L")
        self._create_pnl_tab(pnl_frame)
        
        # Scrolling ticker at bottom
        self.ticker = ScrollingTicker(self.frame)
        self.ticker.pack(fill=tk.X, side=tk.BOTTOM, padx=5, pady=2)
        
        # Bind tab change to update ticker
        self.sub_notebook.bind("<<NotebookTabChanged>>", lambda e: self._update_ticker())
        
        # Lock overlay (hidden by default).  Shown during material filter +
        # refresh so the user can't interact while the panel is computing.
        # See _show_filter_overlay / _hide_filter_overlay.
        self._overlay_frame = None
        self._overlay_status_var = None
        self._overlay_progress = None

        self._refresh_label_timer: Optional[str] = None
        self._start_refresh_label_timer()
    
    def _create_risk_tab(self, parent: ttk.Frame, risk_level: str):
        """Create a risk category tab."""
        panel = RiskCategoryPanel(
            parent,
            hub_key=self.hub_key,
            risk_level=risk_level,
            profiles=self.profiles,
            filters=self.filters,
            get_client=self.get_client,
            set_status=self.set_status,
            on_item_selected=self._on_item_added_to_holdings,
            on_double_click=self._on_item_double_click,
            settings=getattr(self, "settings", None),
        )
        self.risk_panels[risk_level] = panel
    
    def _create_holdings_tab(self, parent: ttk.Frame):
        """Create the holdings sub-tab."""
        from gui.stockmarket.gui_stockmarket_holdings import HoldingsPanel
        
        self.holdings_panel = HoldingsPanel(
            parent,
            hub_key=self.hub_key,
            profiles=self.profiles,
            get_client=self.get_client,
            set_status=self.set_status,
        )
    
    def _create_pnl_tab(self, parent: ttk.Frame):
        """Create the P&L sub-tab."""
        from gui.stockmarket.gui_stockmarket_pnl import PnLPanel
        
        self.pnl_panel = PnLPanel(
            parent,
            hub_key=self.hub_key,
            set_status=self.set_status,
        )
    
    def _on_item_added_to_holdings(self, type_id: int, type_name: str):
        """Called when user selects an item from a risk panel to watch."""
        if self.holdings_panel:
            self.holdings_panel.add_watched_item(type_id, type_name)
            # Switch to holdings tab (index 3: Low=0, Med=1, High=2, Holdings=3, P&L=4)
            self.sub_notebook.select(3)
    
    def reload_filters_from_cache(self):
        """Reload fee rates from cached skills JSON (called after ESI refresh)."""
        _check_thread(f"HubPanel.reload_filters_from_cache({self.hub_key})")
        self.filters.load_from_cached_skills(self.hub_key)
    
    def _on_item_double_click(self, type_id: int, type_name: str):
        """Handle double-click to open price history graph."""
        from analytics.graphing import show_price_graph
        
        show_price_graph(
            self.frame,
            type_id=type_id,
            type_name=type_name,
            region_id=self.region_id,
            profiles=self.profiles,
        )
    
    def _update_ticker(self):
        """Update ticker in background thread to avoid blocking UI."""
        if not self.ticker:
            return
        
        # Capture current state for thread
        try:
            current_tab = self.sub_notebook.index(self.sub_notebook.select())
        except Exception:
            current_tab = 0
        
        # Get holdings type_ids if needed (safe to read from main thread)
        holdings_type_ids = []
        if current_tab in (3, 4) and self.holdings_panel:
            holdings_type_ids = [e.type_id for e in self.holdings_panel.holdings.get_all()]
        
        # Copy live prices for thread safety
        live_prices_copy = dict(self.live_prices)
        
        def compute_ticker():
            """Background thread: compute ticker items (DB queries here)."""
            from sde.sde_manager import get_sde_manager
            sde = get_sde_manager()
            
            # Tab indices: 0=Low Risk, 1=Med Risk, 2=High Risk, 3=Holdings, 4=P&L
            if current_tab in (3, 4):
                # Holdings or P&L - show only holdings
                profiles_to_show = [
                    self.profiles.get_computed_profile(tid, self.region_id)
                    for tid in holdings_type_ids
                ]
                profiles_to_show = [p for p in profiles_to_show if p]
            else:
                # Risk tabs - filter by risk level
                risk_map = {0: "low", 1: "medium", 2: "high"}
                risk_level = risk_map.get(current_tab, "low")
                
                all_profiles = self.profiles.get_all_profiles()
                profiles_to_show = []
                
                for profile in all_profiles:
                    if profile.region_id != self.region_id:
                        continue
                    
                    yearly_stats = self.profiles.get_yearly_stats(profile.type_id, self.region_id)
                    trend = self._get_trend_for_ticker(yearly_stats)
                    
                    if trend == risk_level:
                        profiles_to_show.append(profile)
            
            # Calculate % change for all items
            items_with_change = []
            for profile in profiles_to_show:
                if not profile:
                    continue
                
                current = live_prices_copy.get(profile.type_id, 0)
                if current <= 0 or profile.weighted_p_low <= 0:
                    continue
                
                pct_change = ((current - profile.weighted_p_low) / profile.weighted_p_low) * 100
                type_name = sde.get_type_name(profile.type_id) or f"Type {profile.type_id}"
                items_with_change.append((type_name, pct_change))
            
            # Sort by absolute % change (biggest movers), take top 20
            items_with_change.sort(key=lambda x: abs(x[1]), reverse=True)
            ticker_items = items_with_change[:20]
            
            # Update UI on main thread
            submit(lambda: self._apply_ticker_items(ticker_items))
        
        threading.Thread(target=compute_ticker, daemon=True).start()
    
    def _apply_ticker_items(self, ticker_items: list):
        """Apply computed ticker items to UI (main thread only)."""
        if self.ticker:
            self.ticker.update_items(ticker_items)
    
    def _get_trend_for_ticker(self, yearly_stats: dict) -> str:
        """Determine trend from yearly stats for ticker filtering."""
        if len(yearly_stats) < 2:
            return "none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "none"
        
        # Declining = high risk
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "high"
        
        # Rising = medium risk
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "medium"
        
        # Check stability = low risk
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                if max_deviation <= 15:
                    return "low"
        
        return "none"
    
    # =========================================================================
    # External API
    # =========================================================================
    
    def update_settings(self, settings: "StockMarketSettings"):
        """Update settings reference."""
        self.settings = settings
        self.refresh_display_async()
    
    def update_live_prices(self, prices: Dict[int, float]):
        """Update live prices in all sub-tabs.
        
        Only updates price-dependent columns, NOT full refresh.
        Full refresh (with DB queries) only happens on startup or daily material filter.
        """
        old_count = len(self.live_prices)
        self.live_prices.update(prices)
        new_count = len(self.live_prices)
        print(f"[StockMarket-{self.hub_key}] update_live_prices: received {len(prices)}, total now {new_count} (was {old_count})")
        
        # Update price data in panels
        for panel in self.risk_panels.values():
            panel.live_prices.update(prices)
        
        if self.holdings_panel:
            self.holdings_panel.live_prices.update(prices)
        
        # Update only price columns (fast, no DB queries)
        self._update_prices_only()
        
        # Update ticker with new prices
        self._update_ticker()
    
    def _update_prices_only(self):
        """Update only price-dependent columns in all panels.
        
        Called on every scan. Much faster than refresh_display().
        """
        for panel in self.risk_panels.values():
            if hasattr(panel, 'update_prices_only'):
                panel.update_prices_only()
        
        if self.holdings_panel and hasattr(self.holdings_panel, 'update_prices_only'):
            self.holdings_panel.update_prices_only()
    
    def get_type_ids(self) -> List[int]:
        """Get all type IDs being tracked (from holdings)."""
        if self.holdings_panel:
            return [e.type_id for e in self.holdings_panel.holdings.get_all()]
        return []
    
    def refresh_display(self):
        """Full refresh of all sub-panels with DB queries.
        
        Use sparingly - only on startup, manual refresh, or after
        apply_material_filter().  For price updates use
        update_live_prices() instead.
        
        Note: This runs on main thread. For startup, use
        refresh_display_async().
        
        Material filter is NOT gated here.  _get_trend() in the risk
        panels reads from the pre-populated material risk cache.
        apply_material_filter() is the single entry-point that clears
        the cache, re-analyzes, marks the tracker complete, and then
        calls this method.
        """
        for panel in self.risk_panels.values():
            panel.refresh_display()
        
        if self.holdings_panel:
            self.holdings_panel.refresh_display()
        
        self._update_ticker()
    
    def add_item(self, type_id: int, type_name: str, auto_build_profile: bool = True):
        """Add item to holdings."""
        if self.holdings_panel:
            self.holdings_panel.add_watched_item(type_id, type_name)
        
        if auto_build_profile and not self.profiles.has_profile(type_id, self.region_id):
            self._build_profile_async(type_id, self.region_id, type_name)
    
    def sync_from_orders(self, orders: List[dict]):
        """Sync holdings from ESI order data."""
        if self.holdings_panel:
            self.holdings_panel.sync_from_orders(orders)
    
    def sync_from_esi_wallet(self, wallet) -> dict:
        """Sync holdings from ESI wallet transactions."""
        if self.holdings_panel:
            return self.holdings_panel.sync_from_esi_wallet(wallet)
        return {"buys_synced": 0, "sales_synced": 0}
    
    def _build_profile_async(self, type_id: int, region_id: int, type_name: str):
        """Build profile in background."""
        self.set_status(f"Building profile for {type_name}...")
        
        def build():
            success = self.profiles.extract_item(type_id, region_id)
            submit(lambda: self._on_profile_built(type_name, success))
        
        threading.Thread(target=build, daemon=True).start()
    
    def _build_profiles_batch(self, items: List[tuple]):
        """Build profiles for multiple items."""
        self.set_status(f"Building profiles for {len(items)} items...")
        
        def build():
            built = 0
            for type_id, type_name in items:
                if self.profiles.extract_item(type_id, self.region_id):
                    built += 1
            submit(lambda: self._on_batch_complete(built, len(items)))
        
        threading.Thread(target=build, daemon=True).start()
    
    def _on_profile_built(self, type_name: str, success: bool):
        """Called when single profile build completes."""
        if success:
            self.set_status(f"Profile built for {type_name}")
        else:
            self.set_status(f"Failed to build profile for {type_name}")
        self.refresh_display()
    
    def _on_batch_complete(self, built: int, total: int):
        """Called when batch profile build completes."""
        self.set_status(f"Built {built}/{total} profiles")
        self.refresh_display()
    
    def _start_refresh_label_timer(self):
        self._refresh_label_timer = self.frame.after(30_000, self._on_refresh_label_tick)

    def _on_refresh_label_tick(self):
        client = self.get_client() if self.get_client else None
        if client:
            self.update_refresh_labels(client.order_cache)
        self._refresh_label_timer = self.frame.after(30_000, self._on_refresh_label_tick)

    def destroy(self):
        """Clean up."""
        if self._refresh_label_timer is not None:
            self.frame.after_cancel(self._refresh_label_timer)
            self._refresh_label_timer = None
