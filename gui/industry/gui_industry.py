"""Industry tab manager, data acquisition coordination and calculation context.

The browser/layout/filtering lives in gui_industry_browser; selected-item
breakdowns, build time and dialogs live in gui_industry_details. Calculation
rules remain in industry_engine and data comes from industry_market_data.
Shared display helpers remain importable here for the Owned BPO/BPC panel.
"""

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from core.tk_queue import submit
from core.config import get_enabled_hubs, get_hub_config
from sde.sde_industry import get_sde_industry_db
from sde.sde_manager import get_sde_manager
from industry.industry_engine import IndustryCalculator, SellFees, FacilityParams
from industry.industry_market_data import (
    IndustryMarketData, IndustryProvider, JobCostConstants, BpcPricing,
)
from industry.industry_diagnostics import format_counts, summarize_results
from industry.industry_characters import IndustryRoster
from industry.industry_skills import IndustrySkills
from industry.industry_standings import IndustryStandings
from industry.industry_blueprints import IndustryBlueprintsDB, BlueprintPuller
from industry.industry_ignore import IgnoreList
from gui.industry.gui_industry_browser import (
    IndustryBrowserMixin, CATEGORIES, DEFAULT_TECH, TECH_ORDER, tech_label,
)
# Preserve the helper imports used by the existing Owned BPO/BPC panel.
from gui.industry.gui_industry_details import (
    IndustryDetailsMixin, CLR_GOOD, CLR_OK, CLR_BAD, CLR_MUTED, CLR_OVERRIDE,
    isk, isk_m, margin_color, fmt_duration, show_ignore_manager,
    show_research_popup, _f, _print,
)

# Mineral type_ids. A manufacturable item whose entire recipe is a single
# 1-unit mineral is a CCP placeholder ("dummy") recipe — special-edition ships
# (Gnosis, Praxis, faction Catalysts, …) that aren't normally buildable and
# whose ~1-ISK build cost produces absurd fake profit. Excluded at compute.
MINERALS = frozenset({34, 35, 36, 37, 38, 39, 40, 11399})


def _is_dummy_recipe(recipe: Optional[dict]) -> bool:
    """True for a single 1-unit-mineral placeholder recipe (see MINERALS)."""
    if not recipe:
        return False
    mats = recipe["materials"]
    return (len(mats) == 1 and mats[0][1] == 1 and mats[0][0] in MINERALS)


# Market-group ancestry substrings that mark big-investment classes the user
# usually wants hidden. Capital catches capital ships + capital components/rigs/
# modules (incl. "anticapital"). Upwell hulls are matched by their four
# unambiguous hull group names — NOT the bare word "structure", which also
# appears in module groups like "Nanofiber Internal Structures".
CAPITAL_MATCHERS = ("capital",)
# Upwell structures (Citadels/Engineering Complexes/Refineries/FLEX) vs the
# older POS gear (Control Towers/arrays/silos under "Starbase Structures").
# Kept separate so each can be hidden independently — NOT the bare word
# "structure", which also tags module groups like "Nanofiber Internal Structures".
UPWELL_MATCHERS = ("citadels", "engineering complexes", "refineries",
                   "flex structures")
POS_MATCHERS = ("starbase structures",)


