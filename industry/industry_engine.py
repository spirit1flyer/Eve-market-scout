"""Industry (manufacturing) profitability — pure calculation logic.

No GUI, no network, no SDE/ESI imports. Every input (recipes, prices, cost
indices, facility params) is injected via a provider object so the engine is
deterministic and unit-testable. The GUI/headless layer (industry_market_data.py
+ sde_industry.py) builds the provider; this module only does the math.

Verified EVE mechanics this encodes (see PLAN_industry_tab.md §1):
  * Material quantity depends only on blueprint ME (+ structure/rig material
    bonus). No character skill reduces materials.
  * Job install cost = EIV × (SCI × (1 - cost_bonus) + facility_tax + SCC +
    alpha_clone_tax), where EIV = Σ(base_qty × CCP adjusted_price) at ME 0.
    No skill or standing affects job cost.
  * Skills/implants affect TIME only (not modelled here; Phase 4).

The material-cost path is a recursive node (cost = market buy OR sub-build) even
though Tier-1 is flat (all materials terminal buys). This is the extension point
T2 components / reactions plug into later with no engine rewrite: a provider that
returns a recipe for a material makes that material a "produced" node instead of
a "market" node.
"""

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, Tuple


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class Recipe:
    """A manufacturing/reaction recipe at base (ME 0) quantities.

    `activity` selects the cost mechanics (PLAN §1b, Stage 5.3): "manufacturing"
    is ME-adjusted and uses the manufacturing facility/cost-index; "reaction"
    has NO ME (formulas carry none) and uses the reaction facility/cost-index.
    Defaulting to "manufacturing" preserves every pre-5.3 construction.
    """
    output_per_run: int
    materials: List[Tuple[int, int]]  # (material_type_id, base_qty_per_run)
    activity: str = "manufacturing"


@dataclass
class SellInfo:
    """Output-side market snapshot for a finished product."""
    lowest_sell: float = 0.0   # cheapest sell order now
    highest_buy: float = 0.0   # best buy order now (immediate dump)
    avg_7d: float = 0.0        # 7-day average transaction price
    avg_30d: float = 0.0       # 30-day average transaction price
    volume: float = 0.0        # average daily volume (7d)
    history_days: int = 0      # how many days of history we actually have


@dataclass
class FacilityParams:
    """Cost-side facility inputs. No character data enters here.

    Constants flagged ⚠ are CCP-tuned and must be verified live before trusting
    a job-cost figure (PLAN §1, Stage 0.1). They are carried as overridable
    settings in industry_market_data.py and passed in here.
    """
    system_cost_index: float = 0.0   # manufacturing SCI for the facility system
    cost_bonus_pct: float = 0.0      # structure/rig job-cost reduction (%)
    material_bonus_pct: float = 0.0  # structure/rig material reduction (%)
    facility_tax_pct: float = 0.25   # NPC default 0.25%; player = owner-set
    scc_surcharge_pct: float = 0.0   # ⚠ CCP-tuned global surcharge — verify live
    alpha_clone_tax_pct: float = 0.0 # 0 for Omega, 0.25 for Alpha


@dataclass
class FacilitySet:
    """Per-activity facility inputs (PLAN §1b, Stage 5.3).

    A chain can mix manufacturing and reaction jobs, each with its own facility
    (cost index / tax / bonuses). `manufacturing` is always present; `reaction`
    falls back to `manufacturing` when unset. Public entry points also accept a
    bare `FacilityParams` (the live GUI passes one today) — the engine wraps it
    as `FacilitySet(manufacturing=fac)` via `_as_facility_set`.
    """
    manufacturing: FacilityParams
    reaction: Optional[FacilityParams] = None

    def for_activity(self, activity: str) -> FacilityParams:
        if activity == "reaction" and self.reaction is not None:
            return self.reaction
        return self.manufacturing


def _as_facility_set(fac) -> FacilitySet:
    """Normalize a bare FacilityParams (legacy/GUI) or a FacilitySet to a
    FacilitySet. isinstance keeps existing single-facility callers working."""
    if isinstance(fac, FacilitySet):
        return fac
    return FacilitySet(manufacturing=fac)


