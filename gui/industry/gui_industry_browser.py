"""Industry page layout, product browser, filters and list interactions.

IndustryBrowserMixin uses the shared manager state; it owns no separate cache
or calculation lifecycle. Product selectors live in gui_industry_filters.
"""

import tkinter as tk
from tkinter import ttk

from core.config import get_enabled_hubs
from gui.gui_window_utils import make_scrollable
from gui.industry.gui_industry_characters import CharactersPanel
from gui.industry.gui_industry_filters import (
    IndustryFiltersMixin,
)
from gui.industry.gui_industry_details import (
    CLR_GOOD, CLR_OK, CLR_BAD, CLR_MUTED, isk_m, show_ignore_manager, _print,
)


class IndustryBrowserMixin(IndustryFiltersMixin):
    """Layout, filtering and list callbacks for IndustryTabManager."""

    # ===================================================================== build

    def _build_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Industry")
        self.frame = outer
        self.sub_nb = ttk.Notebook(outer)
        self.sub_nb.pack(fill=tk.BOTH, expand=True)
        browser = ttk.Frame(self.sub_nb)
        self.sub_nb.add(browser, text="Build browser")

        # Maintenance/status stays outside the two working columns.
        top = ttk.Frame(browser, padding=(8, 4))
        top.pack(fill=tk.X)
        self.refresh_btn = ttk.Button(top, text="Refresh from caches",
                                      command=lambda: self._compute(refetch=True))
        self.refresh_btn.pack(side=tk.LEFT)
        self.sde_btn = ttk.Button(top, text="Update SDE", command=self._on_update_sde)
        self.sde_btn.pack(side=tk.LEFT, padx=4)
        self.progress_label = ttk.Label(top, text="", foreground=CLR_MUTED)
        self.progress_label.pack(side=tk.LEFT, padx=8)

        columns = ttk.Panedwindow(browser, orient=tk.HORIZONTAL)
        columns.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        left = ttk.Frame(columns)
        right = ttk.Panedwindow(columns, orient=tk.VERTICAL)
        columns.add(left, weight=1)
        columns.add(right, weight=1)

        # Both left panels retain their headers/summaries when collapsed.
        left.columnconfigure(0, weight=1)
        self.left_panels = left
        self._build_item_browser(left)
        self.results_panel = ttk.LabelFrame(left, text="Matching products", padding=6)
        self.results_panel.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._results_open = False
        self.results_toggle_btn = ttk.Button(
            self.results_panel, text="Results ▸", command=self._toggle_results_panel)
        self.results_toggle_btn.pack(anchor="w")
        self.result_count_label = ttk.Label(
            self.results_panel, text="Choose items, then press Show", foreground=CLR_MUTED)
        self.result_count_label.pack(anchor="w", pady=(4, 4))
        self.results_body = ttk.Frame(self.results_panel)
        self._headings = {"#0": "Item", "cost": "Build cost", "profit": "Profit",
                          "margin": "Margin%", "vol": "Vol/day"}
        self._list_menu = tk.Menu(self.frame, tearoff=0)
        self._list_menu.add_command(label="View price history", command=self._show_list_history)
        self._list_menu.add_command(label="Copy name", command=self._copy_selected_name)
        self._list_menu.add_separator()
        self._list_menu.add_command(label="Ignore this item", command=self._ignore_selected)
        self.tree = self._make_list_tree(self.results_body)
        self._trees = (self.tree,)
        self._update_headings()

        # Filters occupy the upper right; selected build occupies the lower right.
        # Each scrolls independently and the horizontal divider is adjustable.
        settings_host = ttk.Frame(right)
        build_host = ttk.Frame(right)
        right.add(settings_host, weight=1)
        right.add(build_host, weight=2)
        settings = make_scrollable(settings_host)
        self._build_filters(settings)
        self._build_calculation_settings(settings)
        self.status_label = ttk.Label(
            settings, text=f"Data: {self.market.get_last_update()}", foreground=CLR_MUTED)
        self.status_label.pack(anchor="w", padx=8, pady=4)
        settings.bind("<Configure>", lambda event: self.tech_note_label.configure(
            wraplength=max(180, event.width - 24)), add="+")

        ttk.Label(build_host, text="Individual build", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=8, pady=(4, 0))
        self.legend_label = ttk.Label(build_host, foreground=CLR_MUTED, wraplength=420)
        self.legend_label.pack(fill=tk.X, padx=8, pady=(2, 4))
        self._update_legend()
        build_host.bind("<Configure>", lambda event: self.legend_label.configure(
            wraplength=max(180, event.width - 24)))
        scroll_host = ttk.Frame(build_host)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        self.detail = make_scrollable(scroll_host)
        self._detail_canvas = self.detail.master
        ttk.Label(self.detail, text="Select a result to view its build breakdown",
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=8)

        # Dedicated blueprint and character tools retain their existing paths.
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
        chars_frame = ttk.Frame(self.sub_nb)
        self.sub_nb.add(chars_frame, text="Characters")
        self.characters_panel = CharactersPanel(
            chars_frame, self.roster, self.char_skills, self.char_standings,
            self.set_status, root=self.root,
            bp_puller=self.bp_puller, bp_db=self.bp_db,
            on_blueprints_pulled=self._on_blueprints_pulled)

    def _build_calculation_settings(self, parent):
        hubs = get_enabled_hubs()
        hub_names = [name for _key, name in hubs]
        self._hub_key_by_name = {name: key for key, name in hubs}
        markets = ttk.LabelFrame(parent, text="Markets", padding=(8, 4))
        markets.pack(fill=tk.X, padx=8, pady=4)
        self.buy_var = tk.StringVar(value="Jita")
        self.sell_var = tk.StringVar(value="Jita")
        for column, label, var, attr in (
                (0, "Buy materials at:", self.buy_var, "buy_combo"),
                (2, "Sell products at:", self.sell_var, "sell_combo")):
            ttk.Label(markets, text=label).grid(row=0, column=column, sticky="w")
            combo = ttk.Combobox(markets, textvariable=var, values=hub_names,
                                 state="readonly", width=14)
            combo.grid(row=0, column=column + 1, sticky="ew", padx=(4, 8))
            combo.bind("<<ComboboxSelected>>", lambda event: self._on_input_change())
            setattr(self, attr, combo)
            markets.columnconfigure(column + 1, weight=1)
        ttk.Label(markets, text="Input:").grid(row=1, column=0, sticky="w", pady=4)
        self.input_var = tk.StringVar(value="Patient (buy orders)")
        combo = ttk.Combobox(markets, textvariable=self.input_var, state="readonly",
                             values=["Patient (buy orders)", "Impatient (sell orders)"], width=22)
        combo.grid(row=1, column=1, columnspan=3, sticky="w", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda event: self._on_input_change())

        build = ttk.LabelFrame(parent, text="Build assumptions", padding=(8, 4))
        build.pack(fill=tk.X, padx=8, pady=4)
        facility = ttk.Frame(build)
        facility.pack(fill=tk.X)
        ttk.Label(facility, text="Facility system:").pack(side=tk.LEFT)
        self.facility_var = tk.StringVar(value="Jita")
        self.facility_combo = self._combo(facility, self.facility_var, hub_names,
                                          self._on_input_change, width=16)
        basic = ttk.Frame(build)
        basic.pack(fill=tk.X, pady=4)
        for attr, label, default, callback in (
                ("me_var", "ME:", "10", lambda: self._compute(refetch=False)),
                ("te_var", "TE:", "20", self._refresh_detail_if_selected),
                ("batch_var", "Batch:", "1", lambda: self._compute(refetch=False))):
            ttk.Label(basic, text=label).pack(side=tk.LEFT, padx=(0, 2))
            var = tk.StringVar(value=default)
            setattr(self, attr, var)
            entry = ttk.Entry(basic, textvariable=var, width=5)
            entry.pack(side=tk.LEFT, padx=(0, 8))
            entry.bind("<Return>", lambda event, cb=callback: cb())
        ttk.Button(basic, text="Apply ME/Batch",
                   command=lambda: self._compute(refetch=False)).pack(side=tk.LEFT)
        self.facility_settings_btn = ttk.Button(
            build, text="Facility settings ▸", command=self._toggle_facility_settings)
        self.facility_settings_btn.pack(anchor="w")
        self.facility_settings = ttk.Frame(build)
        taxes = ttk.Frame(self.facility_settings)
        taxes.pack(fill=tk.X)
        self.fac_tax_var = self._mini_field(taxes, "Facility tax %:",
                                            f"{self.constants.facility_tax_pct}")
        self.scc_var = self._mini_field(taxes, "SCC %:",
                                        f"{self.constants.scc_surcharge_pct}")
        ttk.Label(self.facility_settings, text="SCC/tax are CCP-tuned — verify in-game.",
                  foreground=CLR_MUTED).pack(anchor="w", padx=10)
        bonuses = ttk.Frame(self.facility_settings)
        bonuses.pack(fill=tk.X)
        self.cost_bonus_var = self._mini_field(bonuses, "Cost bonus %:", "0")
        self.mat_bonus_var = self._mini_field(bonuses, "Material bonus %:", "0")
        time_row = ttk.Frame(self.facility_settings)
        time_row.pack(fill=tk.X)
        self.time_bonus_var = self._mini_field(time_row, "Time bonus %:", "0")
        self._build_blueprint_contract_controls(parent)

    def _on_blueprints_pulled(self):
        """Refresh the Owned panel after a Characters-tab blueprint pull."""
        if getattr(self, "owned_panel", None):
            self.owned_panel.refresh()
        self._rebuild_list()

    def _toggle_facility_settings(self):
        if self.facility_settings.winfo_manager():
            self.facility_settings.pack_forget()
            self.facility_settings_btn.configure(text="Facility settings ▸")
        else:
            self.facility_settings.pack(fill=tk.X, pady=(4, 0))
            self.facility_settings_btn.configure(text="Facility settings ▾")

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

    def _make_list_tree(self, parent):
        """Ranked products; profitable-only is a composable global filter."""
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
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
