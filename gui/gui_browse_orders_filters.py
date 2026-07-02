"""Filter mixin + popup dialogs for the Browse Orders dialog.

Chip-based, EVE-style hierarchical filtering: pick a Category, then optionally
refine which Groups within that category are shown. Multiple chips compose with
OR semantics across both the Current Orders and History (observed) tabs.

The mixin attaches to `BrowseStructureOrdersDialog` and owns:
  * Filter state (chips, taxonomy cache, available categories/groups).
  * The filter row UI (chip container + Add Filter / Clear All buttons).
  * Re-rendering both treeviews when the filter changes.

The host class is expected to provide these attributes/methods:
  * tree, items_tree, trail_tree         (ttk.Treeview widgets)
  * trail_summary_var                    (tk.StringVar)
  * _type_names                          (dict[int, str])
  * _load_trail_for(type_id), _fmt_issued(raw)
"""

import tkinter as tk
from tkinter import ttk
from typing import Iterable
from gui.gui_window_utils import fit_window, make_scrollable


class BrowseOrdersFilterMixin:
    """Adds chip-based Category/Group filtering to BrowseStructureOrdersDialog."""

    def _init_filter_state(self):
        """Initialize filter state. Call from host __init__ before _build()."""
        self._current_orders: list = []
        self._all_items: list = []
        self._type_taxonomy: dict[int, tuple] = {}
        self._available_categories: dict[int, str] = {}
        self._groups_by_category: dict[int, dict[int, str]] = {}
        self._filter_chips: list[dict] = []
        from sde.sde_manager import get_sde_manager
        self._sde = get_sde_manager()

    def _build_filter_row(self, parent):
        """Build the chip row above the notebook."""
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, padx=10, pady=(0, 4))

        ttk.Label(row, text="Filters:", font=("Segoe UI", 9, "bold")).pack(
            side=tk.LEFT, padx=(0, 6)
        )

        self._add_filter_btn = ttk.Button(
            row, text="+ Add Category", command=self._open_add_filter
        )
        self._add_filter_btn.pack(side=tk.LEFT)

        self._clear_filters_btn = ttk.Button(
            row, text="Clear All", command=self._clear_all_chips
        )
        self._clear_filters_btn.pack(side=tk.LEFT, padx=(4, 8))

        # Container chips pack into. Uses LEFT pack so chips flow horizontally.
        self._chips_container = ttk.Frame(row)
        self._chips_container.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self._filter_hint_var = tk.StringVar(value="")
        ttk.Label(
            row, textvariable=self._filter_hint_var,
            font=("Segoe UI", 8), foreground="gray",
        ).pack(side=tk.RIGHT, padx=(8, 0))

    # ---------------------------------------------------------------- data in

    def _ensure_taxonomy_for(self, type_ids: Iterable[int]):
        """Resolve and cache market-group ancestry for any new type_ids.

        Each type gets mapped to (depth_2_market_group_id, depth_1_market_group_id)
        by walking the market-group parent chain. Stored in _type_taxonomy as
        (refinement_id, category_id) so existing filter / chip code shapes work
        unchanged — only the source taxonomy has changed.

        `_available_categories[depth_1_id] = depth_1_name` and
        `_groups_by_category[depth_1_id][depth_2_id] = depth_2_name`. Both grow
        as data arrives — only top-/second-level groups whose items appear in
        current data show up.
        """
        if not self._sde or not self._sde.has_market_group_data():
            return
        missing = [t for t in type_ids if t not in self._type_taxonomy]
        if not missing:
            return

        type_infos = self._sde.get_type_info_bulk(missing)

        for tid, info in type_infos.items():
            ancestry = self._sde.get_market_group_ancestry(info.market_group_id)
            depth_1 = ancestry[0] if len(ancestry) >= 1 else None
            depth_2 = ancestry[1] if len(ancestry) >= 2 else None
            self._type_taxonomy[tid] = (depth_2, depth_1)

            if depth_1 is not None:
                self._available_categories.setdefault(
                    depth_1, self._sde.get_market_group_name(depth_1) or f"#{depth_1}"
                )
            if depth_1 is not None and depth_2 is not None:
                self._groups_by_category.setdefault(depth_1, {})[depth_2] = (
                    self._sde.get_market_group_name(depth_2) or f"#{depth_2}"
                )

    def set_current_orders(self, orders):
        """Host calls this after a fetch to feed orders into the filter."""
        self._current_orders = list(orders)
        self._ensure_taxonomy_for(o["type_id"] for o in orders)
        self._refresh_filter_chip_totals()

    def set_history_items(self, items):
        """Host calls this after StructureHistoryDB query."""
        self._all_items = list(items)
        self._ensure_taxonomy_for(it["type_id"] for it in items)
        self._refresh_filter_chip_totals()

    # ----------------------------------------------------------------- filter

    def _filter_passes(self, type_id) -> bool:
        if not self._filter_chips:
            return True
        gid, cid = self._type_taxonomy.get(type_id, (None, None))
        for chip in self._filter_chips:
            if chip["category_id"] != cid:
                continue
            if not chip["group_ids"]:
                return True
            if gid in chip["group_ids"]:
                return True
        return False

    def _apply_filters_and_render(self):
        """Re-render both tables through the current filter."""
        if self._filter_chips:
            visible_orders = [
                o for o in self._current_orders
                if self._filter_passes(o["type_id"])
            ]
            visible_items = [
                it for it in self._all_items
                if self._filter_passes(it["type_id"])
            ]
        else:
            visible_orders = self._current_orders
            visible_items = self._all_items

        self._render_current_orders_view(visible_orders)
        self._render_history_items_view(visible_items)

        self._update_filter_hint(
            len(visible_orders), len(self._current_orders),
            len(visible_items), len(self._all_items),
        )

    def _update_filter_hint(self, shown_orders, total_orders,
                            shown_items, total_items):
        if not self._sde or not self._sde.has_market_group_data():
            self._filter_hint_var.set(
                'Download SDE on Stock Market tab to enable filters'
            )
            self._add_filter_btn.configure(state=tk.DISABLED)
            return

        self._add_filter_btn.configure(state=tk.NORMAL)
        if not self._filter_chips:
            self._filter_hint_var.set(
                f"Showing all {total_orders:,} orders, {total_items:,} items"
            )
        else:
            self._filter_hint_var.set(
                f"Orders {shown_orders:,}/{total_orders:,} · "
                f"History {shown_items:,}/{total_items:,}"
            )

    # ---------------------------------------------------------------- render

    def _render_current_orders_view(self, visible_orders):
        for row in self.tree.get_children():
            self.tree.delete(row)

        sells = sorted(
            (o for o in visible_orders if not o.get("is_buy_order")),
            key=lambda o: (o.get("type_id", 0), o.get("price", 0.0)),
        )
        buys = sorted(
            (o for o in visible_orders if o.get("is_buy_order")),
            key=lambda o: (o.get("type_id", 0), -o.get("price", 0.0)),
        )

        for o in sells + buys:
            tid = o["type_id"]
            side = "Buy" if o.get("is_buy_order") else "Sell"
            tag = "buy" if o.get("is_buy_order") else "sell"
            self.tree.insert(
                "", tk.END,
                values=(
                    side,
                    self._type_names.get(tid, f"(type {tid})"),
                    tid,
                    f"{o.get('price', 0.0):,.2f}",
                    f"{o.get('volume_remain', 0):,}",
                    self._fmt_issued(o.get("issued")),
                ),
                tags=(tag,),
            )

    def _render_history_items_view(self, visible_items):
        for row in self.items_tree.get_children():
            self.items_tree.delete(row)

        for it in visible_items:
            tid = it["type_id"]
            avg = it["avg_price"]
            pmin = it["price_min"]
            pmax = it["price_max"]
            range_str = (
                f"{pmin:,.2f} – {pmax:,.2f}"
                if (pmin is not None and pmax is not None) else ""
            )
            self.items_tree.insert(
                "", tk.END, iid=str(tid),
                values=(
                    tid,
                    self._type_names.get(tid, f"(type {tid})"),
                    it["sales_count"],
                    f"{it['volume_sold']:,}" if it["volume_sold"] else "",
                    f"{avg:,.2f}" if avg is not None else "",
                    range_str,
                    it["days_with_fills"],
                ),
            )

        # If selection is now stale, clear the trail pane.
        sel = self.items_tree.selection()
        if not sel:
            self.trail_summary_var.set("Select an item above.")
            for row in self.trail_tree.get_children():
                self.trail_tree.delete(row)
        else:
            self._load_trail_for(int(sel[0]))

    # ------------------------------------------------------------ chip mgmt

    def _render_filter_chips(self):
        for w in self._chips_container.winfo_children():
            w.destroy()

        for i, chip in enumerate(self._filter_chips):
            cf = ttk.Frame(self._chips_container, relief=tk.SOLID,
                           borderwidth=1, padding=(4, 1))
            cf.pack(side=tk.LEFT, padx=2)

            n = len(chip["group_ids"])
            total = chip["total_groups"]
            refinement = "all" if n == 0 else f"{n}/{total}"
            label_text = f"{chip['category_name']} ({refinement})"

            lbl = ttk.Label(cf, text=label_text, cursor="hand2",
                            foreground="#1a4a7a")
            lbl.pack(side=tk.LEFT)
            lbl.bind("<Button-1>", lambda _e, idx=i: self._edit_chip(idx))

            ttk.Button(cf, text="✕", width=2,
                       command=lambda idx=i: self._remove_chip(idx)).pack(
                side=tk.LEFT, padx=(4, 0)
            )

    def _refresh_filter_chip_totals(self):
        """After taxonomy data extends, update total_groups counts on chips."""
        for chip in self._filter_chips:
            cid = chip["category_id"]
            chip["total_groups"] = len(self._groups_by_category.get(cid, {}))
        if self._filter_chips:
            self._render_filter_chips()

    def _open_add_filter(self):
        if not self._sde or not self._sde.has_market_group_data():
            return

        excluded = {c["category_id"] for c in self._filter_chips}
        available = sorted(
            ((cid, name) for cid, name in self._available_categories.items()
             if cid not in excluded),
            key=lambda x: x[1].lower(),
        )
        if not available:
            return

        def on_category_picked(category_id, category_name):
            groups_dict = self._groups_by_category.get(category_id, {})
            groups_list = sorted(groups_dict.items(), key=lambda x: x[1].lower())

            def on_groups_saved(selected_group_ids):
                self._filter_chips.append({
                    "category_id": category_id,
                    "category_name": category_name,
                    "group_ids": set(selected_group_ids),
                    "total_groups": len(groups_dict),
                })
                self._render_filter_chips()
                self._apply_filters_and_render()

            _GroupRefinementDialog(
                self, category_name, groups_list, set(), on_groups_saved
            )

        _CategoryPickerDialog(self, available, on_category_picked)

    def _edit_chip(self, idx):
        if idx >= len(self._filter_chips):
            return
        chip = self._filter_chips[idx]
        groups_dict = self._groups_by_category.get(chip["category_id"], {})
        groups_list = sorted(groups_dict.items(), key=lambda x: x[1].lower())

        def on_groups_saved(selected_group_ids):
            chip["group_ids"] = set(selected_group_ids)
            chip["total_groups"] = len(groups_dict)
            self._render_filter_chips()
            self._apply_filters_and_render()

        _GroupRefinementDialog(
            self, chip["category_name"], groups_list,
            set(chip["group_ids"]), on_groups_saved,
        )

    def _remove_chip(self, idx):
        if idx >= len(self._filter_chips):
            return
        del self._filter_chips[idx]
        self._render_filter_chips()
        self._apply_filters_and_render()

    def _clear_all_chips(self):
        if not self._filter_chips:
            return
        self._filter_chips = []
        self._render_filter_chips()
        self._apply_filters_and_render()


