"""Invention sub-tab (2026-07-11): invent-vs-buy-BPC across all invention items.

The T2/T3 (invention) list page ranks items by build profit; this sub-tab flips
the lens to the BLUEPRINT decision — for every item with an invention path, is
self-invention or a contract BPC the cheaper way to get runs, and do I even own
the source BPO to invent from? Columns put the amortized invent cost/run next
to the cheapest cached contract BPC/run (real ME/TE from cached contract
items) with the cheaper side named, plus the item's build profit so a cheap
blueprint on an unprofitable hull is still obviously a pass.

Row set = the manager's computed results that carry invention facts (same data
as the T2/T3 page — no compute of its own). Contract BPC prices resolve in a
worker thread (cached-contracts SQLite only, no ESI): live offers win and are
snapshotted into `ObservedBpcPrices`; with none live, the last snapshot's
average serves as an explicitly-stale fallback (`~`); "none" = never seen.

"Owned BPOs only" (default ON, per Caleb's ask) keeps rows whose invention
source blueprint is owned as a BPO (Characters-tab pull). T3 relic paths have
no BPO and only show when the filter is off.
"""

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from core.tk_queue import submit
from industry.industry_ignore import IgnoreList, is_auto_hidden

# Shared formatting + colour helpers from the main Industry tab.
from gui.industry.gui_industry import (
    isk_m, CLR_GOOD, CLR_OK, CLR_BAD, CLR_MUTED,
)


def _print(msg: str) -> None:
    print(f"[IndustryInvent] {msg}")


