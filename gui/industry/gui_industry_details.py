"""Industry item breakdowns, build/research time and shared display dialogs.

IndustryDetailsMixin uses the owning manager's selected item, calculation
context and character roster. Helpers are also re-exported by gui_industry
for compatibility with the Owned BPO/BPC panel.
"""

import tkinter as tk
from tkinter import ttk
from typing import Dict, Optional

from industry.industry_diagnostics import summarize_results
from industry.industry_engine import (
    BuildTimeParams, manufacturing_time, max_runs_for_time,
    ResearchParams, research_time, research_install_cost,
)
from sde.sde_industry import ACTIVITY_RESEARCH_ME, ACTIVITY_RESEARCH_TE

CLR_GOOD = "#1a7f37"
CLR_OK = "#b45309"
CLR_BAD = "#b91c1c"
CLR_MUTED = "#666666"
CLR_OVERRIDE = "#0369a1"


def _print(msg: str) -> None:
    print(f"[IndustryDiag] {msg}")


def isk(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def isk_m(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.1f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:,.0f}K"
    return f"{value:,.0f}"


def margin_color(margin: Optional[float]) -> str:
    if margin is None:
        return CLR_MUTED
    if margin >= 30:
        return CLR_GOOD
    if margin >= 10:
        return CLR_OK
    return CLR_BAD


class IndustryDetailsMixin:
    """Selected-item details and character-dependent time estimates."""

    # ===================================================================== detail

    def _clear_detail(self):
        for w in self.detail.winfo_children():
            w.destroy()
        self._chain_rows.clear()
        self._iid_seq = 0

    def _show_detail(self, tid: int):
        r = self.results[tid]
        self._clear_detail()
        self._detail_canvas.yview_moveto(0)
        name = self.name_map.get(tid, str(tid))
        summary = summarize_results({tid: r})
        detail_diag = (
            tid, r.get("patient_source") or "unpriced",
            summary["zero_input_rows"], round(float(r["unit_cost"]), 2),
        )
        if detail_diag != self._last_detail_diag:
            self._last_detail_diag = detail_diag
            _print(
                f"detail type_id={tid} "
                f"blueprint_id={self.sde.get_blueprint_for_item(tid)} "
                f"price_basis={detail_diag[1]} inputs={len(r.get('inputs') or [])} "
                f"zero_inputs={summary['zero_input_rows']} "
                f"unit_cost={r['unit_cost']:.2f}"
            )

        header = ttk.Frame(self.detail)
        header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(header, text=name,
                  font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text=f"  (ME {r['me']:.0f} × batch {r['batch']}, "
                               f"{r['output_per_run']}/run → {r['units']} units)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT)

        self._build_built_by(tid)
        self._build_margins(r)
        self._build_chain(r, tid)
        self._build_totals(r)
        self._build_time_section(r, tid)

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
        sell = r["sell"]
        patient_source = r.get("patient_source") or "no history"
        for label, net, profit, margin in (
                (f"Patient ({patient_source} list)", r["patient_net"], r["patient_profit"], r["patient_margin"]),
                ("Immediate (buy order)", r["immediate_net"], r["immediate_profit"], r["immediate_margin"]),
                ("30-day list", r["d30_net"], r["d30_profit"], r["d30_margin"])):
            row = ttk.Frame(frame)
            row.pack(fill=tk.X, padx=8, pady=(3, 0))
            ttk.Label(row, text=label, width=22).pack(side=tk.LEFT)
            ttk.Label(row, text=f"net {isk(net)}", width=16,
                      foreground=CLR_MUTED).pack(side=tk.LEFT)
            ptxt = (f"{profit:+,.0f} ({margin:+.1f}%)"
                    if profit is not None else "no history")
            ttk.Label(row, text=ptxt, font=("Segoe UI", 9, "bold"),
                      foreground=margin_color(margin)).pack(side=tk.RIGHT)
        ttk.Label(frame,
                  text=f"    7d {isk(sell.avg_7d)}   30d {isk(sell.avg_30d)}   "
                       f"90d {isk(sell.avg_90d)}   "
                       f"low-sell {isk(sell.lowest_sell)}   "
                       f"best-buy {isk(sell.highest_buy)}   "
                       f"({sell.history_days}d history)",
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=(2, 0))

    def _build_chain(self, r, tid):
        frame = ttk.LabelFrame(self.detail, text="Materials (whole batch)",
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
        # A material with no buy/sell order in the cached dump prices at 0, which
        # understates build cost (and inflates profit). Flag those rows red so a
        # too-good-to-be-true profit is traceable to a missing input price.
        tree.tag_configure("missing", foreground=CLR_BAD)
        # Materials are interactive: double-click → price graph, right-click →
        # copy / graph (the list-level menu acts on the product, not materials).
        tree.bind("<Double-1>", self._on_chain_double)
        tree.bind("<Button-3>", self._show_chain_menu)

        for entry in r["inputs"]:
            self._add_chain_row(tree, "", entry)

    def _add_chain_row(self, tree, parent, entry):
        node = entry["node"]
        tid = node["type_id"]
        name = self.name_map.get(tid) or self.names.get_type_name(tid) or str(tid)
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
                    values=(f"{entry['total_qty']:,}",
                            unit_txt,
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
        if tid is None:
            return
        self._chain_graph(tid)

    def _chain_graph(self, tid):
        name = self.name_map.get(tid) or self.names.get_type_name(tid) or str(tid)
        # Materials are bought at the input hub, so graph that region.
        region = self._hub_cfg(self.buy_var)["region_id"]
        try:
            from analytics import graphing
            graphing.show_price_graph(self.frame, tid, name, region)
        except Exception as e:
            self.set_status(f"Industry: graph failed - {e}")

    def _show_chain_menu(self, event):
        tid = self._chain_tid_from_event(event)
        if tid is None:
            return
        if getattr(self, "_chain_menu", None) is None:
            m = tk.Menu(self.frame, tearoff=0)
            m.add_command(label="View price history",
                          command=lambda: self._chain_graph(self._chain_selected))
            m.add_command(label="Copy name",
                          command=lambda: self._copy_text(
                              self.name_map.get(self._chain_selected)
                              or self.names.get_type_name(self._chain_selected)
                              or str(self._chain_selected)))
            m.add_command(label="Copy type_id",
                          command=lambda: self._copy_text(str(self._chain_selected)))
            self._chain_menu = m
        self._chain_menu.tk_popup(event.x_root, event.y_root)

    def _copy_text(self, text):
        try:
            self.frame.clipboard_clear()
            self.frame.clipboard_append(text)
            self.set_status(f"Industry: copied {text}")
        except tk.TclError:
            pass

    def _build_totals(self, r):
        frame = ttk.LabelFrame(self.detail, text="Totals (whole batch)", padding=4)
        frame.pack(fill=tk.X)
        self._kv(frame, "Materials:", f"{isk(r['material_cost'])} ISK")
        self._kv(frame, f"Job install cost (EIV {isk(r['eiv'])}):",
                 f"{isk(r['job_cost'])} ISK")
        self._kv(frame, f"Total build ({r['units']} units):",
                 f"{isk(r['total_build'])} ISK", bold=True)
        self._kv(frame, "Build cost / unit:", f"{isk(r['unit_cost'])} ISK", bold=True)

    # ----------------------------------------------------------- build time (P4)

    def _refresh_detail_if_selected(self):
        if self.selected is not None and self.selected in self.results:
            self._show_detail(self.selected)

    def _te_value(self) -> float:
        return max(0.0, min(20.0, _f(self.te_var, 0.0)))

    def _time_bonus(self) -> float:
        return _f(self.time_bonus_var, 0.0)

    def _build_time_params(self, tid: int, te: float):
        """(BuildTimeParams, char_id, skills_state) for an item's build time.

        Skills come from the 'Built by' character's cached manual pull. Missing
        skills stay at zero until the user opens Characters > Skills.
        """
        char_id = self._built_by.get(tid)
        ind = adv = 0
        implant = 0.0
        state = "none"
        if char_id:
            levels = self.char_skills.peek_levels(char_id)
            if levels is not None:
                ind = levels.get("industry", 0)
                adv = levels.get("advanced_industry", 0)
                state = "cached"
            else:
                state = "not_pulled"
            rc = self.roster.get(char_id)
            if rc:
                implant = rc.implant_mfg_pct
        params = BuildTimeParams(te=te, industry_level=ind, adv_industry_level=adv,
                                 facility_time_bonus_pct=self._time_bonus(),
                                 implant_time_pct=implant)
        return params, char_id, state

    def _build_time_section(self, r, tid: int):
        frame = ttk.LabelFrame(self.detail, text="Build time (Phase 4)", padding=4)
        frame.pack(fill=tk.X, pady=(6, 0))
        if not self.sde.has_activity_time_data():
            ttk.Label(frame, text="Re-download the SDE (Update SDE) to enable "
                                  "build-time estimates.",
                      foreground=CLR_BAD).pack(anchor="w", padx=8)
            return
        base = self.sde.get_base_build_time(tid)
        if not base:
            ttk.Label(frame, text="No base build time for this item.",
                      foreground=CLR_MUTED).pack(anchor="w", padx=8)
            return

        te = self._te_value()
        params, char_id, state = self._build_time_params(tid, te)
        t = manufacturing_time(float(base), r["batch"], params)
        per_run = t["per_run_seconds"]
        self._kv(frame, "Per run:", fmt_duration(per_run))
        self._kv(frame, f"Total (batch {r['batch']}):",
                 fmt_duration(t["total_seconds"]))

        cap = max_runs_for_time(per_run)
        cap_row = ttk.Frame(frame)
        cap_row.pack(fill=tk.X, padx=8, pady=(2, 0))
        ttk.Label(cap_row, text=f"Max runs in 30 days: {cap:,}",
                  foreground=CLR_MUTED).pack(side=tk.LEFT)
        ttk.Button(cap_row, text="Use as batch",
                   command=lambda c=cap: self._apply_batch_cap(c)).pack(side=tk.LEFT, padx=6)
        ttk.Button(cap_row, text="Research…",
                   command=lambda: self.open_research(tid, r["eiv"], 0, 0)
                   ).pack(side=tk.RIGHT)

        # Provenance: which character's manually pulled skills and TE were used.
        if char_id and state == "cached":
            rc = self.roster.get(char_id)
            who = (rc.character_name if rc else str(char_id))
            note = f"TE {te:.0f}, skills from {who}"
        elif state == "not_pulled":
            note = f"TE {te:.0f}, skills not pulled (Characters > Skills)"
        else:
            note = f"TE {te:.0f}, no 'Built by' char (skills = 0)"
        ttk.Label(frame, text="    " + note,
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=(2, 0))

    def _apply_batch_cap(self, cap: int):
        self.batch_var.set(str(int(cap)))
        self._compute(refetch=False)

    def build_time_for(self, tid: int, batch: int, te=None) -> dict:
        """Build-time summary for the Owned panel (which passes the blueprint's
        real TE). Reuses the same skills/implant/facility path as Top Profit.
        Returns {state, per_run, total, cap, ...}; state in
        {ok, no_sde, no_base}."""
        if not self.sde.has_activity_time_data():
            return {"state": "no_sde"}
        base = self.sde.get_base_build_time(tid)
        if not base:
            return {"state": "no_base"}
        te_val = self._te_value() if te is None else max(0.0, min(20.0, float(te)))
        params, char_id, state = self._build_time_params(tid, te_val)
        t = manufacturing_time(float(base), batch, params)
        rc = self.roster.get(char_id) if char_id else None
        return {"state": "ok", "per_run": t["per_run_seconds"],
                "total": t["total_seconds"],
                "cap": max_runs_for_time(t["per_run_seconds"]),
                "te": te_val, "skills_state": state,
                "char": (rc.character_name if rc else None)}

    # ----------------------------------------------------------- research (4.3)

    def open_research(self, tid: int, eiv: float, cur_me: int, cur_te: int):
        """Open the ME/TE research popup for an item. Shared by Top Profit and
        the Owned panel (which passes the blueprint's real current ME/TE)."""
        ctx = self._research_context(tid, eiv, cur_me, cur_te)
        _print(
            f"research type_id={tid} character_id={ctx['char_id'] or 'none'} "
            f"skills_cached={ctx['skills_cached']} cur_me={cur_me} cur_te={cur_te} "
            f"base_me={ctx['base_me']} base_te={ctx['base_te']} "
            f"me_index={ctx['me_index']:.6f} te_index={ctx['te_index']:.6f}"
        )
        show_research_popup(self.frame, ctx, self.set_status)

    def _research_context(self, tid: int, eiv: float,
                          cur_me: int, cur_te: int) -> dict:
        bp = self.sde.get_blueprint_for_item(tid)
        base_me = self.sde.get_activity_time(bp, ACTIVITY_RESEARCH_ME) if bp else None
        base_te = self.sde.get_activity_time(bp, ACTIVITY_RESEARCH_TE) if bp else None
        p = self._params()
        sys = p["facility_system"]
        char_id = self._built_by.get(tid)
        levels = self.char_skills.peek_levels(char_id) if char_id else None
        rc = self.roster.get(char_id) if char_id else None
        rparams = ResearchParams(
            metallurgy_level=(levels or {}).get("metallurgy", 0),
            research_level=(levels or {}).get("research", 0),
            adv_industry_level=(levels or {}).get("advanced_industry", 0),
            facility_time_bonus_pct=self._time_bonus(),
            implant_me_pct=rc.implant_me_pct if rc else 0.0,
            implant_te_pct=rc.implant_te_pct if rc else 0.0)
        return {
            "name": self.name_map.get(tid, str(tid)),
            "base_me": base_me, "base_te": base_te,
            "eiv": eiv,
            "me_index": self.market.cost_index(sys, "research_material_efficiency"),
            "te_index": self.market.cost_index(sys, "research_time_efficiency"),
            "fac": self._facility_from(p),
            "rparams": rparams,
            "cur_me": int(cur_me), "cur_te": int(cur_te),
            "skills_cached": levels is not None,
            "char_id": char_id,
            "char_name": (rc.character_name if rc else None),
            "has_time_data": self.sde.has_activity_time_data(),
        }

    def _build_built_by(self, tid: int):
        """Phase 2.5 'Built by' selector: pick a roster character for this item.
        Persisted, but INERT until Phase 4 (only affects build/research time)."""
        bb = ttk.Frame(self.detail)
        bb.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(bb, text="Built by:", foreground=CLR_MUTED).pack(side=tk.LEFT)
        chars = self.roster.characters
        name_by_id = {c.character_id: (c.character_name or str(c.character_id))
                      for c in chars}
        values = ["(none)"] + list(name_by_id.values())
        cur_id = self._built_by.get(tid)
        var = tk.StringVar(value=name_by_id.get(cur_id, "(none)"))
        combo = ttk.Combobox(bb, values=values, textvariable=var, width=20,
                             state="readonly")
        combo.pack(side=tk.LEFT, padx=4)
        combo.bind("<<ComboboxSelected>>",
                   lambda e, t=tid, v=var: self._on_built_by(t, v.get()))
        ttk.Label(bb, text="(skills/implants set build + research time)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT, padx=4)

    def _on_built_by(self, tid: int, name: str):
        if name == "(none)":
            self._built_by.pop(tid, None)
        else:
            cid = next((c.character_id for c in self.roster.characters
                        if (c.character_name or str(c.character_id)) == name), None)
            if cid is not None:
                self._built_by[tid] = cid
        self._save_built_by()
        self._refresh_detail_if_selected()  # build/research time depends on it

    def _built_by_path(self):
        from core.sound_manager import get_data_dir
        return get_data_dir() / "industry_built_by.json"

    def _load_built_by(self) -> Dict[int, int]:
        import json
        try:
            with open(self._built_by_path()) as f:
                raw = json.load(f)
            return {int(k): int(v) for k, v in raw.items()}
        except Exception:
            return {}

    def _save_built_by(self):
        import json
        try:
            with open(self._built_by_path(), "w") as f:
                json.dump({str(k): v for k, v in self._built_by.items()}, f)
        except Exception as e:
            _print(f"built_by save failed: {e}")


def show_ignore_manager(parent, ignore_list, names, on_change):
    """Modal to review + un-ignore items. Shared by Top Profit and Owned panels.

    `ignore_list` is an `IgnoreList`; `names` is the sde_manager (to freshen the
    display name); `on_change` is called after any removal so the caller can
    re-filter its list(s).
    """
    from gui.gui_window_utils import fit_window

    win = tk.Toplevel(parent)
    win.title("Ignored items")
    win.transient(parent.winfo_toplevel())

    ttk.Label(win, text="Items hidden from both Industry lists. "
                        "Select and remove to show them again.",
              foreground=CLR_MUTED, padding=(8, 6)).pack(anchor="w")

    body = ttk.Frame(win, padding=8)
    body.pack(fill=tk.BOTH, expand=True)
    tree = ttk.Treeview(body, columns=("tid",), height=14, selectmode="extended")
    tree.heading("#0", text="Item")
    tree.heading("tid", text="type_id")
    tree.column("#0", width=260)
    tree.column("tid", width=80, anchor="e")
    sb = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    sb.pack(side=tk.LEFT, fill=tk.Y)

    def reload():
        tree.delete(*tree.get_children())
        for tid, name in ignore_list.all():
            fresh = (names.get_type_name(tid) if names else None) or name
            tree.insert("", "end", iid=str(tid), text=fresh, values=(tid,))

    def remove_selected():
        for iid in tree.selection():
            ignore_list.remove(int(iid))
        reload()
        on_change()

    def clear_all():
        for tid, _name in ignore_list.all():
            ignore_list.remove(tid)
        reload()
        on_change()

    btns = ttk.Frame(win, padding=(8, 4))
    btns.pack(fill=tk.X)
    ttk.Button(btns, text="Remove selected", command=remove_selected
               ).pack(side=tk.LEFT)
    ttk.Button(btns, text="Clear all", command=clear_all).pack(side=tk.LEFT, padx=4)
    ttk.Button(btns, text="Close", command=win.destroy).pack(side=tk.RIGHT)

    reload()
    fit_window(win, min_width=420, min_height=320)


def fmt_duration(seconds: float) -> str:
    """Human-readable d/h/m/s duration. '—' for non-positive (no time data)."""
    s = int(round(seconds))
    if s <= 0:
        return "—"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    if not parts:
        parts.append(f"{sec}s")
    return " ".join(parts)


def show_research_popup(parent, ctx: dict, set_status):
    """ME/TE research time + cost popup (Phase 4.3). `ctx` from
    `IndustryTabManager._research_context`. Shared by Top Profit + Owned panels.

    Research TIME uses the per-level table in `industry_engine` (⚠ CCP-tuned —
    verify in-game); research COST reuses the research-activity cost index and is
    modelled per-job (level-independent), also flagged for verification.
    """
    from gui.gui_window_utils import fit_window

    win = tk.Toplevel(parent)
    win.title(f"Research — {ctx['name']}")
    win.transient(parent.winfo_toplevel())

    if not ctx.get("has_time_data"):
        ttk.Label(win, text="Re-download the SDE (Update SDE) to enable research "
                            "time estimates.", foreground=CLR_BAD,
                  padding=12).pack()
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 8))
        fit_window(win, min_width=360, min_height=120)
        return

    rp = ctx["rparams"]
    fac = ctx["fac"]
    eiv = ctx["eiv"]
    cur_me, cur_te = ctx["cur_me"], ctx["cur_te"]

    hdr = ttk.Frame(win, padding=(10, 8))
    hdr.pack(fill=tk.X)
    skills_txt = (f"skills from {ctx['char_name']}" if ctx.get("skills_cached")
                  and ctx.get("char_name") else
                  "no 'Built by' char — skills = 0 (set one on the item)")
    ttk.Label(hdr, text=f"{ctx['name']}   (current ME {cur_me} / TE {cur_te})",
              font=("Segoe UI", 11, "bold")).pack(anchor="w")
    ttk.Label(hdr, text=skills_txt, foreground=CLR_MUTED).pack(anchor="w")

    body = ttk.Frame(win, padding=(10, 4))
    body.pack(fill=tk.BOTH, expand=True)

    # ---- ME row ----
    me_frame = ttk.LabelFrame(body, text="Material Efficiency research", padding=6)
    me_frame.pack(fill=tk.X, pady=(0, 6))
    me_targets = [str(l) for l in range(cur_me + 1, 11)]
    me_time_lbl = ttk.Label(me_frame, text="—")
    me_cost_lbl = ttk.Label(me_frame, text="—")
    if me_targets:
        row = ttk.Frame(me_frame); row.pack(fill=tk.X)
        ttk.Label(row, text=f"ME {cur_me} → ").pack(side=tk.LEFT)
        me_target_var = tk.StringVar(value=me_targets[-1])
        ttk.Combobox(row, values=me_targets, textvariable=me_target_var, width=4,
                     state="readonly").pack(side=tk.LEFT)
        ttk.Label(row, text="   Time:").pack(side=tk.LEFT, padx=(8, 2))
        me_time_lbl.pack(in_=row, side=tk.LEFT)
        ttk.Label(row, text="   Cost:").pack(side=tk.LEFT, padx=(8, 2))
        me_cost_lbl.pack(in_=row, side=tk.LEFT)
    else:
        me_target_var = None
        ttk.Label(me_frame, text="Already at ME 10.",
                  foreground=CLR_MUTED).pack(anchor="w")

    # ---- TE row (optional) ----
    te_frame = ttk.LabelFrame(body, text="Time Efficiency research", padding=6)
    te_frame.pack(fill=tk.X, pady=(0, 6))
    te_targets = [str(l) for l in range(cur_te + 1, 11)]
    te_enabled_var = tk.BooleanVar(value=False)
    te_time_lbl = ttk.Label(te_frame, text="—")
    te_cost_lbl = ttk.Label(te_frame, text="—")
    if te_targets:
        row = ttk.Frame(te_frame); row.pack(fill=tk.X)
        ttk.Checkbutton(row, text="also research TE", variable=te_enabled_var
                        ).pack(side=tk.LEFT)
        ttk.Label(row, text=f"   TE {cur_te} → ").pack(side=tk.LEFT)
        te_target_var = tk.StringVar(value=te_targets[-1])
        ttk.Combobox(row, values=te_targets, textvariable=te_target_var, width=4,
                     state="readonly").pack(side=tk.LEFT)
        ttk.Label(row, text="   Time:").pack(side=tk.LEFT, padx=(8, 2))
        te_time_lbl.pack(in_=row, side=tk.LEFT)
        ttk.Label(row, text="   Cost:").pack(side=tk.LEFT, padx=(8, 2))
        te_cost_lbl.pack(in_=row, side=tk.LEFT)
    else:
        te_target_var = None
        ttk.Label(te_frame, text="Already at TE 10.",
                  foreground=CLR_MUTED).pack(anchor="w")

    totals = ttk.Frame(body); totals.pack(fill=tk.X, pady=(2, 0))
    total_lbl = ttk.Label(totals, text="", font=("Segoe UI", 9, "bold"))
    total_lbl.pack(anchor="w")

    base_me = ctx.get("base_me")
    base_te = ctx.get("base_te")

    def recompute(*_):
        t_total = c_total = 0.0
        if me_target_var is not None and base_me:
            tgt = int(me_target_var.get())
            tme = research_time(float(base_me), cur_me, tgt, kind="me", params=rp)
            cme = research_install_cost(eiv, ctx["me_index"], fac)
            me_time_lbl.configure(text=fmt_duration(tme))
            me_cost_lbl.configure(text=f"{isk(cme)} ISK")
            t_total += tme; c_total += cme
        elif me_target_var is not None:
            me_time_lbl.configure(text="no data")
        if te_target_var is not None and te_enabled_var.get() and base_te:
            tgt = int(te_target_var.get())
            tte = research_time(float(base_te), cur_te, tgt, kind="te", params=rp)
            cte = research_install_cost(eiv, ctx["te_index"], fac)
            te_time_lbl.configure(text=fmt_duration(tte))
            te_cost_lbl.configure(text=f"{isk(cte)} ISK")
            t_total += tte; c_total += cte
        else:
            te_time_lbl.configure(text="—")
            te_cost_lbl.configure(text="—")
        total_lbl.configure(text=f"Total: {fmt_duration(t_total)}   "
                                 f"{isk(c_total)} ISK")

    for var in (me_target_var, te_target_var):
        if var is not None:
            var.trace_add("write", recompute)
    te_enabled_var.trace_add("write", recompute)
    recompute()

    ttk.Label(win, text="⚠ Research time/cost are CCP-tuned estimates — verify "
                        "one blueprint in-game. Cost is modelled per job "
                        "(level-independent).",
              foreground=CLR_MUTED, wraplength=460, padding=(10, 4)).pack(anchor="w")
    ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 8))
    fit_window(win, min_width=480, min_height=300)


def _f(var: tk.StringVar, default: float) -> float:
    try:
        return float(var.get().strip().replace(",", ""))
    except (ValueError, AttributeError):
        return default