@dataclass
class SellFees:
    """Output-side fees. Listing a sell order pays broker + tax on the sale;
    filling an existing buy order (immediate dump) pays sales tax only."""
    broker_fee_pct: float = 0.0
    sales_tax_pct: float = 0.0


class MarketDataProvider(Protocol):
    """Duck-typed data source injected into IndustryCalculator. Tests pass a
    fake; the live app backs it with sde_industry.py + industry_market_data.py."""

    def recipe(self, type_id: int) -> Optional[Recipe]:
        """Recipe for a producible item, or None if it is a terminal buy.
        For Tier-1 this returns None for every *material* (flat chain)."""
        ...

    def input_price(self, type_id: int) -> float:
        """Chosen input price for a material: highest buy (patient) or lowest
        sell (impatient). The provider resolves which side based on the toggle."""
        ...

    def adjusted_price(self, type_id: int) -> float:
        """CCP 'adjusted' price for EIV. Not a market price."""
        ...

    def cost_index(self, system_id: int, activity: str = "manufacturing") -> float:
        """System cost index for the facility system and industry activity
        (PLAN §1b, Stage 5.3). Wave-2 providers implement the 2-arg form
        natively; legacy single-arg implementations still satisfy the protocol
        and are supported via the engine's TypeError fallback."""
        ...

    def sell_info(self, type_id: int) -> SellInfo:
        """Output-side market snapshot for a finished product."""
        ...


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def me_adjusted_qty(base_qty: int, me: float, material_bonus_pct: float = 0.0) -> int:
    """ME-adjusted material quantity for a *single run*.

    PLAN §1.5 formula: max(1, ceil(base × (100 - ME) / 100)), with an optional
    structure/rig material bonus applied multiplicatively. A base quantity of 1
    never drops below 1 (ME does not reduce single-unit inputs).
    """
    if base_qty <= 1:
        return base_qty
    factor = (100.0 - me) / 100.0 * (1.0 - material_bonus_pct / 100.0)
    return max(1, math.ceil(base_qty * factor))


def job_material_qty(base_qty: int, runs: int, me: float,
                     material_bonus_pct: float = 0.0) -> int:
    """Job-level (whole-batch) material quantity — PLAN §1b.

    EVE rounds materials once PER JOB, not per run:
        max(runs, ceil(round₂(runs × base × (100-ME)/100 × (1 - bonus/100))))
    The 2-decimal pre-round dodges float artifacts (matches eve-ref's
    `ManufactureCalculator.materialQuantity`). This INTENTIONALLY differs from
    `me_adjusted_qty(base, me) × runs`, which overstates multi-run batches; use
    this wherever a batch total is computed. For `runs == 1` it equals
    `me_adjusted_qty` (single-run/display path), so single-run results are
    unchanged.
    """
    if base_qty <= 0:
        return 0
    runs = max(1, int(runs))
    factor = (100.0 - me) / 100.0 * (1.0 - material_bonus_pct / 100.0)
    return max(runs, math.ceil(round(runs * base_qty * factor, 2)))


def estimated_item_value(recipe: Recipe, adjusted_fn: Callable[[int], float]) -> float:
    """EIV = Σ(base_qty × adjusted_price) over the recipe's base materials."""
    return sum(base_qty * adjusted_fn(mat) for mat, base_qty in recipe.materials)


def job_install_cost(eiv: float, fac: FacilityParams) -> float:
    """Job install cost for a single run (PLAN §1 formula)."""
    bonus = 1.0 - fac.cost_bonus_pct / 100.0
    surcharges = (fac.facility_tax_pct + fac.scc_surcharge_pct
                  + fac.alpha_clone_tax_pct) / 100.0
    return eiv * fac.system_cost_index * bonus + eiv * surcharges


# ---------------------------------------------------------------------------
# Build time (Phase 4) — pure, character-dependent
# ---------------------------------------------------------------------------
#
# Manufacturing time is the ONLY place character data legitimately enters the
# numbers (PLAN §1): it changes how long a job takes — and therefore the
# time-based batch cap — but never the ISK cost. All multipliers are applied to
# the blueprint's base per-run time and stack multiplicatively.

# Per-level skill reductions to manufacturing time (verified EVE mechanics).
INDUSTRY_TIME_PER_LEVEL = 0.04      # Industry skill: 4%/level
ADV_INDUSTRY_TIME_PER_LEVEL = 0.03  # Advanced Industry skill: 3%/level


