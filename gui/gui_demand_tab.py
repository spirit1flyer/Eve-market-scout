"""Demand / Restock tab.

Different lens than cross-hub: answers "how much should I ship from source to
dest to fill an actual demand gap?" — not "where can I exploit a spread?".
Lives in its own tab so cross-hub stays untouched.

Layout mirrors the cross-hub dual-row format:
- Row 1: source (buy) station — supply-side facts.
- Row 2: destination (sell) station, indented — demand-side facts + profit.
"""

import tkinter as tk
from tkinter import ttk
from typing import Callable, Optional

from core.config import get_hub_config
from scanning.scanner_demand import (
    DemandRow, load_demand_settings, sort_demand_rows,
    DEFAULT_SORT_MODE,
)


DEMAND_COLUMNS = (
    "name",
    "supply",         # supply price (buy row)
    "est_sell",       # estimated sell price (sell row)
    "avg_7d",         # dest 7d historical avg (sell row)
    "avg_30d",        # dest 30d historical avg (sell row)
    "velocity",       # velocity/day — both rows
    "stock",          # remaining stock — both rows
    "days_to_dep",    # estimated days to depletion (sell row)
    "rec_amount",     # recommended stock amount (sell row)
    "profit_unit",    # profit per unit (sell row)
    "total_profit",   # total profit (sell row)
    "cargo_m3",       # cargo volume (sell row)
)

COLUMN_TITLES = {
    "name": "Item",
    "supply": "Supply Price",
    "est_sell": "Est Sell Price",
    "avg_7d": "Dest 7d Avg",
    "avg_30d": "Dest 30d Avg",
    "velocity": "Velocity/Day",
    "stock": "Remaining Stock",
    "days_to_dep": "Days to Depletion",
    "rec_amount": "Recommended Buy",
    "profit_unit": "Profit / Unit",
    "total_profit": "Total Profit",
    "cargo_m3": "Cargo m³",
}

COLUMN_WIDTHS = {
    "name": 190,
    "supply": 95,
    "est_sell": 100,
    "avg_7d": 95,
    "avg_30d": 95,
    "velocity": 80,
    "stock": 95,
    "days_to_dep": 100,
    "rec_amount": 110,
    "profit_unit": 95,
    "total_profit": 100,
    "cargo_m3": 85,
}

NUMERIC_COLUMNS = {
    "supply", "est_sell", "avg_7d", "avg_30d", "velocity", "stock",
    "days_to_dep", "rec_amount", "profit_unit", "total_profit", "cargo_m3",
}

# Target-vs-history ratio thresholds for sanity coloring on the sell row.
SUSPICIOUS_RATIO = 2.0   # target_sell > 2× dest avg → yellow
JUNK_RATIO = 5.0          # target_sell > 5× dest avg → red


# =============================================================================
# FORMATTERS
# =============================================================================

def _fmt_isk(v: float) -> str:
    if v is None or v == 0:
        return "-"
    if abs(v) >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:.0f}"


def _fmt_qty(v) -> str:
    if v is None:
        return "-"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if v >= 1000:
        return f"{v/1000:.1f}K"
    return f"{int(v)}"


def _fmt_vel(v: float) -> str:
    if v is None or v <= 0:
        return "-"
    if v >= 100:
        return f"{v:.0f}/d"
    return f"{v:.1f}/d"


def _fmt_days(v: float) -> str:
    if v is None:
        return "-"
    if v == float("inf"):
        return ">999d"
    return f"{v:.1f}d"


def _fmt_m3(v: float) -> str:
    if v is None or v <= 0:
        return "-"
    if v >= 1_000_000:
        return f"{v/1_000_000:.2f}Mm³"
    if v >= 1000:
        return f"{v/1000:.1f}km³"
    return f"{v:.1f}"


# =============================================================================
# MANAGER
# =============================================================================

