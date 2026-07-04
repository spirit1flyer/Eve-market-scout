"""NPC Orders tab for EVE Market Scout.

Duplicate of watchlist functionality for organizing calculated flips separately.
Supports copy/paste between this tab and watchlist.
"""

import tkinter as tk
from tkinter import ttk
import os
import json
from typing import Callable, Optional
from dataclasses import asdict

from gui.watchlist.gui_watchlist_dialogs import (
    WatchlistItem,
    watchlist_item_from_dict,
    AddItemDialog,
    BulkAddDialog,
    EditItemDialog
)
from core.sound_manager import get_data_dir
from gui.gui_npc_orders_pnl import NPCSalesTracker


# NPC Orders persistence file - use centralized data directory
NPC_ORDERS_FILE = str(get_data_dir() / "npc_orders.json")

# Magic header for TSV clipboard format (must match watchlist for cross-tab paste)
WATCHLIST_TSV_HEADER = "EVE_MARKET_SCOUT_WATCHLIST_V1"

# Columns that should sort numerically
NPC_ORDERS_NUMERIC_COLUMNS = {"price_under", "price_over", "current_price", "qty"}


def _trends_from_activity(sales, buys) -> dict:
    """Day/week/month/year buckets of cash-flow per period.

    Each sale contributes +(gross - tax) on its sale date; each buy
    contributes -total on its buy date. Periods where you've spent without
    selling come out negative, which is the signal the user wants.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    cutoffs = {
        "day":   now - timedelta(days=1),
        "week":  now - timedelta(days=7),
        "month": now - timedelta(days=30),
        "year":  now - timedelta(days=365),
    }
    totals = {k: 0.0 for k in cutoffs}

    def _parse(iso: str):
        try:
            t = datetime.fromisoformat(iso)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            return t
        except (ValueError, TypeError):
            return None

    for s in sales:
        t = _parse(s.date)
        if t is None:
            continue
        contrib = s.gross - s.sales_tax
        for period, cutoff in cutoffs.items():
            if t >= cutoff:
                totals[period] += contrib

    for b in buys:
        t = _parse(b.date)
        if t is None:
            continue
        contrib = -b.total
        for period, cutoff in cutoffs.items():
            if t >= cutoff:
                totals[period] += contrib

    return totals


class NPCOrdersTabManager:
    """Manages the NPC Orders tab - same functionality as watchlist."""

    def __init__(self, notebook: ttk.Notebook, get_client: Callable = None, set_status: Callable = None):
        self.notebook = notebook
        self.get_client = get_client
        self.set_status = set_status or (lambda x: None)
        
        # NPC orders data
        self.orders: dict[int, WatchlistItem] = {}  # type_id -> WatchlistItem
        self._load_orders()
        
        # Track alert state for tab coloring
        self.has_alerts = False
        self.tab_index = None
        
        # Clipboard for copy/paste (shared via setter)
        self._clipboard_getter = None
        self._clipboard_setter = None
        
        # Skills getter and region for max-buy calc in Add dialog (wired by gui_main)
        self.get_skills: Optional[Callable] = None
        self.region_id: Optional[int] = None
        # Nearest-station-rep calc getters (wired by gui_main). Used by the
        # Add/Edit dialogs to filter buy orders by jumps and to look up the
        # user's standings at the chosen buyer's station for the tax math.
        self.get_origin_system: Optional[Callable] = None
        self.get_esi_standings: Optional[Callable] = None
        
        # Sort state tracking
        self.sort_state: dict[str, bool] = {}  # column -> reverse

        # Auto-tracked sales ledger (per-character JSON, rolls forward).
        # Character name is wired in later by gui_main once auth is available;
        # we start with "unknown" so any pre-auth state stays out of the way.
        self.sales_tracker = NPCSalesTracker(character_name="unknown")

        self._create_tab()

    def set_skills_getter(self, getter: Callable):
        """Set the function used to retrieve current TradingSkills (with standings/overrides)."""
        self.get_skills = getter

    def set_origin_system_getter(self, getter: Callable):
        """Set the function used to retrieve the origin system_id for the
        max-buy calc's jump filter (the scanner's selected sell hub)."""
        self.get_origin_system = getter

    def set_esi_standings_getter(self, getter: Callable):
        """Set the function used to retrieve the live ESIStandings instance,
        so the max-buy calc can look up rep against any corp/faction."""
        self.get_esi_standings = getter

    def set_region_id(self, region_id: int):
        """Set the region used for fetching buy orders in the Add dialog calc."""
        self.region_id = region_id

    def set_clipboard_functions(self, getter: Callable, setter: Callable):
        """Set clipboard getter/setter for cross-tab copy/paste."""
        self._clipboard_getter = getter
        self._clipboard_setter = setter

    def set_seller_character(self, character_name: str):
        """Point the sales ledger at the current seller character's file."""
        self.sales_tracker.set_character(character_name)
        self._refresh_sales_panel()

    def _reset_sales_tracker(self):
        """Confirm + wipe the sales/buys ledger with a cutoff at now."""
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Reset sales tracker",
            "Wipe all tracked sales and buys for this character?\n\n"
            "Future ESI refreshes will only pick up transactions dated AFTER "
            "this moment, so what's already in the ESI window won't repopulate.",
            parent=self.frame,
        ):
            return
        self.sales_tracker.reset()
        self._refresh_sales_panel()
        self.set_status("NPC Orders sales tracker reset.")

    def on_wallet_refresh(self, wallet):
        """Called by TrackingTabManager after each ESI wallet sync.

        Ingests new sell-side AND buy-side transactions for items in our NPC
        Orders list. Buys feed the FIFO cost-basis calc so Net reflects real
        spending; sells feed the revenue/tax totals. Idempotent.
        """
        try:
            tracked = set(self.orders.keys())
            new_sales, new_buys = self.sales_tracker.ingest(wallet, tracked)
            if new_sales or new_buys:
                self.set_status(
                    f"NPC Orders: tracked {new_sales} sale(s), {new_buys} buy(s)"
                )
        except Exception as e:
            print(f"[NPCSales] ingest error: {e}")
        self._refresh_sales_panel()

    def _load_orders(self):
        """Load NPC orders from JSON file."""
        try:
            if os.path.exists(NPC_ORDERS_FILE):
                with open(NPC_ORDERS_FILE, "r") as f:
                    data = json.load(f)
                    for item_data in data.get("items", []):
                        item = watchlist_item_from_dict(item_data)
                        # Clear market data - should only come from fresh scans
                        item.current_price = None
                        item.current_qty = None
                        self.orders[item.type_id] = item
        except Exception as e:
            print(f"Error loading NPC orders: {e}")

    def _save_orders(self):
        """Save NPC orders to JSON file."""
        try:
            items = [asdict(item) for item in self.orders.values()]
            with open(NPC_ORDERS_FILE, "w") as f:
                json.dump({"items": items}, f, indent=2)
        except Exception as e:
            print(f"Error saving NPC orders: {e}")

    def _create_tab(self):
        """Create the NPC Orders tab."""
        self.frame = ttk.Frame(self.notebook)
        self.notebook.add(self.frame, text="NPC Orders")
        
        self.tab_index = self.notebook.index(self.frame)

        # Button bar
        btn_frame = ttk.Frame(self.frame, padding=5)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="+ Add Item", command=self._show_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="+ Bulk Add", command=self._show_bulk_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Edit", command=self._edit_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove", command=self._remove_selected).pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)
        
        ttk.Button(btn_frame, text="Copy", command=self._copy_selected).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Paste", command=self._paste_items).pack(side=tk.LEFT, padx=5)
        
        ttk.Label(btn_frame, text="Ctrl+C/V or right-click", foreground="gray").pack(side=tk.RIGHT, padx=10)

        # Horizontal split: Summary on the left (matches Tracking tab),
        # orders treeview fills the rest.
        content_frame = ttk.Frame(self.frame)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # Summary pane (left, narrow column)
        self._build_sales_panel(content_frame)

        tree_pane = ttk.Frame(content_frame)
        tree_pane.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Treeview with EXTENDED selection for multi-select
        columns = ("name", "price_under", "price_over", "current_price", "qty", "status", "notes")
        self.tree = ttk.Treeview(
            tree_pane,
            columns=columns,
            show="headings",
            style="Deals.Treeview",
            selectmode="extended"  # Allow multi-select
        )

        # Column headings with sort commands
        self.tree.heading("name", text="Item Name", command=lambda: self._sort_tree("name"))
        self.tree.heading("price_under", text="Price Under", command=lambda: self._sort_tree("price_under"))
        self.tree.heading("price_over", text="Price Over", command=lambda: self._sort_tree("price_over"))
        self.tree.heading("current_price", text="Current Price", command=lambda: self._sort_tree("current_price"))
        self.tree.heading("qty", text="Qty", command=lambda: self._sort_tree("qty"))
        self.tree.heading("status", text="Status", command=lambda: self._sort_tree("status"))
        self.tree.heading("notes", text="Notes", command=lambda: self._sort_tree("notes"))

        # Column widths
        self.tree.column("name", width=200, minwidth=150)
        self.tree.column("price_under", width=100, anchor=tk.E)
        self.tree.column("price_over", width=100, anchor=tk.E)
        self.tree.column("current_price", width=100, anchor=tk.E)
        self.tree.column("qty", width=70, anchor=tk.E)
        self.tree.column("status", width=100, anchor=tk.CENTER)
        self.tree.column("notes", width=200)

        # Scrollbars
        vsb = ttk.Scrollbar(tree_pane, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_pane, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags for status colors
        self.tree.tag_configure("alert", foreground="white", background="#228B22")
        self.tree.tag_configure("normal", foreground="black")
        self.tree.tag_configure("no_data", foreground="gray")

        # Context menu
        self.context_menu = tk.Menu(self.frame, tearoff=0)
        self.context_menu.add_command(label="Edit Item", command=self._edit_selected)
        self.context_menu.add_command(label="Remove Item", command=self._remove_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy (Ctrl+C)", command=self._copy_selected)
        self.context_menu.add_command(label="Paste (Ctrl+V)", command=self._paste_items)

        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self._edit_selected())
        
        # Keyboard shortcuts
        self.tree.bind("<Control-c>", lambda e: self._copy_selected())
        self.tree.bind("<Control-v>", lambda e: self._paste_items())

        self._refresh_display()

    def _sort_tree(self, col: str):
        """Sort treeview by column when header is clicked."""
        # Toggle sort direction
        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        # Get all items with their values
        items = [(self.tree.set(item, col), item) for item in self.tree.get_children("")]

        if col in NPC_ORDERS_NUMERIC_COLUMNS:
            def parse_num(val):
                if val == "-" or val == "No data":
                    return float("-inf") if reverse else float("inf")
                val = val.replace(",", "").replace("%", "")
                try:
                    return float(val)
                except ValueError:
                    return float("-inf") if reverse else float("inf")
            items.sort(key=lambda x: parse_num(x[0]), reverse=reverse)
        else:
            items.sort(key=lambda x: x[0].lower(), reverse=reverse)

        # Rearrange items
        for idx, (_, item) in enumerate(items):
            self.tree.move(item, "", idx)

        # Update header to show sort direction
        for c in self.tree["columns"]:
            text = self._get_column_title(c)
            self.tree.heading(c, text=text)
        
        arrow = " v" if reverse else " ^"
        self.tree.heading(col, text=self._get_column_title(col) + arrow)

    def _get_column_title(self, col: str) -> str:
        """Get display title for a column."""
        titles = {
            "name": "Item Name",
            "price_under": "Price Under",
            "price_over": "Price Over",
            "current_price": "Current Price",
            "qty": "Qty",
            "status": "Status",
            "notes": "Notes"
        }
        return titles.get(col, col)

    def _show_context_menu(self, event):
        """Show right-click context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            # Add to selection if not already selected
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def _copy_selected(self):
        """
        Unified copy to OS clipboard.
        - 1 item: just the name (paste-friendly for EVE).
        - Multiple items: TSV with magic header for round-trip into the app.
        """
        selection = self.tree.selection()
        if not selection:
            self.set_status("No items selected to copy")
            return

        items = []
        for iid in selection:
            try:
                type_id = int(iid)
            except ValueError:
                continue
            if type_id in self.orders:
                items.append(self.orders[type_id])

        if not items:
            self.set_status("No items selected to copy")
            return

        if len(items) == 1:
            text = items[0].name
            status_msg = f"Copied name: {items[0].name}"
        else:
            lines = [WATCHLIST_TSV_HEADER]
            lines.append("type_id\tname\tprice_under\tprice_over\tnotes")
            for it in items:
                lines.append("\t".join([
                    str(it.type_id),
                    it.name,
                    str(it.price_under) if it.price_under else "",
                    str(it.price_over) if it.price_over else "",
                    (it.notes or "").replace("\t", " ").replace("\n", " "),
                ]))
            text = "\n".join(lines)
            status_msg = f"Copied {len(items)} item(s) to clipboard"

        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
            self.frame.update()
            self.set_status(status_msg)
        except tk.TclError as e:
            self.set_status(f"Clipboard error: {e}")

    def _paste_items(self):
        """
        Unified paste from OS clipboard.
        - If our TSV header is detected: restore items with full data.
        - Otherwise: treat clipboard as a list of names and open BulkAddDialog
          pre-filled, which auto-resolves type_ids via ESI.
        """
        try:
            text = self.frame.clipboard_get()
        except tk.TclError:
            self.set_status("Clipboard is empty or not text")
            return

        if not text or not text.strip():
            self.set_status("Clipboard is empty")
            return

        lines = text.split("\n")
        if lines and lines[0].strip() == WATCHLIST_TSV_HEADER:
            self._paste_from_tsv(lines)
        else:
            BulkAddDialog(self.frame, self._on_bulk_add, self.get_client, prefill_text=text)

    def _paste_from_tsv(self, lines):
        """Parse our internal TSV format and restore items with full data."""
        if len(lines) < 3:
            self.set_status("Clipboard format invalid")
            return

        added = 0
        skipped = 0
        # Old exports carried a margin_over column between price_over and
        # notes; detect it via the column header so both layouts paste.
        legacy_margin = "margin_over" in lines[1].split("\t")
        notes_idx = 5 if legacy_margin else 4
        for line in lines[2:]:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            try:
                type_id = int(parts[0])
            except ValueError:
                continue

            if type_id in self.orders:
                skipped += 1
                continue

            name = parts[1]
            price_under = self._parse_optional_float(parts[2] if len(parts) > 2 else "")
            price_over = self._parse_optional_float(parts[3] if len(parts) > 3 else "")
            notes = parts[notes_idx] if len(parts) > notes_idx else ""

            self.orders[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=price_under,
                price_over=price_over,
                notes=notes,
            )
            added += 1

        if added > 0:
            self._save_orders()
            self._refresh_display()
            self.set_status(f"Pasted {added} item(s)" + (f" ({skipped} already in list)" if skipped else ""))
        else:
            self.set_status("All items already in NPC Orders")

    @staticmethod
    def _parse_optional_float(s):
        """Parse a possibly-empty string into float or None."""
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _update_tab_color(self):
        """Update tab appearance based on alert status."""
        alert_items = self.get_alert_items()
        alert_count = len(alert_items)
        
        if alert_count > 0:
            self.has_alerts = True
            self.notebook.tab(self.tab_index, text=f"NPC Orders ({alert_count}!)")
        else:
            self.has_alerts = False
            item_count = len(self.orders)
            if item_count > 0:
                self.notebook.tab(self.tab_index, text=f"NPC Orders ({item_count})")
            else:
                self.notebook.tab(self.tab_index, text="NPC Orders")

    def _refresh_display(self):
        """Refresh the treeview with current data."""
        for item in self.tree.get_children():
            self.tree.delete(item)

        for order_item in self.orders.values():
            self._insert_item(order_item)

        self._update_tab_color()
        self._refresh_sales_panel()

    # ---- Auto-tracked sales summary ---------------------------------------

    def _build_sales_panel(self, parent):
        """Summary-style aggregate panel, modelled on the Tracking tab's
        left SummaryPanel. Per-item / per-station math is computed internally
        (see _refresh_sales_panel) but only headline numbers are displayed.
        """
        panel = ttk.LabelFrame(parent, text="Sales Summary (auto-tracked)", padding=10)
        panel.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)

        # Header line: which character + record count.
        self.sales_char_label = ttk.Label(
            panel, text="—", foreground="gray", font=("Segoe UI", 8)
        )
        self.sales_char_label.pack(anchor=tk.W, pady=(0, 2))

        # Reset button -- sets a cutoff at "now" so transactions already in
        # the ESI window don't re-populate the ledger.
        ttk.Button(
            panel, text="Reset (start fresh)",
            command=self._reset_sales_tracker, width=18
        ).pack(anchor=tk.W, pady=(0, 8))

        # Totals block ------------------------------------------------------
        ttk.Label(panel, text="Totals:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        totals_frame = ttk.Frame(panel)
        totals_frame.pack(anchor=tk.W, pady=5)
        totals_rows = [
            ("Sold:",     "sales_qty_label"),
            ("Revenue:",  "sales_revenue_label"),
            ("Cost:",     "sales_cost_label"),
            ("Tax:",      "sales_tax_label"),
            ("Tax %:",    "sales_tax_pct_label"),
            ("Net:",      "sales_net_label"),
        ]
        for text, attr in totals_rows:
            row = ttk.Frame(totals_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=10, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="—", width=14, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)

        # Compact planned-from-price_under footnotes under the Totals block
        # (kept here instead of a separate section so the panel matches the
        # Tracking-tab summary's two-block visual).
        planned_rows = [
            ("Planned Cost:", "sales_planned_cost_label"),
            ("Planned Net:",  "sales_planned_net_label"),
        ]
        for text, attr in planned_rows:
            row = ttk.Frame(totals_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=12, anchor=tk.W,
                      foreground="gray", font=("Segoe UI", 8)).pack(side=tk.LEFT)
            label = ttk.Label(row, text="—", width=12, anchor=tk.E,
                              foreground="gray", font=("Segoe UI", 8))
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)

        # Note appears under Cost when not all sales had a matching buy lot.
        self.sales_cost_note = ttk.Label(
            totals_frame, text="", foreground="#a00000",
            font=("Segoe UI", 8), wraplength=180, justify=tk.LEFT
        )
        self.sales_cost_note.pack(anchor=tk.W, pady=(2, 0))

        # Profit trends block ----------------------------------------------
        ttk.Separator(panel, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=10)
        ttk.Label(panel, text="Profit Trends:", font=("Segoe UI", 9, "bold")).pack(anchor=tk.W)
        trends_frame = ttk.Frame(panel)
        trends_frame.pack(anchor=tk.W, pady=5)
        trend_rows = [
            ("Day:",   "sales_trend_day"),
            ("Week:",  "sales_trend_week"),
            ("Month:", "sales_trend_month"),
            ("Year:",  "sales_trend_year"),
        ]
        for text, attr in trend_rows:
            row = ttk.Frame(trends_frame)
            row.pack(fill=tk.X, pady=1)
            ttk.Label(row, text=text, width=10, anchor=tk.W).pack(side=tk.LEFT)
            label = ttk.Label(row, text="—", width=14, anchor=tk.E)
            label.pack(side=tk.RIGHT)
            setattr(self, attr, label)

    def _refresh_sales_panel(self):
        """Recompute the headline numbers from the persisted ledger.

        Math is internal:
          - Revenue/Tax come from sells.
          - Cost is FIFO-matched against tracked buys (per-item).
          - Planned Cost uses the NPC Order's price_under as the assumed buy.
        UI displays only the totals; per-item / per-station detail stays
        inside compute_cost_basis.
        """
        if not hasattr(self, "sales_qty_label"):
            return

        from core.calculate import format_isk

        tracked_ids = set(self.orders.keys())
        relevant = [s for s in self.sales_tracker.sales.values()
                    if s.type_id in tracked_ids]
        relevant_buys = [b for b in self.sales_tracker.buys.values()
                         if b.type_id in tracked_ids]

        # True empty state only when neither buys nor sales exist for tracked
        # items. If only buys exist (catch-up state), we still show the panel
        # with Cost populated so the user can see something is being tracked.
        if not relevant and not relevant_buys:
            self.sales_char_label.configure(
                text=f"character: {self.sales_tracker.character_name}  ·  "
                     "no transactions recorded yet"
            )
            for attr in ("sales_qty_label", "sales_revenue_label",
                         "sales_cost_label", "sales_tax_label",
                         "sales_tax_pct_label", "sales_net_label",
                         "sales_planned_cost_label", "sales_planned_net_label",
                         "sales_trend_day", "sales_trend_week",
                         "sales_trend_month", "sales_trend_year"):
                getattr(self, attr).configure(text="—", foreground="black")
            self.sales_cost_note.configure(text="")
            return

        # Headline totals from sells
        total_qty = sum(s.quantity for s in relevant)
        total_gross = sum(s.gross for s in relevant)
        total_tax = sum(s.sales_tax for s in relevant)
        tax_pct = (total_tax / total_gross * 100.0) if total_gross > 0 else 0.0

        # Cash-flow Cost: every ISK spent on tracked-item buys, regardless of
        # whether the inventory has been sold yet. Net therefore goes negative
        # when buys exist without offsetting sales -- which is the signal the
        # user wants while inventory is sitting.
        actual_cost = sum(b.total for b in relevant_buys)
        net = total_gross - actual_cost - total_tax

        # Planned cost: what we WOULD have spent if every buy had been at the
        # item's Price Under threshold. Comparing Cost vs Planned Cost shows
        # whether actual buy prices stayed within the planned max-buy.
        planned_cost = 0.0
        planned_unknown_qty = 0
        for b in relevant_buys:
            item = self.orders.get(b.type_id)
            if item and item.price_under:
                planned_cost += b.quantity * item.price_under
            else:
                planned_unknown_qty += b.quantity
        planned_net = total_gross - planned_cost - total_tax

        self.sales_qty_label.configure(text=f"{total_qty:,}")
        self.sales_revenue_label.configure(text=format_isk(total_gross, short=True))
        self.sales_cost_label.configure(text=format_isk(actual_cost, short=True))
        self.sales_tax_label.configure(text=format_isk(total_tax, short=True))
        self.sales_tax_pct_label.configure(text=f"{tax_pct:.2f}%")
        self.sales_net_label.configure(
            text=format_isk(net, short=True),
            foreground="#006400" if net >= 0 else "#8B0000"
        )

        # Cost line already shows total buys spend; no extra note needed.
        self.sales_cost_note.configure(text="")

        self.sales_planned_cost_label.configure(text=format_isk(planned_cost, short=True))
        self.sales_planned_net_label.configure(
            text=format_isk(planned_net, short=True),
            foreground="#006400" if planned_net >= 0 else "#8B0000"
        )

        # Trends: cash-flow per period.
        #   sales contribute +(gross - tax) on sale date
        #   buys contribute  -(total)        on buy date
        # So a week of only-buys shows up as red/negative, matching the
        # user's mental model of "I spent ISK and haven't earned it back yet".
        trends = _trends_from_activity(relevant, relevant_buys)
        for period, attr in [("day", "sales_trend_day"),
                             ("week", "sales_trend_week"),
                             ("month", "sales_trend_month"),
                             ("year", "sales_trend_year")]:
            value = trends[period]
            getattr(self, attr).configure(
                text=format_isk(value, short=True),
                foreground="#006400" if value >= 0 else "#8B0000"
            )

        planned_note = ""
        if planned_unknown_qty > 0:
            planned_note = (f"  ·  {planned_unknown_qty:,} bought w/o "
                            "Price Under set")
        self.sales_char_label.configure(
            text=f"character: {self.sales_tracker.character_name}  ·  "
                 f"{len(relevant)} sale(s), {len(relevant_buys)} buy(s)"
                 f"{planned_note}"
        )

    # ---- end auto-tracked sales summary -----------------------------------

    def _insert_item(self, item: WatchlistItem):
        """Insert an item into the tree."""
        price_under = f"{item.price_under:,.0f}" if item.price_under else "-"
        price_over = f"{item.price_over:,.0f}" if item.price_over else "-"

        # Distinguish "no listings" (qty=0) from "never scanned" (qty=None)
        if item.current_price:
            current = f"{item.current_price:,.0f}"
            qty = f"{item.current_qty:,}" if item.current_qty else "-"
        elif item.current_qty == 0:
            current = "No listings"
            qty = "0"
        else:
            current = "No data"
            qty = "-"
        
        status = "Watching"
        tag = "normal"
        
        if item.current_price:
            alerts = []
            if item.price_under and item.current_price <= item.price_under:
                alerts.append("UNDER")
            if item.price_over and item.current_price >= item.price_over:
                alerts.append("OVER")

            if alerts:
                status = " | ".join(alerts)
                tag = "alert"
        else:
            tag = "no_data"

        self.tree.insert("", tk.END, iid=str(item.type_id), values=(
            item.name,
            price_under,
            price_over,
            current,
            qty,
            status,
            item.notes[:30] + "..." if len(item.notes) > 30 else item.notes
        ), tags=(tag,))

    def _get_selected_type_id(self) -> Optional[int]:
        """Get type_id of first selected item."""
        selection = self.tree.selection()
        if selection:
            return int(selection[0])
        return None

    def _nearest_calc_kwargs(self) -> dict:
        """Common kwargs for Add/Edit dialogs that enable the nearest-station
        rep-aware max-buy calc. Used everywhere we open a dialog from this
        tab so all entry points behave consistently."""
        return {
            "nearest_station_mode": True,
            "get_origin_system": self.get_origin_system,
            "get_esi_standings": self.get_esi_standings,
        }

    def _show_add_dialog(self):
        """Show dialog to add new item."""
        AddItemDialog(
            self.frame,
            self._on_add_item,
            self.get_client,
            get_skills=self.get_skills,
            region_id=self.region_id,
            **self._nearest_calc_kwargs(),
        )

    def _show_bulk_add_dialog(self):
        """Show dialog to bulk add items."""
        BulkAddDialog(self.frame, self._on_bulk_add, self.get_client)

    def _on_add_item(self, type_id: int, name: str, conditions: dict):
        """Callback when item is added."""
        if type_id in self.orders:
            item = self.orders[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.notes = conditions.get("notes", "")
        else:
            self.orders[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=conditions.get("price_under"),
                price_over=conditions.get("price_over"),
                notes=conditions.get("notes", "")
            )
        
        self._save_orders()
        self._refresh_display()
        self.set_status(f"Added to NPC Orders: {name}")

    def _on_bulk_add(self, items: list[dict]):
        """Callback when items are bulk added."""
        added = 0
        for item_data in items:
            type_id = item_data["type_id"]
            name = item_data["name"]
            
            if type_id not in self.orders:
                self.orders[type_id] = WatchlistItem(
                    type_id=type_id,
                    name=name
                )
                added += 1
        
        self._save_orders()
        self._refresh_display()
        self.set_status(f"Added {added} items to NPC Orders")

    def _edit_selected(self):
        """Edit the selected item."""
        type_id = self._get_selected_type_id()
        if type_id and type_id in self.orders:
            item = self.orders[type_id]
            EditItemDialog(
                self.frame, item, self._on_edit_item,
                get_client=self.get_client,
                get_skills=self.get_skills,
                region_id=self.region_id,
                show_max_buy_calc=True,
                **self._nearest_calc_kwargs(),
            )

    def _on_edit_item(self, type_id: int, conditions: dict):
        """Callback when item is edited."""
        if type_id in self.orders:
            item = self.orders[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.notes = conditions.get("notes", "")

            self._save_orders()
            self._refresh_display()

    def _remove_selected(self):
        """Remove selected items."""
        selection = self.tree.selection()
        if not selection:
            return
        
        removed = 0
        for iid in selection:
            type_id = int(iid)
            if type_id in self.orders:
                del self.orders[type_id]
                removed += 1
        
        if removed > 0:
            self._save_orders()
            self._refresh_display()
            self.set_status(f"Removed {removed} item(s) from NPC Orders")

    def add_from_deal(self, type_id: int, name: str, current_price: float = None):
        """Add item from deals context menu."""
        if type_id in self.orders:
            item = self.orders[type_id]
            EditItemDialog(
                self.frame, item, self._on_edit_item,
                get_client=self.get_client,
                get_skills=self.get_skills,
                region_id=self.region_id,
                show_max_buy_calc=True,
                **self._nearest_calc_kwargs(),
            )
        else:
            temp_item = WatchlistItem(
                type_id=type_id,
                name=name,
                current_price=current_price
            )
            AddItemDialog(
                self.frame,
                self._on_add_item,
                self.get_client,
                prefill=temp_item,
                get_skills=self.get_skills,
                region_id=self.region_id,
                **self._nearest_calc_kwargs(),
            )

    def update_from_local_orders(self, orders: list[dict]):
        """Update prices from local hub market orders."""
        if not self.orders:
            return
        
        # Build lookup: type_id -> (lowest sell price, quantity at that price)
        sell_data = {}  # type_id -> {"price": float, "qty": int}
        for order in orders:
            if order.get("is_buy_order"):
                continue
            
            type_id = order["type_id"]
            price = order["price"]
            volume = order.get("volume_remain", 0)
            
            if type_id not in sell_data or price < sell_data[type_id]["price"]:
                # New lowest price - reset quantity
                sell_data[type_id] = {"price": price, "qty": volume}
            elif price == sell_data[type_id]["price"]:
                # Same price - accumulate quantity
                sell_data[type_id]["qty"] += volume
        
        for type_id, item in self.orders.items():
            if type_id in sell_data:
                item.current_price = sell_data[type_id]["price"]
                item.current_qty = sell_data[type_id]["qty"]
            else:
                # No sell orders for this item - clear stale data
                item.current_price = None
                item.current_qty = 0
        
        self._refresh_display()

    def get_alert_items(self) -> list[WatchlistItem]:
        """Get items that have triggered alerts."""
        alerts = []
        for item in self.orders.values():
            if item.current_price:
                if item.price_under and item.current_price <= item.price_under:
                    alerts.append(item)
                elif item.price_over and item.current_price >= item.price_over:
                    alerts.append(item)
        return alerts