@dataclass
class BuildTimeParams:
    """Time-only inputs for a manufacturing job (from the 'Built by' character +
    blueprint TE + facility/implant bonuses). None of these affect ISK cost."""
    te: float = 0.0                     # blueprint time efficiency, 0–20 (% off)
    industry_level: int = 0            # Industry skill 0–5 (4%/lvl)
    adv_industry_level: int = 0        # Advanced Industry skill 0–5 (3%/lvl)
    facility_time_bonus_pct: float = 0.0  # structure/rig time reduction (%)
    implant_time_pct: float = 0.0      # write-in mfg-time implant reduction (%)


def manufacturing_time(base_time_per_run: float, runs: int,
                       params: BuildTimeParams) -> dict:
    """Adjusted manufacturing time for `runs` runs of a blueprint.

    per_run = base × (1 − TE/100)
                  × (1 − 0.04·Industry) × (1 − 0.03·AdvIndustry)
                  × (1 − facility_time_bonus) × (1 − implant_time)
    total   = per_run × runs

    Returns {per_run_seconds, total_seconds}. A non-positive base time (unknown
    — old SDE) yields zeros so callers can treat "no time data" uniformly.
    """
    if base_time_per_run <= 0:
        return {"per_run_seconds": 0.0, "total_seconds": 0.0}
    runs = max(1, int(runs))
    mult = (
        (1.0 - params.te / 100.0)
        * (1.0 - INDUSTRY_TIME_PER_LEVEL * max(0, min(5, params.industry_level)))
        * (1.0 - ADV_INDUSTRY_TIME_PER_LEVEL * max(0, min(5, params.adv_industry_level)))
        * (1.0 - params.facility_time_bonus_pct / 100.0)
        * (1.0 - params.implant_time_pct / 100.0)
    )
    mult = max(0.0, mult)
    per_run = base_time_per_run * mult
    return {"per_run_seconds": per_run, "total_seconds": per_run * runs}


# Time-based batch cap default: a job's total time should fit in 30 days
# (PLAN §4). Selectable in the UI; a single run already over the cap → max 1.
DEFAULT_MAX_JOB_SECONDS = 30 * 24 * 3600