class DemandTabManager:
    """Owns the Demand/Restock tab — dual-row treeview, sort toggle, settings."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        set_status: Callable[[str], None],
        root: Optional[tk.Tk] = None,
        get_buy_station: Optional[Callable[[], str]] = None,
        get_sell_station: Optional[Callable[[], str]] = None,
    ):
        self.notebook = notebook
        self.set_status = set_status
        self.root = root
        self.get_buy_station = get_buy_station
        self.get_sell_station = get_sell_station

        # Unfiltered set (kept so filter toggles can re-filter without re-scanning).
        self._unfiltered_rows: list[DemandRow] = []
        self.all_rows: list[DemandRow] = []
        self.sort_mode: str = load_demand_settings().get("sort_mode", DEFAULT_SORT_MODE)

        # External managers wired by gui_main after construction.
        self.watchlist_manager = None
        self.stock_market_tab = None

        # Demand-tab-local category toggles. Independent from the main filter
        # bar — the top-bar toggles drive the Low/High/Steals tabs; this tab
        # has its own inline set so users don't have to guess which row of
        # checkboxes affects which tab.
        self.show_blueprints_var = tk.BooleanVar(value=False)
        self.show_skins_var = tk.BooleanVar(value=False)
        self.show_skillbooks_var = tk.BooleanVar(value=False)
        self.show_apparel_var = tk.BooleanVar(value=False)
        self.show_limited_var = tk.BooleanVar(value=False)
        self.show_unlimited_var = tk.BooleanVar(value=False)

        self._create_tab()
        self._create_context_menu()

    # -------------------------------------------------------------------------
    # Construction
    # -------------------------------------------------------------------------

    def _create_tab(self):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text="Demand / Restock")
        self.frame = frame

        # Top control strip
        controls = ttk.Frame(frame, padding=(4, 4))
        controls.grid(row=0, column=0, columnspan=2, sticky="ew")

        ttk.Label(controls, text="Sort:").pack(side=tk.LEFT)
        self.sort_var = tk.StringVar(value=self.sort_mode)
        ttk.Radiobutton(
            controls, text="Total profit", variable=self.sort_var,
            value="total_profit", command=self._on_sort_changed,
        ).pack(side=tk.LEFT, padx=(4, 0))
        ttk.Radiobutton(
            controls, text="Days to depletion (asc)", variable=self.sort_var,
            value="days_of_stock", command=self._on_sort_changed,
        ).pack(side=tk.LEFT, padx=(4, 0))

        # Inline category toggles, separated from the sort radios with a divider.
        # Independent from the top filter bar by design — see __init__ note.
        ttk.Separator(controls, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=8, fill=tk.Y)
        ttk.Label(controls, text="Show:").pack(side=tk.LEFT)
        for text, var in (
            ("Blueprints", self.show_blueprints_var),
            ("SKINs", self.show_skins_var),
            ("Skillbooks", self.show_skillbooks_var),
            ("Apparel", self.show_apparel_var),
            ("Limited", self.show_limited_var),
            ("Unlimited", self.show_unlimited_var),
        ):
            ttk.Checkbutton(
                controls, text=text, variable=var,
                command=self.refresh_filter,
            ).pack(side=tk.LEFT, padx=2)

        ttk.Button(controls, text="Settings…", command=self._open_settings).pack(side=tk.RIGHT)

        self.summary_label = ttk.Label(controls, text="", foreground="gray")
        self.summary_label.pack(side=tk.RIGHT, padx=(0, 12))

        # Tree
        self.tree = ttk.Treeview(
            frame,
            columns=DEMAND_COLUMNS,
            show="headings",
        )
        for col in DEMAND_COLUMNS:
            self.tree.heading(
                col, text=COLUMN_TITLES[col],
                command=lambda c=col: self._sort_by_column(c),
            )
            anchor = tk.E if col in NUMERIC_COLUMNS else tk.W
            self.tree.column(col, width=COLUMN_WIDTHS[col], anchor=anchor)

        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        hsb.grid(row=2, column=0, sticky="ew")
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)

        # Row tags
        self.tree.tag_configure("buy_station", background="#f0f0f0")          # supply row
        self.tree.tag_configure("sell_station", foreground="#003a8c")          # standard demand row
        self.tree.tag_configure("instant_sale", background="#E0F2E0",          # dest buy order beat the undercut
                                foreground="#003a8c")
        # Sanity coloring — target sell price drifting from dest historical avg
        self.tree.tag_configure("suspicious", background="#FFE680", foreground="black")  # 2×–5× avg
        self.tree.tag_configure("junk_listing", background="#FF8585", foreground="white")  # > 5× avg

        # Bindings
        self.tree.bind("<Button-3>", self._on_right_click)
        self.tree.bind("<Double-1>", self._on_double_click)

        # Empty-state hint
        self._set_empty_hint("Run a Cross-Hub scan (different buy / sell stations) to populate.")

    def _create_context_menu(self):
        self.context_menu = tk.Menu(self.notebook, tearoff=0)
        self.context_menu.add_command(label="View Price History", command=self._cm_view_graph)
        self.context_menu.add_command(label="Copy Item Name", command=self._cm_copy_name)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Add to Watchlist", command=self._cm_add_watchlist)
        self.context_menu.add_command(label="Add to Stock Market", command=self._cm_add_stock_market)

    # -------------------------------------------------------------------------
    # External API
    # -------------------------------------------------------------------------

    def display_rows(self, rows: list[DemandRow]):
        """Replace contents with the given rows; clears empty-state hint."""
        self._unfiltered_rows = list(rows)
        self._apply_category_filter_and_refresh()

    def refresh_filter(self):
        """Re-apply the FilterManager category toggles without re-scanning.
        Called from gui_main when the user clicks a Show: checkbox.
        """
        if not self._unfiltered_rows and not self.all_rows:
            return
        self._apply_category_filter_and_refresh()

    def _apply_category_filter_and_refresh(self):
        """Filter _unfiltered_rows by this tab's inline category toggles,
        then repaint. Uses the shared keyword logic from gui_filters so the
        Demand/Restock toggles match the top-bar toggles' definitions.
        """
        from gui.gui_filters import passes_category_filters_for_name

        try:
            rows = [
                r for r in self._unfiltered_rows
                if passes_category_filters_for_name(
                    r.name,
                    show_blueprints=self.show_blueprints_var.get(),
                    show_skins=self.show_skins_var.get(),
                    show_skillbooks=self.show_skillbooks_var.get(),
                    show_apparel=self.show_apparel_var.get(),
                    show_limited=self.show_limited_var.get(),
                    show_unlimited=self.show_unlimited_var.get(),
                )
            ]
        except Exception as e:
            print(f"[Demand] category filter error: {e}")
            rows = list(self._unfiltered_rows)

        self.all_rows = rows
        self._refresh_tree(apply_sort_mode=True)

        n = len(self.all_rows)
        buy_label = self._hub_label(self.get_buy_station() if self.get_buy_station else None)
        sell_label = self._hub_label(self.get_sell_station() if self.get_sell_station else None)
        total = len(self._unfiltered_rows)
        suffix = "" if n == total else f" of {total}"
        self.summary_label.configure(text=f"{buy_label} → {sell_label}: {n}{suffix} item(s)")
        self._update_tab_label(n)

    def clear(self, hint: Optional[str] = None):
        self._unfiltered_rows = []
        self.all_rows = []
        self._set_empty_hint(hint or "No demand rows (same-station scan, or no items pass the gates).")
        self.summary_label.configure(text="")
        self._update_tab_label(0)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _hub_label(hub_key: Optional[str]) -> str:
        if not hub_key:
            return "?"
        try:
            return get_hub_config(hub_key).get("name", hub_key)
        except Exception:
            return hub_key

    def _on_sort_changed(self):
        self.sort_mode = self.sort_var.get()
        self._refresh_tree(apply_sort_mode=True)

    def _open_settings(self):
        from gui.gui_demand_settings import DemandSettingsDialog

        def on_saved(new_settings: dict):
            mode = new_settings.get("sort_mode", DEFAULT_SORT_MODE)
            self.sort_mode = mode
            self.sort_var.set(mode)
            self.set_status("Demand settings saved — run a Cross-Hub scan to apply the new gates.")

        DemandSettingsDialog(self.root or self.notebook, on_saved=on_saved)

    def _update_tab_label(self, count: int):
        for idx in range(self.notebook.index("end")):
            tab_id = self.notebook.tabs()[idx]
            if self.notebook.nametowidget(tab_id) is self.frame:
                self.notebook.tab(idx, text=f"Demand / Restock ({count})")
                return

    def _set_empty_hint(self, text: str):
        for item in self.tree.get_children():
            self.tree.delete(item)
        blank = ("",) * (len(DEMAND_COLUMNS) - 1)
        self.tree.insert("", tk.END, values=(text, *blank))

    # -------------------------------------------------------------------------
    # Row formatting
    # -------------------------------------------------------------------------

    def _format_buy_row(self, row: DemandRow) -> tuple:
        """Source (Jita-side) row — supply facts only."""
        buy_label = self._hub_label(row.buy_station)
        return (
            f"{row.name}  [{buy_label}]",
            _fmt_isk(row.source_price),                              # supply price
            "-",                                                      # est sell (sell row)
            "-",                                                      # avg 7d (sell row)
            "-",                                                      # avg 30d (sell row)
            _fmt_vel(row.source_velocity),                            # velocity
            _fmt_qty(row.source_available_qty),                       # remaining stock at source
            "-", "-", "-", "-", "-",                                  # days, rec, profit/u, total, cargo
        )

    def _format_sell_row(self, row: DemandRow) -> tuple:
        """Destination (Amarr-side) row — demand facts + profit math."""
        sell_label = self._hub_label(row.sell_station)
        return (
            f"    ↳ {sell_label}",
            "-",                                                      # supply price (buy row)
            _fmt_isk(row.target_sell_price),                          # estimated sell price
            _fmt_isk(row.dest_avg_7d),                                # dest 7d avg
            _fmt_isk(row.dest_avg_30d),                               # dest 30d avg
            _fmt_vel(row.dest_velocity),                              # velocity
            _fmt_qty(row.dest_stock),                                 # remaining stock at dest
            _fmt_days(row.days_of_stock),                             # days to depletion
            _fmt_qty(row.ship_qty),                                   # recommended stock amount
            _fmt_isk(row.profit_per_unit),                            # profit / unit
            _fmt_isk(row.total_profit),                               # total profit
            _fmt_m3(row.cargo_m3),                                    # cargo m³
        )

    def _sell_row_tag(self, row: DemandRow) -> str:
        """Pick the color tag for the sell row based on sanity signals.

        Order of precedence: junk (target >>5× avg) > suspicious (target >2× avg) >
        instant_sale > default. Junk should already be filtered by the margin
        gate but coloring stays in case a user lowers min_margin_pct.
        """
        ratio = row.target_over_avg_ratio
        if ratio >= JUNK_RATIO:
            return "junk_listing"
        if ratio >= SUSPICIOUS_RATIO:
            return "suspicious"
        if row.target_uses_buy_order:
            return "instant_sale"
        return "sell_station"

    def _refresh_tree(self, apply_sort_mode: bool):
        for item in self.tree.get_children():
            self.tree.delete(item)
        # tree-item-id → DemandRow, for context-menu lookups (both rows of a
        # pair map to the same DemandRow).
        self._item_to_row: dict[str, DemandRow] = {}

        if not self.all_rows:
            self._set_empty_hint("No items passed the demand gates.")
            return

        rows = sort_demand_rows(self.all_rows, self.sort_mode) if apply_sort_mode else self.all_rows

        for row in rows:
            buy_id = self.tree.insert(
                "", tk.END, values=self._format_buy_row(row), tags=("buy_station",)
            )
            self._item_to_row[buy_id] = row
            sell_id = self.tree.insert(
                "", tk.END, values=self._format_sell_row(row), tags=(self._sell_row_tag(row),)
            )
            self._item_to_row[sell_id] = row

    # -------------------------------------------------------------------------
    # Column-header sorting (preserves dual-row pairs)
    # -------------------------------------------------------------------------

    def _sort_by_column(self, col: str):
        if not self.all_rows:
            return

        # Toggle direction per column
        if not hasattr(self, "_col_dir"):
            self._col_dir = {}
        reverse = self._col_dir.get(col, False)
        self._col_dir[col] = not reverse

        key_funcs = {
            "name": lambda r: r.name.lower(),
            "supply": lambda r: r.source_price,
            "est_sell": lambda r: r.target_sell_price,
            "avg_7d": lambda r: r.dest_avg_7d,
            "avg_30d": lambda r: r.dest_avg_30d,
            "velocity": lambda r: r.dest_velocity,           # sort by dest vel — it's the gate
            "stock": lambda r: r.dest_stock,                  # sort by dest stock — the gap signal
            "days_to_dep": lambda r: r.days_of_stock,
            "rec_amount": lambda r: r.ship_qty,
            "profit_unit": lambda r: r.profit_per_unit,
            "total_profit": lambda r: r.total_profit,
            "cargo_m3": lambda r: r.cargo_m3,
        }
        key = key_funcs.get(col)
        if key is None:
            return
        self.all_rows.sort(key=key, reverse=reverse)
        # Header-click sort overrides the radio for this view, but doesn't
        # change the persisted sort_mode setting.
        self._refresh_tree(apply_sort_mode=False)

    # -------------------------------------------------------------------------
    # Right-click context menu
    # -------------------------------------------------------------------------

    def _on_right_click(self, event):
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self.context_menu.post(event.x_root, event.y_root)

    def _on_double_click(self, event):
        """Double-click opens both source + dest graphs side-by-side."""
        item = self.tree.identify_row(event.y)
        if not item:
            return
        self.tree.selection_set(item)
        self._cm_view_graph()

    def _selected_row(self) -> Optional[DemandRow]:
        sel = self.tree.selection()
        if not sel:
            return None
        item_to_row = getattr(self, "_item_to_row", {})
        return item_to_row.get(sel[0])

    def _cm_view_graph(self):
        """Open price-history graphs for both the source and destination hubs,
        positioned side-by-side so the user can eyeball the spread directly.
        Falls back to a single window if source == dest (shouldn't happen in
        Demand/Restock since it's cross-hub only, but be safe).
        """
        row = self._selected_row()
        if not row or not self.root:
            return
        try:
            from analytics.graphing import PriceGraphDialog

            buy_config = get_hub_config(row.buy_station)
            sell_config = get_hub_config(row.sell_station)

            same_region = buy_config["region_id"] == sell_config["region_id"]

            src_dlg = PriceGraphDialog(
                self.root, row.type_id,
                f"{row.name} - {buy_config.get('name', row.buy_station)} (Source)",
                buy_config["region_id"], profiles=None,
            )
            src_dlg.show()

            if same_region:
                return

            dst_dlg = PriceGraphDialog(
                self.root, row.type_id,
                f"{row.name} - {sell_config.get('name', row.sell_station)} (Dest)",
                sell_config["region_id"], profiles=None,
            )
            dst_dlg.show()

            # Place the pair side-by-side, centered on screen, so the user can
            # eyeball the spread without dragging windows around.
            try:
                self.root.update_idletasks()
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                w, h, gap = 950, 680, 20
                total = w * 2 + gap
                left = max(0, (sw - total) // 2)
                top = max(0, (sh - h) // 2 - 60)
                if src_dlg.popup:
                    src_dlg.popup.geometry(f"{w}x{h}+{left}+{top}")
                if dst_dlg.popup:
                    dst_dlg.popup.geometry(f"{w}x{h}+{left + w + gap}+{top}")
            except Exception as e:
                print(f"[Demand] graph positioning skipped: {e}")
        except Exception as e:
            print(f"[Demand] graph open failed: {e}")
            self.set_status(f"Could not open price history: {e}")

    def _cm_copy_name(self):
        row = self._selected_row()
        if not row or not self.root:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(row.name)
            self.set_status(f"Copied: {row.name}")
        except Exception as e:
            print(f"[Demand] copy failed: {e}")

    def _cm_add_watchlist(self):
        row = self._selected_row()
        if not row:
            return
        if not self.watchlist_manager:
            self.set_status("Watchlist not available.")
            return
        try:
            # The watchlist tracks the destination price (where we'd sell).
            self.watchlist_manager.add_from_deal(
                type_id=row.type_id,
                name=row.name,
                current_price=row.target_sell_price,
            )
            self.set_status(f"Added to watchlist: {row.name}")
        except Exception as e:
            print(f"[Demand] add_from_deal failed: {e}")
            self.set_status(f"Add to watchlist failed: {e}")

    def _cm_add_stock_market(self):
        row = self._selected_row()
        if not row:
            return
        if not self.stock_market_tab:
            self.set_status("Stock Market tab not available.")
            return
        try:
            hub_config = get_hub_config(row.sell_station)
            self.stock_market_tab.add_item_from_external(
                type_id=row.type_id,
                region_id=hub_config["region_id"],
                station_id=hub_config["station_id"],
                type_name=row.name,
            )
            self.set_status(f"Added to Stock Market: {row.name}")
        except Exception as e:
            print(f"[Demand] add_item_from_external failed: {e}")
            self.set_status(f"Add to Stock Market failed: {e}")
