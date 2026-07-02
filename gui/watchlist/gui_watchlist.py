"""Persistent watchlist tab for EVE Market Scout.

Features:
- Add items by name (ESI search or local cache)
- Bulk add from pasted fitting/market format
- Set custom alert conditions per item (price under X, margin over Y, etc.)
- Persist watchlist to JSON
- Right-click to edit/remove items
- Green tab + row highlighting when alerts trigger
- Multi-select with copy/paste to NPC Orders tab
- User-defined categories (multi-tag) shown as sub-tabs

Structure:
- `WatchlistTreePanel` owns one filtered table view (the treeview + sorting +
  context menu + clipboard-copy). It is the per-sub-tab unit. All data
  mutation is delegated back to the manager so an edit/remove in one tab
  refreshes every tab the item appears in.
- `WatchlistTabManager` owns the data (the watchlist dict + category list),
  persistence, the add/edit/remove dialogs, and fans scan updates out to all
  panels. It is the public face used by the rest of the app.
"""

import tkinter as tk
from tkinter import ttk
import os
import json
from typing import Callable, Optional
from dataclasses import asdict

from gui.watchlist.gui_watchlist_dialogs import (
    WatchlistItem,
    AddItemDialog,
    BulkAddDialog,
    EditItemDialog
)
from core.sound_manager import get_data_dir


# Watchlist persistence file - use centralized data directory
WATCHLIST_FILE = str(get_data_dir() / "watchlist.json")

# Magic header for TSV clipboard format (round-trip copy/paste between tabs)
WATCHLIST_TSV_HEADER = "EVE_MARKET_SCOUT_WATCHLIST_V1"


# Columns that should sort numerically
WATCHLIST_NUMERIC_COLUMNS = {"price_under", "price_over", "margin_over", "current_price", "qty"}

# Treeview columns + their display titles (shared by every panel).
WATCHLIST_COLUMNS = ("name", "price_under", "price_over", "margin_over", "current_price", "qty", "system", "status", "notes")
WATCHLIST_COLUMN_TITLES = {
    "name": "Item Name",
    "price_under": "Price Under",
    "price_over": "Price Over",
    "margin_over": "Margin Over %",
    "current_price": "Current Price",
    "qty": "Qty",
    "system": "System",
    "status": "Status",
    "notes": "Notes",
}


# Sentinel category for the "Uncategorized" sub-tab (items with no tags). A
# distinct object so it can never collide with a user category name (a str) or
# the "All" view (None).
UNCATEGORIZED = object()


