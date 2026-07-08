"""Industry tab — Tier-1 manufacturing buy-vs-build profitability.

Top Profit T1: every manufacturable T1 item ranked by patient-sell profit
(7-day average list price minus build cost minus fees). Mirrors the Boosters
tab's shape — top-bar hub/facility/input selectors, ranked list on the left,
recursive build-breakdown detail panel on the right — but is a committed,
public feature (no drug_ prefix). All math is industry_engine; all data is
read from Scout's existing caches via industry_market_data.

Phase 1 (T1): materials are terminal market buys (flat chain). Stage 5.5 adds
the T2 lens on top: items with an invention path are re-costed with the FULL
input chain (components → reactions → moon goo, per-node cheapest-of build/buy)
plus the amortized invented-BPC cost per run, manufactured at the invented
ME (2 + decryptor mods) — surfaced in a "T2 (invention)" list page, an
Invention detail section (decryptor dropdown, probability, attempts, BPC
ME/TE/runs), and a persisted Reaction-facility row for the chain's reaction
jobs. Character data is optional throughout (assumed invention skill level
write-in when no "Built by" character is set).
"""

import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable, Dict, List, Optional

from core.tk_queue import submit
from gui.gui_window_utils import make_scrollable
from core.config import get_enabled_hubs, get_hub_config
from sde.sde_industry import get_sde_industry_db
from sde.sde_manager import get_sde_manager

from industry.industry_engine import (IndustryCalculator, SellFees, FacilityParams,
                             FacilitySet, DECRYPTORS,
                             BuildTimeParams, manufacturing_time,
                             max_runs_for_time, DEFAULT_MAX_JOB_SECONDS,
                             ResearchParams, research_time, research_install_cost)
from sde.sde_industry import (ACTIVITY_RESEARCH_ME, ACTIVITY_RESEARCH_TE,
                              ACTIVITY_REACTION)
from industry.industry_market_data import (IndustryMarketData, IndustryProvider,
                                   JobCostConstants, BpcPricing, InventionPricing,
                                   SETTINGS_FILE)
from industry.industry_characters import IndustryRoster
from industry.industry_skills import IndustrySkills
from industry.industry_standings import IndustryStandings
from industry.industry_blueprints import IndustryBlueprintsDB, BlueprintPuller
from industry.industry_ignore import IgnoreList, is_auto_hidden
from gui.industry.gui_industry_characters import CharactersPanel
# OwnedBlueprintsPanel is imported lazily in _build_tab to avoid a circular
# import (gui_industry_owned reuses this module's formatting helpers).

CLR_GOOD = "#1a7f37"
CLR_OK = "#b45309"
CLR_BAD = "#b91c1c"
CLR_MUTED = "#666666"
CLR_OVERRIDE = "#0369a1"

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
# T2 has its own invention-costed list page (5.5); T3 modeling is Phase 6.
DEFAULT_TECH = {"T1", "Faction"}


def tech_label(meta_group_id: Optional[int]) -> str:
    if meta_group_id == 2:
        return "T2"
    if meta_group_id == 14:
        return "T3"
    if meta_group_id == 4:
        return "Faction"
    return "T1"


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


