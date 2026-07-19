"""Cross-Hub Arbitrage GUI Display - Dual-row format.

Displays crosshub deals with two rows per item:
- Row 1 (Buy Station): Where you buy - shows buy station market data
- Row 2 (Sell Station, indented): Where you sell - shows sell station data + profits
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional, Any

from scanning.scanner_crosshub import CrossHubDeal
from core.config import get_hub_config


def format_isk(value: float) -> str:
    """Format ISK values with K/M/B suffixes."""
    if value >= 1_000_000_000:
        return f"{value/1_000_000_000:.1f}B"
    elif value >= 1_000_000:
        return f"{value/1_000_000:.1f}M"
    elif value >= 1_000:
        return f"{value/1_000:.1f}K"
    else:
        return f"{value:.0f}"


def format_volume(value: float) -> str:
    """Format volume values."""
    if value >= 1000:
        return f"{value/1000:.1f}K"
    return f"{value:.1f}"


def format_days(value: float) -> str:
    """Format days to flip."""
    if value == float("inf") or value > 999:
        return ">999d"
    return f"{value:.1f}d"


def et_flip_days(deal: CrossHubDeal) -> float:
    """Estimated days to flip at the sell station's safe velocity."""
    if deal.avg_volume_7d > 0 and deal.avg_volume_30d > 0:
        safe_vel = min(deal.avg_volume_7d, deal.avg_volume_30d)
    else:
        safe_vel = max(deal.avg_volume_7d, deal.avg_volume_30d)
    return deal.volume / safe_vel if safe_vel > 0 else float("inf")


# Column definitions for Buy Station row (source)
BUY_STATION_COLUMNS = (
    "name", "system", "buy_at", "buy_order", "volume", "available",
    "avg_7d", "avg_30d", "vol_7d", "vol_30d"
)

# Column definitions for Sell Station row (destination)
SELL_STATION_COLUMNS = (
    "name", "system", "buy_at", "buy_order", "ceiling", "break_even",
    "profit_unit", "et_flip", "total_profit", "avg_7d", "avg_30d", "vol_7d", "vol_30d"
)

# Combined columns for the tree - MUST match gui_deals.py DEAL_COLUMNS
# Original: "name", "system", "buy_price", "buy_order", "ceiling", "break_even",
#           "unit_profit", "et_flip", "volume", "raw_volume", "total_profit",
#           "avg_7d", "avg_30d", "vol_30d", "vol_7d"
CROSSHUB_COLUMNS = (
    "name", "system", "buy_price", "buy_order", "ceiling", "break_even",
    "unit_profit", "et_flip", "volume", "raw_volume", "total_profit",
    "avg_7d", "avg_30d", "vol_30d", "vol_7d"
)

COLUMN_TITLES = {
    "name": "Name",
    "system": "System",
    "buy_price": "Buy At",
    "buy_order": "Buy Order",
    "ceiling": "Ceiling",
    "break_even": "Break Even",
    "unit_profit": "Profit/Unit",
    "et_flip": "ET Flip",
    "volume": "Volume",
    "raw_volume": "Available",
    "total_profit": "Total Profit",
    "avg_7d": "Avg 7d",
    "avg_30d": "Avg 30d",
    "vol_7d": "Vol 7d",
    "vol_30d": "Vol 30d",
}

COLUMN_WIDTHS = {
    "name": 180,
    "system": 80,
    "buy_price": 90,
    "buy_order": 90,
    "ceiling": 90,
    "break_even": 90,
    "unit_profit": 80,
    "et_flip": 60,
    "volume": 60,
    "raw_volume": 70,
    "total_profit": 90,
    "avg_7d": 80,
    "avg_30d": 80,
    "vol_7d": 60,
    "vol_30d": 60,
}

NUMERIC_COLUMNS = {
    "buy_price", "buy_order", "ceiling", "break_even", "unit_profit",
    "et_flip", "volume", "raw_volume", "total_profit", "avg_7d", "avg_30d",
    "vol_7d", "vol_30d"
}

