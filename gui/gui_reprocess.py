"""Reprocess-or-Sell tab.

Paste a chunk of EVE items, and for each one decide whether to SELL it at the
lowest sell order or REPROCESS it for material value, priced at the currently
selected sell hub. The pure calc lives in `reprocess_engine`; this module is
the view + the ESI/SDE plumbing (prices, skill auto-pull, standings-based
reprocessing tax, SDE re-download).

v1 = general junk (modules / ammo / salvage) via Scrapmetal Processing. Ore /
ice use a different skill set and are shown "not calculated" (a later phase).

Settings row mirrors the ESI-settings UX: auto-pulled values with write-in
overrides and a refresh button. Diagnostics carry `[ReproDiag]`.
"""

import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Callable, Optional

from core.config import get_hub_config
from core.tk_queue import submit
from analytics import reprocess_engine as engine


def _print(msg: str) -> None:
    print(f"[ReproDiag] {msg}")


def _fmt_isk(v) -> str:
    if v is None:
        return "-"
    if v == 0:
        return "0"
    if abs(v) >= 1_000_000_000:
        return f"{v/1_000_000_000:.2f}B"
    if abs(v) >= 1_000_000:
        return f"{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"{v/1_000:.1f}K"
    return f"{v:,.0f}"


def _fmt_vol(v: float) -> str:
    if not v:
        return "-"
    if v >= 1000:
        return f"{v:,.0f} m³"
    return f"{v:,.1f} m³"


# Reprocessing tax: 5% at 0 standing, falling 0.75%/point to 0% at 6.67 with
# the station owner (the better of corp / faction standing wins).
def _tax_from_standing(standing: float) -> float:
    return max(0.0, 0.05 - 0.0075 * standing)


COLUMNS = ("qty", "volume", "sell", "reprocess", "verdict")
COLUMN_TITLES = {
    "qty": "Qty",
    "volume": "Volume",
    "sell": "Sell value",
    "reprocess": "Reprocess",
    "verdict": "Verdict",
}
COLUMN_WIDTHS = {"qty": 70, "volume": 100, "sell": 110, "reprocess": 110, "verdict": 130}

# Human-readable reason shown in the Verdict column when we can't value the
# reprocess path.
FLAG_LABELS = {
    "unmatched": "unmatched",
    "ore_ice": "ore/ice (later)",
    "no_materials": "can't reprocess",
    "below_portion": "below 1 batch",
    "no_price": "no price",
}