class IndustryTabManager:
    """Industry tab: T1 manufacturing buy-vs-build profitability."""

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
        # Stage 5.5: invented-BPC cost resolver (T2). Assumed skill level is
        # persisted inside it; per-item decryptor choice is session-only.
        self.inv_pricing = InventionPricing(self.market, self.sde)
        self._decryptor_choice: Dict[int, int] = {}  # tid -> decryptor type_id
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

    # ===================================================================== build

    def _build_tab(self):
        outer = ttk.Frame(self.notebook)
        self.notebook.add(outer, text="Industry")
        self.frame = outer

        # Industry-level sub-notebook: Top Profit (T1 + the Stage 5.5 T2 list
        # page share this tab's filters/detail panel) + Owned BPO/BPC (Phase 3)
        # + Characters (Phase 2). Phase 6/7 add T3/Extra.
        self.sub_nb = ttk.Notebook(outer)
        self.sub_nb.pack(fill=tk.BOTH, expand=True)
        t1 = ttk.Frame(self.sub_nb)
        self.sub_nb.add(t1, text="Top Profit")

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

        # ---- reaction facility row (Stage 5.5, persisted) ----
        # Reaction jobs in a T2 input chain use their OWN facility: reactions
        # run in refineries (low/null sec) with their own cost index, tax and
        # reaction-rig material bonus. Without this row the engine would fall
        # back to the manufacturing facility above — including its material
        # bonus, which per PLAN §1b must NOT apply to reactions.
        rx_saved = self._load_rx_settings()
        rx = ttk.LabelFrame(t1, text="Reaction facility (T2 input chains)",
                            padding=(8, 4))
        rx.pack(fill=tk.X, padx=8)
        ttk.Label(rx, text="System (reaction cost index):").pack(side=tk.LEFT)
        rx_sys = rx_saved.get("system")
        self.rx_system_var = tk.StringVar(
            value=rx_sys if rx_sys in hub_names else self.facility_var.get())
        self.rx_system_combo = self._combo(rx, self.rx_system_var, hub_names,
                                           self._on_input_change)
        self.rx_tax_var = self._mini_field(
            rx, "Facility tax %:",
            f"{rx_saved.get('tax', self.constants.facility_tax_pct)}")
        self.rx_cost_bonus_var = self._mini_field(
            rx, "Cost bonus %:", f"{rx_saved.get('cost_bonus', 0)}")
        self.rx_mat_bonus_var = self._mini_field(
            rx, "Material bonus %:", f"{rx_saved.get('mat_bonus', 0)}")
        ttk.Label(rx, text="(material bonus = reaction rigs only)",
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

        # Assumed invention skill level (Stage 5.5): used for probability when
        # an item has no "Built by" character. Persisted via InventionPricing.
        ttk.Label(tech_row, text="Inv. skills:").pack(side=tk.LEFT, padx=(12, 2))
        self.inv_skill_var = tk.StringVar(value=str(self.inv_pricing.assumed_level))
        inv_e = ttk.Entry(tech_row, textvariable=self.inv_skill_var, width=3)
        inv_e.pack(side=tk.LEFT)
        inv_e.bind("<Return>", lambda e: self._on_assumed_skill())

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
        # Three list views sharing the detail panel: full ranked list (incl.
        # negatives), a profit>0 view, and the Stage 5.5 T2 page (every item
        # with an invention path, full-chain + invention cost, ignoring the
        # tech chips). All render from the same results dict.
        self.list_nb = ttk.Notebook(left)
        self.list_nb.pack(fill=tk.BOTH, expand=True)
        self.tree = self._make_list_tree(self.list_nb, "All")
        self.tree_pos = self._make_list_tree(self.list_nb, "Positive only")
        self.tree_t2 = self._make_list_tree(self.list_nb, "T2 (invention)")
        self._trees = (self.tree, self.tree_pos, self.tree_t2)
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
        for combo in (self.buy_combo, self.sell_combo, self.facility_combo,
                      self.rx_system_combo):
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
            "rx_system": self._hub_cfg(self.rx_system_var)["system_id"],
            "rx_tax": _f(self.rx_tax_var, self.constants.facility_tax_pct),
            "rx_cost_bonus": _f(self.rx_cost_bonus_var, 0.0),
            "rx_mat_bonus": _f(self.rx_mat_bonus_var, 0.0),
        }

    def _facility_from(self, p: dict) -> FacilityParams:
        # cost_index read here (non-Tk) so it reflects a just-completed refetch.
        return FacilityParams(
            system_cost_index=self.market.cost_index(p["facility_system"], "manufacturing"),
            cost_bonus_pct=p["cost_bonus"], material_bonus_pct=p["mat_bonus"],
            facility_tax_pct=p["fac_tax"], scc_surcharge_pct=p["scc"],
            alpha_clone_tax_pct=self.constants.alpha_clone_tax_pct)

    def _facility_set_from(self, p: dict) -> FacilitySet:
        """Per-activity facilities for chain costing (Stage 5.5): manufacturing
        from the Facility row, reactions from the Reaction-facility row (own
        cost index / tax / rig material bonus — never the mfg material bonus,
        per PLAN §1b). SCC + alpha tax are global."""
        rx = FacilityParams(
            system_cost_index=self.market.cost_index(p["rx_system"], "reaction"),
            cost_bonus_pct=p["rx_cost_bonus"],
            material_bonus_pct=p["rx_mat_bonus"],
            facility_tax_pct=p["rx_tax"], scc_surcharge_pct=p["scc"],
            alpha_clone_tax_pct=self.constants.alpha_clone_tax_pct)
        return FacilitySet(manufacturing=self._facility_from(p), reaction=rx)

    def _load_rx_settings(self) -> dict:
        import json, os
        try:
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE) as f:
                    return json.load(f).get("reaction_facility") or {}
        except Exception as e:
            _print(f"reaction_facility load failed: {e}")
        return {}

    def _save_rx_settings(self):
        import json, os
        try:
            data = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE) as f:
                    data = json.load(f)
            data["reaction_facility"] = {
                "system": self.rx_system_var.get(),
                "tax": _f(self.rx_tax_var, self.constants.facility_tax_pct),
                "cost_bonus": _f(self.rx_cost_bonus_var, 0.0),
                "mat_bonus": _f(self.rx_mat_bonus_var, 0.0),
            }
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            _print(f"reaction_facility save failed: {e}")

    def _on_assumed_skill(self):
        lvl = int(_f(self.inv_skill_var, float(self.inv_pricing.assumed_level)))
        self.inv_pricing.set_assumed_level(max(0, min(5, lvl)))
        self.inv_skill_var.set(str(self.inv_pricing.assumed_level))
        self._compute(refetch=False)

    def _provider_from(self, p: dict) -> IndustryProvider:
        return IndustryProvider(
            self.market, self.sde,
            buy_station=p["buy_station"], sell_station=p["sell_station"],
            sell_region=p["sell_region"], facility_system=p["facility_system"],
            input_side=p["input_side"], buy_region=p["buy_region"])

    def _fees(self) -> SellFees:
        """Sell-side fees from the cached trading skills/standings at the sell
        hub (the REVIEW_handoff stray, deferred to Stage 5.5). Falls back to
        the old Phase 1 flat defaults if the skills cache is unavailable."""
        try:
            from core.calculate import (load_cached_skills, get_broker_fee_rate,
                                        get_sales_tax_rate)
            hub_key = self._hub_key_by_name.get(self.sell_var.get(), "jita")
            skills = load_cached_skills(hub_key)
            return SellFees(broker_fee_pct=get_broker_fee_rate(skills),
                            sales_tax_pct=get_sales_tax_rate(skills))
        except Exception as e:
            _print(f"fee lookup failed (defaults 3/4.5): {e}")
            return SellFees(broker_fee_pct=3.0, sales_tax_pct=4.5)

    # ===================================================================== compute

    def _compute(self, refetch: bool):
        if self._computing:
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
        self._save_rx_settings()   # persist the reaction-facility row (5.5)

        def _work():
            err = None
            note = ""
            try:
                products = self.sde.get_all_manufacturable_items()
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

                # Stage 5.5: T2 needs the FULL chain priced (components →
                # reactions → moon goo) plus invention consumables (datacores
                # per invention path, all 8 decryptors). Skipped entirely on a
                # pre-5.1 SDE (has_invention_data False → no T2 modeling).
                has_inv = self.sde.has_invention_data()
                invention_tids = set()
                if has_inv:
                    tids = self._expand_chain_tids(tids)
                    for tid in products:
                        bp = self.sde.get_blueprint_for_item(tid)
                        sources = self.sde.get_invention_sources(bp) if bp else []
                        info = self.sde.get_invention(sources[0]) if sources else None
                        if info:
                            invention_tids.add(tid)
                            tids.update(t for t, _q in info["datacores"])
                    tids.update(DECRYPTORS)

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
                facset = self._facility_set_from(p)
                provider = self._provider_from(p)
                calc = IndustryCalculator(provider, buildable=lambda t: False)
                results = {}
                for tid in products:
                    res = calc.calc_full(tid, fac, fees,
                                         me=p["me"], batch=p["batch"])
                    if res:
                        results[tid] = res

                # Stage 5.5 T2 pass: every T2 item with an invention path is
                # RE-costed full-chain (buildable=None → cheapest-of per node)
                # at its invented ME, with the amortized invented-BPC cost/run
                # folded in — replacing the understated flat calc above. The
                # memo is shared across items so common components/reactions
                # cost once. T3 (meta 14) invents from relics — Phase 6.
                if has_inv and invention_tids:
                    calc_t2 = IndustryCalculator(provider, buildable=None)
                    memo = {}
                    for tid in invention_tids:
                        if self.meta_map.get(tid) != 2:
                            continue
                        res = self._calc_t2(tid, calc_t2, facset, fac,
                                            provider, fees, p, memo=memo)
                        if res:
                            results[tid] = res
            except Exception as e:
                err = str(e)
                _print(f"compute failed: {e}")
                import traceback
                traceback.print_exc()
                results = {}
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

    # ------------------------------------------------------------ T2 (5.5)

    def _expand_chain_tids(self, tids: set) -> set:
        """Transitive closure of `tids` through manufacturing + reaction
        recipes, so every node of a T2 chain (components → advanced moon
        materials → raw goo) gets a price/adjusted price. EVE chains are DAGs;
        the seen-set guards re-walks."""
        seen = set()
        frontier = list(tids)
        while frontier:
            tid = frontier.pop()
            if tid in seen:
                continue
            seen.add(tid)
            r = (self.sde.get_recipe(tid)
                 or self.sde.get_recipe_for_activity(tid, ACTIVITY_REACTION))
            if r:
                frontier.extend(m for m, _q in r["materials"]
                                if m not in seen)
        return seen

    def _calc_t2(self, tid: int, calc: IndustryCalculator,
                 facset: FacilitySet, inv_fac: FacilityParams,
                 provider: IndustryProvider, fees: SellFees, p: dict,
                 *, memo: Optional[dict] = None,
                 decryptor=None, skill_fn=None) -> Optional[dict]:
        """Full-chain + invention costing for one T2 item. `calc` must be
        buildable=None (cheapest-of chain). The item is manufactured at the
        INVENTED ME (2 + decryptor mods), never the global ME write-in; note
        the engine threads that one ME through the chain, so component
        sub-builds also use it (slightly conservative vs researched component
        BPOs). Returns the calc_full result with the invention facts attached
        under "invention", or None if there's no invention path."""
        inv = self.inv_pricing.resolve(
            tid, input_price_fn=provider.input_price, skill_level_fn=skill_fn,
            decryptor=decryptor, invention_fac=inv_fac,
            invention_system=p["facility_system"])
        if not inv:
            return None
        res = calc.calc_full(tid, facset, fees, me=inv["me"], batch=p["batch"],
                             blueprint_cost_per_run=inv["cost_per_run"],
                             memo=memo)
        if res:
            res["invention"] = inv
        return res

    def _recompute_item_t2(self, tid: int):
        """Recompute ONE T2 item on the UI thread (decryptor / Built-by
        change). Pure cache reads + SQLite — fast enough inline. Uses the
        Built-by character's cached skills for probability when set (peek
        only; falls back per-skill to the assumed level)."""
        p = self._params()
        fac = self._facility_from(p)
        facset = self._facility_set_from(p)
        provider = self._provider_from(p)
        dec = DECRYPTORS.get(self._decryptor_choice.get(tid))
        char_id = self._built_by.get(tid)
        skill_fn = ((lambda sid: self.char_skills.peek_skill_level(char_id, sid))
                    if char_id else None)
        calc = IndustryCalculator(provider, buildable=None)
        res = self._calc_t2(tid, calc, facset, fac, provider, self._fees(), p,
                            decryptor=dec, skill_fn=skill_fn)
        if res:
            self.results[tid] = res
            self._rebuild_list()
            self._show_detail(tid)

    def _on_decryptor(self, tid: int, name: str):
        dec_id = next((d.type_id for d in DECRYPTORS.values()
                       if d.name == name), None)
        if dec_id:
            self._decryptor_choice[tid] = dec_id
        else:
            self._decryptor_choice.pop(tid, None)
        self._recompute_item_t2(tid)

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
        elif not self.sde.has_invention_data():
            self.tech_note_label.configure(
                text="⚠ industry SDE predates invention data — click Update SDE "
                     "to light up the T2 list (T2/T3 costs understated)",
                foreground=CLR_BAD)
        else:
            self.tech_note_label.configure(
                text="(T2 = full chain + invention; T3 not modeled yet — its "
                     "build cost is understated)",
                foreground=CLR_MUTED)

    def _on_update_sde(self):
        """Rebuild the names/meta SDE (sde_types.db) behind the shared progress
        dialog, then rebuild metadata + recompute so the tech filter lights up
        without an app restart."""
        try:
            from gui.sde_download_dialog import download_sde_with_progress
        except Exception as e:
            self.set_status(f"Industry: SDE updater unavailable - {e}")
            return
        download_sde_with_progress(self.frame, self.set_status,
                                   on_complete=self._after_sde_download)

    def _after_sde_download(self, success: bool):
        if not success:
            return
        # Names/meta SDE is fresh; now rebuild the INDUSTRY SDE too (Stage
        # 5.5). The all-activities schema + invention probability/skill tables
        # (Stage 5.1) only exist after a rebuild, and the only other trigger
        # is buried in the Stock Market settings page — without this the T2
        # list can never light up on an existing install.
        self.progress_label.configure(text="Updating industry SDE…")
        self.sde_btn.configure(state="disabled")

        def work():
            try:
                ok = self.sde.refresh(progress_callback=lambda m, _p: submit(
                    lambda mm=m: self.progress_label.configure(
                        text=f"Industry SDE: {mm}")))
            except Exception as e:
                _print(f"industry SDE refresh failed: {e}")
                ok = False
            submit(lambda: self._after_industry_sde(ok))

        threading.Thread(target=work, daemon=True,
                         name="IndustrySDERefresh").start()

    def _after_industry_sde(self, ok: bool):
        self.sde_btn.configure(state="normal")
        if not ok:
            self.progress_label.configure(
                text="Industry SDE update failed — see eve_scout.log.")
        # Force names/categories/meta to rebuild from the fresh SDE and
        # recompute either way (the names SDE did change).
        self.name_map = {}
        self.category_map = {}
        self.meta_map = {}
        self.bpo_map = {}
        self.capital_map = {}
        self.upwell_map = {}
        self.pos_map = {}
        self._update_tech_note()
        self._compute(refetch=False)

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
            text=f"Profit = patient-sell (7d) at {self.sell_var.get()} net of "
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

        # Items passing the category filter (independent of tech) drive both the
        # tech-chip greying and the row set. A tech chip greys out when the
        # active category contains no item of that tech level.
        cat_items = [tid for tid in self.results
                     if cat == "All" or self.category_map.get(tid) == cat]
        avail_tech = {tech_label(self.meta_map.get(tid)) for tid in cat_items}
        for label, b in self.tech_buttons.items():
            b.state(["!disabled"] if label in avail_tech else ["disabled"])

        def passes(tid, r, name):
            """Shared row filter (everything except the tech chips)."""
            # Auto-hide junk products ("Expired …" filaments) + user-ignored.
            if is_auto_hidden(name) or self.ignore.contains(tid):
                return False
            if subcap_only and self.capital_map.get(tid):
                return False
            if hide_upwell and self.upwell_map.get(tid):
                return False
            if hide_pos and self.pos_map.get(tid):
                return False
            has_bpo = self.bpo_map.get(tid, True)
            if bp_mode == "BPO only" and not has_bpo:
                return False
            if bp_mode == "BPC-only" and has_bpo:
                return False
            if search and search not in name.lower():
                return False
            priced = r["has_7d"] and r["patient_profit"] is not None
            if not priced and not show_unpriced:
                return False
            if priced and min_profit is not None and r["patient_profit"] < min_profit:
                return False
            return True

        rows = []
        t2_rows = []   # Stage 5.5: invention-costed items, tech chips ignored
        for tid in cat_items:
            r = self.results[tid]
            name = self.name_map.get(tid, str(tid))
            if not passes(tid, r, name):
                continue
            if tech_label(self.meta_map.get(tid)) in self.tech_selected:
                rows.append(tid)
            if r.get("invention"):
                t2_rows.append(tid)

        sort_key = lambda t: self._sort_value(t, self.sort_col)
        rows.sort(key=sort_key, reverse=self.sort_reverse)
        t2_rows.sort(key=sort_key, reverse=self.sort_reverse)
        self.display_order = rows
        # "Positive only" sub-tab: same base filter, profit strictly > 0.
        pos_rows = [t for t in rows
                    if self.results[t]["patient_profit"] is not None
                    and self.results[t]["patient_profit"] > 0]
        self._fill_tree(self.tree, rows)
        self._fill_tree(self.tree_pos, pos_rows)
        self._fill_tree(self.tree_t2, t2_rows)

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

        header = ttk.Frame(self.detail)
        header.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(header, text=name,
                  font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        ttk.Label(header, text=f"  (ME {r['me']:.0f} × batch {r['batch']}, "
                               f"{r['output_per_run']}/run → {r['units']} units)",
                  foreground=CLR_MUTED).pack(side=tk.LEFT)

        self._build_built_by(tid)
        self._build_margins(r)
        self._build_invention_section(r, tid)
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
        for label, net, profit, margin in (
                ("Patient (7d list)", r["patient_net"], r["patient_profit"], r["patient_margin"]),
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
                       f"low-sell {isk(sell.lowest_sell)}   "
                       f"best-buy {isk(sell.highest_buy)}   "
                       f"({sell.history_days}d history)",
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=(2, 0))

    def _build_invention_section(self, r, tid):
        """Stage 5.5 Invention section: decryptor dropdown (None default),
        probability / expected attempts at the effective skills, invented BPC
        ME/TE/runs, consumables, job + copy costs, amortized cost/run."""
        inv = r.get("invention")
        if not inv:
            return
        frame = ttk.LabelFrame(self.detail, text="Invention (T2)", padding=4)
        frame.pack(fill=tk.X, pady=(0, 6))

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, padx=8, pady=(0, 2))
        ttk.Label(row, text="Decryptor:", foreground=CLR_MUTED).pack(side=tk.LEFT)
        dec_names = ["None"] + [d.name for d in DECRYPTORS.values()]
        cur = DECRYPTORS.get(self._decryptor_choice.get(tid))
        dec_var = tk.StringVar(value=cur.name if cur else "None")
        combo = ttk.Combobox(row, values=dec_names, textvariable=dec_var,
                             width=24, state="readonly")
        combo.pack(side=tk.LEFT, padx=4)
        combo.bind("<<ComboboxSelected>>",
                   lambda e, t=tid, v=dec_var: self._on_decryptor(t, v.get()))

        self._kv(frame, "Success probability:", f"{inv['probability'] * 100:.1f}%")
        att = inv["expected_attempts_per_copy"]
        self._kv(frame, "Expected attempts / copy:",
                 f"{att:.2f}" if att > 0 else "—")
        self._kv(frame, "Invented BPC:",
                 f"ME {inv['me']} / TE {inv['te']} / {inv['runs_per_copy']} runs",
                 bold=True)

        datacore_cost = 0.0
        for dtid, qty, px in inv["datacores"]:
            dname = (self.name_map.get(dtid)
                     or self.names.get_type_name(dtid) or str(dtid))
            datacore_cost += qty * px
            self._kv(frame, f"  {qty}× {dname}:",
                     "no price" if px <= 0 else f"{isk(qty * px)} ISK",
                     color=CLR_BAD if px <= 0 else None)
        if cur is not None:
            dec_cost = inv["attempt_materials_cost"] - datacore_cost
            self._kv(frame, f"  1× {cur.name} Decryptor:",
                     "no price" if dec_cost <= 0 else f"{isk(dec_cost)} ISK",
                     color=CLR_BAD if dec_cost <= 0 else None)
        self._kv(frame, "Attempt materials:",
                 f"{isk(inv['attempt_materials_cost'])} ISK")
        self._kv(frame, "Invention job cost:",
                 f"{isk(inv['invention_job_cost'])} ISK")
        copy_txt = f"{isk(inv['copy_job_cost'])} ISK"
        if inv.get("copy_cost_estimated"):
            copy_txt += " (est.)"
        self._kv(frame, "Copy job cost (per attempt):", copy_txt)
        self._kv(frame, "Amortized BPC cost / run:",
                 f"{isk(inv['cost_per_run'])} ISK", bold=True)

        char_id = self._built_by.get(tid)
        rc = self.roster.get(char_id) if char_id else None
        who = (f"skills from {rc.character_name}" if rc else
               f"assumed skill level {self.inv_pricing.assumed_level} "
               f"(set a 'Built by' char for real skills)")
        ttk.Label(frame, text=f"    {who} — ⚠ verified vs eve-ref, "
                              "spot-check one item in-game",
                  foreground=CLR_MUTED).pack(anchor="w", padx=8, pady=(2, 0))
        if inv.get("unpriced"):
            ttk.Label(frame,
                      text=f"    ⚠ {len(inv['unpriced'])} consumable(s) "
                           "unpriced — invention cost understated",
                      foreground=CLR_BAD).pack(anchor="w", padx=8)

    def _build_chain(self, r, tid):
        frame = ttk.LabelFrame(self.detail, text="Materials (whole batch)",
                               padding=4)
        frame.pack(fill=tk.BOTH, expand=True, pady=(0, 6))
        cols = ("qty", "unit", "total", "src")
        tree = ttk.Treeview(frame, columns=cols, height=12)
        tree.heading("#0", text="Item")
        for c, t, w in (("qty", "Qty", 80), ("unit", "Unit cost", 110),
                        ("total", "Total", 120), ("src", "Source", 130)):
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

    def _add_chain_row(self, tree, parent, entry, units=None):
        """One material row. Top-level entries (from calc_full) carry
        whole-batch figures (total_qty/cost); nested produced-node entries
        (from cost_node) are per parent UNIT, so they're scaled by `units` —
        the parent row's total quantity — to keep the whole tree in batch
        terms. Nested quantities can be fractional (per-job rounding isn't
        modeled per sub-job)."""
        node = entry["node"]
        tid = node["type_id"]
        name = self.name_map.get(tid) or self.names.get_type_name(tid) or str(tid)
        self._iid_seq += 1
        iid = f"row{self._iid_seq}"
        self._chain_rows[iid] = tid
        if units is None:
            qty = entry["total_qty"]
            cost = entry["cost"]
        else:
            qty = entry["qty_per_unit"] * units
            cost = entry["cost"] * units
        src = self._node_source(node)
        missing = (node["kind"] == "market" and node["unit_cost"] <= 0
                   and node.get("source") != "buy_cheaper")
        tags = (("produced",) if node["kind"] == "produced"
                else ("missing",) if missing else ())
        unit_txt = "no price" if missing else isk(node["unit_cost"])
        qty_txt = (f"{qty:,.0f}" if float(qty).is_integer() or qty >= 100
                   else f"{qty:,.1f}")
        tree.insert(parent, "end", iid=iid, text=name, open=True,
                    values=(qty_txt, unit_txt, isk(cost), src), tags=tags)
        # Only produced nodes expand; a buy_cheaper node keeps its build
        # subtree on the node but is billed as a buy, so its children would
        # not sum to the row — the forgone build cost shows in Source instead.
        if node["kind"] == "produced":
            for child in node["inputs"]:
                self._add_chain_row(tree, iid, child, units=qty)

    @staticmethod
    def _node_source(node) -> str:
        """Source label for a chain node (Stage 5.3/5.5 cheapest-of labels)."""
        src = node.get("source")
        if src == "buy_cheaper":
            return f"buy (build {isk_m(node.get('build_cost'))})"
        if src == "build_cheaper":
            return "build ✓"
        if node["kind"] == "override":
            return "OVERRIDE"
        if node["kind"] == "produced":
            return "react" if node.get("activity") == "reaction" else "build"
        return "buy"

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
        if r.get("blueprint_cost"):
            label = ("Invented BPC (amortized):" if r.get("invention")
                     else "BPC cost (amortized):")
            self._kv(frame, label, f"{isk(r['blueprint_cost'])} ISK")
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

        Skills come from the 'Built by' character's cached pull (peek only — no
        UI-thread ESI). If a char is selected but skills aren't cached, kicks a
        background warm and reports 'warming' so the panel shows a placeholder.
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
                state = "warming"
                self._warm_skills(char_id, tid)
            rc = self.roster.get(char_id)
            if rc:
                implant = rc.implant_mfg_pct
        params = BuildTimeParams(te=te, industry_level=ind, adv_industry_level=adv,
                                 facility_time_bonus_pct=self._time_bonus(),
                                 implant_time_pct=implant)
        return params, char_id, state

    def _warm_skills(self, char_id: int, tid: int):
        if getattr(self, "_warming_skill_id", None) == char_id:
            return
        self._warming_skill_id = char_id

        def work():
            try:
                self.char_skills.fetch(char_id)
            finally:
                self._warming_skill_id = None
            submit(lambda: self._refresh_detail_if_selected()
                   if self.selected == tid else None)

        threading.Thread(target=work, daemon=True, name="IndustrySkillWarm").start()

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

        # Provenance: which character's skills, TE used, warming state.
        if char_id and state == "cached":
            rc = self.roster.get(char_id)
            who = (rc.character_name if rc else str(char_id))
            note = f"TE {te:.0f}, skills from {who}"
        elif state == "warming":
            note = f"TE {te:.0f}, loading skills…"
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
            "me_index": self.market.cost_index(sys, "researching_material_efficiency"),
            "te_index": self.market.cost_index(sys, "researching_time_efficiency"),
            "fac": self._facility_from(p),
            "rparams": rparams,
            "cur_me": int(cur_me), "cur_te": int(cur_te),
            "skills_cached": levels is not None,
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
        # Build/research time depend on it; for a T2 item the invention
        # probability does too (real skills vs assumed level).
        if self.results.get(tid, {}).get("invention"):
            self._recompute_item_t2(tid)
        else:
            self._refresh_detail_if_selected()

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

    def _on_tab_changed(self, _event=None):
        try:
            if self.notebook.select() != str(self.frame):
                return
        except tk.TclError:
            return
        if self.sde.is_available() and time.time() - self._last_refresh > 300:
            self._compute(refetch=True)


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
