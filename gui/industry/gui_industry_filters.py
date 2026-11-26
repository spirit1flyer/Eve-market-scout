"""Industry EVE-style tree and Show/apply workflow, independent of costing.

All selectors combine, including search. Market-group IDs, rather than name
substrings, define category membership; choosing a parent includes descendants.
Editing selectors is staged until Show; the class panel then collapses to a
summary and the separate matching-products panel expands beneath it.
"""

import tkinter as tk
from tkinter import ttk
from typing import Optional

from industry.industry_diagnostics import format_counts, _zero_input_count
from industry.industry_ignore import is_auto_hidden
from gui.industry.gui_industry_details import CLR_MUTED, _print


TECH_ORDER = ["T1", "Faction", "T2", "T3"]
DEFAULT_TECH = {"T1", "Faction"}


def tech_label(meta_group_id: Optional[int]) -> str:
    if meta_group_id == 2:
        return "T2"
    if meta_group_id == 14:
        return "T3"
    if meta_group_id == 4:
        return "Faction"
    return "T1"


def category_children(paths, names):
    """Index only branches containing manufacturable products, by parent ID."""
    children = {}
    for path in paths.values():
        parent = None
        for group_id in path:
            children.setdefault(parent, set()).add(group_id)
            parent = group_id
    return {
        parent: sorted(ids, key=lambda gid: (names.get(gid, str(gid)).lower(), gid))
        for parent, ids in children.items()
    }