class IndustryTabManager(IndustryBrowserMixin, IndustryDetailsMixin):
    """Coordinate the Industry page and its existing calculation lifecycle."""

    def __init__(self, notebook: ttk.Notebook,
                 get_client: Callable,
                 set_status: Optional[Callable[[str], None]] = None,
                 root: Optional[tk.Tk] = None):
        self.notebook = notebook
        self.set_status = set_status or (lambda msg: None)
        self.root = root

        self.sde = get_sde_industry_db()
        self.names = get_sde_manager()
        self.market = IndustryMarketData(get_client)
        self.constants = JobCostConstants.load()

        # Phase 2: industry character roster (separate from trading auth).
        self.roster = IndustryRoster.singleton()
        self.char_skills = IndustrySkills(self.roster)
        self.char_standings = IndustryStandings(self.roster, self.char_skills)

        # Phase 3: owned BPO/BPC store + ESI puller + BPC pricing (3.4).
        self.bp_db = IndustryBlueprintsDB.singleton()
        self.bp_puller = BlueprintPuller(self.roster, self.bp_db)
        self.bpc_pricing = BpcPricing()
        try:
            from contracts.contracts_db import ContractsDB
            self.contracts_db = ContractsDB.singleton()
        except Exception as e:
            _print(f"contracts_db unavailable: {e}")
            self.contracts_db = None

        self.ignore = IgnoreList.singleton()     # shared with Owned panel
        self.results: Dict[int, dict] = {}       # tid -> calc result
        self.name_map: Dict[int, str] = {}       # tid -> name
        self.category_map: Dict[int, str] = {}   # tid -> category label
        self.meta_map: Dict[int, Optional[int]] = {}  # tid -> meta_group_id
        self.bpo_map: Dict[int, bool] = {}       # tid -> has buyable BPO (else BPC-only)
        self.capital_map: Dict[int, bool] = {}   # tid -> is capital-class (excl. by sub-cap filter)
        self.upwell_map: Dict[int, bool] = {}    # tid -> is Upwell structure hull (excl. by Hide Upwell)
        self.pos_map: Dict[int, bool] = {}       # tid -> is POS/starbase gear (excl. by Hide POS)
        self.tech_selected = set(DEFAULT_TECH)   # active tech-level buckets
        self.display_order: List[int] = []       # filtered+sorted tids
        self.selected: Optional[int] = None
        # Phase 2.5: per-item "Built by" roster selection. Persists but is
        # INTENTIONALLY INERT until Phase 4 (its only effect is build/research
        # time, which doesn't exist yet) — the no-op is by design, not a bug.
        self._built_by: Dict[int, int] = self._load_built_by()

        self._computing = False
        self._last_refresh = 0.0
        self._chain_rows: Dict[str, int] = {}    # tree iid -> type_id
        self._iid_seq = 0
        self._active_category = "All"
        self._compute_run_seq = 0
        self._last_filter_diag = None
        self._filter_diag_after = None
        self._last_detail_diag = None
        self._pending_sde_recompute = False
        self.sort_col = "profit"
        self.sort_reverse = True

        self._build_tab()
        if self.sde.is_available():
            self.frame.after(800, lambda: self._compute(refetch=True))
        else:
            self.progress_label.configure(
                text="Industry SDE not downloaded — use the Reprocess tab's "
                     "Download/Update SDE button.")
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed,
                           add="+")

    def build_calc_context(self):
        """Snapshot a calc context (calc, facility, fees, params) from the current
        top-bar selections. Called on the UI thread (reads Tk vars); the returned
        objects are plain so a worker can cost items off-thread. Shared with the
        Owned BPO/BPC panel so its breakdowns reconcile with Top Profit.
        """
        p = self._params()
        fac = self._facility_from(p)
        provider = self._provider_from(p)
        calc = IndustryCalculator(provider, buildable=lambda t: False)
        return calc, fac, self._fees(), p

    # ===================================================================== params

    def _hub_cfg(self, var: tk.StringVar) -> dict:
        return get_hub_config(self._hub_key_by_name.get(var.get(), "jita"))

    def _params(self) -> dict:
        """Snapshot all Tk-var inputs as plain values, so the worker thread can
        build the facility/provider AFTER a refetch without touching Tk."""
        buy = self._hub_cfg(self.buy_var)
        sell = self._hub_cfg(self.sell_var)
        return {
            "me": _f(self.me_var, 10.0),
            "batch": max(1, int(_f(self.batch_var, 1.0))),
            "buy_station": buy["station_id"],
            "buy_region": buy["region_id"],
            "sell_station": sell["station_id"],
            "sell_region": sell["region_id"],
            "facility_system": self._hub_cfg(self.facility_var)["system_id"],
            "input_side": ("patient" if self.input_var.get().startswith("Patient")
                           else "impatient"),
            "cost_bonus": _f(self.cost_bonus_var, 0.0),
            "mat_bonus": _f(self.mat_bonus_var, 0.0),
            "time_bonus": _f(self.time_bonus_var, 0.0),
            "fac_tax": _f(self.fac_tax_var, self.constants.facility_tax_pct),
            "scc": _f(self.scc_var, self.constants.scc_surcharge_pct),
        }

    def _facility_from(self, p: dict) -> FacilityParams:
        # cost_index read here (non-Tk) so it reflects a just-completed refetch.
        return FacilityParams(
            system_cost_index=self.market.cost_index(p["facility_system"], "manufacturing"),
            cost_bonus_pct=p["cost_bonus"], material_bonus_pct=p["mat_bonus"],
            facility_tax_pct=p["fac_tax"], scc_surcharge_pct=p["scc"],
            alpha_clone_tax_pct=self.constants.alpha_clone_tax_pct)

    def _provider_from(self, p: dict) -> IndustryProvider:
        return IndustryProvider(
            self.market, self.sde,
            buy_station=p["buy_station"], sell_station=p["sell_station"],
            sell_region=p["sell_region"], facility_system=p["facility_system"],
            input_side=p["input_side"], buy_region=p["buy_region"])

    def _fees(self) -> SellFees:
        # Fees are skill-dependent in EVE; Phase 1 uses sensible defaults.
        # (Wire to calculate.TradingSkills later if needed.)
        return SellFees(broker_fee_pct=3.0, sales_tax_pct=4.5)

    # ===================================================================== compute

    def _compute(self, refetch: bool):
        if self._computing:
            _print(f"compute_skip reason=busy refetch={refetch}")
            return
        if not self.sde.is_available():
            self.progress_label.configure(text="Industry SDE not downloaded.")
            return
        self._computing = True
        self.refresh_btn.configure(state="disabled")
        self.progress_label.configure(
            text="Refreshing…" if refetch else "Computing…")

        p = self._params()
        fees = self._fees()
        need_meta = not self.name_map
        self._compute_run_seq += 1
        run_id = self._compute_run_seq
        started = time.perf_counter()
        diag_context = {
            "buy": self.buy_var.get(), "sell": self.sell_var.get(),
            "facility": self.facility_var.get(),
            "input": self.input_var.get(), "me": p["me"],
            "batch": p["batch"],
        }

        def _work():
            err = None
            note = ""
            raw_count = 0
            dummy_count = 0
            try:
                products = self.sde.get_all_manufacturable_items()
                raw_count = len(products)
                # gather all type_ids needing prices; drop CCP placeholder
                # 1-unit-mineral recipes (special-edition ships → fake profit).
                tids = set(products)
                dummies = set()
                for tid in products:
                    r = self.sde.get_recipe(tid)
                    if r:
                        tids.update(m for m, _q in r["materials"])
                    if _is_dummy_recipe(r):
                        dummies.add(tid)
                products = [t for t in products if t not in dummies]
                dummy_count = len(dummies)
                _print(
                    f"compute_start run={run_id} refetch={refetch} "
                    f"products={raw_count} usable={len(products)} "
                    f"dummy={dummy_count} buy={diag_context['buy']!r} "
                    f"sell={diag_context['sell']!r} "
                    f"facility={diag_context['facility']!r} "
                    f"input={diag_context['input']!r} me={diag_context['me']} "
                    f"batch={diag_context['batch']}"
                )

                if refetch:
                    systems = [get_hub_config(k)["system_id"]
                               for k, _ in get_enabled_hubs()]
                    hubs = [(get_hub_config(k)["region_id"],
                             get_hub_config(k)["station_id"])
                            for k, _ in get_enabled_hubs()]
                    note = self.market.refresh_all(
                        list(tids), systems, hubs, products,
                        callback=lambda m, c, t: submit(
                            lambda mm=m: self.progress_label.configure(text=mm)))

                if need_meta:
                    self._build_metadata(products)

                # Build facility/provider AFTER any refetch so cost index is fresh.
                fac = self._facility_from(p)
                provider = self._provider_from(p)
                calc = IndustryCalculator(provider, buildable=lambda t: False)
                results = {}
                for tid in products:
                    res = calc.calc_full(tid, fac, fees,
                                         me=p["me"], batch=p["batch"])
                    if res:
                        results[tid] = res
            except Exception as e:
                err = str(e)
                _print(f"compute failed: {e}")
                import traceback
                traceback.print_exc()
                results = {}
            summary = summarize_results(results)
            _print(
                f"compute_done run={run_id} "
                f"status={'failed' if err else 'ok'} "
                f"seconds={time.perf_counter() - started:.2f} "
                f"products={raw_count} dummy={dummy_count} "
                f"results={len(results)} "
                f"price_sources={format_counts(summary['price_sources'])} "
                f"zero_input_products={summary['zero_input_products']} "
                f"zero_input_rows={summary['zero_input_rows']}"
            )
            submit(lambda: self._compute_done(results, err, note, refetch))

        threading.Thread(target=_work, daemon=True, name="IndustryCompute").start()

    def _build_metadata(self, products: List[int]):
        """Resolve names + categories once (bulk)."""
        infos = self.names.get_type_info_bulk(products)
        names = self.names.get_type_names_bulk(products)
        # Classify buyable-BPO vs BPC-only: an item has a buyable BPO iff its
        # blueprint type sits on the market (market_group_id not None). BPC-only
        # = faction/Triglavian drops + T2/T3 invented (blueprint never seeded).
        bp_of = {tid: self.sde.get_blueprint_for_item(tid) for tid in products}
        bp_infos = self.names.get_type_info_bulk([b for b in bp_of.values() if b])
        self.name_map = names
        cat = {}
        meta = {}
        has_bpo = {}
        is_cap = {}
        is_upwell = {}
        is_pos = {}
        for tid in products:
            info = infos.get(tid)
            mg_id = info.market_group_id if info else None
            ancestry = self._ancestry_names(mg_id)
            cat[tid] = self._category_of(ancestry)
            meta[tid] = info.meta_group_id if info else None
            bpinfo = bp_infos.get(bp_of.get(tid))
            has_bpo[tid] = bool(bpinfo and bpinfo.market_group_id is not None)
            is_cap[tid] = any(m in ancestry for m in CAPITAL_MATCHERS)
            is_upwell[tid] = any(m in ancestry for m in UPWELL_MATCHERS)
            is_pos[tid] = any(m in ancestry for m in POS_MATCHERS)
        self.category_map = cat
        self.meta_map = meta
        self.bpo_map = has_bpo
        self.capital_map = is_cap
        self.upwell_map = is_upwell
        self.pos_map = is_pos

    def _ancestry_names(self, mg_id: Optional[int]) -> str:
        """Lowercased ' | '-joined market-group ancestry name string (leaf
        included), used for category + capital/structure classification."""
        if mg_id is None:
            return ""
        try:
            ancestry = self.names.get_market_group_ancestry(mg_id)
        except Exception:
            return ""
        return " | ".join(
            (self.names.get_market_group_name(m) or "").lower() for m in ancestry)

    def _category_of(self, ancestry: str) -> str:
        if not ancestry:
            return "Other"
        for label, matchers in CATEGORIES:
            if matchers and any(m in ancestry for m in matchers):
                return label
        return "Other"

    def _compute_done(self, results, err, note, refetch):
        self._computing = False
        self.refresh_btn.configure(state="normal")
        if self._pending_sde_recompute:
            # The completed run started against the pre-update recipe DB and
            # may contain a mixture of old/new lookups. Discard it, invalidate
            # metadata only now (so names remain usable while it winds down),
            # and immediately run once against the fully swapped SDE.
            self._pending_sde_recompute = False
            self._invalidate_sde_metadata()
            _print("sde_recompute_start reason=queued_after_active_compute")
            self._compute(refetch=False)
            return
        if refetch:
            self._last_refresh = time.time()
            self.status_label.configure(text=f"Data: {self.market.get_last_update()}")
        self.results = results
        if err:
            self.progress_label.configure(text=f"Failed: {err}")
        else:
            self.progress_label.configure(
                text=note or f"{len(results)} items computed")
        self._update_tech_note()
        self._rebuild_list()
        if self.selected in self.results:
            self._show_detail(self.selected)

    def _on_update_sde(self):
        """Rebuild type/meta and Industry recipe SDEs, then recompute."""
        try:
            from gui.sde_download_dialog import download_sde_with_progress
        except Exception as e:
            self.set_status(f"Industry: SDE updater unavailable - {e}")
            return
        download_sde_with_progress(self.frame, self.set_status,
                                   on_complete=self._after_sde_download)

    def _after_sde_download(self, success: bool):
        _print(f"sde_update_complete success={success}")
        if not success:
            return
        if self._computing:
            self._pending_sde_recompute = True
            self.progress_label.configure(
                text="SDE updated; recompute queued after current run")
            _print("sde_recompute_queued reason=compute_busy")
            return
        self._invalidate_sde_metadata()
        _print("sde_recompute_start reason=update_complete")
        self._compute(refetch=False)

    def _invalidate_sde_metadata(self):
        """Force names/classification to rebuild against the current SDE."""
        self.name_map = {}
        self.category_map = {}
        self.meta_map = {}
        self.bpo_map = {}
        self.capital_map = {}
        self.upwell_map = {}
        self.pos_map = {}
        self._update_tech_note()

    def _on_tab_changed(self, _event=None):
        try:
            if self.notebook.select() != str(self.frame):
                return
        except tk.TclError:
            return
        if self.sde.is_available() and time.time() - self._last_refresh > 300:
            self._compute(refetch=True)