class InventionPanel:
    """Invent-vs-buy-BPC master list over the manager's invention results."""

    def __init__(self, parent, bp_db, sde, names, contracts_db, bpc_observed,
                 hub_regions: Callable, show_full_detail: Callable,
                 set_status: Optional[Callable] = None):
        self.parent = parent
        self.bp_db = bp_db               # IndustryBlueprintsDB (owned BPOs)
        self.sde = sde                   # SDEIndustryDB (bp lookups)
        self.names = names               # sde_manager (type-name fallback)
        self.contracts_db = contracts_db # ContractsDB or None
        self.bpc_observed = bpc_observed # ObservedBpcPrices (stale fallback)
        self.hub_regions = hub_regions   # () -> [region_id, ...] (UI thread)
        self.show_full_detail = show_full_detail  # (tid) -> jump to Top Profit
        self.set_status = set_status or (lambda m: None)
        self.ignore = IgnoreList.singleton()

        self.rows: List[dict] = []
        self.selected: Optional[int] = None
        self._bpc_gen = 0                # discard stale worker results
        self.sort_col = "profit"
        self.sort_reverse = True
        self._menu = None
        self._build()

    # ----------------------------------------------------------------- layout

    def _build(self):
        bar = ttk.Frame(self.parent, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Invention — invent vs buy BPC",
                  font=("Segoe UI", 11, "bold")).pack(side=tk.LEFT)
        self.count_label = ttk.Label(bar, text="", foreground=CLR_MUTED)
        self.count_label.pack(side=tk.LEFT, padx=10)
        ttk.Label(bar, text="Rows come from the Top Profit compute; "
                            "double-click for the full detail.",
                  foreground=CLR_MUTED).pack(side=tk.RIGHT, padx=8)

        filt = ttk.Frame(self.parent, padding=(8, 0))
        filt.pack(fill=tk.X)
        self.owned_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(filt, text="Owned BPOs only", variable=self.owned_var,
                        command=self._fill_tree).pack(side=tk.LEFT)
        ttk.Label(filt, text="Search:").pack(side=tk.LEFT, padx=(12, 2))
        self.search_var = tk.StringVar()
        se = ttk.Entry(filt, textvariable=self.search_var, width=18)
        se.pack(side=tk.LEFT)
        se.bind("<KeyRelease>", lambda e: self._fill_tree())
        self.positive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(filt, text="Positive profit only",
                        variable=self.positive_var,
                        command=self._fill_tree).pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(filt, text="  ~ = stale observed avg   relic = T3 (no BPO)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT, padx=(12, 0))

        main = ttk.Frame(self.parent, padding=8)
        main.pack(fill=tk.BOTH, expand=True)
        cols = ("src", "invent", "bpc", "cheaper", "profit", "margin")
        self._headings = {
            "#0": "Item", "src": "Src BPO", "invent": "Invent /run",
            "bpc": "BPC /run", "cheaper": "Cheaper", "profit": "Profit",
            "margin": "Margin%"}
        tree = ttk.Treeview(main, columns=cols, height=28)
        tree.heading("#0", text="Item", command=lambda: self._on_sort("name"))
        tree.column("#0", width=230)
        widths = {"src": 70, "invent": 95, "bpc": 110, "cheaper": 70,
                  "profit": 100, "margin": 70}
        for c in cols:
            tree.heading(c, text=self._headings[c],
                         command=lambda cc=c: self._on_sort(cc))
            tree.column(c, width=widths[c],
                        anchor="e" if c not in ("src", "cheaper") else "center")
        vsb = ttk.Scrollbar(main, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.LEFT, fill=tk.Y)
        tree.tag_configure("good", foreground=CLR_GOOD)
        tree.tag_configure("ok", foreground=CLR_OK)
        tree.tag_configure("bad", foreground=CLR_BAD)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", self._on_double)
        tree.bind("<Button-3>", self._show_menu)
        self.tree = tree
        self._update_headings()

    # ------------------------------------------------------------------- data

    def refresh(self, results: Dict[int, dict], name_map: Dict[int, str]):
        """Rebuild rows from a fresh compute (manager's _compute_done). Fast
        UI-thread pass off warmed SDE memos; contract-BPC pricing then resolves
        in a worker and back-fills the BPC column."""
        owned_bpo = {b["type_id"] for b in self.bp_db.get_blueprints()
                     if not b["is_copy"]}
        rows = []
        for tid, r in results.items():
            inv = r.get("invention")
            if not inv:
                continue
            name = (name_map.get(tid) or self.names.get_type_name(tid)
                    or str(tid))
            invented_bp = self.sde.get_blueprint_for_item(tid)
            relic = bool(inv.get("relic"))
            sources = [o.get("blueprint_id")
                       for o in inv.get("source_options", [])]
            if not sources and inv.get("source_bp"):
                sources = [inv["source_bp"]]
            owned = (not relic) and any(s in owned_bpo for s in sources)
            profit, margin, basis = self._display_pm(r)
            rows.append({
                "tid": tid, "name": name, "invented_bp": invented_bp,
                "relic": relic, "owned": owned,
                "invent": inv.get("cost_per_run") or None,  # 0 = unpriced
                "profit": profit, "margin": margin, "basis": basis,
                "bpc": None,           # None = resolving; False = none found
                "bpc_stale": False,
            })
        self.rows = rows
        self._fill_tree()
        self._start_bpc_worker()

    @staticmethod
    def _display_pm(r):
        """Profit/margin with the Owned-panel fallback: 7d → 30d → spot (~)."""
        if r.get("has_7d") and r.get("patient_profit") is not None:
            return r["patient_profit"], r["patient_margin"], "7d"
        if r.get("has_30d") and r.get("d30_profit") is not None:
            return r["d30_profit"], r["d30_margin"], "30d"
        if r.get("has_spot") and r.get("spot_profit") is not None:
            return r["spot_profit"], r["spot_margin"], "spot"
        return None, None, None

    def _start_bpc_worker(self):
        if self.contracts_db is None:
            for row in self.rows:
                row["bpc"] = False
            self._fill_tree()
            return
        self._bpc_gen += 1
        gen = self._bpc_gen
        regions = self.hub_regions()   # Tk vars — read on the UI thread
        work = [(row["tid"], row["invented_bp"]) for row in self.rows
                if row["invented_bp"]]
        threading.Thread(target=self._resolve_bpcs,
                         args=(gen, work, regions), daemon=True).start()

    def _resolve_bpcs(self, gen, work, regions):
        """Worker: cheapest cached contract offer per invented blueprint
        (SQLite only), with the ObservedBpcPrices snapshot as stale fallback.
        Live sightings are recorded in one batch (single file write)."""
        out: Dict[int, tuple] = {}   # tid -> (per_run, stale)
        sightings: Dict[int, list] = {}
        for tid, bp in work:
            best, offers_all = None, []
            for rid in regions:
                try:
                    offers = self.contracts_db.find_bpc_offers(bp, rid)
                except Exception as e:
                    _print(f"find_bpc_offers({bp}, {rid}) failed: {e}")
                    continue
                offers_all.extend(offers)
                for o in offers:
                    pr = o["price"] / max(1, o["runs"])
                    if best is None or pr < best:
                        best = pr
            if best is not None:
                out[tid] = (best, False)
                sightings[bp] = offers_all
            else:
                snap = self.bpc_observed.get(bp) if self.bpc_observed else None
                if snap:
                    out[tid] = (snap["avg_per_run"], True)
        if self.bpc_observed and sightings:
            self.bpc_observed.record_many(sightings)
        _print(f"bpc pass: {len(work)} blueprints, "
               f"{sum(1 for v in out.values() if not v[1])} live, "
               f"{sum(1 for v in out.values() if v[1])} stale-avg")
        submit(lambda: self._bpcs_done(gen, out))

    def _bpcs_done(self, gen, out):
        if gen != self._bpc_gen:
            return   # a newer compute superseded this pass
        for row in self.rows:
            got = out.get(row["tid"])
            if got is None:
                row["bpc"] = False
            else:
                row["bpc"], row["bpc_stale"] = got[0], got[1]
        self._fill_tree()

    # ------------------------------------------------------------------- list

    def _visible_rows(self):
        search = self.search_var.get().strip().lower()
        owned_only = self.owned_var.get()
        positive = self.positive_var.get()
        out = []
        for row in self.rows:
            if is_auto_hidden(row["name"]) or self.ignore.contains(row["tid"]):
                continue
            if owned_only and not row["owned"]:
                continue
            if search and search not in row["name"].lower():
                continue
            if positive and (row["profit"] is None or row["profit"] <= 0):
                continue
            out.append(row)
        key = {
            "name": lambda r: r["name"].lower(),
            "src": lambda r: (r["relic"], not r["owned"]),
            "invent": lambda r: r["invent"] if r["invent"] else float("inf"),
            "bpc": lambda r: (r["bpc"] if isinstance(r["bpc"], float)
                              else float("inf")),
            "cheaper": lambda r: self._cheaper(r) or "",
            "profit": lambda r: (r["profit"] if r["profit"] is not None
                                 else float("-inf")),
            "margin": lambda r: (r["margin"] if r["margin"] is not None
                                 else float("-inf")),
        }[self.sort_col]
        out.sort(key=key, reverse=self.sort_reverse)
        return out

    @staticmethod
    def _cheaper(row) -> Optional[str]:
        inv, bpc = row["invent"], row["bpc"]
        has_bpc = isinstance(bpc, float)
        if inv and has_bpc:
            return "BPC" if bpc < inv else "invent"
        if inv:
            return "invent"
        if has_bpc:
            return "BPC"
        return None

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        shown = self._visible_rows()
        for row in shown:
            margin = row["margin"]
            tag = ("good" if margin is not None and margin >= 30 else
                   "ok" if margin is not None and margin >= 10 else
                   "bad" if margin is not None else "")
            if row["bpc"] is None:
                bpc_txt = "…"
            elif row["bpc"] is False:
                bpc_txt = "none"
            else:
                bpc_txt = isk_m(row["bpc"]) + ("~" if row["bpc_stale"] else "")
            sfx = "~" if row["basis"] == "spot" else ""
            self.tree.insert(
                "", "end", iid=str(row["tid"]), text=row["name"],
                tags=(tag,) if tag else (),
                values=(
                    "relic" if row["relic"] else
                    ("✓ owned" if row["owned"] else "✗"),
                    isk_m(row["invent"]) if row["invent"] else "—",
                    bpc_txt,
                    self._cheaper(row) or "—",
                    (isk_m(row["profit"]) + sfx)
                    if row["profit"] is not None else "—",
                    (f"{margin:+.0f}" + sfx) if margin is not None else "—",
                ))
        self.count_label.configure(
            text=f"{len(shown)} of {len(self.rows)} invention items")
        if self.selected is not None and self.tree.exists(str(self.selected)):
            self.tree.selection_set(str(self.selected))

    def _on_sort(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = col in ("invent", "bpc", "profit", "margin")
        self._update_headings()
        self._fill_tree()

    def _update_headings(self):
        for key, base in self._headings.items():
            arrow = ""
            if key == self.sort_col or (key == "#0" and self.sort_col == "name"):
                arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(key, text=base + arrow)

    # ------------------------------------------------------------ interactions

    def _on_select(self, event=None):
        sel = self.tree.selection()
        if sel:
            self.selected = int(sel[0])

    def _on_double(self, event=None):
        sel = self.tree.selection()
        if sel:
            self.show_full_detail(int(sel[0]))

    def _show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        self.selected = int(iid)
        if self._menu is not None:
            self._menu.destroy()
        row = next((r for r in self.rows if r["tid"] == self.selected), None)
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="Full detail (Top Profit)",
                         command=lambda: self.show_full_detail(self.selected))
        if row:
            menu.add_separator()
            menu.add_command(label="Copy name",
                             command=lambda: self._copy(row["name"]))
            menu.add_command(label="Copy type_id",
                             command=lambda: self._copy(str(row["tid"])))
        self._menu = menu
        menu.tk_popup(event.x_root, event.y_root)

    def _copy(self, text: str):
        self.tree.clipboard_clear()
        self.tree.clipboard_append(text)
        self.set_status(f"Copied: {text}")