# =====================================================================
# Popup dialogs
# =====================================================================


class _CategoryPickerDialog(tk.Toplevel):
    """Modal: pick one category to add as a filter chip.

    On OK, calls on_pick(category_id, category_name).
    """

    def __init__(self, parent, categories: list, on_pick):
        super().__init__(parent)
        self.title("Pick a Category")
        self.transient(parent)
        self.grab_set()

        self._categories = categories
        self._on_pick = on_pick

        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Next →", command=self._on_ok).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        ttk.Label(inner, text="Pick a category to filter by:").pack(
            anchor=tk.W, padx=10, pady=(10, 4)
        )

        list_frame = ttk.Frame(inner)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        self.listbox = tk.Listbox(
            list_frame, selectmode=tk.SINGLE, exportselection=False
        )
        for _cid, name in categories:
            self.listbox.insert(tk.END, name)
        vsb = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.listbox.yview
        )
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._on_ok())

        fit_window(self, min_width=300)

    def _on_ok(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        cid, name = self._categories[sel[0]]
        self.destroy()
        self._on_pick(cid, name)


class _GroupRefinementDialog(tk.Toplevel):
    """Modal: select which groups within a category to include.

    Empty selection = include the whole category.
    On Save, calls on_save(set_of_group_ids).
    """

    def __init__(self, parent, category_name: str, groups: list,
                 initial_ids: set, on_save):
        super().__init__(parent)
        self.title(f"Refine — {category_name}")
        self.transient(parent)
        self.grab_set()

        self._groups = groups
        self._on_save = on_save

        # Buttons pinned to window bottom (outside scroll area)
        btn_frame = ttk.Frame(self)
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=10)
        ttk.Button(btn_frame, text="Save", command=self._on_ok).pack(side=tk.RIGHT, padx=5)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Separator(self, orient=tk.HORIZONTAL).pack(side=tk.BOTTOM, fill=tk.X)

        # Scrollable content area above the buttons.
        inner = make_scrollable(self)

        ttk.Label(
            inner,
            text=f'Select groups within “{category_name}”\n'
                 '(leave all unchecked to include the entire category):',
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=10, pady=(10, 4))

        list_frame = ttk.Frame(inner)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        self.listbox = tk.Listbox(
            list_frame, selectmode=tk.MULTIPLE, exportselection=False
        )
        for _gid, name in groups:
            self.listbox.insert(tk.END, name)
        for i, (gid, _) in enumerate(groups):
            if gid in initial_ids:
                self.listbox.selection_set(i)
        vsb = ttk.Scrollbar(
            list_frame, orient=tk.VERTICAL, command=self.listbox.yview
        )
        self.listbox.configure(yscrollcommand=vsb.set)
        self.listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        toggle_frame = ttk.Frame(inner)
        toggle_frame.pack(fill=tk.X, padx=10, pady=(0, 4))
        ttk.Button(
            toggle_frame, text="Select All",
            command=lambda: self.listbox.selection_set(0, tk.END),
        ).pack(side=tk.LEFT)
        ttk.Button(
            toggle_frame, text="Clear",
            command=lambda: self.listbox.selection_clear(0, tk.END),
        ).pack(side=tk.LEFT, padx=4)

        fit_window(self, min_width=340)

    def _on_ok(self):
        selected = {
            self._groups[i][0] for i in self.listbox.curselection()
        }
        self.destroy()
        self._on_save(selected)