def max_runs_for_time(per_run_seconds: float,
                      max_total_seconds: float = DEFAULT_MAX_JOB_SECONDS) -> int:
    """Largest run count whose total time fits within `max_total_seconds`.

    A single run already longer than the cap returns 1 (you can still queue it;
    the cap just stops the GUI suggesting an impossible batch). Unknown per-run
    time (≤0) returns 1 — no basis to suggest more.
    """
    if per_run_seconds <= 0 or max_total_seconds <= 0:
        return 1
    if per_run_seconds > max_total_seconds:
        return 1
    return max(1, int(max_total_seconds // per_run_seconds))


# ---------------------------------------------------------------------------
# Research time + cost (Phase 4.3) — ME/TE research popup
# ---------------------------------------------------------------------------
#
# Cumulative research time (seconds) to reach each level for a RANK-1 blueprint.
# Source: EVE University "Research" wiki. The SDE activity time (activity 4 = ME,
# 3 = TE) equals the level-1 value (105) × blueprint rank, so for a given
# blueprint:  time_to_level(L) = sde_base_time × RESEARCH_TIME_RANK1[L] / 105.
# ⚠ CCP-tuned table — verify one blueprint's ME10 time against in-game before
# fully trusting (PLAN §4 "LIVE-verify ME10 cost/time vs in-game").
RESEARCH_TIME_RANK1 = {
    0: 0, 1: 105, 2: 250, 3: 595, 4: 1414, 5: 3360,
    6: 8000, 7: 19000, 8: 45255, 9: 107700, 10: 256000,
}
RESEARCH_TIME_BASE = 105  # the level-1 value the SDE base time corresponds to

# Per-level research-time reductions (verified EVE mechanics).
METALLURGY_TIME_PER_LEVEL = 0.05       # Metallurgy → ME research time
RESEARCH_SKILL_TIME_PER_LEVEL = 0.05   # Research skill → TE research time
# Advanced Industry (3%/lvl, ADV_INDUSTRY_TIME_PER_LEVEL) applies to BOTH.


@dataclass
class ResearchParams:
    """Skill/implant/facility inputs for ME or TE research (time-only)."""
    metallurgy_level: int = 0       # ME research, 5%/lvl
    research_level: int = 0         # TE research, 5%/lvl
    adv_industry_level: int = 0     # both, 3%/lvl
    facility_time_bonus_pct: float = 0.0
    implant_me_pct: float = 0.0     # ME-research-time implant %
    implant_te_pct: float = 0.0     # TE-research-time implant %


def _research_time_raw(base_time: float, from_level: int, to_level: int) -> float:
    """Unadjusted research time (seconds) for from_level → to_level."""
    if base_time <= 0:
        return 0.0
    f = max(0, min(10, int(from_level)))
    t = max(0, min(10, int(to_level)))
    if t <= f:
        return 0.0
    return base_time * (RESEARCH_TIME_RANK1[t] - RESEARCH_TIME_RANK1[f]) / RESEARCH_TIME_BASE


def research_time(base_time: float, from_level: int, to_level: int, *,
                  kind: str, params: ResearchParams) -> float:
    """Skill/implant/facility-adjusted research time (seconds), from_level→to_level.

    kind: "me" (Metallurgy) or "te" (Research). Advanced Industry + the facility
    time bonus apply to both; the implant uses the matching ME/TE field.
    """
    raw = _research_time_raw(base_time, from_level, to_level)
    if raw <= 0:
        return 0.0
    adv_mult = 1.0 - ADV_INDUSTRY_TIME_PER_LEVEL * max(0, min(5, params.adv_industry_level))
    fac_mult = 1.0 - params.facility_time_bonus_pct / 100.0
    if kind == "me":
        skill_mult = 1.0 - METALLURGY_TIME_PER_LEVEL * max(0, min(5, params.metallurgy_level))
        imp_mult = 1.0 - params.implant_me_pct / 100.0
    else:  # te
        skill_mult = 1.0 - RESEARCH_SKILL_TIME_PER_LEVEL * max(0, min(5, params.research_level))
        imp_mult = 1.0 - params.implant_te_pct / 100.0
    return max(0.0, raw * skill_mult * adv_mult * fac_mult * imp_mult)


def research_install_cost(eiv: float, research_cost_index: float,
                          fac: FacilityParams) -> float:
    """Research job install cost (per job, NOT per level).

    Same shape as `job_install_cost` but with the research-activity system cost
    index; alpha-clone tax excluded (research is an Omega activity). EIV is the
    blueprint's estimated item value (Σ base material × adjusted price).
    ⚠ Research cost modelling is approximate — verify against in-game.
    """
    bonus = 1.0 - fac.cost_bonus_pct / 100.0
    surcharges = (fac.facility_tax_pct + fac.scc_surcharge_pct) / 100.0
    return eiv * research_cost_index * bonus + eiv * surcharges


# ---------------------------------------------------------------------------
# Invention (Stage 5.2) — pure, no network
# ---------------------------------------------------------------------------
#
# T2/T3 blueprints are invented, not bought: an invention job consumes datacores
# (+ optionally one decryptor) and succeeds with some probability, yielding a BPC
# at ME 2 / TE 4 (base) modified by the decryptor, with a run count from the SDE
# plus the decryptor's run modifier. The amortized blueprint cost per produced
# unit folds into the T2 build (PLAN §1b). These functions are pure orchestration
# inputs — datacore/decryptor PRICES are summed by the CALLER into
# `attempt_materials_cost`; nothing here fetches.

@dataclass
class Decryptor:
    """One invention decryptor (optional consumable that shifts probability and
    the invented BPC's ME/TE/runs). Field order matches PLAN §1b:
    probability_mult / me_mod / te_mod / run_mod."""
    type_id: int
    name: str
    probability_mult: float
    me_mod: int
    te_mod: int
    run_mod: int


# The 8 decryptors, keyed (and insertion-ordered) by type_id. Data lifted from
# eve-ref's decryptors.csv (MIT-0). Fields: probability_mult, me_mod, te_mod,
# run_mod. Data verified against eve-ref (MIT-0) 2026-07-07 —
# ⚠ re-verify in-game if CCP rebalances decryptors.
DECRYPTORS: Dict[int, Decryptor] = {
    34201: Decryptor(34201, "Accelerant",              1.2,  2, 10, 1),
    34202: Decryptor(34202, "Attainment",              1.8, -1,  4, 4),
    34203: Decryptor(34203, "Augmentation",            0.6, -2,  2, 9),
    34204: Decryptor(34204, "Parity",                  1.5,  1, -2, 3),
    34205: Decryptor(34205, "Process",                 1.1,  3,  6, 0),
    34206: Decryptor(34206, "Symmetry",                1.0,  1,  8, 2),
    34207: Decryptor(34207, "Optimized Attainment",    1.9,  1, -2, 2),
    34208: Decryptor(34208, "Optimized Augmentation",  0.9,  2,  0, 7),
}

# Invented-BPC base research levels before any decryptor modifier (PLAN §1b).
INVENTION_BASE_ME = 2
INVENTION_BASE_TE = 4

# Science-job cost base rate: invention AND copying job cost is 2% × EIV
# (eve-ref JOB_COST_BASE_RATE, PLAN §1b). ⚠ CCP-tuned — threaded as an
# overridable parameter (like SCC), never buried.
SCIENCE_JOB_COST_RATE = 0.02


def invention_probability(base_prob: float, sci1_level: int, sci2_level: int,
                          encryption_level: int,
                          decryptor_prob_mult: float = 1.0) -> float:
    """Invention success probability (PLAN §1b, confirmed in eve-ref + EVE-IPH):

        base × (1 + (sci1 + sci2)/30 + encryption/40) × decryptor_mult

    where sci1/sci2 are the two datacore science skills and encryption is the
    encryption-methods skill. Clamped to [0.0, 1.0].
    """
    p = base_prob * (1.0 + (sci1_level + sci2_level) / 30.0
                     + encryption_level / 40.0) * decryptor_prob_mult
    return max(0.0, min(1.0, p))


def invention_outcome(base_runs: int,
                      decryptor: Optional[Decryptor] = None) -> dict:
    """Invented BPC attributes: {"me", "te", "runs"} (PLAN §1b).

    ME 2 / TE 4 base plus the decryptor's modifiers (all 0 when no decryptor);
    runs = SDE base runs + run modifier, floored at 1.
    """
    me_mod = te_mod = run_mod = 0
    if decryptor is not None:
        me_mod, te_mod, run_mod = (decryptor.me_mod, decryptor.te_mod,
                                   decryptor.run_mod)
    return {
        "me": INVENTION_BASE_ME + me_mod,
        "te": INVENTION_BASE_TE + te_mod,
        "runs": max(1, base_runs + run_mod),
    }


def science_job_cost(eiv: float, cost_index: float, fac: FacilityParams,
                     base_rate: float = SCIENCE_JOB_COST_RATE) -> float:
    """Science-activity (invention or copying) job install cost (PLAN §1b).

    The job-cost base is `eiv × base_rate` (2% of EIV, vs full EIV for
    manufacturing/reactions); the same surcharge shape as `job_install_cost`
    is then applied to that base. `cost_index` is the invention/copying system
    cost index. `base_rate` is overridable (⚠ CCP-tuned).
    """
    jcb = eiv * base_rate
    bonus = 1.0 - fac.cost_bonus_pct / 100.0
    surcharges = (fac.facility_tax_pct + fac.scc_surcharge_pct
                  + fac.alpha_clone_tax_pct) / 100.0
    return jcb * cost_index * bonus + jcb * surcharges


def invention_cost_per_run(attempt_materials_cost: float,
                           attempt_job_cost: float,
                           attempt_copy_cost: float,
                           probability: float,
                           runs_per_copy: int) -> float:
    """Amortized invented-BPC cost charged per produced run (PLAN §1b):

        (datacore+decryptor materials + invention job + copy job)
        ÷ (probability × runs_per_copy)

    i.e. the expected cost of one usable run given the success rate and the runs
    a successful copy yields. `attempt_materials_cost` already includes datacore
    and decryptor prices (summed by the caller). Returns 0.0 as a "not
    computable / flag as unpriced" sentinel when probability ≤ 0 or
    runs_per_copy ≤ 0 (a real amortized cost is always > 0, so callers detect
    the guard by testing for 0.0).
    """
    if probability <= 0 or runs_per_copy <= 0:
        return 0.0
    total = attempt_materials_cost + attempt_job_cost + attempt_copy_cost
    return total / (probability * runs_per_copy)


# ---------------------------------------------------------------------------
# Calculator
# ---------------------------------------------------------------------------

class IndustryCalculator:
    """Build-cost + profitability for manufacturable items. Pure: all data via
    the injected provider, all facility/fee params passed per call."""

    def __init__(self, data: MarketDataProvider,
                 buildable: Optional[Callable[[int], bool]] = None):
        self.data = data
        self.overrides: Dict[int, float] = {}  # type_id -> manual unit price
        # Gate on which *materials* may be sub-built during recursion. None =
        # build anything that has a recipe (T2+ default). Tier-1 passes
        # `lambda tid: False` so every material resolves to a terminal market
        # buy (flat chain). The top product in calc_full always builds — this
        # predicate only governs recursion into materials.
        self.buildable = buildable

    # -- price overrides -----------------------------------------------------

    def set_override(self, type_id: int, price: float) -> None:
        if price > 0:
            self.overrides[type_id] = price
        else:
            self.overrides.pop(type_id, None)

    def clear_overrides(self) -> None:
        self.overrides.clear()

    # -- provider cost-index resolution (activity-aware, defensive) ----------

    def _cost_index(self, system_id: int,
                    activity: str = "manufacturing") -> float:
        """Resolve a system's cost index for an industry activity via the
        provider. Wave-2 providers implement the 2-arg form natively; legacy
        single-arg providers are supported via the TypeError fallback.

        Not on the current job-cost hot path — FacilityParams carries
        `system_cost_index` pre-resolved by the caller (and FacilitySet keeps it
        per-activity), so the engine reads that directly. Provided for
        activity-aware callers/Wave-2 wiring (PLAN §1b, Stage 5.3)."""
        try:
            return self.data.cost_index(system_id, activity)
        except TypeError:
            return self.data.cost_index(system_id)

    # -- recursive cost node -------------------------------------------------

    def cost_node(self, type_id: int, fac, me: float, memo: dict) -> dict:
        """Per-*unit* cost node for one item. `fac` may be FacilityParams or
        FacilitySet (Stage 5.3); a bare FacilityParams is wrapped.

        kind: "override" (manual price) | "produced" (sub-build, recursive)
              | "market" (terminal buy at input price).
        Produced nodes carry per-unit material/job breakdown and an `inputs`
        list of {node, qty_per_unit, cost}. Memoized per pass so shared
        sub-chains are computed once. For Tier-1 every material resolves to a
        "market" node (buildable=False), so the tree stays flat.

        Cheapest-of(build, buy): when a material is buildable AND has a recipe
        AND has a market price > 0, both costs are computed and the cheaper wins
        (`source` = "build_cheaper"/"buy_cheaper"); the produced subtree is kept
        on the node either way so the GUI can render the comparison. Without a
        market price a buildable item is "only_build"; a non-buildable item is a
        terminal "market" node (source "market" — current T1 behavior).
        """
        return self._cost_node(type_id, _as_facility_set(fac), me, memo)

    def _cost_node(self, type_id: int, facset: FacilitySet,
                   me: float, memo: dict) -> dict:
        # Override short-circuits everything (activity-independent).
        if type_id in self.overrides:
            key = (type_id, "override")
            if key in memo:
                return memo[key]
            node = {"type_id": type_id, "kind": "override",
                    "unit_cost": self.overrides[type_id], "inputs": []}
            memo[key] = node
            return node

        # Resolve a buildable recipe (if any) up front so the memo key can
        # carry the node's activity (the same type could otherwise appear under
        # different facilities). For T1 (buildable=False) the recipe lookup is
        # skipped entirely, so the flat-chain path is unchanged.
        recipe = None
        if self.buildable is None or self.buildable(type_id):
            r = self.data.recipe(type_id)
            if r and r.output_per_run > 0:
                recipe = r
        activity = recipe.activity if recipe else "market"
        key = (type_id, activity)
        if key in memo:
            return memo[key]

        if recipe:
            node = self._produced_node(type_id, recipe, facset, me, memo)
            market_price = self.data.input_price(type_id)
            if market_price and market_price > 0:
                node["market_price"] = market_price
                if market_price < node["unit_cost"]:
                    # Buying is cheaper: keep the produced subtree for the GUI
                    # comparison but bill at the market price.
                    node["build_cost"] = node["unit_cost"]
                    node["unit_cost"] = market_price
                    node["kind"] = "market"
                    node["source"] = "buy_cheaper"
                else:
                    node["source"] = "build_cheaper"
            else:
                node["source"] = "only_build"
        else:
            node = {"type_id": type_id, "kind": "market",
                    "unit_cost": self.data.input_price(type_id),
                    "source": "market", "inputs": []}

        memo[key] = node
        return node

    def _produced_node(self, type_id: int, recipe: Recipe,
                       facset: FacilitySet, me: float, memo: dict) -> dict:
        # Reactions carry no ME and use the reaction facility/cost-index (its
        # FacilityParams.system_cost_index); manufacturing uses ME + the mfg
        # facility (PLAN §1b, Stage 5.3). The original `me` is threaded through
        # recursion unchanged — each node decides its own effective ME by its
        # own activity, so a manufacturing sub-node under a reaction still uses ME.
        activity = getattr(recipe, "activity", "manufacturing")
        node_fac = facset.for_activity(activity)
        eff_me = 0.0 if activity == "reaction" else me
        out = recipe.output_per_run
        inputs = []
        mat_cost = 0.0
        for mat, base_qty in recipe.materials:
            child = self._cost_node(mat, facset, me, memo)
            adj_qty = me_adjusted_qty(base_qty, eff_me, node_fac.material_bonus_pct)
            qty_per_unit = adj_qty / out
            cost = qty_per_unit * child["unit_cost"]
            inputs.append({"node": child, "qty_per_unit": qty_per_unit,
                           "cost": cost})
            mat_cost += cost
        eiv = estimated_item_value(recipe, self.data.adjusted_price)
        job_per_unit = job_install_cost(eiv, node_fac) / out
        return {"type_id": type_id, "kind": "produced",
                "activity": activity,
                "unit_cost": mat_cost + job_per_unit,
                "material_cost": mat_cost, "job_cost": job_per_unit,
                "output_per_run": out, "inputs": inputs}

    # -- full product analysis ----------------------------------------------

    def calc_full(self, product_type_id: int, fac: FacilityParams,
                  fees: SellFees, *, me: float = 10.0, batch: int = 1,
                  blueprint_cost_per_run: float = 0.0,
                  memo: Optional[dict] = None) -> Optional[dict]:
        """Full build-cost + profitability for one finished product.

        Returns per-unit costs/margins plus batch totals, or None if the item
        is not manufacturable. Profit basis (default sort) is the patient-sell
        7-day price; immediate (buy-order dump) and 30-day are also computed.

        `blueprint_cost_per_run` is the amortized blueprint cost charged per run
        (Phase 3.4): 0 for a BPO (a one-time buy amortizes to ~nothing over its
        unlimited runs) or for an unpriced BPC; for a priced BPC it is the
        acquisition price ÷ its run count. Added to the batch's total build cost.
        """
        recipe = self.data.recipe(product_type_id)
        if not recipe or recipe.output_per_run <= 0:
            return None
        if memo is None:
            memo = {}
        facset = _as_facility_set(fac)
        batch = max(1, int(batch))

        # Product activity picks the facility + ME handling (Stage 5.3): a
        # reaction product uses the reaction facility and NO ME (its formulas
        # carry none); manufacturing is unchanged.
        activity = getattr(recipe, "activity", "manufacturing")
        pfac = facset.for_activity(activity)
        eff_me = 0.0 if activity == "reaction" else me

        out = recipe.output_per_run
        units = out * batch

        inputs = []
        material_cost = 0.0  # total over the whole batch
        for mat, base_qty in recipe.materials:
            child = self._cost_node(mat, facset, me, memo)
            # Per-run qty is the ME-adjusted single-run figure (display); the
            # batch total uses job-level rounding (PLAN §1b) — max(runs,
            # ceil(round₂(...))) — which is NOT run_qty × batch for fractional
            # per-run quantities. batch=1 collapses to run_qty (unchanged).
            run_qty = me_adjusted_qty(base_qty, eff_me, pfac.material_bonus_pct)
            total_qty = job_material_qty(base_qty, batch, eff_me,
                                         pfac.material_bonus_pct)
            cost = total_qty * child["unit_cost"]
            inputs.append({"node": child, "run_qty": run_qty,
                           "total_qty": total_qty, "cost": cost})
            material_cost += cost

        eiv = estimated_item_value(recipe, self.data.adjusted_price)
        job_cost = job_install_cost(eiv, pfac) * batch

        bpc_cost = max(0.0, blueprint_cost_per_run) * batch
        total_build = material_cost + job_cost + bpc_cost
        unit_cost = total_build / units if units else 0.0

        sell = self.data.sell_info(product_type_id)
        margins = self._margins(unit_cost, sell, fees)

        result = {
            "type_id": product_type_id,
            "me": me,
            "activity": activity,
            "batch": batch,
            "output_per_run": out,
            "units": units,
            "inputs": inputs,
            "eiv": eiv,
            "material_cost": material_cost,
            "job_cost": job_cost,
            "blueprint_cost": bpc_cost,
            "total_build": total_build,
            "unit_cost": unit_cost,
            "sell": sell,
        }
        result.update(margins)
        return result

    def _margins(self, unit_cost: float, sell: SellInfo,
                 fees: SellFees) -> dict:
        """Per-unit revenue/profit/margin for the three output strategies.

        patient  = list a sell order at max(lowest_sell, 7d avg); pays broker+tax.
        immediate= dump to the best buy order; pays sales tax only.
        d30      = list at the 30-day average; pays broker+tax (long-horizon view).
        spot     = list at the CURRENT lowest sell order; pays broker+tax. Unlike
                   patient it needs no transaction history — just a live sell
                   order — so thinly-traded items (most rigs, faction gear) still
                   get an estimate. The Owned-blueprints lens falls back to this.
        Availability flags let the GUI exclude unpriced items from the ranked
        sort. `avg_7d`/`avg_30d` are CALENDAR-WINDOW averages (the average of the
        trades that fell in the last 7 / 30 days), NOT "7 distinct trade-days" —
        a thinly-traded item with 2 trades in the last week still has a 7-day
        average. So availability is simply "was there any trade in that window"
        (avg > 0); `sell.history_days` is the trade-day count, for display only.
        """
        list_keep = 1.0 - (fees.broker_fee_pct + fees.sales_tax_pct) / 100.0
        dump_keep = 1.0 - fees.sales_tax_pct / 100.0

        has_7d = sell.avg_7d > 0
        has_30d = sell.avg_30d > 0
        has_spot = sell.lowest_sell > 0

        patient_price = max(sell.lowest_sell, sell.avg_7d) if has_7d else 0.0
        patient_net = patient_price * list_keep
        immediate_net = sell.highest_buy * dump_keep
        d30_net = sell.avg_30d * list_keep if has_30d else 0.0
        spot_net = sell.lowest_sell * list_keep if has_spot else 0.0

        def margin(net: float) -> float:
            profit = net - unit_cost
            return (profit / unit_cost * 100.0) if unit_cost > 0 else 0.0

        return {
            "has_7d": has_7d,
            "has_30d": has_30d,
            "has_spot": has_spot,
            "patient_price": patient_price,
            "patient_net": patient_net,
            "patient_profit": patient_net - unit_cost if has_7d else None,
            "patient_margin": margin(patient_net) if has_7d else None,
            "immediate_net": immediate_net,
            "immediate_profit": immediate_net - unit_cost if sell.highest_buy > 0 else None,
            "immediate_margin": margin(immediate_net) if sell.highest_buy > 0 else None,
            "d30_net": d30_net,
            "d30_profit": d30_net - unit_cost if has_30d else None,
            "d30_margin": margin(d30_net) if has_30d else None,
            "spot_net": spot_net,
            "spot_profit": spot_net - unit_cost if has_spot else None,
            "spot_margin": margin(spot_net) if has_spot else None,
            "volume": sell.volume,
        }
