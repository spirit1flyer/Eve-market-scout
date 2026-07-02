"""Stock Market risk category panel for EVE Market Scout.

Contains:
- format_isk(): Shared ISK formatting utility
- RiskCategoryPanel: Panel for Low/Medium/High risk tabs
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional, Callable, List, Dict, TYPE_CHECKING

from core.config import get_hub_config
from analytics.historical_profiles import ProfileManager, YearlyStats

if TYPE_CHECKING:
    from core.api import ESIClient
    from analytics.stockmarket_filters import StockMarketFilters


def format_isk(value: float) -> str:
    """Format ISK value with K/M/B suffix."""
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    elif value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    elif value >= 1_000:
        return f"{value / 1_000:.1f}K"
    else:
        return f"{value:.0f}"


class RiskCategoryPanel:
    """Panel showing items filtered by risk category."""
    
    def __init__(
        self,
        parent: ttk.Frame,
        hub_key: str,
        risk_level: str,  # "low", "medium", "high"
        profiles: ProfileManager,
        filters: "StockMarketFilters" = None,
        get_client: Optional[Callable] = None,
        set_status: Optional[Callable[[str], None]] = None,
        on_item_selected: Optional[Callable[[int, str], None]] = None,
        on_double_click: Optional[Callable[[int, str], None]] = None,
        settings=None,
    ):
        self.parent = parent
        self.hub_key = hub_key
        self.risk_level = risk_level
        self.profiles = profiles
        self.filters = filters
        self.settings = settings
        self.get_client = get_client
        self.set_status = set_status or (lambda s: None)
        self.on_item_selected = on_item_selected
        self.on_double_click = on_double_click
        
        self.hub_config = get_hub_config(hub_key)
        self.region_id = self.hub_config["region_id"]
        
        # Live prices
        self.live_prices: Dict[int, float] = {}
        
        # Create UI
        self.frame = ttk.Frame(parent)
        self.frame.pack(fill=tk.BOTH, expand=True)
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create panel widgets."""
        # Toolbar
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        
        ttk.Button(
            toolbar,
            text="Add to Holdings",
            command=self._on_add_to_holdings
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(
            toolbar,
            text="Refresh",
            command=self.refresh_display
        ).pack(side=tk.LEFT, padx=2)
        
        ttk.Frame(toolbar).pack(side=tk.LEFT, expand=True)  # Spacer
        
        self.count_label = ttk.Label(toolbar, text="0 items")
        self.count_label.pack(side=tk.RIGHT, padx=5)
        
        # Treeview
        self._create_treeview()
    
    def _create_treeview(self):
        """Create the treeview with sortable columns."""
        tree_frame = ttk.Frame(self.frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=(0, 5))
        
        columns = (
            "sig", "li", "profit", "name", "qty", "buying", "selling", "avg_cost", "current",
            "target_buy", "target_sell", "hist_min", "hist_max", "volume", "7d_day"
        )
        
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="extended"
        )
        
        # Column config: (id, title, width, anchor)
        self.col_config = [
            ("sig", "Signal", 50, tk.CENTER),
            ("li", "LI", 32, tk.CENTER),
            ("profit", "Profit", 70, tk.E),
            ("name", "Item Name", 180, tk.W),
            ("qty", "Qty", 50, tk.E),
            ("buying", "Buy", 45, tk.E),
            ("selling", "Sell", 45, tk.E),
            ("avg_cost", "Avg Cost", 75, tk.E),
            ("current", "Current", 75, tk.E),
            ("target_buy", "Buy Target", 80, tk.E),
            ("target_sell", "Sell Target", 80, tk.E),
            ("hist_min", "Hist Min", 75, tk.E),
            ("hist_max", "Hist Max", 75, tk.E),
            ("volume", "Volume", 70, tk.E),
            ("7d_day", "7d/Day", 70, tk.E),
        ]
        
        # Build base titles dict for sort manager
        self.col_titles = {col_id: title for col_id, title, _, _ in self.col_config}
        
        # Numeric columns for sorting
        self.numeric_cols = {
            "profit", "qty", "buying", "selling", "avg_cost", "current",
            "target_buy", "target_sell", "hist_min", "hist_max", "volume", "7d_day"
        }
        
        # Nested sort manager
        from gui.gui_tree_utils import NestedSortManager
        self.sort_manager = NestedSortManager(numeric_columns=self.numeric_cols)
        
        for col_id, heading, width, anchor in self.col_config:
            self.tree.heading(col_id, text=heading, command=lambda c=col_id: self._sort_by(c))
            self.tree.column(col_id, width=width, anchor=anchor)
        
        # Scrollbars
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Tags for trend-based row coloring (raw floor direction, before material filter)
        self.tree.tag_configure("trend_down", background="#DC143C", foreground="white")   # Red - declining floors
        self.tree.tag_configure("trend_up", background="#FFD700", foreground="black")     # Yellow - rising floors
        self.tree.tag_configure("trend_stable", background="#228B22", foreground="white") # Green - stable floors
        self.tree.tag_configure("trend_none", background="#ffffff", foreground="black")   # White - insufficient data
        
        # Context menu
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="Add to Holdings", command=self._on_add_to_holdings)
        self.context_menu.add_command(label="View Graph", command=self._on_view_graph)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Show Indicator Details", command=self._on_show_indicator_details)
        self.context_menu.add_command(label="Indicator Help", command=self._on_indicator_help)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Name", command=self._on_copy_name)
        
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", self._on_double_click_handler)
    
    def _sort_by(self, column: str):
        """Sort treeview by column using nested sort."""
        import time as _pt
        _pt0 = _pt.perf_counter()
        self.sort_manager.on_column_click(column)
        _t1 = _pt.perf_counter()
        self.sort_manager.apply_sort(self.tree)
        _t2 = _pt.perf_counter()
        self.sort_manager.update_headers(self.tree, self.col_titles)
        _pt_total = _pt.perf_counter() - _pt0
        _rows = len(self.tree.get_children())
        print(
            f"[PerfTimer] RiskCategoryPanel._sort_by column={column} total={_pt_total*1000:.0f}ms rows={_rows} "
            f"on_column_click={(_t1-_pt0)*1000:.0f}ms "
            f"apply_sort={(_t2-_t1)*1000:.0f}ms "
            f"update_headers={(_pt.perf_counter()-_t2)*1000:.0f}ms"
        )
    
    def _on_right_click(self, event):
        """Show context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)
    
    def _on_double_click_handler(self, event):
        """Handle double-click."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        type_name = sde.get_type_name(type_id) or f"Type {type_id}"
        
        if self.on_double_click:
            self.on_double_click(type_id, type_name)
        else:
            self._show_graph(type_id, type_name)
    
    def _on_view_graph(self):
        """View price history graph for selected item."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        type_name = sde.get_type_name(type_id) or f"Type {type_id}"
        
        self._show_graph(type_id, type_name)
    
    def _show_graph(self, type_id: int, type_name: str):
        """Show price history graph."""
        from analytics.graphing import show_price_graph
        show_price_graph(
            self.frame,
            type_id=type_id,
            type_name=type_name,
            region_id=self.region_id,
            profiles=self.profiles,
        )
    
    def _on_add_to_holdings(self):
        """Add selected items to holdings."""
        selected = self.tree.selection()
        if not selected:
            return
        
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        
        for iid in selected:
            type_id = int(iid)
            type_name = sde.get_type_name(type_id) or f"Type {type_id}"
            
            if self.on_item_selected:
                self.on_item_selected(type_id, type_name)
        
        self.set_status(f"Added {len(selected)} item(s) to holdings")
    
    def _on_copy_name(self):
        """Copy item name to clipboard."""
        selected = self.tree.selection()
        if not selected:
            return
        
        item = self.tree.item(selected[0])
        name = item["values"][3]  # name column (was index 2 before LI insertion)
        
        self.frame.clipboard_clear()
        self.frame.clipboard_append(name)
        self.set_status(f"Copied: {name}")
    
    def _on_show_indicator_details(self):
        """Show leading indicator details for the selected item."""
        selected = self.tree.selection()
        if not selected:
            return
        
        type_id = int(selected[0])
        item = self.tree.item(selected[0])
        try:
            type_name = item["values"][3]
        except (IndexError, KeyError):
            type_name = f"Type {type_id}"
        
        # Load current cached result for this item
        try:
            from analytics import leading_indicators_storage
            cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
            result = cache.get(type_id)
        except Exception as e:
            print(f"[RiskPanel] LI details lookup error: {e}")
            result = None
        
        from gui.gui_indicator_help import show_indicator_details_dialog
        show_indicator_details_dialog(self.frame, type_name, result)
    
    def _on_indicator_help(self):
        """Show the general indicator reference dialog."""
        from gui.gui_indicator_help import show_indicator_help_dialog
        show_indicator_help_dialog(self.frame)
    
    def _get_trend(self, yearly_stats: Dict[int, YearlyStats], profile=None) -> str:
        """Determine trend from yearly stats.
        
        Uses year-over-year floor comparison:
        - Declining floors = high risk
        - Rising floors = medium risk  
        - Stable floors = low risk
        
        If material filter is enabled, low-risk items with declining TBC
        get promoted to medium risk.
        
        Leading indicators promotion (after material filter):
        UNDERCUT SPIRAL or LIQUIDITY DRAIN bumps the item one tier up
        (low -> medium, medium -> high). High Risk stays High Risk.
        """
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
        base_tier = None
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            base_tier = "medium"
        else:
            # Check stability = low risk candidate
            is_stable = False
            if len(floors) >= 2:
                avg_floor = sum(floors) / len(floors)
                if avg_floor > 0:
                    max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                    if max_deviation <= 15:
                        is_stable = True
            
            if not is_stable:
                return "none"
            
            # Stable floors - check material filter using cached results
            # Cache is pre-populated by HubPanel.apply_material_filter()
            if profile:
                from analytics.stockmarket_filters import check_material_risk
                material_result = check_material_risk(profile.type_id, self.region_id)
                if material_result == 'medium':
                    base_tier = "medium"  # Promote to medium risk
                else:
                    base_tier = "low"
            else:
                base_tier = "low"
        
        # Leading indicators promotion: UNDERCUT SPIRAL or LIQUIDITY DRAIN
        # bumps one tier up. High Risk caps out (no further promotion).
        if profile and base_tier in ("low", "medium"):
            li_result = self._li_lookup(profile.type_id)
            if li_result and li_result.is_promotion:
                if base_tier == "low":
                    return "medium"
                if base_tier == "medium":
                    return "high"
        
        return base_tier
    
    def _li_lookup(self, type_id: int):
        """Lookup the cached leading indicator result for one item.
        
        Loads the per-region cache lazily and memoizes it for the
        duration of one populate pass. The cache is invalidated when
        populate is called again (which clears _li_cache_for_trend).
        """
        if not hasattr(self, "_li_cache_for_trend") or self._li_cache_for_trend is None:
            try:
                from analytics import leading_indicators_storage
                self._li_cache_for_trend = (
                    leading_indicators_storage.load_for_region(self.region_id)
                )
            except Exception as e:
                print(f"[RiskPanel-{self.risk_level}] _li_lookup error: {e}")
                self._li_cache_for_trend = {}
        return self._li_cache_for_trend.get(type_id)
    
    def _get_trend_tag(self, yearly_stats: Dict[int, YearlyStats]) -> str:
        """Get raw floor direction for row coloring (no material filter).
        
        Returns trend tag based purely on floor comparison:
        - trend_down: declining floors (red)
        - trend_up: rising floors (yellow)
        - trend_stable: stable floors (green)
        - trend_none: insufficient data (white)
        """
        if len(yearly_stats) < 2:
            return "trend_none"
        
        years = sorted(yearly_stats.keys(), reverse=True)
        floors = [yearly_stats[y].p_low for y in years[:3]]
        
        if len(floors) < 2:
            return "trend_none"
        
        # Declining floors
        declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
        if declining:
            return "trend_down"
        
        # Rising floors
        rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
        if rising:
            return "trend_up"
        
        # Check stability
        if len(floors) >= 2:
            avg_floor = sum(floors) / len(floors)
            if avg_floor > 0:
                max_deviation = max(abs(f - avg_floor) / avg_floor * 100 for f in floors)
                if max_deviation <= 15:
                    return "trend_stable"
        
        return "trend_none"
    
    def _calculate_trend(self, type_id: int) -> Optional[float]:
        """Calculate 7d vs 30d price trend."""
        if not self.get_client:
            return None
        
        client = self.get_client()
        if not client:
            return None
        
        from scanning.scanner_common import parse_history_stats
        
        region_cache = client.history_cache.get(self.region_id, {})
        history = region_cache.get(type_id, [])
        
        if not history or len(history) < 7:
            return None
        
        stats = parse_history_stats(history)
        
        if stats.avg_price_7d <= 0 or stats.avg_price_30d <= 0:
            return None
        
        return ((stats.avg_price_7d - stats.avg_price_30d) / stats.avg_price_30d) * 100
    
    def refresh_display(self, run_material_filter: bool = False):
        """Refresh the display with filtered items.

        Args:
            run_material_filter: Legacy parameter, unused. Material filter
                                 results are read from the session cache
                                 (populated by HubPanel.apply_material_filter).
        """
        import time as _pt
        _pt0 = _pt.perf_counter()
        # Bump generation so any in-flight chunked insert from a prior
        # refresh aborts before writing into the freshly-cleared tree.
        self._populate_gen = getattr(self, "_populate_gen", 0) + 1
        gen = self._populate_gen

        # Clear existing
        _ts = _pt.perf_counter()
        for item in self.tree.get_children():
            self.tree.delete(item)
        _step_tree_clear = _pt.perf_counter() - _ts

        # Load leading indicators cache for this region (one query)
        _ts = _pt.perf_counter()
        try:
            from analytics import leading_indicators_storage
            li_cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
        except Exception as e:
            print(f"[RiskPanel-{self.risk_level}] LI cache load error: {e}")
            li_cache = {}
        _step_li_cache = _pt.perf_counter() - _ts
        self._li_cache_for_trend = li_cache

        # Get all profiles for this region
        _ts = _pt.perf_counter()
        all_profiles = self.profiles.get_all_profiles()
        region_profiles = [p for p in all_profiles if p.region_id == self.region_id]
        _step_profiles_filter = _pt.perf_counter() - _ts

        # Volume gate — drop items below the configured min_daily_volume.
        # Reads from StockMarketSettings (what the Settings dialog writes
        # to), not StockMarketFilters which is a separate object.
        min_volume = (
            self.settings.min_daily_volume if self.settings else 0
        )
        print(
            f"[RiskPanel-{self.hub_key}-{self.risk_level}] "
            f"gate: settings={self.settings is not None} "
            f"min_volume={min_volume} "
            f"profiles={len(region_profiles)}"
        )

        # Filter by risk level
        items_to_show = []

        _ts = _pt.perf_counter()
        _yearly_stats_calls = 0
        for profile in region_profiles:
            if min_volume > 0 and getattr(profile, "avg_daily_volume", 0) < min_volume:
                continue
            yearly_stats = self.profiles.get_yearly_stats(profile.type_id, self.region_id)
            _yearly_stats_calls += 1
            trend = self._get_trend(yearly_stats, profile)

            if trend == self.risk_level:
                current_price = self.live_prices.get(profile.type_id, 0)
                trend_pct = self._calculate_trend(profile.type_id)
                trend_tag = self._get_trend_tag(yearly_stats)
                items_to_show.append({
                    "profile": profile,
                    "yearly_stats": yearly_stats,
                    "current_price": current_price,
                    "trend_pct": trend_pct,
                    "trend_tag": trend_tag,
                })
        _step_per_item_loop = _pt.perf_counter() - _ts

        # Get SDE for names
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()

        # Build rows (insertion happens via _chunked_insert below)
        rows = []
        for item in items_to_show:
            profile = item["profile"]
            current_price = item["current_price"]
            trend_pct = item["trend_pct"]
            trend_tag = item["trend_tag"]

            type_name = sde.get_type_name(profile.type_id) or f"Type {profile.type_id}"

            # Determine signal and profit separately
            sig = ""
            profit_str = "--"
            if current_price > 0 and self.filters:
                signal_result = self.filters.calculate_signal_profit(
                    current_price, profile.weighted_p_low, profile.weighted_p_high
                )
                if signal_result:
                    signal_type, profit = signal_result
                    sig = f"[{signal_type}]"
                    sign = "+" if profit > 0 else "-"
                    profit_str = f"{sign}{format_isk(abs(profit))}"
            elif current_price > 0:
                # Fallback if no filters
                if current_price < profile.weighted_p_low:
                    sig = "[B]"
                elif current_price > profile.weighted_p_high:
                    sig = "[S]"

            # Format trend
            if trend_pct is not None:
                trend_str = f"{trend_pct:+.1f}%"
            else:
                trend_str = "--"

            # Leading indicator letter (worst flag wins, blank if no data)
            from gui.gui_indicator_help import get_indicator_letter
            li_result = li_cache.get(profile.type_id)
            li_letter = (
                get_indicator_letter(li_result.flags)
                if li_result else ""
            )

            values = (
                sig,
                li_letter,
                profit_str,
                type_name,
                "--",  # qty
                "--",  # buying
                "--",  # selling
                "--",  # avg_cost
                format_isk(current_price) if current_price > 0 else "--",
                format_isk(profile.weighted_p_low),
                format_isk(profile.weighted_p_high),
                format_isk(profile.hist_min) if profile.hist_min > 0 else "--",
                format_isk(profile.hist_max) if profile.hist_max > 0 else "--",
                format_isk(profile.avg_daily_volume) if profile.avg_daily_volume > 0 else "--",
                trend_str,
            )
            rows.append((str(profile.type_id), values, trend_tag))

        # Update count up front so the user sees the total while rows stream in
        self.count_label.configure(text=f"{len(rows)} items")

        def _on_done():
            if self.sort_manager.primary:
                self.sort_manager.apply_sort(self.tree)
                self.sort_manager.update_headers(self.tree, self.col_titles)

        self._chunked_insert(rows, gen, _on_done)
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] RiskCategoryPanel.refresh_display hub={self.hub_key} risk={self.risk_level} "
            f"total={_pt_total*1000:.0f}ms profiles={len(region_profiles)} rows={len(rows)} "
            f"yearly_stats_calls={_yearly_stats_calls} "
            f"tree_clear={_step_tree_clear*1000:.0f}ms "
            f"li_cache={_step_li_cache*1000:.0f}ms "
            f"profiles_filter={_step_profiles_filter*1000:.0f}ms "
            f"per_item_loop={_step_per_item_loop*1000:.0f}ms "
            f"(chunked_insert async, not in total)"
        )

    def update_filters(self, filters: "StockMarketFilters"):
        """Update filters reference."""
        self.filters = filters
    
    def refresh_from_data(self, items: list):
        """Refresh display from pre-computed data (no DB queries).

        Args:
            items: List of dicts with keys: type_id, type_name, profile, current_price, trend_tag
        """
        # Bump generation so any in-flight chunked insert from a prior
        # refresh aborts before writing into the freshly-cleared tree.
        self._populate_gen = getattr(self, "_populate_gen", 0) + 1
        gen = self._populate_gen

        # Clear existing
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Load leading indicators cache for this region (one query)
        try:
            from analytics import leading_indicators_storage
            li_cache = leading_indicators_storage.load_for_region(
                self.region_id
            )
        except Exception as e:
            print(f"[RiskPanel-{self.risk_level}] LI cache load error: {e}")
            li_cache = {}
        self._li_cache_for_trend = li_cache

        # Build rows (insertion happens via _chunked_insert below)
        rows = []
        for item in items:
            profile = item["profile"]
            type_name = item["type_name"]
            current_price = item["current_price"]
            trend_tag = item["trend_tag"]
            trend_pct = item.get("trend_pct")

            # Determine signal and profit
            sig = ""
            profit_str = "--"
            if current_price > 0 and self.filters:
                signal_result = self.filters.calculate_signal_profit(
                    current_price, profile.weighted_p_low, profile.weighted_p_high
                )
                if signal_result:
                    signal_type, profit = signal_result
                    sig = f"[{signal_type}]"
                    sign = "+" if profit > 0 else "-"
                    profit_str = f"{sign}{format_isk(abs(profit))}"
            elif current_price > 0:
                if current_price < profile.weighted_p_low:
                    sig = "[B]"
                elif current_price > profile.weighted_p_high:
                    sig = "[S]"

            # Leading indicator letter (worst flag wins, blank if no data)
            from gui.gui_indicator_help import get_indicator_letter
            li_result = li_cache.get(profile.type_id)
            li_letter = (
                get_indicator_letter(li_result.flags)
                if li_result else ""
            )

            values = (
                sig,
                li_letter,
                profit_str,
                type_name,
                "--",  # qty
                "--",  # buying
                "--",  # selling
                "--",  # avg_cost
                format_isk(current_price) if current_price > 0 else "--",
                format_isk(profile.weighted_p_low),
                format_isk(profile.weighted_p_high),
                format_isk(profile.hist_min) if profile.hist_min > 0 else "--",
                format_isk(profile.hist_max) if profile.hist_max > 0 else "--",
                format_isk(profile.avg_daily_volume) if profile.avg_daily_volume > 0 else "--",
                f"{trend_pct:+.1f}%" if trend_pct is not None else "--",
            )
            rows.append((str(profile.type_id), values, trend_tag))

        # Update count up front so the user sees the total while rows stream in
        self.count_label.configure(text=f"{len(rows)} items")

        def _on_done():
            if self.sort_manager.primary:
                self.sort_manager.apply_sort(self.tree)
                self.sort_manager.update_headers(self.tree, self.col_titles)

        self._chunked_insert(rows, gen, _on_done)

    def _chunked_insert(self, rows, gen, on_done, idx=0, chunk_size=100):
        """Insert tree rows in chunks, yielding the mainloop between chunks.

        Avoids freezing tab clicks while a refresh populates hundreds of
        items. A generation token is captured at the start of a populate;
        if a newer refresh has bumped self._populate_gen, the in-flight
        chunks abort so they don't write into the cleared tree.
        """
        if gen != getattr(self, "_populate_gen", 0):
            return
        end = min(idx + chunk_size, len(rows))
        for i in range(idx, end):
            iid, values, trend_tag = rows[i]
            self.tree.insert(
                "",
                tk.END,
                iid=iid,
                values=values,
                tags=(trend_tag,),
            )
        if end < len(rows):
            self.tree.after(
                1,
                lambda: self._chunked_insert(rows, gen, on_done, end, chunk_size),
            )
        else:
            on_done()

    def update_prices_only(self):
        """Update only price-dependent columns without rebuilding the treeview.
        
        This is called on every scan (~5 min) to update current prices.
        Much faster than refresh_display() since it doesn't query the DB.
        """
        for iid in self.tree.get_children():
            type_id = int(iid)
            current_price = self.live_prices.get(type_id, 0)
            
            # Get existing values
            values = list(self.tree.item(iid, "values"))
            if len(values) < 15:
                continue
            
            # Get profile for signal calculation (from cached data, not DB)
            # We need p_low and p_high which are stored in the treeview already
            # Parse them from columns 9 and 10 (target_buy, target_sell)
            try:
                p_low_str = values[9]  # target_buy column
                p_high_str = values[10]  # target_sell column
                p_low = self._parse_isk(p_low_str)
                p_high = self._parse_isk(p_high_str)
            except (ValueError, IndexError):
                p_low = 0
                p_high = 0
            
            # Calculate signal and profit
            sig = ""
            profit_str = "--"
            if current_price > 0 and p_low > 0 and p_high > 0:
                if self.filters:
                    signal_result = self.filters.calculate_signal_profit(
                        current_price, p_low, p_high
                    )
                    if signal_result:
                        signal_type, profit = signal_result
                        sig = f"[{signal_type}]"
                        sign = "+" if profit > 0 else "-"
                        profit_str = f"{sign}{format_isk(abs(profit))}"
                else:
                    # Fallback if no filters
                    if current_price < p_low:
                        sig = "[B]"
                    elif current_price > p_high:
                        sig = "[S]"
            
            # Update only the columns that depend on live price
            values[0] = sig  # signal (col 0)
            values[2] = profit_str  # profit (col 2 after LI insertion)
            values[8] = format_isk(current_price) if current_price > 0 else "--"  # current price (col 8)
            
            self.tree.item(iid, values=values)
    
    def _parse_isk(self, value_str: str) -> float:
        """Parse ISK string back to float (e.g., '1.5M' -> 1500000)."""
        if not value_str or value_str == "--":
            return 0
        
        value_str = value_str.strip()
        multiplier = 1
        
        if value_str.endswith("B"):
            multiplier = 1_000_000_000
            value_str = value_str[:-1]
        elif value_str.endswith("M"):
            multiplier = 1_000_000
            value_str = value_str[:-1]
        elif value_str.endswith("K"):
            multiplier = 1_000
            value_str = value_str[:-1]
        
        try:
            return float(value_str) * multiplier
        except ValueError:
            return 0
