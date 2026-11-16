"""Industry page layout, product browser, filters and list interactions.

IndustryBrowserMixin uses the shared manager state; it owns no separate cache
or calculation lifecycle. This keeps the existing callbacks and filter behavior.
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional

from core.config import get_enabled_hubs
from gui.gui_window_utils import make_scrollable
from gui.industry.gui_industry_characters import CharactersPanel
from industry.industry_diagnostics import format_counts
from industry.industry_ignore import is_auto_hidden
from gui.industry.gui_industry_details import (
    CLR_GOOD, CLR_OK, CLR_BAD, CLR_MUTED, isk_m, show_ignore_manager, _print,
)

# Category chips -> substrings matched against an item's market-group ancestry.
CATEGORIES = [
    ("All", None),
    ("Ships", ("ships",)),
    ("Modules", ("ship equipment",)),
    ("Ammo", ("ammunition & charges",)),
    ("Components", ("components",)),
]


# Tech-level filter buckets. meta_group_id → bucket (catch-all to T1 so nothing
# vanishes for unlisted/storyline/special-edition metas). 2=Tech II, 14=Tech III,
# 4=Faction; everything else (1, None, 3, 5, 6, 17, 19, …) is T1. Needs the
# SDE meta_group_id column (re-download); without it everything reads as T1.
TECH_ORDER = ["T1", "Faction", "T2", "T3"]
DEFAULT_TECH = {"T1", "Faction"}  # T2/T3 cost modeling not built yet


def tech_label(meta_group_id: Optional[int]) -> str:
    if meta_group_id == 2:
        return "T2"
    if meta_group_id == 14:
        return "T3"
    if meta_group_id == 4:
        return "Faction"
    return "T1"


class IndustryBrowserMixin:
    """Layout, filtering and list callbacks for IndustryTabManager."""

    # ===================================================================== build

    def _build_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Industry")
        self.frame = outer

        # Industry-level sub-notebook: Top Profit T1 (here) + Characters
        # (Phase 2). Phases 5-7 add T2/T3/Extra; Phase 3 adds Owned BPO/BPC.
        self.sub_nb = ttk.Notebook(outer)
        self.sub_nb.pack(fill=tk.BOTH, expand=True)
        t1 = ttk.Frame(self.sub_nb)
        self.sub_nb.add(t1, text="Top Profit — T1")

        hub_names = [name for _key, name in get_enabled_hubs()]
        self._hub_key_by_name = {name: key for key, name in get_enabled_hubs()}

        # ---- top bar: refresh + hub/facility/input selectors ----
        top = ttk.Frame(t1, padding=(8, 6))
        top.pack(fill=tk.X)

        self.refresh_btn = ttk.Button(top, text="Refresh from caches",
                                      command=lambda: self._compute(refetch=True))
        self.refresh_btn.pack(side=tk.LEFT)
        self.sde_btn = ttk.Button(top, text="Update SDE",
                                  command=self._on_update_sde)
        self.sde_btn.pack(side=tk.LEFT, padx=(4, 0))
        self.progress_label = ttk.Label(top, text="", foreground=CLR_MUTED)
        self.progress_label.pack(side=tk.LEFT, padx=10)

        ttk.Label(top, text="Buy at:").pack(side=tk.LEFT, padx=(12, 2))
        self.buy_var = tk.StringVar(value="Jita")
        self.buy_combo = self._combo(top, self.buy_var, hub_names,
                                     self._on_input_change)

        ttk.Label(top, text="Sell at:").pack(side=tk.LEFT, padx=(12, 2))
        self.sell_var = tk.StringVar(value="Jita")
        self.sell_combo = self._combo(top, self.sell_var, hub_names,
                                      self._on_input_change)

        ttk.Label(top, text="Input:").pack(side=tk.LEFT, padx=(12, 2))
        self.input_var = tk.StringVar(value="Patient (buy orders)")
        self._combo(top, self.input_var,
                    ["Patient (buy orders)", "Impatient (sell orders)"],
                    self._on_input_change, width=20)

        self.status_label = ttk.Label(
            top, text=f"Data: {self.market.get_last_update()}",
            foreground=CLR_MUTED)
        self.status_label.pack(side=tk.RIGHT)

        # ---- facility row (SCI source + write-in structure bonuses) ----
        fac = ttk.LabelFrame(t1, text="Facility", padding=(8, 4))
        fac.pack(fill=tk.X, padx=8)
        ttk.Label(fac, text="System (cost index):").pack(side=tk.LEFT)
        self.facility_var = tk.StringVar(value="Jita")
        self.facility_combo = self._combo(fac, self.facility_var, hub_names,
                                          self._on_input_change)
        self.fac_tax_var = self._mini_field(fac, "Facility tax %:",
                                            f"{self.constants.facility_tax_pct}")
        self.cost_bonus_var = self._mini_field(fac, "Cost bonus %:", "0")
        self.mat_bonus_var = self._mini_field(fac, "Material bonus %:", "0")
        # Structure/rig TIME reduction — feeds Phase 4 build + research time only
        # (never cost). Default 0 (generic NPC station, no time bonus).
        self.time_bonus_var = self._mini_field(fac, "Time bonus %:", "0")
        self.scc_var = self._mini_field(fac, "SCC %:",
                                        f"{self.constants.scc_surcharge_pct}")
        ttk.Label(fac, text="(SCC/tax are CCP-tuned — verify in-game)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT, padx=8)

        # ---- filter row: chips + search + min profit + ME/batch ----
        filt = ttk.Frame(t1, padding=(8, 4))
        filt.pack(fill=tk.X)
        self._chip_buttons = {}
        for label, _match in CATEGORIES:
            b = ttk.Button(filt, text=label, width=11,
                           command=lambda l=label: self._on_chip(l))
            b.pack(side=tk.LEFT, padx=1)
            self._chip_buttons[label] = b
        self._highlight_chip("All")

        ttk.Label(filt, text="Search:").pack(side=tk.LEFT, padx=(12, 2))
        self.search_var = tk.StringVar()
        se = ttk.Entry(filt, textvariable=self.search_var, width=16)
        se.pack(side=tk.LEFT)
        se.bind("<KeyRelease>", lambda e: self._rebuild_list())

        ttk.Label(filt, text="Min profit:").pack(side=tk.LEFT, padx=(12, 2))
        # Blank = no lower bound (negatives shown for contrast). Type a number
        # to floor the list; the "Positive only" sub-tab is the >0 view.
        self.min_profit_var = tk.StringVar(value="")
        mp = ttk.Entry(filt, textvariable=self.min_profit_var, width=12)
        mp.pack(side=tk.LEFT)
        mp.bind("<KeyRelease>", lambda e: self._rebuild_list())

        self.show_unpriced_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Show unpriced", variable=self.show_unpriced_var,
                        command=self._rebuild_list).pack(side=tk.LEFT, padx=(12, 0))

        # Capitals are hidden by default — hugely expensive and need months of
        # BPO ME research to be worth building. Upwell + POS structures are
        # reasonable builds, so they stay visible unless the user opts out.
        self.subcap_only_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt, text="Sub-cap only", variable=self.subcap_only_var,
                        command=self._rebuild_list).pack(side=tk.LEFT, padx=(12, 0))
        self.hide_upwell_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Hide Upwell", variable=self.hide_upwell_var,
                        command=self._rebuild_list).pack(side=tk.LEFT, padx=(8, 0))
        self.hide_pos_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Hide POS", variable=self.hide_pos_var,
                        command=self._rebuild_list).pack(side=tk.LEFT, padx=(8, 0))
        self.ignored_btn = ttk.Button(filt, text="Ignored…",
                                      command=self._manage_ignored)
        self.ignored_btn.pack(side=tk.LEFT, padx=(12, 0))
        self._update_ignored_btn()

        ttk.Label(filt, text="ME:").pack(side=tk.LEFT, padx=(12, 2))
        self.me_var = tk.StringVar(value="10")
        me = ttk.Entry(filt, textvariable=self.me_var, width=4)
        me.pack(side=tk.LEFT)
        me.bind("<Return>", lambda e: self._compute(refetch=False))

        # TE only affects build TIME (Phase 4), not cost, so it doesn't trigger a
        # recompute — the detail panel reads it live. Default 20 (researched BPO).
        ttk.Label(filt, text="TE:").pack(side=tk.LEFT, padx=(8, 2))
        self.te_var = tk.StringVar(value="20")
        te = ttk.Entry(filt, textvariable=self.te_var, width=4)
        te.pack(side=tk.LEFT)
        te.bind("<Return>", lambda e: self._refresh_detail_if_selected())

        ttk.Label(filt, text="Batch:").pack(side=tk.LEFT, padx=(8, 2))
        self.batch_var = tk.StringVar(value="1")
        ba = ttk.Entry(filt, textvariable=self.batch_var, width=5)
        ba.pack(side=tk.LEFT)
        ba.bind("<Return>", lambda e: self._compute(refetch=False))
        ttk.Button(filt, text="Apply ME/Batch",
                   command=lambda: self._compute(refetch=False)).pack(
                       side=tk.LEFT, padx=(4, 0))

        # ---- tech-level chips (multi-select; grey out where a category has none) ----
        tech_row = ttk.Frame(t1, padding=(8, 0))
        tech_row.pack(fill=tk.X)
        ttk.Label(tech_row, text="Tech:").pack(side=tk.LEFT)
        self.tech_buttons = {}
        for label in TECH_ORDER:
            b = ttk.Button(tech_row, text=label, width=9,
                           command=lambda l=label: self._on_tech_chip(l))
            b.pack(side=tk.LEFT, padx=1)
            self.tech_buttons[label] = b
        self._refresh_tech_chip_styles()

        # Blueprint-source filter: buyable BPO vs BPC-only (no BPO — drops/
        # invented; build cost ignores blueprint cost until Stage C pricing).
        ttk.Label(tech_row, text="Blueprint:").pack(side=tk.LEFT, padx=(12, 2))
        self.bp_filter_var = tk.StringVar(value="All")
        self._combo(tech_row, self.bp_filter_var,
                    ["All", "BPO only", "BPC-only"],
                    self._rebuild_list, width=10)

        # Persistent note, re-evaluated after an SDE update (no restart needed).
        self.tech_note_label = ttk.Label(tech_row, foreground=CLR_MUTED)
        self.tech_note_label.pack(side=tk.LEFT, padx=8)
        self._update_tech_note()

        # ---- main split: list | detail ----
        main = ttk.Frame(t1, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        self._headings = {"#0": "Item", "cost": "Build cost", "profit": "Profit",
                          "margin": "Margin%", "vol": "Vol/day"}
        # Shared right-click menu (acts on self.selected, tree-agnostic).
        self._list_menu = tk.Menu(self.frame, tearoff=0)
        self._list_menu.add_command(label="View price history",
                                    command=self._show_list_history)
        self._list_menu.add_command(label="Copy name",
                                    command=self._copy_selected_name)
        self._list_menu.add_separator()
        self._list_menu.add_command(label="Ignore this item",
                                    command=self._ignore_selected)
        # Two list views sharing the detail panel: full ranked list (incl.
        # negatives) and a profit>0 view. Both render from the same results.
        self.list_nb = ttk.Notebook(left)
        self.list_nb.pack(fill=tk.BOTH, expand=True)
        self.tree = self._make_list_tree(self.list_nb, "All")
        self.tree_pos = self._make_list_tree(self.list_nb, "Positive only")
        self._trees = (self.tree, self.tree_pos)
        self._update_headings()

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.legend_label = ttk.Label(right, foreground=CLR_MUTED)
        self.legend_label.pack(anchor="w")
        self._update_legend()

        # Scrollable detail panel via the shared make_scrollable helper (same
        # model as every dialog: recursive wheel binding incl. Linux Button-4/5,
        # smart latching for inner scroll-own widgets). The hand-rolled canvas
        # this replaced bound only <MouseWheel>, so it did nothing on Linux.
        scroll_host = ttk.Frame(right)
        scroll_host.pack(fill=tk.BOTH, expand=True, pady=(4, 0))
        self.detail = make_scrollable(scroll_host)
        self._detail_canvas = self.detail.master  # the Canvas it created
        ttk.Label(self.detail, text="Select an item to view its build breakdown",
                  foreground=CLR_MUTED).pack(expand=True)

        # ---- Owned BPO/BPC sub-tab (Phase 3) ----
        # Keep this lazy: Owned imports shared helpers from gui_industry.
        from gui.industry.gui_industry_owned import OwnedBlueprintsPanel
        owned_frame = ttk.Frame(self.sub_nb)
        self.sub_nb.add(owned_frame, text="Owned BPO/BPC")
        self.owned_panel = OwnedBlueprintsPanel(
            owned_frame, self.bp_db, self.sde, self.names, self.roster,
            self.build_calc_context, self.set_status,
            bpc_pricing=self.bpc_pricing, contracts_db=self.contracts_db,
            on_hubs_changed=self._refresh_hub_selectors,
            on_ignore_changed=self._apply_ignore_change,
            build_time_for=self.build_time_for, on_research=self.open_research)

        # ---- Characters sub-tab (Phase 2) ----
        chars_frame = ttk.Frame(self.sub_nb)
        self.sub_nb.add(chars_frame, text="Characters")
        self.characters_panel = CharactersPanel(
            chars_frame, self.roster, self.char_skills, self.char_standings,
            self.set_status, root=self.root,
            bp_puller=self.bp_puller, bp_db=self.bp_db,
            on_blueprints_pulled=self._on_blueprints_pulled)

    def _on_blueprints_pulled(self):
        """Refresh the Owned panel after a Characters-tab blueprint pull."""
        if getattr(self, "owned_panel", None):
            self.owned_panel.refresh()

    def _refresh_hub_selectors(self):
        """Repopulate the buy/sell/facility hub comboboxes after a structure is
        registered as a custom hub (Phase 3.5), so it appears without restart."""
        hubs = get_enabled_hubs()
        hub_names = [name for _k, name in hubs]
        self._hub_key_by_name = {name: key for key, name in hubs}
        for combo in (self.buy_combo, self.sell_combo, self.facility_combo):
            try:
                combo.configure(values=hub_names)
            except tk.TclError:
                pass
        self.set_status("Industry: hub list updated — new structure available")

    def _combo(self, parent, var, values, cb, width=8):
        c = ttk.Combobox(parent, values=values, textvariable=var, width=width,
                         state="readonly")
        c.pack(side=tk.LEFT)
        c.bind("<<ComboboxSelected>>", lambda e: cb())
        return c

    def _mini_field(self, parent, label, default):
        ttk.Label(parent, text=label).pack(side=tk.LEFT, padx=(10, 2))
        var = tk.StringVar(value=default)
        e = ttk.Entry(parent, textvariable=var, width=6)
        e.pack(side=tk.LEFT)
        e.bind("<Return>", lambda ev: self._compute(refetch=False))
        return var

    def _make_list_tree(self, notebook, title):
        """One ranked-list Treeview as a sub-tab. All trees share sort state,
        the detail panel, and the right-click menu (keyed off self.selected)."""
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        cols = ("cost", "profit", "margin", "vol")
        tree = ttk.Treeview(frame, columns=cols, height=24)
        tree.heading("#0", text="Item", command=lambda: self._on_sort("name"))
        tree.column("#0", width=230)
        for c, w in (("cost", 95), ("profit", 110), ("margin", 75), ("vol", 75)):
            tree.heading(c, text=self._headings[c],
                         command=lambda cc=c: self._on_sort(cc))
            tree.column(c, width=w, anchor="e")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        tree.tag_configure("good", foreground=CLR_GOOD)
        tree.tag_configure("ok", foreground=CLR_OK)
        tree.tag_configure("bad", foreground=CLR_BAD)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", lambda e: self._show_list_history())
        tree.bind("<Button-3>", self._show_list_menu)
        return tree

    # ===================================================================== list

    def _on_chip(self, label):
        self._active_category = label
        self._highlight_chip(label)
        self._rebuild_list()

    def _highlight_chip(self, label):
        for l, b in self._chip_buttons.items():
            b.state(["pressed"] if l == label else ["!pressed"])

    def _on_tech_chip(self, label):
        if label in self.tech_selected:
            self.tech_selected.discard(label)
        else:
            self.tech_selected.add(label)
        self._refresh_tech_chip_styles()
        self._rebuild_list()

    def _refresh_tech_chip_styles(self):
        for label, b in self.tech_buttons.items():
            b.state(["pressed"] if label in self.tech_selected else ["!pressed"])

    def _update_tech_note(self):
        if not getattr(self, "tech_note_label", None):
            return
        if not self.names.has_meta_group_data():
            self.tech_note_label.configure(
                text="⚠ SDE has no tech data — click Update SDE; all items read "
                     "as T1 until then",
                foreground=CLR_BAD)
        else:
            self.tech_note_label.configure(
                text="(T2/T3 cost modeling not built yet — their build cost is "
                     "understated)",
                foreground=CLR_MUTED)

    def _on_sort(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = col in ("cost", "profit", "margin", "vol")
        self._update_headings()
        self._rebuild_list()

    def _update_headings(self):
        for tree in self._trees:
            for key, base in self._headings.items():
                arrow = ""
                if key == self.sort_col or (key == "#0" and self.sort_col == "name"):
                    arrow = " ▼" if self.sort_reverse else " ▲"
                tree.heading(key, text=base + arrow)

    def _on_input_change(self):
        self._update_legend()
        self._compute(refetch=False)

    def _update_legend(self):
        self.legend_label.configure(
            text=f"Profit = patient-sell (7d/30d/90d history) at "
                 f"{self.sell_var.get()} net of "
                 f"fees − build cost.   Input = {self.input_var.get()}.")

    def _sort_value(self, tid, col):
        r = self.results[tid]
        if col == "name":
            return self.name_map.get(tid, "")
        if col == "cost":
            return r["unit_cost"]
        if col == "profit":
            return r["patient_profit"] if r["patient_profit"] is not None else -1e18
        if col == "margin":
            return r["patient_margin"] if r["patient_margin"] is not None else -1e18
        if col == "vol":
            return r["volume"]
        return 0

    def _rebuild_list(self):
        search = self.search_var.get().strip().lower()
        mp_raw = self.min_profit_var.get().strip().replace(",", "")
        try:
            min_profit = float(mp_raw) if mp_raw else None  # blank = no floor
        except ValueError:
            min_profit = None
        show_unpriced = self.show_unpriced_var.get()
        subcap_only = self.subcap_only_var.get()
        hide_upwell = self.hide_upwell_var.get()
        hide_pos = self.hide_pos_var.get()
        cat = self._active_category
        bp_mode = self.bp_filter_var.get()
        excluded = {
            "category": 0, "hidden": 0, "search_miss": 0, "tech": 0,
            "capital": 0, "upwell": 0, "pos": 0, "blueprint": 0,
            "unpriced": 0, "min_profit": 0,
        }

        # Items passing the category filter (independent of tech) drive both the
        # tech-chip greying and the row set. A tech chip greys out when the
        # active category contains no item of that tech level.
        cat_items = [tid for tid in self.results
                     if search or cat == "All" or self.category_map.get(tid) == cat]
        excluded["category"] = len(self.results) - len(cat_items)
        avail_tech = {tech_label(self.meta_map.get(tid)) for tid in cat_items}
        for label, b in self.tech_buttons.items():
            b.state(["!disabled"] if label in avail_tech else ["disabled"])

        rows = []
        for tid in cat_items:
            r = self.results[tid]
            name = self.name_map.get(tid, str(tid))
            hidden = is_auto_hidden(name) or self.ignore.contains(tid)
            # Search is a direct item lookup and bypasses every visibility
            # filter, including auto-hidden/user-ignored status.
            if search:
                if search in name.lower():
                    rows.append(tid)
                    excluded["hidden"] += int(hidden)
                else:
                    excluded["search_miss"] += 1
                continue
            if hidden:
                excluded["hidden"] += 1
                continue
            if tech_label(self.meta_map.get(tid)) not in self.tech_selected:
                excluded["tech"] += 1
                continue
            if subcap_only and self.capital_map.get(tid):
                excluded["capital"] += 1
                continue
            if hide_upwell and self.upwell_map.get(tid):
                excluded["upwell"] += 1
                continue
            if hide_pos and self.pos_map.get(tid):
                excluded["pos"] += 1
                continue
            has_bpo = self.bpo_map.get(tid, True)
            if bp_mode == "BPO only" and not has_bpo:
                excluded["blueprint"] += 1
                continue
            if bp_mode == "BPC-only" and has_bpo:
                excluded["blueprint"] += 1
                continue
            priced = r["patient_profit"] is not None
            if not priced and not show_unpriced:
                excluded["unpriced"] += 1
                continue
            if priced and min_profit is not None and r["patient_profit"] < min_profit:
                excluded["min_profit"] += 1
                continue
            rows.append(tid)

        rows.sort(key=lambda t: self._sort_value(t, self.sort_col),
                  reverse=self.sort_reverse)
        self.display_order = rows
        # "Positive only" sub-tab: same base filter, profit strictly > 0.
        pos_rows = (list(rows) if search else
                    [t for t in rows
                     if self.results[t]["patient_profit"] is not None
                     and self.results[t]["patient_profit"] > 0])
        self._fill_tree(self.tree, rows)
        self._fill_tree(self.tree_pos, pos_rows)
        if search:
            diag = (f"search text={search!r} total={len(self.results)} "
                    f"matches={len(rows)} filters_bypassed=True "
                    f"hidden_matches={excluded['hidden']}")
        else:
            diag = (
                f"filter total={len(self.results)} visible={len(rows)} "
                f"positive={len(pos_rows)} category={cat!r} "
                f"tech={','.join(sorted(self.tech_selected)) or 'none'} "
                f"blueprint={bp_mode!r} excluded={format_counts(excluded)}"
            )
        self._queue_filter_diag(diag, delayed=bool(search))

    def _queue_filter_diag(self, diag: str, delayed: bool = False):
        """Deduplicate filter logs and debounce search typing only."""
        if self._filter_diag_after is not None:
            try:
                self.frame.after_cancel(self._filter_diag_after)
            except tk.TclError:
                pass
            self._filter_diag_after = None
        if delayed:
            self._filter_diag_after = self.frame.after(
                300, lambda d=diag: self._emit_filter_diag(d))
        else:
            self._emit_filter_diag(diag)

    def _emit_filter_diag(self, diag: str):
        self._filter_diag_after = None
        if diag != self._last_filter_diag:
            self._last_filter_diag = diag
            _print(diag)

    def _fill_tree(self, tree, rows):
        tree.delete(*tree.get_children())
        for tid in rows:
            r = self.results[tid]
            name = self.name_map.get(tid, str(tid))
            margin = r["patient_margin"]
            profit = r["patient_profit"]
            tag = ("good" if margin is not None and margin >= 30 else
                   "ok" if margin is not None and margin >= 10 else "bad")
            vol = r["volume"]
            tree.insert(
                "", "end", iid=str(tid), text=name, tags=(tag,),
                values=(isk_m(r["unit_cost"]),
                        isk_m(profit) if profit is not None else "—",
                        f"{margin:+.0f}" if margin is not None else "—",
                        f"{vol:.0f}" if vol >= 1 else "—"))
        if self.selected is not None and tree.exists(str(self.selected)):
            tree.selection_set(str(self.selected))

    def _on_select(self, event=None):
        tree = event.widget if event is not None else self.tree
        sel = tree.selection()
        if not sel:
            return
        self.selected = int(sel[0])
        if self.selected in self.results:
            self._show_detail(self.selected)

    # ===================================================================== misc

    def _show_list_history(self):
        if self.selected is None:
            return
        tid = self.selected
        name = self.name_map.get(tid, str(tid))
        region = self._hub_cfg(self.sell_var)["region_id"]
        try:
            from analytics import graphing
            graphing.show_price_graph(self.frame, tid, name, region)
        except Exception as e:
            self.set_status(f"Industry: graph failed - {e}")

    def _show_list_menu(self, event):
        tree = event.widget
        iid = tree.identify_row(event.y)
        if iid:
            tree.selection_set(iid)
            self.selected = int(iid)
            self._list_menu.tk_popup(event.x_root, event.y_root)

    def _copy_selected_name(self):
        if self.selected is None:
            return
        name = self.name_map.get(self.selected, str(self.selected))
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(name)
            self.set_status(f"Industry: copied {name}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------ ignore list
    def _update_ignored_btn(self):
        n = self.ignore.count()
        try:
            self.ignored_btn.configure(
                text=f"Ignored ({n})…" if n else "Ignored…")
        except (AttributeError, tk.TclError):
            pass

    def _apply_ignore_change(self):
        """Re-filter both Industry lists after the ignore set changes."""
        self._update_ignored_btn()
        self._rebuild_list()
        owned = getattr(self, "owned_panel", None)
        if owned:
            owned._update_ignored_btn()
            owned._fill_tree()

    def _ignore_selected(self):
        if self.selected is None:
            return
        tid = self.selected
        name = self.name_map.get(tid, str(tid))
        self.ignore.add(tid, name)
        self.set_status(f"Industry: ignoring {name}")
        self._apply_ignore_change()

    def _manage_ignored(self):
        show_ignore_manager(self.frame, self.ignore, self.names,
                            self._apply_ignore_change)