class WatchlistTreePanel:
    """One filtered table view of the watchlist (the per-sub-tab unit).

    Owns its own treeview, sort state, context menu and clipboard-copy. Data
    mutations (edit/remove/paste/add-to-stock/price-history) are delegated back
    to the owning `WatchlistTabManager`.

    `category` selects which items the panel shows:
      - None  -> the "All" view (every item)
      - str   -> only items tagged with that category
    """

    def __init__(self, parent: tk.Widget, manager: "WatchlistTabManager", category: Optional[str] = None):
        self.parent = parent
        self.manager = manager
        self.category = category
        self.sort_state: dict[str, bool] = {}  # column -> reverse
        self._build()

    # --- construction -------------------------------------------------------

    def _build(self):
        # Treeview with EXTENDED selection for multi-select
        self.tree = ttk.Treeview(
            self.parent,
            columns=WATCHLIST_COLUMNS,
            show="headings",
            style="Deals.Treeview",
            selectmode="extended"  # Allow multi-select
        )

        # Column headings with sort commands
        for col in WATCHLIST_COLUMNS:
            self.tree.heading(col, text=WATCHLIST_COLUMN_TITLES[col],
                              command=lambda c=col: self._sort_tree(c))

        # Column widths
        self.tree.column("name", width=200, minwidth=150)
        self.tree.column("price_under", width=100, anchor=tk.E)
        self.tree.column("price_over", width=100, anchor=tk.E)
        self.tree.column("margin_over", width=100, anchor=tk.E)
        self.tree.column("current_price", width=100, anchor=tk.E)
        self.tree.column("qty", width=70, anchor=tk.E)
        self.tree.column("system", width=110, anchor=tk.W)
        self.tree.column("status", width=100, anchor=tk.CENTER)
        self.tree.column("notes", width=200)

        # Scrollbars
        vsb = ttk.Scrollbar(self.parent, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(self.parent, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # Tags for status colors - GREEN for alerts
        self.tree.tag_configure("alert", foreground="white", background="#228B22")  # Forest green
        self.tree.tag_configure("normal", foreground="black")
        self.tree.tag_configure("no_data", foreground="gray")

        # Context menu
        self.context_menu = tk.Menu(self.parent, tearoff=0)
        self.context_menu.add_command(label="Edit Item", command=self.edit_selected)
        self.context_menu.add_command(label="Remove Item", command=self.remove_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy (Ctrl+C)", command=self.copy_selected)
        self.context_menu.add_command(label="Paste (Ctrl+V)", command=self.paste)
        self.context_menu.add_separator()
        # The Categories cascade is rebuilt on every right-click so it reflects
        # the current selection and the live category list.
        self.cat_menu = tk.Menu(self.context_menu, tearoff=0)
        self.context_menu.add_cascade(label="Categories", menu=self.cat_menu)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Show Price History", command=self.show_price_history)
        self.context_menu.add_command(label="Add to Stock Market", command=self.add_to_stock_market)

        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self.edit_selected())

        # Keyboard shortcuts
        self.tree.bind("<Control-c>", lambda e: self.copy_selected())
        self.tree.bind("<Control-v>", lambda e: self.paste())

    # --- display ------------------------------------------------------------

    def _matching_items(self) -> list[WatchlistItem]:
        """Items this panel should show, per its category filter."""
        return self.manager.items_for_panel(self.category)

    def refresh(self):
        """Repopulate this panel's tree from its matching items."""
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for item in self._matching_items():
            self._insert_item(item)

    def _insert_item(self, item: WatchlistItem):
        """Insert a watchlist item into the tree."""
        # Format values
        price_under = f"{item.price_under:,.0f}" if item.price_under else "-"
        price_over = f"{item.price_over:,.0f}" if item.price_over else "-"
        margin_over = f"{item.margin_over:.1f}%" if item.margin_over else "-"

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

        # Determine status and tag
        status = "Watching"
        tag = "normal"

        if item.current_price:
            alerts = []
            if item.price_under and item.current_price <= item.price_under:
                alerts.append("UNDER")
            if item.price_over and item.current_price >= item.price_over:
                alerts.append("OVER")
            if item.margin_over and item.current_margin and item.current_margin >= item.margin_over:
                alerts.append("MARGIN")

            if alerts:
                status = " | ".join(alerts)
                tag = "alert"  # Green row
        else:
            tag = "no_data"

        system = self.manager.system_name_for(item.type_id)

        self.tree.insert("", tk.END, iid=str(item.type_id), values=(
            item.name,
            price_under,
            price_over,
            margin_over,
            current,
            qty,
            system,
            status,
            item.notes[:30] + "..." if len(item.notes) > 30 else item.notes
        ), tags=(tag,))

    def _sort_tree(self, col: str):
        """Sort treeview by column when header is clicked."""
        # Toggle sort direction
        reverse = self.sort_state.get(col, False)
        self.sort_state[col] = not reverse

        # Get all items with their values
        items = [(self.tree.set(item, col), item) for item in self.tree.get_children("")]

        if col in WATCHLIST_NUMERIC_COLUMNS:
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
            self.tree.heading(c, text=self._get_column_title(c))

        arrow = " v" if reverse else " ^"
        self.tree.heading(col, text=self._get_column_title(col) + arrow)

    @staticmethod
    def _get_column_title(col: str) -> str:
        """Get display title for a column."""
        return WATCHLIST_COLUMN_TITLES.get(col, col)

    # --- selection ----------------------------------------------------------

    def _show_context_menu(self, event):
        """Show right-click context menu."""
        item = self.tree.identify_row(event.y)
        if item:
            # Add to selection if not already selected
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self._rebuild_category_menu()
            self.context_menu.post(event.x_root, event.y_root)

    def _rebuild_category_menu(self):
        """Repopulate the Categories cascade for the current selection."""
        m = self.cat_menu
        m.delete(0, "end")
        type_ids = self._selected_type_ids()
        wl = self.manager.watchlist
        cats = self.manager.categories

        for cat in cats:
            # Checkmark when every selected item already carries this category.
            tagged_all = bool(type_ids) and all(
                cat in wl[t].categories for t in type_ids if t in wl
            )
            prefix = "✓ " if tagged_all else "    "
            m.add_command(label=prefix + cat,
                          command=lambda c=cat: self.manager.toggle_category(self._selected_type_ids(), c))

        if cats:
            m.add_separator()

        # In a real category tab, offer a quick "remove from here".
        if self.category is not None and self.category is not UNCATEGORIZED:
            m.add_command(label=f"Remove from '{self.category}'",
                          command=lambda: self.manager.untag_items(self._selected_type_ids(), self.category))
            m.add_separator()

        m.add_command(label="New category…",
                      command=lambda: self.manager.new_category_and_tag(self._selected_type_ids()))

    def _get_selected_type_id(self) -> Optional[int]:
        """Get type_id of the first selected item."""
        selection = self.tree.selection()
        if selection:
            return int(selection[0])
        return None

    def _selected_type_ids(self) -> list[int]:
        """Get type_ids of all selected items."""
        ids = []
        for iid in self.tree.selection():
            try:
                ids.append(int(iid))
            except ValueError:
                continue
        return ids

    # --- actions ------------------------------------------------------------
    # Read-only actions (copy) act locally; mutations delegate to the manager
    # so every panel showing the item is refreshed.

    def copy_selected(self):
        """
        Unified copy to OS clipboard.
        - 1 item: just the name (paste-friendly for EVE).
        - Multiple items: TSV with magic header for round-trip into the app.
        """
        selection = self.tree.selection()
        if not selection:
            self.manager.set_status("No items selected to copy")
            return

        items = []
        for iid in selection:
            try:
                type_id = int(iid)
            except ValueError:
                continue
            if type_id in self.manager.watchlist:
                items.append(self.manager.watchlist[type_id])

        if not items:
            self.manager.set_status("No items selected to copy")
            return

        if len(items) == 1:
            text = items[0].name
            status_msg = f"Copied name: {items[0].name}"
        else:
            lines = [WATCHLIST_TSV_HEADER]
            lines.append("type_id\tname\tprice_under\tprice_over\tmargin_over\tnotes")
            for it in items:
                lines.append("\t".join([
                    str(it.type_id),
                    it.name,
                    str(it.price_under) if it.price_under else "",
                    str(it.price_over) if it.price_over else "",
                    str(it.margin_over) if it.margin_over else "",
                    (it.notes or "").replace("\t", " ").replace("\n", " "),
                ]))
            text = "\n".join(lines)
            status_msg = f"Copied {len(items)} item(s) to clipboard"

        try:
            self.tree.clipboard_clear()
            self.tree.clipboard_append(text)
            self.tree.update()  # Force flush so clipboard persists when focus changes
            self.manager.set_status(status_msg)
        except tk.TclError as e:
            self.manager.set_status(f"Clipboard error: {e}")

    def paste(self):
        """Paste from OS clipboard (delegated to the manager)."""
        self.manager.paste_items()

    def edit_selected(self):
        """Edit the selected watchlist item."""
        self.manager.open_edit(self._get_selected_type_id())

    def remove_selected(self):
        """Remove selected items from the watchlist entirely."""
        self.manager.remove_type_ids(self._selected_type_ids())

    def show_price_history(self):
        """Show price history graph for the selected item."""
        self.manager.show_price_history(self._get_selected_type_id())

    def add_to_stock_market(self):
        """Add the selected item to the stock market portfolio."""
        self.manager.add_to_stock_market(self._get_selected_type_id())


class WatchlistTabManager:
    """Manages the persistent watchlist tab: data, persistence, dialogs, and
    the set of `WatchlistTreePanel`s (one per sub-tab)."""

    def __init__(self, notebook: ttk.Notebook, get_client: Callable = None, set_status: Callable = None):
        self.notebook = notebook
        self.get_client = get_client  # Function to get ESIClient for searching
        self.set_status = set_status or (lambda x: None)
        self.get_skills = None  # Function to get TradingSkills for fee calculations
        self.region_id = None  # Current hub's region_id for max buy calculator

        # Watchlist data
        self.watchlist: dict[int, WatchlistItem] = {}  # type_id -> WatchlistItem
        self.categories: list[str] = []  # ordered user-defined category names (sub-tab order)
        self._load_watchlist()

        # Transient: type_id -> system name of the cheapest current listing,
        # rebuilt each scan. With Hub Only off this points at wherever the
        # cheapest order actually is. Not persisted.
        self._current_system: dict[int, str] = {}

        # Track alert state for tab coloring
        self.has_alerts = False
        self.tab_index = None  # Will be set after tab is created

        # Clipboard for copy/paste (shared via setter)
        self._clipboard_getter = None
        self._clipboard_setter = None

        # Stock market tab reference (set by gui_main after creation)
        self.stock_market_tab = None

        # Panels: one WatchlistTreePanel per sub-tab in the category sub-notebook
        # (All, Uncategorized, then one per user category), kept in tab order.
        self._panels: list[WatchlistTreePanel] = []
        self._active_panel: Optional[WatchlistTreePanel] = None

        self._create_tab()

    def set_clipboard_functions(self, getter: Callable, setter: Callable):
        """Set clipboard getter/setter for cross-tab copy/paste."""
        self._clipboard_getter = getter
        self._clipboard_setter = setter

    def set_skills_getter(self, getter: Callable):
        """Set function to retrieve current TradingSkills for fee calculations."""
        self.get_skills = getter

    def set_region_id(self, region_id: int):
        """Update region_id when hub changes (for max buy calculator)."""
        self.region_id = region_id

    def _load_watchlist(self):
        """Load watchlist from JSON file."""
        try:
            if os.path.exists(WATCHLIST_FILE):
                with open(WATCHLIST_FILE, "r") as f:
                    data = json.load(f)
                    self.categories = list(data.get("categories", []))
                    for item_data in data.get("items", []):
                        item = WatchlistItem(**item_data)
                        # Clear market data - should only come from fresh scans
                        old_price = item.current_price
                        item.current_price = None
                        item.current_qty = None
                        item.current_margin = None
                        print(f"[Watchlist] Loaded '{item.name}': cleared price {old_price} -> None")
                        self.watchlist[item.type_id] = item
                    # Reconcile: surface any category referenced by an item but
                    # missing from the ordered list (e.g. hand-edited file) so
                    # its tab still appears.
                    for item in self.watchlist.values():
                        for c in item.categories:
                            if c not in self.categories:
                                self.categories.append(c)
        except Exception as e:
            print(f"Error loading watchlist: {e}")

    def _save_watchlist(self):
        """Save watchlist to JSON file."""
        try:
            items = [asdict(item) for item in self.watchlist.values()]
            with open(WATCHLIST_FILE, "w") as f:
                json.dump({"categories": self.categories, "items": items}, f, indent=2)
        except Exception as e:
            print(f"Error saving watchlist: {e}")

    # --- Category management (data layer) -----------------------------------
    # Categories are user-defined tags; an item may carry several. The ordered
    # `self.categories` list drives sub-tab order and lets empty tabs persist.

    def add_category(self, name: str) -> bool:
        """Create a new category. Returns False on empty/duplicate name."""
        name = (name or "").strip()
        if not name or name in self.categories:
            return False
        self.categories.append(name)
        self._save_watchlist()
        return True

    def rename_category(self, old: str, new: str) -> bool:
        """Rename a category, retagging every item that carries it."""
        new = (new or "").strip()
        if old not in self.categories or not new:
            return False
        if new != old and new in self.categories:
            return False  # would collide with an existing category
        self.categories[self.categories.index(old)] = new
        for item in self.watchlist.values():
            if old in item.categories:
                item.categories = [new if c == old else c for c in item.categories]
        self._save_watchlist()
        return True

    def delete_category(self, name: str) -> bool:
        """Delete a category. Items are kept (untagged), never removed."""
        if name not in self.categories:
            return False
        self.categories.remove(name)
        for item in self.watchlist.values():
            if name in item.categories:
                item.categories = [c for c in item.categories if c != name]
        self._save_watchlist()
        return True

    def tag_item(self, type_id: int, category: str) -> None:
        """Add a category tag to an item (no-op if absent/already tagged)."""
        item = self.watchlist.get(type_id)
        if item and category in self.categories and category not in item.categories:
            item.categories.append(category)
            self._save_watchlist()

    def untag_item(self, type_id: int, category: str) -> None:
        """Remove a category tag from an item (no-op if not tagged)."""
        item = self.watchlist.get(type_id)
        if item and category in item.categories:
            item.categories = [c for c in item.categories if c != category]
            self._save_watchlist()

    # --- tab + panels -------------------------------------------------------

    def _create_tab(self):
        """Create the watchlist tab."""
        self.frame = ttk.Frame(self.notebook)
        self.notebook.add(self.frame, text="Watchlist")

        # Store tab index for later color updates
        self.tab_index = self.notebook.index(self.frame)

        # Button bar
        btn_frame = ttk.Frame(self.frame, padding=5)
        btn_frame.pack(fill=tk.X)

        ttk.Button(btn_frame, text="+ Add Item", command=self._show_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="+ Bulk Add", command=self._show_bulk_add_dialog).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Edit", command=self._edit_active).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove", command=self._remove_active).pack(side=tk.LEFT, padx=5)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        ttk.Button(btn_frame, text="Copy", command=self._copy_active).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Paste", command=self.paste_items).pack(side=tk.LEFT, padx=5)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=2)

        ttk.Button(btn_frame, text="+ Category", command=self._on_add_category_button).pack(side=tk.LEFT, padx=5)

        ttk.Label(btn_frame, text="Ctrl+C/V or right-click", foreground="gray").pack(side=tk.RIGHT, padx=10)

        # Category sub-tabs. "All" + "Uncategorized" are always present; one tab
        # per user category follows. Each tab hosts its own WatchlistTreePanel.
        # Right-click a category tab to rename/delete it.
        self.sub_notebook = ttk.Notebook(self.frame)
        self.sub_notebook.pack(fill=tk.BOTH, expand=True)
        self.sub_notebook.bind("<<NotebookTabChanged>>", self._on_subtab_changed)
        self.sub_notebook.bind("<Button-3>", self._on_subtab_right_click)

        self._add_panel_tab("All", None)
        self._add_panel_tab("Uncategorized", UNCATEGORIZED)
        for cat in self.categories:
            self._add_panel_tab(cat, cat)

        self._active_panel = self._panels[0] if self._panels else None

        # Initial display
        self.refresh_all()

    def items_for_panel(self, category) -> list[WatchlistItem]:
        """Items a panel should show.

        None = the 'All' view (every item); UNCATEGORIZED = items with no tags;
        a str = items carrying that category.
        """
        if category is None:
            return list(self.watchlist.values())
        if category is UNCATEGORIZED:
            return [it for it in self.watchlist.values() if not it.categories]
        return [it for it in self.watchlist.values() if category in it.categories]

    def system_name_for(self, type_id: int) -> str:
        """System of the cheapest current listing for an item ('' if none)."""
        return self._current_system.get(type_id, "")

    def refresh_all(self):
        """Repopulate every panel, refresh sub-tab badges, recolor the tab."""
        for panel in self._panels:
            panel.refresh()
        self._update_subtab_titles()
        self._update_tab_color()

    # --- button-bar delegators (act on the active sub-tab) ------------------

    def _edit_active(self):
        if self._active_panel:
            self._active_panel.edit_selected()

    def _remove_active(self):
        if self._active_panel:
            self._active_panel.remove_selected()

    def _copy_active(self):
        if self._active_panel:
            self._active_panel.copy_selected()

    # --- sub-tab management -------------------------------------------------

    def _panel_label(self, category) -> str:
        """Base (badge-free) label for a panel's sub-tab."""
        if category is None:
            return "All"
        if category is UNCATEGORIZED:
            return "Uncategorized"
        return category

    def _add_panel_tab(self, label: str, category) -> "WatchlistTreePanel":
        """Create a sub-tab + its panel, append both, and populate it."""
        frame = ttk.Frame(self.sub_notebook)
        panel = WatchlistTreePanel(frame, self, category=category)
        self._panels.append(panel)            # append before .add so the
        self.sub_notebook.add(frame, text=label)  # TabChanged event finds it
        panel.refresh()
        self._update_subtab_titles()
        return panel

    def _select_category(self, category) -> None:
        """Select the sub-tab for `category` (fallback: the first/All tab)."""
        for panel in self._panels:
            if panel.category is category or panel.category == category:
                self.sub_notebook.select(panel.parent)
                self._active_panel = panel
                return
        if self._panels:
            self.sub_notebook.select(self._panels[0].parent)
            self._active_panel = self._panels[0]

    def _panel_at_index(self, idx: int) -> Optional["WatchlistTreePanel"]:
        if 0 <= idx < len(self._panels):
            return self._panels[idx]
        return None

    def _on_subtab_changed(self, event=None) -> None:
        """Track the active panel so the button bar targets the visible tab."""
        try:
            idx = self.sub_notebook.index(self.sub_notebook.select())
        except tk.TclError:
            return
        panel = self._panel_at_index(idx)
        if panel is not None:
            self._active_panel = panel

    def _on_subtab_right_click(self, event) -> None:
        """Right-click a category tab to rename/delete it."""
        try:
            idx = self.sub_notebook.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        panel = self._panel_at_index(idx)
        if panel is None or panel.category is None or panel.category is UNCATEGORIZED:
            return  # All / Uncategorized aren't editable
        category = panel.category
        menu = tk.Menu(self.frame, tearoff=0)
        menu.add_command(label=f"Rename '{category}'…",
                         command=lambda c=category: self._rename_category_interactive(c))
        menu.add_command(label=f"Delete '{category}'…",
                         command=lambda c=category: self._delete_category_interactive(c))
        menu.tk_popup(event.x_root, event.y_root)

    def _create_category_via_prompt(self) -> Optional[str]:
        """Prompt for a category name; create it (+ its tab) if new.

        Returns the canonical name (existing or newly created), or None if the
        prompt was cancelled / blank.
        """
        from tkinter import simpledialog
        name = simpledialog.askstring("New Category", "Category name:", parent=self.frame)
        if name is None:
            return None
        name = name.strip()
        if not name:
            return None
        if name in self.categories:
            return name  # already exists; its tab is already present
        self.add_category(name)
        self._add_panel_tab(name, name)
        return name

    def _on_add_category_button(self) -> None:
        """'+ Category' button: create a category and switch to its tab."""
        name = self._create_category_via_prompt()
        if name:
            self._select_category(name)

    def _rename_category_interactive(self, category: str) -> None:
        from tkinter import simpledialog, messagebox
        new = simpledialog.askstring("Rename Category", "New name:",
                                     initialvalue=category, parent=self.frame)
        if new is None:
            return
        new = new.strip()
        if not new or new == category:
            return
        if not self.rename_category(category, new):
            messagebox.showinfo("Rename Category",
                                f"Could not rename to '{new}' (blank or already exists).",
                                parent=self.frame)
            return
        for panel in self._panels:
            if panel.category == category:
                panel.category = new
                self.sub_notebook.tab(panel.parent, text=new)
                break
        self.refresh_all()

    def _delete_category_interactive(self, category: str) -> None:
        from tkinter import messagebox
        if not messagebox.askyesno(
            "Delete Category",
            f"Delete category '{category}'?\n\nItems stay in the watchlist; they "
            f"just lose this tag (and fall back to Uncategorized if untagged).",
            parent=self.frame,
        ):
            return
        self.delete_category(category)  # data layer untags items
        self._remove_panel_tab(category)
        self.refresh_all()

    def _remove_panel_tab(self, category) -> None:
        """Forget + destroy the sub-tab for `category`; select All."""
        for panel in list(self._panels):
            if panel.category == category:
                self.sub_notebook.forget(panel.parent)
                panel.parent.destroy()
                self._panels.remove(panel)
                break
        self._select_category(None)

    def _update_subtab_titles(self) -> None:
        """Badge each sub-tab with its own alert count (the secondary flag)."""
        if not getattr(self, "sub_notebook", None):
            return
        for panel in self._panels:
            alert_n = sum(1 for it in self.items_for_panel(panel.category) if self._is_alerting(it))
            base = self._panel_label(panel.category)
            text = f"{base} ({alert_n}!)" if alert_n else base
            try:
                self.sub_notebook.tab(panel.parent, text=text)
            except tk.TclError:
                pass

    def _default_categories_for_new(self) -> list[str]:
        """New items land in the active tab (Uncategorized if All/Uncategorized)."""
        cat = self._active_panel.category if self._active_panel else None
        return [cat] if isinstance(cat, str) else []

    # --- category tagging (driven by the right-click cascade) ---------------

    def toggle_category(self, type_ids: list[int], category: str) -> None:
        """Toggle a category on the selected items: if every selected item
        already carries it, remove it from all; otherwise add it to all."""
        if not type_ids or category not in self.categories:
            return
        items = [self.watchlist[t] for t in type_ids if t in self.watchlist]
        if not items:
            return
        all_tagged = all(category in it.categories for it in items)
        for it in items:
            if all_tagged:
                it.categories = [c for c in it.categories if c != category]
            elif category not in it.categories:
                it.categories.append(category)
        self._save_watchlist()
        self.refresh_all()

    def untag_items(self, type_ids: list[int], category: str) -> None:
        """Remove a category tag from the selected items."""
        changed = False
        for t in type_ids:
            it = self.watchlist.get(t)
            if it and category in it.categories:
                it.categories = [c for c in it.categories if c != category]
                changed = True
        if changed:
            self._save_watchlist()
            self.refresh_all()

    def new_category_and_tag(self, type_ids: list[int]) -> None:
        """'New category…' from the cascade: create it, then tag the selection."""
        name = self._create_category_via_prompt()
        if not name:
            return
        for t in type_ids:
            it = self.watchlist.get(t)
            if it and name not in it.categories:
                it.categories.append(name)
        self._save_watchlist()
        self.refresh_all()

    # --- clipboard / paste --------------------------------------------------

    def paste_items(self):
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
            # Treat as plain list of names - delegate to BulkAddDialog
            BulkAddDialog(self.frame, self._on_bulk_add, self.get_client, prefill_text=text)

    def _paste_from_tsv(self, lines):
        """Parse our internal TSV format and restore items with full data."""
        if len(lines) < 3:
            self.set_status("Clipboard format invalid")
            return

        added = 0
        skipped = 0
        # lines[0] = magic header, lines[1] = column header, lines[2:] = data
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

            if type_id in self.watchlist:
                skipped += 1
                continue

            name = parts[1]
            price_under = self._parse_optional_float(parts[2] if len(parts) > 2 else "")
            price_over = self._parse_optional_float(parts[3] if len(parts) > 3 else "")
            margin_over = self._parse_optional_float(parts[4] if len(parts) > 4 else "")
            notes = parts[5] if len(parts) > 5 else ""

            self.watchlist[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=price_under,
                price_over=price_over,
                margin_over=margin_over,
                notes=notes,
                categories=self._default_categories_for_new(),
            )
            added += 1

        if added > 0:
            self._save_watchlist()
            self.refresh_all()
            self.set_status(f"Pasted {added} item(s)" + (f" ({skipped} already in list)" if skipped else ""))
        else:
            self.set_status("All items already in watchlist")

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

    # --- selection-driven actions (called by panels with explicit ids) ------

    def show_price_history(self, type_id: Optional[int]):
        """Show price history graph for the given watchlist item."""
        if type_id is None or type_id not in self.watchlist:
            return

        item = self.watchlist[type_id]

        region_id = self.region_id
        if not region_id:
            from core.config import get_hub_config, DEFAULT_HUB
            region_id = get_hub_config(DEFAULT_HUB)["region_id"]

        from analytics.graphing import show_price_graph
        show_price_graph(
            parent=self.frame,
            type_id=type_id,
            type_name=item.name,
            region_id=region_id,
            profiles=None,
        )

    def add_to_stock_market(self, type_id: Optional[int]):
        """Add the given item to stock market portfolio."""
        if type_id is None:
            return

        watchlist_item = self.watchlist.get(type_id)
        if not watchlist_item:
            return

        if self.stock_market_tab:
            from core.config import get_hub_config, DEFAULT_HUB
            hub_config = get_hub_config(DEFAULT_HUB)

            self.stock_market_tab.add_item_from_external(
                type_id=watchlist_item.type_id,
                region_id=self.region_id or hub_config["region_id"],
                station_id=hub_config["station_id"],
                type_name=watchlist_item.name
            )

    def open_edit(self, type_id: Optional[int]):
        """Open the edit dialog for the given watchlist item."""
        if type_id and type_id in self.watchlist:
            item = self.watchlist[type_id]
            EditItemDialog(self.frame, item, self._on_edit_item)

    def remove_type_ids(self, type_ids: list[int]):
        """Remove the given items from the watchlist entirely."""
        if not type_ids:
            return

        removed = 0
        for type_id in type_ids:
            if type_id in self.watchlist:
                del self.watchlist[type_id]
                removed += 1

        if removed > 0:
            self._save_watchlist()
            self.refresh_all()
            self.set_status(f"Removed {removed} item(s) from watchlist")

    # --- alert tab coloring -------------------------------------------------

    def _update_tab_color(self):
        """Update tab appearance based on alert status."""
        alert_items = self.get_alert_items()
        alert_count = len(alert_items)

        if alert_count > 0:
            self.has_alerts = True
            # Update tab text with alert count and use green styling
            self.notebook.tab(self.tab_index, text=f"Watchlist ({alert_count}!)")
        else:
            self.has_alerts = False
            item_count = len(self.watchlist)
            if item_count > 0:
                self.notebook.tab(self.tab_index, text=f"Watchlist ({item_count})")
            else:
                self.notebook.tab(self.tab_index, text="Watchlist")

    # --- add / edit dialogs -------------------------------------------------

    def _show_add_dialog(self):
        """Show dialog to add new item to watchlist."""
        AddItemDialog(self.frame, self._on_add_item, self.get_client, get_skills=self.get_skills, region_id=self.region_id)

    def _show_bulk_add_dialog(self):
        """Show dialog to bulk add items from pasted text."""
        BulkAddDialog(self.frame, self._on_bulk_add, self.get_client)

    def _on_add_item(self, type_id: int, name: str, conditions: dict):
        """Callback when item is added from dialog."""
        if type_id in self.watchlist:
            # Update existing
            item = self.watchlist[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.margin_over = conditions.get("margin_over")
            item.notes = conditions.get("notes", "")
        else:
            # Create new (lands in the active sub-tab's category, if any)
            self.watchlist[type_id] = WatchlistItem(
                type_id=type_id,
                name=name,
                price_under=conditions.get("price_under"),
                price_over=conditions.get("price_over"),
                margin_over=conditions.get("margin_over"),
                notes=conditions.get("notes", ""),
                categories=self._default_categories_for_new(),
            )

        self._save_watchlist()
        self.refresh_all()
        self.set_status(f"Added to watchlist: {name}")

    def _on_bulk_add(self, items: list[dict]):
        """Callback when items are bulk added."""
        added = 0
        for item_data in items:
            type_id = item_data["type_id"]
            name = item_data["name"]

            if type_id not in self.watchlist:
                self.watchlist[type_id] = WatchlistItem(
                    type_id=type_id,
                    name=name,
                    categories=self._default_categories_for_new(),
                )
                added += 1

        self._save_watchlist()
        self.refresh_all()
        self.set_status(f"Added {added} items to watchlist")

    def _on_edit_item(self, type_id: int, conditions: dict):
        """Callback when item is edited."""
        if type_id in self.watchlist:
            item = self.watchlist[type_id]
            item.price_under = conditions.get("price_under")
            item.price_over = conditions.get("price_over")
            item.margin_over = conditions.get("margin_over")
            item.notes = conditions.get("notes", "")

            self._save_watchlist()
            self.refresh_all()

    def add_from_deal(self, type_id: int, name: str, current_price: float = None):
        """Add item to watchlist from deals context menu (pre-populated)."""
        # If already in watchlist, just edit it
        if type_id in self.watchlist:
            item = self.watchlist[type_id]
            EditItemDialog(self.frame, item, self._on_edit_item)
        else:
            # Create with pre-filled data, open edit dialog
            temp_item = WatchlistItem(
                type_id=type_id,
                name=name,
                current_price=current_price
            )
            AddItemDialog(self.frame, self._on_add_item, self.get_client, prefill=temp_item, get_skills=self.get_skills, region_id=self.region_id)

    # --- scan update --------------------------------------------------------

    def update_from_local_orders(self, orders: list[dict]):
        """
        Update watchlist prices from local hub market orders.
        Called after each scan with the raw order data.
        """
        import time as _pt
        _pt0 = _pt.perf_counter()
        if not self.watchlist:
            return

        # Build lookup: type_id -> (lowest sell price, quantity at that price)
        _ts = _pt.perf_counter()
        sell_data = {}  # type_id -> {"price": float, "qty": int, "system_id": int}
        for order in orders:
            if order.get("is_buy_order"):
                continue  # Skip buy orders

            type_id = order["type_id"]
            price = order["price"]
            volume = order.get("volume_remain", 0)

            if type_id not in sell_data or price < sell_data[type_id]["price"]:
                # New lowest price - reset quantity, remember its system
                sell_data[type_id] = {"price": price, "qty": volume,
                                      "system_id": order.get("system_id")}
            elif price == sell_data[type_id]["price"]:
                # Same price - accumulate quantity
                sell_data[type_id]["qty"] += volume
        _step_scan = _pt.perf_counter() - _ts

        # Update watchlist items. Also resolve the cheapest listing's system
        # name (the scan just populated client.system_cache for these systems).
        _ts = _pt.perf_counter()
        client = self.get_client() if self.get_client else None
        sys_cache = getattr(client, "system_cache", {}) if client is not None else {}
        self._current_system = {}
        updated = 0
        for type_id, item in self.watchlist.items():
            if type_id in sell_data:
                item.current_price = sell_data[type_id]["price"]
                item.current_qty = sell_data[type_id]["qty"]
                sid = sell_data[type_id].get("system_id")
                if sid:
                    name = sys_cache.get(sid, {}).get("name") or ""
                    if name:
                        self._current_system[type_id] = name
                updated += 1
            else:
                # No sell orders for this item - clear stale data
                # Use qty=0 to distinguish "no listings" from "never scanned"
                item.current_price = None
                item.current_qty = 0
        _step_apply = _pt.perf_counter() - _ts

        # Refresh display to show new prices and trigger alerts
        _ts = _pt.perf_counter()
        self.refresh_all()
        _step_refresh = _pt.perf_counter() - _ts
        _pt_total = _pt.perf_counter() - _pt0
        print(
            f"[PerfTimer] WatchlistTabManager.update_from_local_orders "
            f"total={_pt_total*1000:.0f}ms orders={len(orders)} watchlist_items={len(self.watchlist)} "
            f"scan_orders={_step_scan*1000:.0f}ms apply={_step_apply*1000:.0f}ms refresh_display={_step_refresh*1000:.0f}ms"
        )

        # Check for alerts
        alerts = self.get_alert_items()
        if alerts:
            self.set_status(f"Watchlist: {len(alerts)} alert(s)!")

    @staticmethod
    def _is_alerting(item: WatchlistItem) -> bool:
        """True if any of the item's alert conditions are currently met."""
        if not item.current_price:
            return False
        if item.price_under and item.current_price <= item.price_under:
            return True
        if item.price_over and item.current_price >= item.price_over:
            return True
        if item.margin_over and item.current_margin and item.current_margin >= item.margin_over:
            return True
        return False

    def get_alert_items(self) -> list[WatchlistItem]:
        """Get items that have triggered alerts."""
        return [item for item in self.watchlist.values() if self._is_alerting(item)]