class IndustryFiltersMixin:
    """EVE tree, pending presets and explicit Show/apply workflow."""

    def _build_item_browser(self, parent):
        self._applied_filters = None
        self._category_paths = ((),)  # Empty path means Everything; no paths means none.
        self._category_children = {}
        self._browser_open = False
        self.selector_panel = ttk.LabelFrame(parent, text="Class browser", padding=6)
        self.selector_panel.grid(row=0, column=0, sticky="nsew")
        header = ttk.Frame(self.selector_panel)
        header.pack(fill=tk.X)
        self.browser_toggle_btn = ttk.Button(
            header, text="Browse items ▸", command=self._toggle_item_browser)
        self.browser_toggle_btn.pack(side=tk.LEFT)
        ttk.Button(header, text="Show", command=self._show_browser_results).pack(
            side=tk.RIGHT, padx=(8, 0))
        self.selection_status_label = ttk.Label(
            self.selector_panel, text="Choose classes or a preset, then press Show.",
            foreground=CLR_MUTED, wraplength=500)
        self.selection_status_label.pack(fill=tk.X, pady=4)
        self.selector_panel.bind("<Configure>", lambda event: self.selection_status_label.configure(
            wraplength=max(180, event.width - 16)))
        self.selector_body = ttk.Frame(self.selector_panel)
        tech_row = ttk.Frame(self.selector_body)
        tech_row.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(tech_row, text="Technology:").pack(side=tk.LEFT)
        self.tech_var = tk.StringVar(value="T1 + Faction")
        self._combo(tech_row, self.tech_var,
                    ["Everything", "T1 + Faction"] + TECH_ORDER,
                    self._mark_filters_pending, width=18)
        ttk.Label(self.selector_body, text="Ctrl-click adds classes; Shift-click selects a range.",
                  foreground=CLR_MUTED).pack(anchor="w", pady=(0, 4))
        tree_host = ttk.Frame(self.selector_body)
        tree_host.pack(fill=tk.BOTH, expand=True)
        self.category_tree = ttk.Treeview(tree_host, show="tree", selectmode="extended")
        self.category_tree.heading("#0", text="Market categories")
        self.category_tree.column("#0", width=320, minwidth=120)
        scrollbar = ttk.Scrollbar(tree_host, orient=tk.VERTICAL,
                                   command=self.category_tree.yview)
        self.category_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.category_tree.pack(fill=tk.BOTH, expand=True)
        self.category_tree.bind("<<TreeviewSelect>>", self._on_category_selected)
        self.category_tree.bind("<Return>", lambda event: self._show_browser_results())
        self._render_category_tree()

    def _build_filters(self, parent):
        global_box = ttk.LabelFrame(parent, text="Filter settings", padding=(8, 4))
        global_box.pack(fill=tk.X, padx=8, pady=4)
        for index, (attr, label, default) in enumerate((
                ("subcap_only_var", "Sub-cap only", True),
                ("hide_missing_inputs_var", "Hide missing input costs", True),
                ("hide_upwell_var", "Hide Upwell / sov", False),
                ("hide_pos_var", "Hide POS", False),
                ("hide_ignored_var", "Hide ignored", True),
                ("show_unpriced_var", "Show unpriced estimates", False),
                ("owned_only_var", "Owned only", False),
                ("profitable_only_var", "Profitable only", False))):
            var = tk.BooleanVar(value=default)
            setattr(self, attr, var)
            row, column = divmod(index, 2)
            ttk.Checkbutton(global_box, text=label, variable=var,
                            command=self._mark_filters_pending).grid(
                                row=row, column=column * 2, columnspan=2,
                                sticky="w", padx=(0, 8))
        ttk.Label(global_box, text="Blueprint:").grid(row=4, column=0, sticky="w", pady=4)
        self.bp_filter_var = tk.StringVar(value="All")
        combo = ttk.Combobox(global_box, textvariable=self.bp_filter_var,
                             values=["All", "BPO only", "BPC-only"], state="readonly", width=12)
        combo.grid(row=4, column=1, sticky="w", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda event: self._mark_filters_pending())
        ttk.Label(global_box, text="Min profit:").grid(row=4, column=2, sticky="w")
        self.min_profit_var = tk.StringVar(value="")
        ttk.Entry(global_box, textvariable=self.min_profit_var, width=12).grid(
            row=4, column=3, sticky="ew")
        self.min_profit_var.trace_add("write", lambda *_: self._mark_filters_pending())
        ttk.Label(global_box, text="Search:").grid(row=5, column=0, sticky="w")
        self.search_var = tk.StringVar()
        search = ttk.Entry(global_box, textvariable=self.search_var, width=24)
        search.grid(row=5, column=1, columnspan=3, sticky="ew", padx=(4, 0))
        search.bind("<Return>", lambda event: self._show_browser_results())
        self.search_var.trace_add("write", lambda *_: self._mark_filters_pending())
        self.ignored_btn = ttk.Button(global_box, text="Ignored…", command=self._manage_ignored)
        self.ignored_btn.grid(row=6, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._update_ignored_btn()
        ttk.Label(global_box, text="Press Show to apply these choices.",
                  foreground=CLR_MUTED).grid(row=6, column=2, columnspan=2, sticky="w")
        global_box.columnconfigure(1, weight=1)
        global_box.columnconfigure(3, weight=1)
        self.tech_note_label = ttk.Label(parent, foreground=CLR_MUTED, wraplength=420)
        self.tech_note_label.pack(fill=tk.X, padx=8, pady=4)
        self._update_tech_note()

    def _mark_filters_pending(self):
        self.selection_status_label.configure(
            text=f"{self.tech_var.get()} · {self._category_summary()} — press Show.")

    def _category_summary(self, paths=None):
        paths = self._category_paths if paths is None else paths
        if () in paths:
            return "Everything"
        if not paths:
            return "No classes selected"
        labels = [self.market_group_names.get(path[-1], str(path[-1])) for path in paths]
        summary = ", ".join(labels[:3])
        if len(labels) > 3:
            summary += f" (+{len(labels) - 3} more)"
        return f"{len(labels)} classes: {summary}" if len(labels) > 1 else summary

    def _toggle_item_browser(self):
        self._set_item_browser_open(not self._browser_open)

    def _set_item_browser_open(self, opened):
        self._browser_open = opened
        if opened:
            self._set_results_open(False)
            self.selector_body.pack(fill=tk.BOTH, expand=True)
        else:
            self.selector_body.pack_forget()
        self.left_panels.rowconfigure(0, weight=1 if opened else 0)
        self.browser_toggle_btn.configure(text="Browse items ▾" if opened else "Browse items ▸")

    def _toggle_results_panel(self):
        self._set_results_open(not self._results_open)

    def _set_results_open(self, opened):
        self._results_open = opened
        if opened:
            self._set_item_browser_open(False)
            self.results_body.pack(fill=tk.BOTH, expand=True)
        else:
            self.results_body.pack_forget()
        self.left_panels.rowconfigure(1, weight=1 if opened else 0)
        self.results_toggle_btn.configure(text="Results ▾" if opened else "Results ▸")

    def _refresh_categories(self):
        self._category_children = category_children(self.market_paths, self.market_group_names)
        previous = self._category_paths
        valid_paths = []
        for path in self._category_paths:
            parent = None
            valid = []
            for gid in path:
                if gid not in self._category_children.get(parent, []):
                    break
                valid.append(gid)
                parent = gid
            if not path or valid:
                valid_paths.append(tuple(valid))
        self._category_paths = tuple(dict.fromkeys(valid_paths))
        self._render_category_tree()
        if self._category_paths != previous:
            self._mark_filters_pending()

    def _render_category_tree(self):
        tree = self.category_tree
        expanded = {int(iid.split(":")[1]) for iid in self._category_tree_ids()
                    if tree.item(iid, "open")}
        expanded.update(gid for path in self._category_paths for gid in path[:-1])
        tree.delete(*tree.get_children())
        tree.insert("", "end", iid="everything", text="Everything", open=True)
        self._tree_paths = {"everything": ()}

        def add_children(parent_iid, parent_gid, path):
            for gid in self._category_children.get(parent_gid, []):
                iid = f"group:{gid}"
                if tree.exists(iid):
                    continue  # Malformed/repeated SDE branches cannot duplicate IDs.
                child_path = path + (gid,)
                self._tree_paths[iid] = child_path
                tree.insert(parent_iid, "end", iid=iid,
                            text=self.market_group_names.get(gid, str(gid)),
                            open=gid in expanded)
                add_children(iid, gid, child_path)

        add_children("everything", None, ())
        selected = [f"group:{path[-1]}" if path else "everything"
                    for path in self._category_paths]
        tree.selection_set(selected)
        if selected:
            tree.focus(selected[0])
            tree.see(selected[0])

    def _category_tree_ids(self):
        return [iid for iid in getattr(self, "_tree_paths", {}) if iid.startswith("group:")]

    def _on_category_selected(self, _event=None):
        tree = self.category_tree
        selection = tree.selection()
        if "everything" in selection and len(selection) > 1:
            # Adding classes replaces Everything; adding Everything replaces classes.
            selection = (tuple(iid for iid in selection if iid != "everything")
                         if () in self._category_paths else ("everything",))
            tree.selection_set(selection)
        paths = tuple(self._tree_paths[iid] for iid in selection if iid in self._tree_paths)
        if paths != self._category_paths:
            self._category_paths = paths
            self._mark_filters_pending()

    def _snapshot_browser_filters(self):
        raw_profit = self.min_profit_var.get().strip().replace(",", "")
        try:
            min_profit = float(raw_profit) if raw_profit else None
        except ValueError:
            min_profit = None
        tech = self.tech_var.get()
        technologies = (set(TECH_ORDER) if tech == "Everything" else
                        set(DEFAULT_TECH) if tech == "T1 + Faction" else {tech})
        return {
            "category_paths": tuple(self._category_paths), "tech": technologies,
            "search": self.search_var.get().strip().lower(), "min_profit": min_profit,
            "blueprint": self.bp_filter_var.get(),
            "owned_only": self.owned_only_var.get(),
            "hide_ignored": self.hide_ignored_var.get(),
            "subcap_only": self.subcap_only_var.get(),
            "hide_upwell": self.hide_upwell_var.get(), "hide_pos": self.hide_pos_var.get(),
            "hide_missing": self.hide_missing_inputs_var.get(),
            "show_unpriced": self.show_unpriced_var.get(),
            "profitable_only": self.profitable_only_var.get(),
        }

    def _show_browser_results(self):
        self._on_category_selected()
        self._applied_filters = self._snapshot_browser_filters()
        self.tech_selected = set(self._applied_filters["tech"])
        self._rebuild_list()
        self._set_results_open(True)
        summary = self._category_summary()
        self.selection_status_label.configure(text=f"{self.tech_var.get()} · {summary}")
        _print(f"browser_show categories={summary!r} technology={self.tech_var.get()!r} "
               f"results={len(self.display_order)} selector_open={self._browser_open} "
               f"results_open={self._results_open}")

    def _rebuild_list(self):
        # Data refresh/sorting keeps the last shown slice until Show applies
        # the next selection. Merely drilling into a branch never applies it.
        options = self._applied_filters
        if options is None:
            return
        search = options["search"]
        min_profit = options["min_profit"]
        paths = options["category_paths"]
        all_categories = () in paths
        group_ids = {path[-1] for path in paths if path}
        bp_mode = options["blueprint"]
        owned = self.bp_db.get_owned_type_ids() if options["owned_only"] else None
        hide_ignored = options["hide_ignored"]
        subcap_only = options["subcap_only"]
        hide_upwell = options["hide_upwell"]
        hide_pos = options["hide_pos"]
        hide_missing = options["hide_missing"]
        show_unpriced = options["show_unpriced"]
        profitable_only = options["profitable_only"]
        excluded = dict.fromkeys(("category", "hidden", "search_miss", "tech",
                                  "capital", "upwell", "pos", "blueprint", "owned",
                                  "missing_inputs", "unpriced", "min_profit", "profitable"), 0)
        rows = []
        for tid, result in self.results.items():
            name = self.name_map.get(tid, str(tid))
            profit = result["patient_profit"]
            reason = None
            if is_auto_hidden(name) or (hide_ignored and self.ignore.contains(tid)):
                reason = "hidden"
            elif subcap_only and self.capital_map.get(tid):
                reason = "capital"
            elif hide_upwell and self.upwell_map.get(tid):
                reason = "upwell"
            elif hide_pos and self.pos_map.get(tid):
                reason = "pos"
            elif owned is not None and self.blueprint_map.get(tid) not in owned:
                reason = "owned"
            elif hide_missing and _zero_input_count(result.get("inputs") or []):
                reason = "missing_inputs"
            elif ((bp_mode == "BPO only" and not self.bpo_map.get(tid, True)) or
                  (bp_mode == "BPC-only" and self.bpo_map.get(tid, True))):
                reason = "blueprint"
            elif not all_categories and not group_ids.intersection(self.market_paths.get(tid, ())):
                reason = "category"
            elif tech_label(self.meta_map.get(tid)) not in options["tech"]:
                reason = "tech"
            elif search and search not in name.lower():
                reason = "search_miss"
            elif profit is None and not show_unpriced:
                reason = "unpriced"
            elif profitable_only and (profit is None or profit <= 0):
                reason = "profitable"
            elif profit is not None and min_profit is not None and profit < min_profit:
                reason = "min_profit"
            if reason:
                excluded[reason] += 1
            else:
                rows.append(tid)
        rows.sort(key=lambda tid: self._sort_value(tid, self.sort_col),
                  reverse=self.sort_reverse)
        self.display_order = rows
        if self.selected is not None and self.selected not in rows:
            self.selected = None
            self._clear_detail()
            ttk.Label(self.detail, text="Select an item to view its build breakdown",
                      foreground=CLR_MUTED).pack(anchor="w")
        self._fill_tree(self.tree, rows)
        self.result_count_label.configure(text=f"{len(rows):,} matching products")
        summary = self._category_summary(paths)
        diag = (f"filter total={len(self.results)} visible={len(rows)} "
                f"categories={summary!r} groups={','.join(map(str, sorted(group_ids))) or 'none'} "
                f"tech={','.join(sorted(options['tech'])) or 'none'} "
                f"blueprint={bp_mode!r} owned_only={owned is not None} "
                f"search={search!r} filters_bypassed=False excluded={format_counts(excluded)}")
        self._queue_filter_diag(diag, delayed=bool(search))
