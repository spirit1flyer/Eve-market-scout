"""Deal display tabs for EVE Market Scout - Low Risk, High Risk, Steals."""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from scanning.scanner_common import Deal, StealColor
from gui.gui_filters import FilterManager


# Column definitions for deal treeviews
DEAL_COLUMNS = (
    "name", "system", "buy_price", "buy_order", "ceiling", "break_even",
    "unit_profit", "et_flip", "volume", "raw_volume", "total_profit",
    "avg_7d", "avg_30d", "vol_30d", "vol_7d"
)

NUMERIC_COLUMNS = {
    "buy_price", "ceiling", "break_even", "unit_profit", "et_flip",
    "volume", "raw_volume", "total_profit", "buy_order", "avg_7d", "avg_30d", "vol_30d", "vol_7d"
}


class DealsTabManager:
    """Manages the three deal tabs: Low Risk, High Risk, and Steals."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        filter_manager: FilterManager,
        set_status: Callable[[str], None],
        get_column_title: Callable[[str], str],
        sort_state: dict,
        root: Optional[tk.Tk] = None,
        get_client: Optional[Callable] = None,
        get_buy_station: Optional[Callable[[], str]] = None,
        get_sell_station: Optional[Callable[[], str]] = None
    ):
        self.notebook = notebook
        self.filter_manager = filter_manager
        self.set_status = set_status
        self.get_column_title = get_column_title
        self.sort_state = sort_state
        
        # For price history graph
        self.root = root
        self.get_client = get_client
        self.get_buy_station = get_buy_station
        self.get_sell_station = get_sell_station
        
        # Store current deals for lookups (all categories combined)
        self.all_deals: list[Deal] = []
        self.current_deal_ids: set[int] = set()
        
        # Watchlist manager reference (set by gui_main after creation)
        self.watchlist_manager = None
        
        # Tracking manager reference (set by gui_main after creation)
        self.tracking_manager = None
        
        # Stock market tab reference (set by gui_main after creation)
        self.stock_market_tab = None

        # Create the three tabs
        self._create_low_risk_tab()
        self._create_high_risk_tab()
        self._create_steals_tab()
        
        # Context menu
        self._create_context_menu()

    def _create_low_risk_tab(self):
        """Create Low Risk deals tab."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Low Risk")

        self.low_risk_tree = self._create_tree(frame)
        self._configure_tags(self.low_risk_tree, is_low_risk=True)

    def _create_high_risk_tab(self):
        """Create High Risk deals tab."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="High Risk")

        self.high_risk_tree = self._create_tree(frame)
        self._configure_tags(self.high_risk_tree, is_low_risk=False)

    def _create_steals_tab(self):
        """Create Steals tab for fat-finger mistakes."""
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Steals")

        self.steals_tree = self._create_tree(frame)
        self._configure_steal_tags(self.steals_tree)

    def _create_tree(self, parent: ttk.Frame) -> ttk.Treeview:
        """Create a treeview with columns and scrollbars."""
        tree = ttk.Treeview(
            parent,
            columns=DEAL_COLUMNS,
            show="headings",
            style="Deals.Treeview"
        )

        # Column headings with sorting
        for col in DEAL_COLUMNS:
            tree.heading(
                col,
                text=self.get_column_title(col),
                command=lambda c=col, t=tree: self._sort_tree(t, c)
            )

        # Column widths
        self._configure_columns(tree)

        # Scrollbars
        vsb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        hsb = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        # Bindings
        tree.bind("<Double-1>", self._show_price_history)
        tree.bind("<Button-3>", self._show_context_menu)

        return tree

    def _configure_columns(self, tree: ttk.Treeview):
        """Configure column widths."""
        tree.column("name", width=180, minwidth=120)
        tree.column("system", width=70, minwidth=60)
        tree.column("buy_price", width=90, anchor=tk.E)
        tree.column("buy_order", width=90, anchor=tk.E)
        tree.column("ceiling", width=90, anchor=tk.E)
        tree.column("break_even", width=90, anchor=tk.E)
        tree.column("unit_profit", width=80, anchor=tk.E)
        tree.column("et_flip", width=50, anchor=tk.E)
        tree.column("volume", width=60, anchor=tk.E)
        tree.column("raw_volume", width=70, anchor=tk.E)
        tree.column("total_profit", width=100, anchor=tk.E)
        tree.column("avg_7d", width=110, anchor=tk.E)
        tree.column("avg_30d", width=90, anchor=tk.E)
        tree.column("vol_30d", width=70, anchor=tk.E)
        tree.column("vol_7d", width=90, anchor=tk.E)

    def _configure_tags(self, tree: ttk.Treeview, is_low_risk: bool):
        """Configure color tags for a tree."""
        tree.tag_configure("loss", foreground="red")
        tree.tag_configure("mistake", foreground="#00AA00", background="#E8FFE8")
        tree.tag_configure("buy_flagged", foreground="white", background="#0066CC")
        tree.tag_configure("ignored", foreground="gray")
        
        if is_low_risk:
            tree.tag_configure("profit", foreground="green")
        else:
            # High risk color coding by number of risk flags (background colors like steals)
            tree.tag_configure("risky_yellow", foreground="black", background="#FFD700")  # 1 flag - yellow
            tree.tag_configure("risky_orange", foreground="white", background="#FF8C00")  # 2 flags - orange
            tree.tag_configure("risky_red", foreground="white", background="#DC143C")     # 3+ flags - red
            tree.tag_configure("risky", foreground="white", background="#FF8C00")         # fallback

    def _configure_steal_tags(self, tree: ttk.Treeview):
        """Configure tags for steals tree with color coding."""
        # Green = 0 risk flags (safe steal)
        tree.tag_configure("steal_green", foreground="white", background="#228B22")
        # Yellow = 1 risk flag (minor concern)
        tree.tag_configure("steal_yellow", foreground="black", background="#FFD700")
        # Red = 2+ risk flags (risky steal)
        tree.tag_configure("steal_red", foreground="white", background="#DC143C")
        # Flagged/ignored
        tree.tag_configure("buy_flagged", foreground="white", background="#0066CC")
        tree.tag_configure("ignored", foreground="gray")

    def _create_context_menu(self):
        """Create right-click context menu."""
        self.context_menu = tk.Menu(self.notebook, tearoff=0)
        self.context_menu.add_command(label="Copy Item Name", command=self._copy_selected_name)
        self.context_menu.add_command(label="View Price History", command=self._show_price_history_menu)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Track Trade", command=self._track_trade)
        self.context_menu.add_command(label="Add to Watchlist", command=self._add_to_watchlist)
        self.context_menu.add_command(label="Add to Stock Market", command=self._add_to_stock_market)
        self.context_menu.add_separator()
        # Ignore submenu
        self.ignore_menu = tk.Menu(self.context_menu, tearoff=0)
        self.ignore_menu.add_command(label="This Session", command=self._ignore_item_session)
        self.ignore_menu.add_command(label="Always", command=self._ignore_item_always)
        self.context_menu.add_cascade(label="Ignore Item", menu=self.ignore_menu)
        self.context_menu.add_command(label="Unignore Item", command=self._unignore_item)

    def _sort_tree(self, tree: ttk.Treeview, col: str):
        """Sort treeview by column when header is clicked."""
        tree_id = id(tree)
        if tree_id not in self.sort_state:
            self.sort_state[tree_id] = {}

        # Toggle sort direction
        reverse = self.sort_state[tree_id].get(col, False)
        self.sort_state[tree_id][col] = not reverse

        # Get all items with their values
        items = [(tree.set(item, col), item) for item in tree.get_children("")]

        if col in NUMERIC_COLUMNS:
            def parse_num(val):
                if val == "-" or val == "inf":
                    return float("-inf") if reverse else float("inf")
                val = val.replace(",", "").replace("%", "").replace("+", "")
                if val.endswith("M"):
                    return float(val[:-1]) * 1_000_000
                if val.endswith("B"):
                    return float(val[:-1]) * 1_000_000_000
                try:
                    return float(val)
                except ValueError:
                    return 0

            items.sort(key=lambda x: parse_num(x[0]), reverse=reverse)
        else:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)

        # Rearrange items
        for idx, (_, item) in enumerate(items):
            tree.move(item, "", idx)

        # Update column headers
        arrow = " [v]" if reverse else " [^]"
        for c in tree["columns"]:
            title = self.get_column_title(c)
            if c == col:
                tree.heading(c, text=title + arrow)
            else:
                tree.heading(c, text=title)

    def restore_tree_bindings(self):
        """Re-apply this tab's own event bindings and header sort commands.

        Cross-hub mode (CrossHubDisplayManager.configure_tree_for_crosshub)
        rebinds double-click/right-click and every column header on these same
        trees. Switching back to same-station must undo that, or the stale
        cross-hub handlers keep resolving rows against the old CrossHubDeal
        list.
        """
        for tree in (self.low_risk_tree, self.high_risk_tree):
            tree.bind("<Double-1>", self._show_price_history)
            tree.bind("<Button-3>", self._show_context_menu)
            for col in DEAL_COLUMNS:
                tree.heading(
                    col,
                    text=self.get_column_title(col),
                    command=lambda c=col, t=tree: self._sort_tree(t, c),
                )

    def _get_active_tree(self) -> ttk.Treeview:
        """Get currently active treeview based on selected tab."""
        current_tab = self.notebook.index(self.notebook.select())
        if current_tab == 0:
            return self.low_risk_tree
        elif current_tab == 1:
            return self.high_risk_tree
        else:
            return self.steals_tree

    def _get_selected_type_id(self) -> int | None:
        """Get type_id of currently selected item."""
        tree = self._get_active_tree()
        selection = tree.selection()
        if not selection:
            return None
        
        item = tree.item(selection[0])
        name = item["values"][0]
        
        for deal in self.all_deals:
            if deal.name == name:
                return deal.type_id
        return None

    def _copy_item_name(self, event):
        """Copy item name on double-click."""
        tree = self._get_active_tree()
        selection = tree.selection()
        if selection:
            item = tree.item(selection[0])
            name = item["values"][0]
            tree.clipboard_clear()
            tree.clipboard_append(name)
            self.set_status(f"Copied: {name}")

    def _copy_selected_name(self):
        """Copy selected item name via context menu."""
        self._copy_item_name(None)

    def _show_price_history(self, event):
        """Show price history graph on double-click."""
        type_id = self._get_selected_type_id()
        if not type_id:
            return
        
        # Find the deal to get the name
        deal_name = None
        for deal in self.all_deals:
            if deal.type_id == type_id:
                deal_name = deal.name
                break
        
        if not deal_name:
            return
        
        # Check we have the required callbacks
        if not all([self.root, self.get_sell_station]):
            self.set_status("Price history not available")
            return
        
        # Get region_id from sell station config
        from core.config import get_hub_config
        sell_station = self.get_sell_station()
        config = get_hub_config(sell_station)
        region_id = config["region_id"]
        
        # Show the graph
        from analytics.graphing import show_price_graph
        show_price_graph(
            parent=self.root,
            type_id=type_id,
            type_name=deal_name,
            region_id=region_id,
            profiles=None,  # Regular scanner doesn't have profiles
        )

    def _show_price_history_menu(self):
        """Show price history from context menu."""
        self._show_price_history(None)

    def _show_context_menu(self, event):
        """Show right-click context menu."""
        tree = self._get_active_tree()
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _add_to_watchlist(self):
        """Add selected item to watchlist."""
        type_id = self._get_selected_type_id()
        if type_id and self.watchlist_manager:
            for deal in self.all_deals:
                if deal.type_id == type_id:
                    self.watchlist_manager.add_from_deal(
                        type_id=deal.type_id,
                        name=deal.name,
                        current_price=deal.buy_price
                    )
                    break

    def _track_trade(self):
        """Add selected item to trade tracking."""
        type_id = self._get_selected_type_id()
        if type_id and self.tracking_manager:
            for deal in self.all_deals:
                if deal.type_id == type_id:
                    self.tracking_manager.flag_deal(
                        type_id=deal.type_id,
                        type_name=deal.name,
                        buy_price=deal.buy_price,
                        sell_price=deal.ceiling_price,
                        profit_per_unit=deal.net_profit
                    )
                    break

    def _add_to_stock_market(self):
        """Add selected item to stock market portfolio."""
        type_id = self._get_selected_type_id()
        if type_id and self.stock_market_tab:
            for deal in self.all_deals:
                if deal.type_id == type_id:
                    # Get region/station from current context
                    from core.config import get_hub_config
                    # Use sell station since that's where we're trading
                    sell_hub = self.get_sell_station() if self.get_sell_station else "amarr"
                    hub_config = get_hub_config(sell_hub)
                    
                    self.stock_market_tab.add_item_from_external(
                        type_id=deal.type_id,
                        region_id=hub_config["region_id"],
                        station_id=hub_config["station_id"],
                        type_name=deal.name
                    )
                    break

    def _ignore_item_session(self):
        """Add selected item to session-only ignore list."""
        type_id = self._get_selected_type_id()
        if type_id:
            self.filter_manager.ignore_item_session(type_id)
            self._refresh_current_display()
            self.set_status("Item ignored (this session)")

    def _ignore_item_always(self):
        """Add selected item to permanent ignore list."""
        type_id = self._get_selected_type_id()
        if type_id:
            self.filter_manager.ignore_item_always(type_id)
            self._refresh_current_display()
            self.set_status("Item ignored (always)")

    def _ignore_item(self):
        """Add selected item to ignore list. Legacy - defaults to always."""
        self._ignore_item_always()

    def _unignore_item(self):
        """Remove selected item from ignore list (auto-detects which list)."""
        type_id = self._get_selected_type_id()
        if type_id:
            ignore_type = self.filter_manager.get_ignore_type(type_id)
            if ignore_type:
                self.filter_manager.unignore_item(type_id)
                self._refresh_current_display()
                self.set_status(f"Item unignored (was: {ignore_type})")

    def _refresh_current_display(self):
        """Refresh display - re-filter current deals."""
        # This is a simplified refresh - just re-display with current data
        # The full refresh happens on next scan
        pass

    def display_categorized_deals(
        self,
        steals: list[Deal],
        low_risk: list[Deal],
        high_risk: list[Deal],
        previous_ids: set[int],
        is_auto: bool
    ) -> int:
        """
        Display pre-categorized deals in the treeviews.
        
        Args:
            steals: Deals categorized as steals
            low_risk: Low risk deals
            high_risk: High risk deals
            previous_ids: Deal IDs from previous scan (for new deal detection)
            is_auto: Whether this is an auto-refresh
        
        Returns:
            Count of new deals (for alert purposes)
        """
        # Combine all deals for lookups
        self.all_deals = steals + low_risk + high_risk
        
        # Apply GUI filters (blueprints, SKINs, ignored, etc.)
        filtered_steals = self._apply_gui_filters(steals)
        filtered_low = self._apply_gui_filters(low_risk)
        filtered_high = self._apply_gui_filters(high_risk)

        # Track new deals (all categories for ID tracking)
        all_filtered = filtered_steals + filtered_low + filtered_high
        filtered_ids = {d.type_id for d in all_filtered if not self.filter_manager.is_ignored(d.type_id)}
        self.current_deal_ids = filtered_ids
        
        # Count new alert-worthy deals (steals + low_risk only, not high_risk)
        alert_worthy_ids = {d.type_id for d in filtered_steals + filtered_low if not self.filter_manager.is_ignored(d.type_id)}
        new_alert_deals = alert_worthy_ids - previous_ids

        # Clear trees
        for tree in [self.low_risk_tree, self.high_risk_tree, self.steals_tree]:
            for item in tree.get_children():
                tree.delete(item)

        # Update tab labels with counts
        self.notebook.tab(0, text=f"Low Risk ({len(filtered_low)})")
        self.notebook.tab(1, text=f"High Risk ({len(filtered_high)})")
        self.notebook.tab(2, text=f"Steals ({len(filtered_steals)})")

        # Populate trees
        for deal in filtered_low:
            self._insert_deal(self.low_risk_tree, deal, category="low_risk")
        
        for deal in filtered_high:
            self._insert_deal(self.high_risk_tree, deal, category="high_risk")
        
        for deal in filtered_steals:
            self._insert_deal(self.steals_tree, deal, category="steal")

        return len(new_alert_deals)

    def _apply_gui_filters(self, deals: list[Deal]) -> list[Deal]:
        """Apply GUI-level filters (blueprints, SKINs, ignored, hub only, etc.)."""
        show_ignored = self.filter_manager.show_ignored()
        filtered = []
        
        for d in deals:
            # Show ignored only if checkbox is checked
            if self.filter_manager.is_ignored(d.type_id):
                if show_ignored:
                    filtered.append(d)
                continue
            
            # Apply category filters (blueprints, SKINs, etc.)
            if self.filter_manager.should_show_deal(d):
                filtered.append(d)
        
        return filtered

    def _insert_deal(self, tree: ttk.Treeview, deal: Deal, category: str):
        """Insert a deal into a tree with appropriate tag."""
        tag = self._determine_tag(deal, category)
        values = self._format_deal_values(deal)
        tree.insert("", tk.END, values=values, tags=(tag,))

    def _determine_tag(self, deal: Deal, category: str) -> str:
        """Determine display tag for a deal."""
        # Check special states first
        if self.filter_manager.is_ignored(deal.type_id):
            return "ignored"
        
        # Category-specific tags
        if category == "steal":
            # Use steal color from scanner
            if deal.steal_color == StealColor.GREEN:
                return "steal_green"
            elif deal.steal_color == StealColor.YELLOW:
                return "steal_yellow"
            else:
                return "steal_red"
        
        elif category == "low_risk":
            if deal.net_profit < 0:
                return "loss"
            return "profit"
        
        else:  # high_risk
            if deal.net_profit < 0:
                return "loss"
            # Color by number of risk flags
            flag_count = len(deal.risk_flags) if deal.risk_flags else 0
            if flag_count == 1:
                return "risky_yellow"
            elif flag_count == 2:
                return "risky_orange"
            elif flag_count >= 3:
                return "risky_red"
            return "risky"  # fallback

    def _format_deal_values(self, deal: Deal) -> tuple:
        """Format deal data for treeview display."""
        buy_order_str = f"{deal.local_buy:,.0f}" if deal.local_buy > 0 else "-"
        avg_30d_str = f"{deal.avg_price_30d:,.0f}" if deal.avg_price_30d > 0 else "-"
        
        # 30d velocity
        vol_30d_str = f"{deal.avg_volume_30d:.1f}" if deal.avg_volume_30d > 0 else "-"
        
        # 7d velocity with trend indicator vs 30d
        if deal.avg_volume_7d > 0 and deal.avg_volume_30d > 0:
            pct_change = ((deal.avg_volume_7d - deal.avg_volume_30d) / deal.avg_volume_30d) * 100
            if pct_change >= 0:
                vol_7d_str = f"{deal.avg_volume_7d:.1f} +{pct_change:.0f}%"
            else:
                vol_7d_str = f"{deal.avg_volume_7d:.1f} {pct_change:.0f}%"
        elif deal.avg_volume_7d > 0:
            vol_7d_str = f"{deal.avg_volume_7d:.1f}"
        else:
            vol_7d_str = "-"
        
        # 7d price avg with trend indicator
        if deal.avg_price_7d > 0 and deal.avg_price_30d > 0:
            pct_change = ((deal.avg_price_7d - deal.avg_price_30d) / deal.avg_price_30d) * 100
            if pct_change >= 0:
                avg_7d_str = f"{deal.avg_price_7d:,.0f} +{pct_change:.0f}%"
            else:
                avg_7d_str = f"{deal.avg_price_7d:,.0f} {pct_change:.0f}%"
        elif deal.avg_price_7d > 0:
            avg_7d_str = f"{deal.avg_price_7d:,.0f}"
        else:
            avg_7d_str = "-"
        
        # ET Flip (days to sell)
        if deal.days_to_sell == float("inf"):
            et_flip_str = "inf"
        else:
            et_flip_str = f"{deal.days_to_sell:.1f}"
        
        # Get est_sell_pct for profit and ceiling adjustment
        est_sell_pct = self.filter_manager.get_est_sell_pct() / 100.0
        
        # Adjusted sell price based on est_sell_pct (percentage of ceiling)
        adjusted_ceiling = deal.ceiling_price * est_sell_pct
        
        # Recalculate profit at adjusted price
        from core.calculate import calculate_profit_per_unit, DEFAULT_SKILLS
        skills = DEFAULT_SKILLS
        if self.tracking_manager:
            skills = self.tracking_manager.get_skills()
        
        adjusted_profit = calculate_profit_per_unit(deal.buy_price, adjusted_ceiling, skills)
        adjusted_total = adjusted_profit * deal.volume
        
        # Total profit with M/B suffix (using adjusted values)
        total = adjusted_total
        if abs(total) >= 1_000_000_000:
            total_str = f"{total/1_000_000_000:.2f}B"
        elif abs(total) >= 1_000_000:
            total_str = f"{total/1_000_000:.1f}M"
        else:
            total_str = f"{total:,.0f}"

        return (
            deal.name,
            deal.system_name,
            f"{deal.buy_price:,.0f}",
            buy_order_str,
            f"{adjusted_ceiling:,.0f}",
            f"{deal.break_even:,.0f}",
            f"{adjusted_profit:,.0f}",
            et_flip_str,
            f"{deal.volume:,}",
            f"{deal.raw_volume:,}",
            total_str,
            avg_7d_str,
            avg_30d_str,
            vol_30d_str,
            vol_7d_str
        )

    def get_current_deal_ids(self) -> set[int]:
        """Get set of currently displayed deal type IDs."""
        return self.current_deal_ids

    def refresh_display(self):
        """Refresh the display with current deals and updated settings (no re-scan).
        
        Updates column headers and re-renders all deals with current est_sell_pct.
        """
        if not self.all_deals:
            return
        
        # Update ceiling column header on all trees
        ceiling_title = self.get_column_title("ceiling")
        for tree in [self.low_risk_tree, self.high_risk_tree, self.steals_tree]:
            tree.heading("ceiling", text=ceiling_title)
        
        # Re-display current deals (preserves current_deal_ids so no alert sound)
        # Split deals back into categories based on stored data
        from scanning.scanner_common import STEAL_RATIO_THRESHOLD
        
        steals = [d for d in self.all_deals if d.steal_ratio >= STEAL_RATIO_THRESHOLD]
        non_steals = [d for d in self.all_deals if d.steal_ratio < STEAL_RATIO_THRESHOLD]
        
        # Re-categorize by risk flags
        low_risk = [d for d in non_steals if not d.risk_flags]
        high_risk = [d for d in non_steals if d.risk_flags]
        
        # Pass current IDs as previous to prevent alert counting
        self.display_categorized_deals(steals, low_risk, high_risk, self.current_deal_ids, is_auto=False)

    # Legacy method for compatibility during transition
    def display_deals(self, deals: list[Deal], previous_ids: set[int], is_auto: bool) -> int:
        """Legacy method - splits deals internally. Use display_categorized_deals instead."""
        # This maintains backward compatibility during transition
        from scanning.scanner_common import STEAL_RATIO_THRESHOLD
        
        steals = [d for d in deals if d.steal_ratio >= STEAL_RATIO_THRESHOLD]
        non_steals = [d for d in deals if d.steal_ratio < STEAL_RATIO_THRESHOLD]
        
        # Without proper risk evaluation, just split by a simple check
        low_risk = []
        high_risk = []
        for d in non_steals:
            safe_vel = min(d.avg_volume_7d, d.avg_volume_30d) if d.avg_volume_7d > 0 and d.avg_volume_30d > 0 else max(d.avg_volume_7d, d.avg_volume_30d)
            if safe_vel >= 5:  # Hardcoded for legacy
                low_risk.append(d)
            else:
                high_risk.append(d)
        
        return self.display_categorized_deals(steals, low_risk, high_risk, previous_ids, is_auto)
