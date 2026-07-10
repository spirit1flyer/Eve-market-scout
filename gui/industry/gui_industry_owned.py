"""Owned BPO/BPC master list (Phase 3.3 of the Industry tab).

The Top Profit lists answer "of everything, what's worth building." This sub-tab
flips the lens to "of the blueprints I actually own, what are they worth right
now" — using each blueprint's REAL researched ME (not the global ME write-in) and,
for copies, their remaining runs.

Data comes from `industry_blueprints.IndustryBlueprintsDB` (populated by the
Characters tab's Blueprints pull). Costing reuses the Phase 1 engine via a calc
context handed over by `IndustryTabManager` (`build_calc_context`), so an owned
item's breakdown reconciles with its Top Profit row at the same ME.

BPC pricing (amortized blueprint cost) + a runs-based batch cap are Phase 3.4;
this stage costs the build the same way Phase 1 does (blueprint cost ignored).
"""

import threading
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from core.tk_queue import submit
from gui.gui_window_utils import make_scrollable
from industry.industry_ignore import IgnoreList, is_auto_hidden

# Reuse the shared formatting + colour helpers from the main Industry tab.
from gui.industry.gui_industry import (
    isk, isk_m, margin_color,
    CLR_GOOD, CLR_OK, CLR_BAD, CLR_MUTED, CLR_OVERRIDE,
)


def _print(msg: str) -> None:
    print(f"[IndustryOwned] {msg}")