# Column-header sort keys for cross-hub mode. The buy and sell rows of a pair
# show different data in the same column, so each key picks the pair's
# decision-relevant value — sell-station side for market stats, matching the
# row users read for margin. "system" is absent on purpose: that header resets
# to the default color sort.
PAIR_SORT_KEYS = {
    "name": lambda d: d.name.lower(),
    "buy_price": lambda d: d.buy_price,
    "buy_order": lambda d: d.sell_station_buy,
    "ceiling": lambda d: d.sell_price,
    "break_even": lambda d: d.break_even,
    "unit_profit": lambda d: d.net_profit,
    "et_flip": et_flip_days,
    "volume": lambda d: d.volume,
    "raw_volume": lambda d: d.raw_volume,
    "total_profit": lambda d: d.total_profit,
    "avg_7d": lambda d: d.avg_price_7d,
    "avg_30d": lambda d: d.avg_price_30d,
    "vol_7d": lambda d: d.avg_volume_7d,
    "vol_30d": lambda d: d.avg_volume_30d,
}


class CrossHubDisplayManager:
    """Manages dual-row display for cross-hub arbitrage deals."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        set_status: Callable[[str], None],
        root: Optional[tk.Tk] = None,
        get_client: Optional[Callable] = None,
    ):
        self.notebook = notebook
        self.set_status = set_status
        self.root = root
        self.get_client = get_client
        
        # Store deals for lookups
        self.all_deals: list[CrossHubDeal] = []
        self.current_deal_ids: set[int] = set()
        
        # Sort state per tree
        self.sort_state = {}

        # Per-tree display state (widget path -> {"deals", "is_low_risk"}) so
        # header sorts reorder deal OBJECTS and repaint pairs, never rows.
        self._tree_state: dict[str, dict] = {}
        
        # External references (set by gui_main)
        self.tracking_manager = None
        self.filter_manager = None
        
        # Create context menu
        self._create_context_menu()

    def _create_context_menu(self):
        """Create right-click context menu."""
        self.context_menu = tk.Menu(self.notebook, tearoff=0)
        self.context_menu.add_command(label="Track Trade", command=self._track_selected)
        self.context_menu.add_command(label="View Price History", command=self._view_price_history)
        self.context_menu.add_separator()
        # Ignore submenu
        self.ignore_menu = tk.Menu(self.context_menu, tearoff=0)
        self.ignore_menu.add_command(label="This Session", command=self._ignore_selected_session)
        self.ignore_menu.add_command(label="Always", command=self._ignore_selected_always)
        self.context_menu.add_cascade(label="Ignore Item", menu=self.ignore_menu)
        self.context_menu.add_command(label="Unignore Item", command=self._unignore_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Name", command=self._copy_name)

    def _show_context_menu(self, event, tree: ttk.Treeview):
        """Show context menu on right-click."""
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _get_selected_deal(self, tree: ttk.Treeview) -> Optional[CrossHubDeal]:
        """Get the deal for the selected row."""
        selection = tree.selection()
        if not selection:
            return None
        
        item = selection[0]
        values = tree.item(item, "values")
        if not values:
            return None
        
        # Get name from first column (strip indent spaces)
        name = values[0].strip()
        
        # Find deal by name
        for deal in self.all_deals:
            if deal.name == name:
                return deal
        return None

    def _track_selected(self):
        """Track the selected deal."""
        # Find which tree has selection
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal and self.tracking_manager:
                                self.tracking_manager.flag_deal(
                                    type_id=deal.type_id,
                                    type_name=deal.name,
                                    buy_price=deal.buy_price,
                                    sell_price=deal.ceiling_price,
                                    profit_per_unit=deal.net_profit,
                                )
                                self.set_status(f"Now tracking: {deal.name}")
                            return

    def _view_price_history(self):
        """View price history for selected item."""
        # Find which tree has selection
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal:
                                self._show_price_history(deal)
                            return

    def _show_price_history(self, deal: CrossHubDeal):
        """Show price history graph for a deal."""
        if not self.root:
            return
        
        from analytics.graphing import show_price_graph
        from core.config import get_hub_config
        
        # Use sell station region for the graph
        sell_config = get_hub_config(deal.sell_station)
        
        show_price_graph(
            parent=self.root,
            type_id=deal.type_id,
            type_name=deal.name,
            region_id=sell_config["region_id"],
            profiles=None,
        )

    def _copy_name(self):
        """Copy item name to clipboard."""
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal and self.root:
                                self.root.clipboard_clear()
                                self.root.clipboard_append(deal.name)
                                self.set_status(f"Copied: {deal.name}")
                            return

    def _ignore_selected_session(self):
        """Add selected item to session-only ignore list."""
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal and self.filter_manager:
                                self.filter_manager.ignore_item_session(deal.type_id)
                                self.set_status(f"Ignored (this session): {deal.name}")
                            return

    def _ignore_selected_always(self):
        """Add selected item to permanent ignore list."""
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal and self.filter_manager:
                                self.filter_manager.ignore_item_always(deal.type_id)
                                self.set_status(f"Ignored (always): {deal.name}")
                            return

    def _ignore_selected(self):
        """Add selected item to ignore list. Legacy - defaults to always."""
        self._ignore_selected_always()

    def _unignore_selected(self):
        """Remove selected item from ignore list (auto-detects which list)."""
        for tab_idx in range(self.notebook.index("end")):
            tab_name = self.notebook.tab(tab_idx, "text")
            if "Low Risk" in tab_name or "High Risk" in tab_name:
                frame = self.notebook.nametowidget(self.notebook.tabs()[tab_idx])
                for child in frame.winfo_children():
                    if isinstance(child, ttk.Treeview):
                        if child.selection():
                            deal = self._get_selected_deal(child)
                            if deal and self.filter_manager:
                                ignore_type = self.filter_manager.get_ignore_type(deal.type_id)
                                if ignore_type:
                                    self.filter_manager.unignore_item(deal.type_id)
                                    self.set_status(f"Unignored (was: {ignore_type}): {deal.name}")
                            return

    def display_crosshub_deals(
        self,
        low_risk: list[CrossHubDeal],
        high_risk: list[CrossHubDeal],
        low_risk_tree: ttk.Treeview,
        high_risk_tree: ttk.Treeview,
        previous_ids: set[int],
        is_auto: bool
    ) -> int:
        """
        Display cross-hub deals in dual-row format.
        
        Args:
            low_risk: Low risk crosshub deals
            high_risk: High risk crosshub deals
            low_risk_tree: Treeview for low risk tab
            high_risk_tree: Treeview for high risk tab
            previous_ids: Deal IDs from previous scan
            is_auto: Whether this is an auto-refresh
        
        Returns:
            Count of new deals
        """
        # Store all deals
        self.all_deals = low_risk + high_risk
        
        # Apply filters if available
        if self.filter_manager:
            low_risk = self._apply_filters(low_risk)
            high_risk = self._apply_filters(high_risk)
        
        # Track IDs
        filtered_ids = {d.type_id for d in low_risk + high_risk}
        self.current_deal_ids = filtered_ids
        new_deals = filtered_ids - previous_ids
        
        # Clear trees
        for item in low_risk_tree.get_children():
            low_risk_tree.delete(item)
        for item in high_risk_tree.get_children():
            high_risk_tree.delete(item)
        
        # Populate trees with dual-row format
        self._populate_tree(low_risk_tree, low_risk, is_low_risk=True)
        self._populate_tree(high_risk_tree, high_risk, is_low_risk=False)
        
        # Update tab labels
        self._update_tab_label(low_risk_tree, "Low Risk", len(low_risk))
        self._update_tab_label(high_risk_tree, "High Risk", len(high_risk))
        
        return len(new_deals)

    def _apply_filters(self, deals: list[CrossHubDeal]) -> list[CrossHubDeal]:
        """Apply GUI filters to deals."""
        if not self.filter_manager:
            return deals
        
        filtered = []
        show_ignored = self.filter_manager.show_ignored()
        
        for deal in deals:
            if self.filter_manager.is_ignored(deal.type_id):
                if show_ignored:
                    filtered.append(deal)
                continue
            
            # Check category filters
            if hasattr(self.filter_manager, 'should_show_deal'):
                if self.filter_manager.should_show_deal(deal):
                    filtered.append(deal)
            else:
                filtered.append(deal)
        
        return filtered

    def _get_strike_count(self, deal: CrossHubDeal, is_low_risk: bool) -> int:
        """Calculate strike count for sorting purposes.
        
        Low risk deals always return 0 (green).
        High risk deals count strikes with ABOVE_JITA_AVG as 2.
        """
        from scanning.scanner_common import RiskFlag
        
        if is_low_risk or deal.is_guaranteed:
            return 0
        
        strike_count = 0
        if deal.risk_flags:
            for flag in deal.risk_flags:
                if flag == RiskFlag.ABOVE_JITA_AVG:
                    strike_count += 2
                else:
                    strike_count += 1
        return strike_count

    def _populate_tree(self, tree: ttk.Treeview, deals: list[CrossHubDeal], is_low_risk: bool = False):
        """Populate tree with dual-row format for each deal.
        
        Deals are sorted by strike count (green first) then by total profit within each color.
        """
        # Sort deals by strike count (ascending) then by total profit (descending)
        sorted_deals = sorted(deals, key=lambda d: (self._get_strike_count(d, is_low_risk), -d.total_profit))
        self._tree_state[str(tree)] = {"deals": sorted_deals, "is_low_risk": is_low_risk}

        for deal in sorted_deals:
            self._insert_deal_rows(tree, deal, is_low_risk)

    def _insert_deal_rows(self, tree: ttk.Treeview, deal: CrossHubDeal, is_low_risk: bool):
        """Insert the buy/sell row pair for one deal."""
        # Row 1: Buy Station (source) - where you buy
        # Always use neutral color for buy station row
        buy_values = self._format_buy_station_row(deal)
        tree.insert("", tk.END, values=buy_values, tags=("buy_station",))

        # Row 2: Sell Station (destination) - color based on risk
        sell_values = self._format_sell_station_row(deal)
        sell_tag = self._determine_sell_tag(deal, is_low_risk)
        tree.insert("", tk.END, values=sell_values, tags=(sell_tag,))

    def _repaint_tree(self, tree: ttk.Treeview):
        """Clear and re-insert all row pairs from the stored display state."""
        state = self._tree_state.get(str(tree))
        if not state:
            return
        for item in tree.get_children(""):
            tree.delete(item)
        for deal in state["deals"]:
            self._insert_deal_rows(tree, deal, state["is_low_risk"])

    def _determine_sell_tag(self, deal: CrossHubDeal, is_low_risk: bool) -> str:
        """Determine color tag for sell station row based on risk flags.
        
        Strike counting:
        - Most flags = 1 strike
        - ABOVE_JITA_AVG = 2 strikes (buying above Jita avg is bad)
        
        Colors:
        - 0 strikes = green
        - 1 strike = yellow
        - 2 strikes = orange
        - 3+ strikes = red
        """
        from scanning.scanner_common import RiskFlag
        
        # Check if ignored
        if self.filter_manager and self.filter_manager.is_ignored(deal.type_id):
            return "ignored"
        
        # Loss check
        if deal.net_profit < 0:
            return "loss"
        
        # Low risk = guaranteed profit, always green
        if is_low_risk or deal.is_guaranteed:
            return "profit"
        
        # High risk = color by strike count
        # ABOVE_JITA_AVG counts as 2 strikes, others count as 1
        strike_count = 0
        if deal.risk_flags:
            for flag in deal.risk_flags:
                if flag == RiskFlag.ABOVE_JITA_AVG:
                    strike_count += 2
                else:
                    strike_count += 1
        
        if strike_count == 0:
            return "profit"  # No flags = good deal
        elif strike_count == 1:
            return "risky_yellow"
        elif strike_count == 2:
            return "risky_orange"
        else:  # 3+
            return "risky_red"

    def _format_buy_station_row(self, deal: CrossHubDeal) -> tuple:
        """Format values for buy station row (source)."""
        # Get hub display name
        buy_config = get_hub_config(deal.buy_station)
        buy_hub_name = buy_config.get("name", deal.buy_system_name)
        
        # Column order matches DEAL_COLUMNS:
        # name, system, buy_price, buy_order, ceiling, break_even,
        # unit_profit, et_flip, volume, raw_volume, total_profit,
        # avg_7d, avg_30d, vol_30d, vol_7d
        return (
            deal.name,                                    # name
            buy_hub_name,                                 # system (buy station)
            format_isk(deal.buy_price),                   # buy_price (sell order floor we're buying)
            format_isk(deal.buy_station_buy) if deal.buy_station_buy > 0 else "-",  # buy_order
            "-",                                          # ceiling (N/A for buy station)
            "-",                                          # break_even (N/A)
            "-",                                          # unit_profit (N/A)
            "-",                                          # et_flip (N/A)
            str(deal.volume),                             # volume (how much to buy)
            str(deal.raw_volume),                         # raw_volume (available)
            "-",                                          # total_profit (N/A)
            format_isk(deal.buy_avg_price_7d) if deal.buy_avg_price_7d > 0 else "-",   # avg_7d
            format_isk(deal.buy_avg_price_30d) if deal.buy_avg_price_30d > 0 else "-", # avg_30d
            format_volume(deal.buy_avg_volume_30d) if deal.buy_avg_volume_30d > 0 else "-", # vol_30d
            format_volume(deal.buy_avg_volume_7d) if deal.buy_avg_volume_7d > 0 else "-",  # vol_7d
        )

    def _format_sell_station_row(self, deal: CrossHubDeal) -> tuple:
        """Format values for sell station row (destination) - indented."""
        # Get hub display name
        sell_config = get_hub_config(deal.sell_station)
        sell_hub_name = sell_config.get("name", deal.sell_system_name)
        
        # Calculate ET flip based on sell station velocity
        et_flip = et_flip_days(deal)
        
        # Column order matches DEAL_COLUMNS:
        # name, system, buy_price, buy_order, ceiling, break_even,
        # unit_profit, et_flip, volume, raw_volume, total_profit,
        # avg_7d, avg_30d, vol_30d, vol_7d
        return (
            "    " + deal.name,                           # name (indented)
            sell_hub_name,                                # system (sell station)
            format_isk(deal.sell_price) if deal.is_guaranteed else "-",  # buy_price (for Low Risk: buy order price)
            format_isk(deal.sell_station_buy) if deal.sell_station_buy > 0 else "-",  # buy_order
            format_isk(deal.sell_price) if not deal.is_guaranteed else "-",  # ceiling (for High Risk: target sell)
            format_isk(deal.break_even),                  # break_even
            format_isk(deal.net_profit),                  # unit_profit
            format_days(et_flip),                         # et_flip
            "-",                                          # volume (shown on buy row)
            "-",                                          # raw_volume (shown on buy row)
            format_isk(deal.total_profit),                # total_profit
            format_isk(deal.avg_price_7d) if deal.avg_price_7d > 0 else "-",   # avg_7d
            format_isk(deal.avg_price_30d) if deal.avg_price_30d > 0 else "-", # avg_30d
            format_volume(deal.avg_volume_30d) if deal.avg_volume_30d > 0 else "-", # vol_30d
            format_volume(deal.avg_volume_7d) if deal.avg_volume_7d > 0 else "-",  # vol_7d
        )

    def _update_tab_label(self, tree: ttk.Treeview, base_name: str, count: int):
        """Update tab label with count."""
        # Find the tab containing this tree
        for tab_idx in range(self.notebook.index("end")):
            tab_id = self.notebook.tabs()[tab_idx]
            frame = self.notebook.nametowidget(tab_id)
            for child in frame.winfo_children():
                if child is tree or (hasattr(child, 'winfo_children') and tree in [c for c in child.winfo_children() if isinstance(c, ttk.Treeview)]):
                    self.notebook.tab(tab_idx, text=f"{base_name} ({count})")
                    return

    def get_current_deal_ids(self) -> set[int]:
        """Get IDs of currently displayed deals."""
        return self.current_deal_ids

    def configure_tree_for_crosshub(self, tree: ttk.Treeview):
        """Configure an existing tree for crosshub dual-row display.
        
        Since crosshub uses the same columns as normal deals (DEAL_COLUMNS),
        we only need to configure the visual tags for row coloring.
        Also overrides System column to reset to default color sort.
        """
        # Buy station row - neutral light color
        tree.tag_configure("buy_station", background="#f0f0f0")  # Light gray
        
        # Low risk / good deals - green
        tree.tag_configure("profit", foreground="green")
        
        # High risk color coding by number of risk flags
        tree.tag_configure("risky_yellow", foreground="black", background="#FFD700")  # 1 flag - yellow
        tree.tag_configure("risky_orange", foreground="white", background="#FF8C00")  # 2 flags - orange  
        tree.tag_configure("risky_red", foreground="white", background="#DC143C")     # 3+ flags - red
        
        # Loss / ignored
        tree.tag_configure("loss", foreground="red")
        tree.tag_configure("ignored", foreground="gray")
        
        # Take over ALL column headers: DealsTabManager._sort_tree sorts rows
        # individually, which tears the buy/sell row pairs apart. System resets
        # to the default color sort; every other column sorts deal pairs.
        for col in CROSSHUB_COLUMNS:
            if col == "system":
                command = lambda t=tree: self._reset_to_color_sort(t)
            else:
                command = lambda c=col, t=tree: self._sort_tree_pairs(t, c)
            tree.heading(col, text=COLUMN_TITLES[col], command=command)

        # Store tree references for reset functionality
        if not hasattr(self, '_crosshub_trees'):
            self._crosshub_trees = []
        if tree not in self._crosshub_trees:
            self._crosshub_trees.append(tree)

        # Bind context menu
        tree.bind("<Button-3>", lambda e: self._show_context_menu(e, tree))
        tree.bind("<Double-1>", lambda e: self._on_double_click(e, tree))

    def _on_double_click(self, event, tree: ttk.Treeview):
        """Handle double-click to view price history."""
        deal = self._get_selected_deal(tree)
        if deal:
            self._show_price_history(deal)

    def _sort_tree_pairs(self, tree: ttk.Treeview, col: str):
        """Column-header sort that keeps buy/sell row pairs together.

        Sorts the displayed deal OBJECTS and repaints both rows of each pair
        (same pattern as gui_demand_tab._sort_by_column) instead of moving
        tree rows individually, which interleaves buy and sell rows.
        """
        state = self._tree_state.get(str(tree))
        key = PAIR_SORT_KEYS.get(col)
        if not state or not state["deals"] or key is None:
            return

        dirs = self.sort_state.setdefault(str(tree), {})
        reverse = dirs.get(col, False)
        dirs[col] = not reverse

        state["deals"].sort(key=key, reverse=reverse)
        self._repaint_tree(tree)
        self._update_heading_arrows(tree, col, reverse)

    def _update_heading_arrows(self, tree: ttk.Treeview, sort_col: Optional[str], reverse: bool):
        """Show the sort arrow on the active column, plain titles elsewhere."""
        arrow = " [v]" if reverse else " [^]"
        for c in CROSSHUB_COLUMNS:
            title = COLUMN_TITLES[c]
            tree.heading(c, text=title + arrow if c == sort_col else title)

    def _reset_to_color_sort(self, tree: ttk.Treeview):
        """Reset tree to default color sort (green -> yellow -> orange -> red).

        Called when System column header is clicked in cross-hub mode.
        """
        state = self._tree_state.get(str(tree))
        if not state:
            return

        is_low_risk = state["is_low_risk"]
        state["deals"].sort(
            key=lambda d: (self._get_strike_count(d, is_low_risk), -d.total_profit)
        )
        self.sort_state.pop(str(tree), None)
        self._repaint_tree(tree)
        self._update_heading_arrows(tree, None, False)

        self.set_status("Reset to color sort (green -> red)")