class ReprocessTabManager:
    """Reprocess-or-Sell tab: paste -> SDE match -> sell vs reprocess."""

    def __init__(
        self,
        notebook: ttk.Notebook,
        get_client: Callable,
        set_status: Callable[[str], None],
        get_sell_station: Callable[[], str],
        get_esi_skills: Optional[Callable] = None,
        get_esi_standings: Optional[Callable] = None,
        root: Optional[tk.Tk] = None,
    ):
        self.notebook = notebook
        self.get_client = get_client
        self.set_status = set_status
        self.get_sell_station = get_sell_station
        self.get_esi_skills = get_esi_skills
        self.get_esi_standings = get_esi_standings
        self.root = root

        self._evaluating = False
        self._item_rows: dict[str, object] = {}  # tree iid -> ItemResult

        self._build_tab()
        # Best-effort initial fill from any already-cached ESI data.
        self._autofill_from_esi(force=False)

    # ===================================================================== build

    def _build_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Reprocess")
        self.frame = outer

        # ---- settings row ----
        settings = ttk.LabelFrame(outer, text="Settings", padding=8)
        settings.pack(fill=tk.X, padx=8, pady=(8, 4))

        ttk.Label(settings, text="Station base %:").grid(row=0, column=0, sticky="w")
        self.base_var = tk.StringVar(value="50")
        ttk.Entry(settings, textvariable=self.base_var, width=6).grid(
            row=0, column=1, padx=(2, 12))

        ttk.Label(settings, text="Scrapmetal lvl:").grid(row=0, column=2, sticky="w")
        self.scrap_var = tk.StringVar(value="0")
        ttk.Entry(settings, textvariable=self.scrap_var, width=4).grid(
            row=0, column=3, padx=(2, 12))

        ttk.Label(settings, text="Reprocess tax %:").grid(row=0, column=4, sticky="w")
        self.tax_var = tk.StringVar(value="5.0")
        ttk.Entry(settings, textvariable=self.tax_var, width=6).grid(
            row=0, column=5, padx=(2, 12))

        ttk.Button(settings, text="↻ Refresh from ESI",
                   command=lambda: self._autofill_from_esi(force=True)).grid(
            row=0, column=6, padx=(0, 6))
        ttk.Button(settings, text="Download/Update SDE",
                   command=self._on_download_sde).grid(row=0, column=7)

        self.hub_label = ttk.Label(settings, text="", foreground="#888")
        self.hub_label.grid(row=1, column=0, columnspan=8, sticky="w", pady=(6, 0))

        # ---- paste + evaluate ----
        mid = ttk.Frame(outer)
        mid.pack(fill=tk.X, padx=8, pady=4)

        left = ttk.Frame(mid)
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(left, text="Paste items (EVE inventory, multibuy, or "
                             "'Name Qty' per line):").pack(anchor="w")
        self.paste_box = tk.Text(left, height=6, width=50, wrap="none")
        self.paste_box.pack(fill=tk.X, pady=(2, 0))

        right = ttk.Frame(mid)
        right.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        self.eval_btn = ttk.Button(right, text="Evaluate", command=self._on_evaluate)
        self.eval_btn.pack(pady=(18, 4))
        ttk.Button(right, text="Clear", command=self._on_clear).pack()

        # ---- results ----
        res = ttk.Frame(outer)
        res.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))

        self.tree = ttk.Treeview(res, columns=COLUMNS, show="tree headings")
        self.tree.heading("#0", text="Item")
        self.tree.column("#0", width=260, anchor="w")
        for col in COLUMNS:
            self.tree.heading(col, text=COLUMN_TITLES[col])
            anchor = "w" if col == "verdict" else "e"
            self.tree.column(col, width=COLUMN_WIDTHS[col], anchor=anchor)

        vsb = ttk.Scrollbar(res, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.tag_configure("reprocess", foreground="#1a7f37")
        self.tree.tag_configure("sell", foreground="#0a5dc2")
        self.tree.tag_configure("nocalc", foreground="#999999")
        self.tree.tag_configure("material", foreground="#666666")

        # ---- totals ----
        self.totals_var = tk.StringVar(value="")
        ttk.Label(outer, textvariable=self.totals_var, padding=(8, 2)).pack(
            anchor="w")

        self._update_hub_label()

    # ============================================================== ESI autofill

    def _update_hub_label(self):
        try:
            hub = self.get_sell_station()
            name = get_hub_config(hub).get("name", hub)
        except Exception:
            name = "?"
        self.hub_label.configure(text=f"Pricing at lowest sell order in: {name}")

    def _autofill_from_esi(self, force: bool):
        """Fill scrap level + tax from ESI. force=True does a network refresh."""
        self._update_hub_label()
        skills = self.get_esi_skills() if self.get_esi_skills else None
        standings = self.get_esi_standings() if self.get_esi_standings else None
        if skills is None and standings is None:
            if force:
                messagebox.showinfo(
                    "ESI not connected",
                    "Connect ESI (Tracking tab) to auto-pull your Scrapmetal "
                    "Processing level and station standings. You can still type "
                    "the values in manually.")
            return

        if not force:
            self._apply_esi(skills, standings)
            return

        # Network refresh off-thread, then apply on the Tk thread.
        self.set_status("Refreshing reprocessing settings from ESI...")

        def work():
            try:
                if skills is not None:
                    skills.fetch_skills(force_refresh=True, slot="seller")
                if standings is not None:
                    standings.fetch_standings(force_refresh=True, slot="seller")
            except Exception as e:
                _print(f"ESI refresh error: {e}")
            submit(lambda: self._apply_esi(skills, standings, announce=True))

        threading.Thread(target=work, daemon=True).start()

    def _apply_esi(self, skills, standings, announce: bool = False):
        try:
            if skills is not None:
                lvl = skills.get_skill_level("scrapmetal_processing", slot="seller")
                self.scrap_var.set(str(int(lvl)))
            if standings is not None:
                hub = self.get_sell_station()
                corp, faction = standings.get_standings_for_hub(hub, slot="seller")
                standing = max(corp, faction)
                self.tax_var.set(f"{_tax_from_standing(standing) * 100:.1f}")
        except Exception as e:
            _print(f"apply ESI error: {e}")
        if announce:
            self.set_status("Reprocessing settings refreshed from ESI")

    def _read_settings(self) -> Optional[engine.ReprocessSettings]:
        try:
            base = float(self.base_var.get()) / 100.0
            scrap = int(float(self.scrap_var.get()))
            tax = float(self.tax_var.get()) / 100.0
        except (ValueError, TypeError):
            messagebox.showerror(
                "Invalid settings",
                "Station base %, Scrapmetal level and tax % must be numbers.")
            return None
        scrap = max(0, min(5, scrap))
        return engine.ReprocessSettings(
            station_base_rate=base, scrap_level=scrap, reprocess_tax=tax)

    # ================================================================= evaluate

    def _on_clear(self):
        self.paste_box.delete("1.0", tk.END)
        self.tree.delete(*self.tree.get_children())
        self._item_rows.clear()
        self.totals_var.set("")

    def _on_evaluate(self):
        if self._evaluating:
            return
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        if not sde.has_type_materials_data():
            if messagebox.askyesno(
                "SDE missing reprocessing data",
                "This SDE was built before reprocessing yields were added.\n\n"
                "Download/update the SDE now?"):
                self._on_download_sde()
            return

        text = self.paste_box.get("1.0", tk.END)
        if not text.strip():
            self.set_status("Nothing to evaluate — paste some items first.")
            return
        settings = self._read_settings()
        if settings is None:
            return

        hub = self.get_sell_station()
        self._evaluating = True
        self.eval_btn.configure(state="disabled")
        self.set_status("Fetching market prices...")

        def work():
            from gui.gui_async import run_coro_blocking
            prices = {}
            err = None
            try:
                client = self.get_client()
                # Shared helper owns the throwaway loop + aiohttp session and
                # closes both (via close_loop_state) so we don't leak a session
                # per cold-cache Evaluate click.
                orders = run_coro_blocking(
                    client, lambda c: c.get_orders_for_hub(hub, use_cache=True))
                for o in orders:
                    if o.get("is_buy_order"):
                        continue
                    tid = o.get("type_id")
                    price = o.get("price")
                    if tid is None or price is None:
                        continue
                    if tid not in prices or price < prices[tid]:
                        prices[tid] = price
            except Exception as e:
                err = str(e)
            submit(lambda: self._finish_evaluate(text, settings, sde, prices, err))

        threading.Thread(target=work, daemon=True).start()

    def _finish_evaluate(self, text, settings, sde, prices, err):
        self._evaluating = False
        self.eval_btn.configure(state="normal")
        if err:
            self.set_status(f"Price fetch failed: {err}")
            messagebox.showerror("Price fetch failed", err)
            return

        report = engine.evaluate_paste(
            text, settings, sde, lambda tid: prices.get(tid))
        self._paint(report)
        n = len(report.items)
        self.set_status(
            f"Evaluated {n} item(s) — best-path total {_fmt_isk(report.total_best)}")

    def _paint(self, report):
        self.tree.delete(*self.tree.get_children())
        self._item_rows.clear()

        # Matched/valued items first; unmatched names sink to the bottom (still
        # visible) so a real item that failed to match is easy to spot, and
        # renamed containers / labels don't interleave with verdicts.
        matched = [it for it in report.items if "unmatched" not in it.flags]
        unmatched = [it for it in report.items if "unmatched" in it.flags]

        for it in matched:
            self._insert_item(it)

        if unmatched:
            self.tree.insert(
                "", tk.END, text=f"── Unmatched ({len(unmatched)}) ──",
                values=("", "", "", "", ""), tags=("nocalc",))
            for it in unmatched:
                self._insert_item(it)

        uplift = report.reprocess_uplift
        self.totals_var.set(
            f"Sell everything: {_fmt_isk(report.total_sell)}    |    "
            f"Best path: {_fmt_isk(report.total_best)}    "
            f"(+{_fmt_isk(uplift)} from reprocessing)    |    "
            f"Volume: {_fmt_vol(report.total_volume)}")

    def _insert_item(self, it):
        name = it.matched_name or it.input_name
        calc = it.reprocess_calculated
        sell_txt = _fmt_isk(it.sell_value) if it.sell_value is not None else "-"
        rep_txt = _fmt_isk(it.reprocess_net) if calc else "not calculated"

        if it.verdict in ("SELL", "REPROCESS"):
            verdict_txt = it.verdict
            tag = "reprocess" if it.verdict == "REPROCESS" else "sell"
        else:
            # Nothing valued — surface the primary reason instead.
            verdict_txt = FLAG_LABELS.get(
                it.flags[0] if it.flags else "", "—")
            tag = "nocalc"

        iid = self.tree.insert(
            "", tk.END, text=name,
            values=(f"{it.requested_qty:,}", _fmt_vol(it.total_volume),
                    sell_txt, rep_txt, verdict_txt),
            tags=(tag,), open=False)
        self._item_rows[iid] = it

        # Unmatched: offer the closest SDE name as a hint child row.
        if "unmatched" in it.flags and it.suggestion:
            self.tree.insert(iid, tk.END, text=f"  did you mean: {it.suggestion}?",
                             values=("", "", "", "", ""), tags=("material",))

        # Material breakdown (expandable).
        for m in it.materials:
            self.tree.insert(
                iid, tk.END, text=f"  → {m.name}",
                values=(f"{m.quantity:,}", "",
                        _fmt_isk(m.value) if m.unit_sell_price is not None
                        else "no price",
                        "", ""),
                tags=("material",))

    # ================================================================ SDE button

    def _on_download_sde(self):
        from gui.sde_download_dialog import download_sde_with_progress
        download_sde_with_progress(self.frame, self.set_status)