class OwnedBlueprintsPanel:
    """Owned BPO/BPC master list + per-item build breakdown."""

    def __init__(self, parent, bp_db, sde_industry, names, roster,
                 build_context: Callable, set_status: Optional[Callable] = None,
                 bpc_pricing=None, contracts_db=None, on_hubs_changed=None,
                 on_ignore_changed=None, build_time_for=None, on_research=None):
        self.parent = parent
        self.bp_db = bp_db
        self.sde = sde_industry          # SDEIndustryDB (recipes / bp->product)
        self.names = names               # sde_manager (type names)
        self.roster = roster
        self.build_context = build_context   # () -> (calc, fac, fees, params)
        self.set_status = set_status or (lambda m: None)
        self.bpc_pricing = bpc_pricing   # BpcPricing (write-in/contract resolver)
        self.contracts_db = contracts_db # ContractsDB for cached BPC offers
        self.on_hubs_changed = on_hubs_changed  # Phase 3.5: refresh hub selectors
        # Cross-refresh Top Profit too when the shared ignore set changes here;
        # falls back to re-filtering only this panel if no callback was supplied.
        self.on_ignore_changed = on_ignore_changed or self._fill_tree
        # Phase 4: manager-provided build-time calc (uses the 'Built by' char's
        # skills) + research-popup launcher. Owned items pass their REAL ME/TE.
        self.build_time_for = build_time_for
        self.on_research = on_research
        self.ignore = IgnoreList.singleton()
        self._registering = False

        self.rows: List[dict] = []       # per-blueprint display records
        self.results: Dict[int, dict] = {}   # item_id -> calc result (+_bpc)
        self.selected: Optional[int] = None  # selected item_id
        self._chain_rows: Dict[str, int] = {}
        self._iid_seq = 0
        self._params: Optional[dict] = None  # last calc-context params (regions)
        self._chain_menu = None
        self._chain_selected: Optional[int] = None

        self.sort_col = "profit"
        self.sort_reverse = True
        self._build()

    # ----------------------------------------------------------------- layout

    def _build(self):
        bar = ttk.Frame(self.parent, padding=(8, 6))
        bar.pack(fill=tk.X)
        ttk.Label(bar, text="Owned BPO / BPC", font=("Segoe UI", 11, "bold")
                  ).pack(side=tk.LEFT)
        self.count_label = ttk.Label(bar, text="", foreground=CLR_MUTED)
        self.count_label.pack(side=tk.LEFT, padx=10)
        ttk.Button(bar, text="Reload", command=self.refresh).pack(side=tk.RIGHT)
        # Phase 3.5: register player structures that own blueprints as hubs.
        self.struct_btn = ttk.Button(bar, text="Register structures",
                                     command=self._register_structures,
                                     state="disabled")
        self.struct_btn.pack(side=tk.RIGHT, padx=4)
        ttk.Label(bar, text="Pull blueprints from the Characters tab.",
                  foreground=CLR_MUTED).pack(side=tk.RIGHT, padx=8)

        filt = ttk.Frame(self.parent, padding=(8, 0))
        filt.pack(fill=tk.X)
        ttk.Label(filt, text="Show:").pack(side=tk.LEFT)
        self.kind_var = tk.StringVar(value="All")
        kc = ttk.Combobox(filt, values=["All", "BPO only", "BPC only"],
                          textvariable=self.kind_var, width=10, state="readonly")
        kc.pack(side=tk.LEFT, padx=(2, 0))
        kc.bind("<<ComboboxSelected>>", lambda e: self._fill_tree())
        ttk.Label(filt, text="Search:").pack(side=tk.LEFT, padx=(12, 2))
        self.search_var = tk.StringVar()
        se = ttk.Entry(filt, textvariable=self.search_var, width=18)
        se.pack(side=tk.LEFT)
        se.bind("<KeyRelease>", lambda e: self._fill_tree())
        self.ignored_btn = ttk.Button(filt, text="Ignored…",
                                      command=self._manage_ignored)
        self.ignored_btn.pack(side=tk.LEFT, padx=(12, 0))
        ttk.Label(filt, text="  ~ = spot estimate (no 7d history)   "
                             "* = BPC cost not set (margin overstated)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT, padx=(12, 0))
        self._update_ignored_btn()

        main = ttk.Frame(self.parent, padding=8)
        main.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(main)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        cols = ("type", "me", "te", "runs", "char", "cost", "profit", "margin")
        self._headings = {
            "#0": "Item", "type": "BP", "me": "ME", "te": "TE", "runs": "Runs",
            "char": "Character", "cost": "Build cost", "profit": "Profit",
            "margin": "Margin%"}
        tree = ttk.Treeview(left, columns=cols, height=26)
        tree.heading("#0", text="Item", command=lambda: self._on_sort("name"))
        tree.column("#0", width=210)
        widths = {"type": 45, "me": 40, "te": 40, "runs": 60, "char": 110,
                  "cost": 95, "profit": 100, "margin": 70}
        for c in cols:
            anchor = "e" if c in ("me", "te", "runs", "cost", "profit", "margin") else "w"
            tree.heading(c, text=self._headings[c],
                         command=lambda cc=c: self._on_sort(cc))
            tree.column(c, width=widths[c], anchor=anchor)
        sb = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        tree.tag_configure("good", foreground=CLR_GOOD)
        tree.tag_configure("ok", foreground=CLR_OK)
        tree.tag_configure("bad", foreground=CLR_BAD)
        tree.bind("<<TreeviewSelect>>", self._on_select)
        tree.bind("<Double-1>", lambda e: self._show_history())
        tree.bind("<Button-3>", self._show_menu)
        self.tree = tree

        self._menu = tk.Menu(self.parent, tearoff=0)
        self._menu.add_command(label="View price history",
                               command=self._show_history)
        self._menu.add_command(label="Copy name", command=self._copy_name)
        self._menu.add_separator()
        self._menu.add_command(label="Ignore this item",
                               command=self._ignore_selected)

        right = ttk.Frame(main)
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll_host = ttk.Frame(right)
        scroll_host.pack(fill=tk.BOTH, expand=True)
        self.detail = make_scrollable(scroll_host)
        self._detail_canvas = self.detail.master
        ttk.Label(self.detail,
                  text="Pull blueprints (Characters tab), then select one to see "
                       "its build breakdown.",
                  foreground=CLR_MUTED).pack(expand=True)

        self._update_headings()
        self.refresh()

    # ----------------------------------------------------------------- data load

    def refresh(self):
        """Reload owned blueprints from the DB and recompute build cost/profit.

        Fast part (rows + names) renders immediately; the per-item engine pass
        runs in a worker so the UI never blocks on a cold market cache.
        """
        bps = self.bp_db.get_blueprints()
        rows = []
        for bp in bps:
            bp_tid = bp["type_id"]
            product_tid = self.sde.get_product_for_blueprint(bp_tid)
            if product_tid is None:
                continue  # no manufacturing product (research-only/odd BP)
            char = self.roster.get(bp["character_id"])
            rows.append({
                "item_id": bp["item_id"],
                "bp_type_id": bp_tid,
                "product_tid": product_tid,
                "name": self.names.get_type_name(product_tid) or str(product_tid),
                "is_copy": bool(bp["is_copy"]),
                "me": bp["material_efficiency"],
                "te": bp["time_efficiency"],
                "runs": bp["runs"],
                "char": (char.character_name if char else
                         str(bp["character_id"])),
                # owning character — the default "who" for build/research
                # time + invention skills when no per-item Built-by is set
                # (Stage 3 of PLAN_industry_settings.md)
                "char_id": bp["character_id"],
            })
        self.rows = rows
        self.count_label.configure(
            text=f"{len(rows)} blueprints "
                 f"({sum(1 for r in rows if not r['is_copy'])} BPO / "
                 f"{sum(1 for r in rows if r['is_copy'])} BPC)")
        self._fill_tree()
        self._update_struct_button()
        if not rows:
            return

        # Snapshot the calc context on the UI thread (reads Tk vars), then cost
        # each owned blueprint at its real ME — with its amortized BPC cost — in
        # a worker. Costed per item_id (not shared) because BPC cost differs per
        # owned copy (its write-in/contract price ÷ its runs).
        try:
            calc, fac, fees, params = self.build_context()
        except Exception as e:
            _print(f"context build failed: {e}")
            return
        self._params = params  # buy/sell regions for material/product graphs
        regions = [r for r in (params.get("buy_region"),
                               params.get("sell_region")) if r]
        snapshot = [dict(r) for r in rows]  # decouple from UI-thread mutation

        def work():
            results = {}
            for r in snapshot:
                bpc = self._resolve_bpc(r, regions)
                try:
                    res = calc.calc_full(
                        r["product_tid"], fac, fees, me=r["me"], batch=1,
                        blueprint_cost_per_run=bpc["per_run"])
                except Exception as e:
                    _print(f"calc failed for {r['product_tid']}: {e}")
                    res = None
                if res:
                    res["_bpc"] = bpc
                    results[r["item_id"]] = res
            submit(lambda: self._costs_done(results))

        threading.Thread(target=work, daemon=True, name="OwnedBPCompute").start()

    # ----------------------------------------------------------------- structures (3.5)

    def _pending_structures(self) -> dict:
        """{location_id: character_id} for owned-blueprint structures not yet
        registered as hubs."""
        try:
            from core.custom_stations import get_custom_hub_key
            from core.config import TRADE_HUBS
        except Exception:
            return {}
        locs = self.bp_db.get_structure_locations()
        return {loc: cid for loc, cid in locs.items()
                if get_custom_hub_key(loc) not in TRADE_HUBS}

    def _update_struct_button(self):
        if not hasattr(self, "struct_btn"):
            return
        n = len(self._pending_structures())
        if n and not self._registering:
            self.struct_btn.configure(text=f"Register structures ({n})",
                                      state="normal")
        else:
            self.struct_btn.configure(
                text="Registering…" if self._registering else "Register structures",
                state="disabled")

    def _register_structures(self):
        """Resolve each owned-blueprint structure and add it as a custom
        structure hub (Phase 3.5 R8 harvest). Threaded — ESI per structure."""
        if self._registering:
            return
        pending = self._pending_structures()
        if not pending:
            self.set_status("Industry: no new structures to register")
            return
        self._registering = True
        self._update_struct_button()
        self.set_status(f"Industry: registering {len(pending)} structure(s)…")

        def work():
            from industry.industry_blueprints import fetch_structure_meta
            from esi.esi_structures import resolve_region_for_system
            from core.custom_stations import add_custom_station
            added, failed = [], []
            for loc, cid in pending.items():
                headers = self.roster.get_auth_headers(cid)
                meta = fetch_structure_meta(loc, headers)
                if not meta or not meta.get("system_id"):
                    failed.append(loc)
                    continue
                try:
                    region = resolve_region_for_system(meta["system_id"])
                    add_custom_station(
                        {"station_id": loc, "name": meta["name"],
                         "system_id": meta["system_id"], "region_id": region,
                         "corp_id": None},
                        in_stock_market=False, station_type="structure")
                    added.append(meta["name"])
                except Exception as e:
                    _print(f"register {loc} failed: {e}")
                    failed.append(loc)
            submit(lambda: self._structures_done(added, failed))

        threading.Thread(target=work, daemon=True,
                         name="OwnedRegisterStructures").start()

    def _structures_done(self, added, failed):
        self._registering = False
        msg = f"registered {len(added)} structure(s)"
        if failed:
            msg += f", {len(failed)} failed (no docking access?)"
        self.set_status(f"Industry: {msg}")
        if added and self.on_hubs_changed:
            self.on_hubs_changed()
        self._update_struct_button()

    def _resolve_bpc(self, row: dict, regions) -> dict:
        """Amortized blueprint cost for one owned blueprint.

        A BPO amortizes to ~0 (one-time buy, unlimited runs). A BPC resolves via
        BpcPricing: write-in > cached contract offer > unset. For an owned BPC
        with a write-in we prefer ITS remaining runs for amortization.
        """
        if not row["is_copy"] or not self.bpc_pricing:
            return {"per_run": 0.0, "source": "bpo", "price": 0.0,
                    "runs": row.get("runs", 0), "offer_count": 0}
        bpc = self.bpc_pricing.resolve(row["bp_type_id"], regions,
                                       self.contracts_db)
        return bpc

    def _costs_done(self, results: dict):
        self.results = results
        self._fill_tree()
        if self.selected is not None:
            self._show_detail_for_selected()

    def _result_for(self, row: dict) -> Optional[dict]:
        return self.results.get(row["item_id"])

    @staticmethod
    def _display_pm(res):
        """Profit/margin to show in the list, with graceful fallback:
        7-day patient average → 30-day average → spot (current lowest sell order).
        The averages are calendar-window means, so any trade in the last week
        gives a 7d figure; an item with trades only further back falls to 30d;
        a stale-but-listed item (no trades, live sell order) falls to spot.
        Returns (profit, margin, basis) with basis in {"7d","30d","spot",None}.
        """
        if not res:
            return None, None, None
        if res.get("patient_profit") is not None:
            return res["patient_profit"], res["patient_margin"], "7d"
        if res.get("d30_profit") is not None:
            return res["d30_profit"], res["d30_margin"], "30d"
        if res.get("spot_profit") is not None:
            return res["spot_profit"], res["spot_margin"], "spot"
        return None, None, None

    # ----------------------------------------------------------------- list

    def _visible_rows(self) -> List[dict]:
        kind = self.kind_var.get()
        search = self.search_var.get().strip().lower()
        out = []
        for r in self.rows:
            # Auto-hide junk products ("Expired …") + user-ignored items (shared
            # set, keyed by the manufactured product type_id).
            if is_auto_hidden(r["name"]) or self.ignore.contains(r["product_tid"]):
                continue
            if kind == "BPO only" and r["is_copy"]:
                continue
            if kind == "BPC only" and not r["is_copy"]:
                continue
            if search and search not in r["name"].lower():
                continue
            out.append(r)
        out.sort(key=lambda r: self._sort_value(r), reverse=self.sort_reverse)
        return out

    def _sort_value(self, row):
        col = self.sort_col
        res = self._result_for(row)
        if col == "name":
            return row["name"].lower()
        if col == "type":
            return row["is_copy"]
        if col == "me":
            return row["me"]
        if col == "te":
            return row["te"]
        if col == "runs":
            return row["runs"] if row["runs"] >= 0 else 1e18  # BPO infinite last/first
        if col == "char":
            return (row["char"] or "").lower()
        if col == "cost":
            return res["unit_cost"] if res else 1e18
        if col == "profit":
            p, _m, _b = self._display_pm(res)
            return p if p is not None else -1e18
        if col == "margin":
            _p, m, _b = self._display_pm(res)
            return m if m is not None else -1e18
        return 0

    def _fill_tree(self):
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        for r in self._visible_rows():
            res = self._result_for(r)
            profit, margin, basis = self._display_pm(res)
            cost = res["unit_cost"] if res else None
            tag = ("good" if margin is not None and margin >= 30 else
                   "ok" if margin is not None and margin >= 10 else
                   "bad" if margin is not None else "")
            runs_txt = "∞" if r["runs"] < 0 else f"{r['runs']:,}"
            # "~" = spot-based estimate (no 7d history); "*" = BPC cost not set
            # (build cost omits the blueprint → margin overstated).
            sfx = "~" if basis == "spot" else ""
            if res and (res.get("_bpc") or {}).get("source") == "unset":
                sfx += "*"
            self.tree.insert(
                "", "end", iid=str(r["item_id"]), text=r["name"],
                tags=(tag,) if tag else (),
                values=(
                    "BPC" if r["is_copy"] else "BPO",
                    r["me"], r["te"], runs_txt, r["char"],
                    isk_m(cost) if cost is not None else "…",
                    (isk_m(profit) + sfx) if profit is not None else
                    ("…" if not self.results else "—"),
                    (f"{margin:+.0f}" + sfx) if margin is not None else
                    ("…" if not self.results else "—"),
                ))
        if self.selected is not None and self.tree.exists(str(self.selected)):
            self.tree.selection_set(str(self.selected))

    def _on_sort(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            self.sort_reverse = col in ("me", "te", "runs", "cost", "profit",
                                        "margin", "type")
        self._update_headings()
        self._fill_tree()

    def _update_headings(self):
        for key, base in self._headings.items():
            arrow = ""
            if key == self.sort_col or (key == "#0" and self.sort_col == "name"):
                arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(key, text=base + arrow)

    def _row_by_item_id(self, item_id: int) -> Optional[dict]:
        return next((r for r in self.rows if r["item_id"] == item_id), None)

    def _on_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected = int(sel[0])
        self._show_detail_for_selected()

    # ----------------------------------------------------------------- detail

    def _show_detail_for_selected(self):
        row = self._row_by_item_id(self.selected)
        if not row:
            return
        res = self._result_for(row)
        self._clear_detail()
        self._detail_canvas.yview_moveto(0)

        header = ttk.Frame(self.detail)
        header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(header, text=row["name"], font=("Segoe UI", 12, "bold")
                  ).pack(side=tk.LEFT)
        kind = "BPC" if row["is_copy"] else "BPO"
        runs_txt = "∞" if row["runs"] < 0 else f"{row['runs']:,} runs left"
        ttk.Label(header,
                  text=f"  ({kind}, ME {row['me']} / TE {row['te']}, {runs_txt}, "
                       f"owned by {row['char']})",
                  foreground=CLR_MUTED).pack(side=tk.LEFT)

        if not res:
            ttk.Label(self.detail,
                      text="No build cost yet — refresh the Top Profit tab so the "
                           "market caches are populated, then Reload here.",
                      foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=8)
            return

        self._build_bpc(row, res)
        self._build_margins(res)
        self._build_chain(res)
        self._build_totals(res)
        self._build_time_section(res, row)

    def _build_bpc(self, row, res):
        """Blueprint-cost section: BPO amortizes to ~0; BPC shows the resolved
        source (write-in / contract / unset) + a write-in price+runs entry."""
        bpc = res.get("_bpc") or {}
        frame = ttk.LabelFrame(self.detail, text="Blueprint cost", padding=4)
        frame.pack(fill=tk.X, pady=(0, 6))
        if not row["is_copy"]:
            ttk.Label(frame, text="BPO — one-time buy, amortizes to ~0 per unit.",
                      foreground=CLR_MUTED).pack(anchor="w", padx=8)
            return

        source = bpc.get("source", "unset")
        per_run = bpc.get("per_run", 0.0)
        if source == "write-in":
            desc, color = f"your write-in: {isk(bpc['price'])} ÷ {bpc['runs']} runs", CLR_OVERRIDE
        elif source == "contract":
            desc, color = (f"cheapest of {bpc.get('offer_count', 0)} contract "
                           f"offer(s): {isk(bpc['price'])} ÷ {bpc['runs']} runs"), CLR_GOOD
        else:
            desc, color = "NOT SET — build cost omits the BPC (margin overstated)", CLR_BAD
        ttk.Label(frame, text=desc, foreground=color).pack(anchor="w", padx=8)
        self._kv(frame, "Amortized per run:", f"{isk(per_run)} ISK")

        # write-in entry: price + runs + save
        wi = self.bpc_pricing.get_writein(row["bp_type_id"]) if self.bpc_pricing else None
        entry = ttk.Frame(frame)
        entry.pack(fill=tk.X, padx=8, pady=(4, 0))
        ttk.Label(entry, text="Write-in price:").pack(side=tk.LEFT)
        price_var = tk.StringVar(value=f"{wi['price']:.0f}" if wi else "")
        ttk.Entry(entry, textvariable=price_var, width=14).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Label(entry, text="runs:").pack(side=tk.LEFT)
        runs_var = tk.StringVar(value=str(wi["runs"]) if wi
                                else (str(row["runs"]) if row["runs"] > 0 else "1"))
        ttk.Entry(entry, textvariable=runs_var, width=6).pack(side=tk.LEFT, padx=2)
        ttk.Button(entry, text="Save",
                   command=lambda: self._save_bpc_writein(
                       row["bp_type_id"], price_var.get(), runs_var.get())
                   ).pack(side=tk.LEFT, padx=4)
        if wi:
            ttk.Button(entry, text="Clear",
                       command=lambda: self._save_bpc_writein(
                           row["bp_type_id"], "", "1")).pack(side=tk.LEFT)

    def _save_bpc_writein(self, bp_type_id, price_str, runs_str):
        if not self.bpc_pricing:
            return
        try:
            price = float((price_str or "0").replace(",", "").strip())
        except ValueError:
            price = 0.0
        try:
            runs = int((runs_str or "1").strip())
        except ValueError:
            runs = 1
        self.bpc_pricing.set_writein(bp_type_id, price, runs)
        self.set_status("Industry: saved BPC price" if price > 0
                        else "Industry: cleared BPC price")
        self.refresh()  # recompute with the new blueprint cost

    def _clear_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        self._chain_rows.clear()
        self._iid_seq = 0

    def _kv(self, parent, label, value, bold=False, color=None):
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, padx=8, pady=1)
        font = ("Segoe UI", 9, "bold") if bold else ("Segoe UI", 9)
        ttk.Label(row, text=label, font=font,
                  foreground="" if bold else CLR_MUTED).pack(side=tk.LEFT)
        lbl = ttk.Label(row, text=value, font=font)
        if color:
            lbl.configure(foreground=color)
        lbl.pack(side=tk.RIGHT)

    def _build_margins(self, r):
        frame = ttk.LabelFrame(self.detail, text="Profit (per unit)", padding=4)
        frame.pack(fill=tk.X, pady=(0, 6))
        self._kv(frame, "Build cost / unit:", f"{isk(r['unit_cost'])} ISK", bold=True)
        for label, net, profit, margin, empty in (
                ("Spot (current sell)", r.get("spot_net"), r.get("spot_profit"), r.get("spot_margin"), "no sell order"),
                ("Patient (7d list)", r["patient_net"], r["patient_profit"], r["patient_margin"], "no 7d history"),
                ("Immediate (buy order)", r["immediate_net"], r["immediate_profit"], r["immediate_margin"], "no buy order"),
                ("30-day list", r["d30_net"], r["d30_profit"], r["d30_margin"], "no 30d history")):
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, padx=8, pady=(3, 0))
            ttk.Label(row, text=label, width=22).pack(side=tk.LEFT)
            ttk.Label(row, text=f"net {isk(net)}" if net else "net —", width=16,
                      foreground=CLR_MUTED).pack(side=tk.LEFT)
            ptxt = (f"{profit:+,.0f} ({margin:+.1f}%)"
                    if profit is not None else empty)
            ttk.Label(row, text=ptxt, font=("Segoe UI", 9, "bold"),
                      foreground=margin_color(margin)).pack(side=tk.RIGHT)
        sell = r["sell"]
        ttk.Label(frame,
                  text=f"    low-sell {isk(sell.lowest_sell)}   "
                       f"best-buy {isk(sell.highest_buy)}   "
                       f"7d {isk(sell.avg_7d)}   30d {isk(sell.avg_30d)}   "
                       f"({sell.history_days}d history)",
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=(2, 0))

    def _build_chain(self, r):
        frame = ttk.LabelFrame(self.detail, text="Materials (per run, ME-adjusted)",
                               padding=4)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        cols = ("qty", "unit", "total", "src")
        tree = ttk.Treeview(frame, columns=cols, height=12)
        tree.heading("#0", text="Item")
        for c, t, w in (("qty", "Qty", 80), ("unit", "Unit cost", 110),
                        ("total", "Total", 120), ("src", "Source", 80)):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="e" if c != "src" else "center")
        tree.column("#0", width=240)
        sc = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sc.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sc.pack(side=tk.LEFT, fill=tk.Y)
        tree.tag_configure("produced", foreground=CLR_OVERRIDE)
        # Red-flag a material priced 0 (no order in the cached dump → understated
        # cost / inflated profit), and make rows interactive (graph / copy).
        tree.tag_configure("missing", foreground=CLR_BAD)
        tree.bind("<Double-1>", self._on_chain_double)
        tree.bind("<Button-3>", self._show_chain_menu)
        for entry in r["inputs"]:
            self._add_chain_row(tree, "", entry)

    def _add_chain_row(self, tree, parent, entry):
        node = entry["node"]
        tid = node["type_id"]
        name = self.names.get_type_name(tid) or str(tid)
        self._iid_seq += 1
        iid = f"row{self._iid_seq}"
        self._chain_rows[iid] = tid
        src = {"market": "buy", "override": "OVERRIDE",
               "produced": "build"}[node["kind"]]
        missing = node["kind"] == "market" and node["unit_cost"] <= 0
        tags = (("produced",) if node["kind"] == "produced"
                else ("missing",) if missing else ())
        unit_txt = "no price" if missing else isk(node["unit_cost"])
        tree.insert(parent, "end", iid=iid, text=name, open=True,
                    values=(f"{entry['total_qty']:,}", unit_txt,
                            isk(entry["cost"]), src), tags=tags)
        if node["kind"] == "produced":
            for child in node["inputs"]:
                self._add_chain_row(tree, iid, child)

    # -- chain material interactions ----------------------------------------

    def _chain_tid_from_event(self, event):
        tree = event.widget
        iid = tree.identify_row(event.y)
        if not iid:
            return None
        tree.selection_set(iid)
        self._chain_selected = self._chain_rows.get(iid)
        return self._chain_selected

    def _on_chain_double(self, event):
        tid = self._chain_tid_from_event(event)
        if tid is not None:
            self._chain_graph(tid)

    def _chain_graph(self, tid):
        name = self.names.get_type_name(tid) or str(tid)
        region = (self._params or {}).get("buy_region")
        if region is None:
            self.set_status("Industry: refresh first to graph materials")
            return
        try:
            from analytics import graphing
            graphing.show_price_graph(self.parent, tid, name, region)
        except Exception as e:
            self.set_status(f"Industry: graph failed - {e}")

    def _show_chain_menu(self, event):
        tid = self._chain_tid_from_event(event)
        if tid is None:
            return
        if self._chain_menu is None:
            m = tk.Menu(self.parent, tearoff=0)
            m.add_command(label="View price history",
                          command=lambda: self._chain_graph(self._chain_selected))
            m.add_command(label="Copy name",
                          command=lambda: self._copy_text(
                              self.names.get_type_name(self._chain_selected)
                              or str(self._chain_selected)))
            m.add_command(label="Copy type_id",
                          command=lambda: self._copy_text(str(self._chain_selected)))
            self._chain_menu = m
        self._chain_menu.tk_popup(event.x_root, event.y_root)

    def _copy_text(self, text):
        try:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(text)
            self.set_status(f"Industry: copied {text}")
        except tk.TclError:
            pass

    def _build_time_section(self, res, row):
        """Phase 4 build time + research launcher for an owned blueprint, using
        its REAL TE (and ME for research). Delegates the time math to the manager
        (so it uses the 'Built by' character's skills)."""
        if not self.build_time_for and not self.on_research:
            return
        from gui.industry.gui_industry import fmt_duration
        frame = ttk.LabelFrame(self.detail, text="Build time (Phase 4)", padding=4)
        frame.pack(fill=tk.X, pady=(6, 0))

        info = (self.build_time_for(row["product_tid"], res.get("batch", 1),
                                    te=row["te"],
                                    default_char_id=row.get("char_id"))
                if self.build_time_for else None)
        if info and info.get("state") == "ok":
            self._kv(frame, "Per run:", fmt_duration(info["per_run"]))
            note = f"Max runs in 30 days: {info['cap']:,}"
            if info.get("char"):
                note += f"   (skills from {info['char']})"
            elif info.get("skills_state") == "warming":
                note += "   (loading skills…)"
            else:
                note += "   (no 'Built by' char — skills = 0)"
            ttk.Label(frame, text=note, foreground=CLR_MUTED).pack(anchor="w", padx=8)
        elif info and info.get("state") == "no_sde":
            ttk.Label(frame, text="Re-download the SDE (Top Profit → Update SDE) "
                                  "to enable build-time estimates.",
                      foreground=CLR_BAD).pack(anchor="w", padx=8)
        else:
            ttk.Label(frame, text="No base build time for this item.",
                      foreground=CLR_MUTED).pack(anchor="w", padx=8)

        if self.on_research:
            ttk.Button(frame, text="Research…",
                       command=lambda: self.on_research(
                           row["product_tid"], res["eiv"], row["me"], row["te"],
                           default_char_id=row.get("char_id"))
                       ).pack(anchor="w", padx=8, pady=(4, 0))

    def _build_totals(self, r):
        frame = ttk.LabelFrame(self.detail, text="Totals (whole batch)", padding=4)
        frame.pack(fill=tk.X)
        self._kv(frame, "Materials:", f"{isk(r['material_cost'])} ISK")
        self._kv(frame, f"Job install cost (EIV {isk(r['eiv'])}):",
                 f"{isk(r['job_cost'])} ISK")
        bpc_cost = r.get("blueprint_cost", 0.0)
        src = (r.get("_bpc") or {}).get("source", "bpo")
        bpc_note = {"write-in": "write-in", "contract": "contract",
                    "unset": "NOT SET", "bpo": "BPO ~0"}.get(src, "")
        self._kv(frame, f"Blueprint cost ({bpc_note}):", f"{isk(bpc_cost)} ISK",
                 color=CLR_BAD if src == "unset" else None)
        self._kv(frame, f"Total build ({r['units']} units):",
                 f"{isk(r['total_build'])} ISK", bold=True)
        self._kv(frame, "Build cost / unit:", f"{isk(r['unit_cost'])} ISK", bold=True)

    # ----------------------------------------------------------------- misc

    def _show_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.selected = int(iid)
            self._menu.tk_popup(event.x_root, event.y_root)

    def _show_history(self):
        row = self._row_by_item_id(self.selected) if self.selected else None
        if not row:
            return
        try:
            calc, fac, fees, params = self.build_context()
            region = params["sell_region"]
            from analytics import graphing
            graphing.show_price_graph(self.parent, row["product_tid"],
                                      row["name"], region)
        except Exception as e:
            self.set_status(f"Industry: graph failed - {e}")

    def _copy_name(self):
        row = self._row_by_item_id(self.selected) if self.selected else None
        if not row:
            return
        try:
            self.parent.clipboard_clear()
            self.parent.clipboard_append(row["name"])
            self.set_status(f"Industry: copied {row['name']}")
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

    def _ignore_selected(self):
        row = self._row_by_item_id(self.selected) if self.selected else None
        if not row:
            return
        self.ignore.add(row["product_tid"], row["name"])
        self.set_status(f"Industry: ignoring {row['name']}")
        self._update_ignored_btn()
        self.on_ignore_changed()  # re-filter this panel (+ Top Profit if wired)

    def _manage_ignored(self):
        from gui.industry.gui_industry import show_ignore_manager
        def changed():
            self._update_ignored_btn()
            self.on_ignore_changed()
        show_ignore_manager(self.parent, self.ignore, self.names, changed)
