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
    """A manufacturing/reaction recipe at base (ME 0) quantities."""
    output_per_run: int
    materials: List[Tuple[int, int]]  # (material_type_id, base_qty_per_run)


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

    def cost_index(self, system_id: int) -> float:
        """Manufacturing system cost index for the facility system."""
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

    # -- recursive cost node -------------------------------------------------

    def cost_node(self, type_id: int, fac: FacilityParams,
                  me: float, memo: dict) -> dict:
        """Per-*unit* cost node for one item.

        kind: "override" (manual price) | "produced" (sub-build, recursive)
              | "market" (terminal buy at input price).
        Produced nodes carry per-unit material/job breakdown and an `inputs`
        list of {node, qty_per_unit, cost}. Memoized per pass so shared
        sub-chains are computed once. For Tier-1 every material resolves to a
        "market" node (provider.recipe returns None), so the tree stays flat.
        """
        if type_id in memo:
            return memo[type_id]

        if type_id in self.overrides:
            node = {"type_id": type_id, "kind": "override",
                    "unit_cost": self.overrides[type_id], "inputs": []}
        elif self.buildable is None or self.buildable(type_id):
            recipe = self.data.recipe(type_id)
            if recipe and recipe.output_per_run > 0:
                node = self._produced_node(type_id, recipe, fac, me, memo)
            else:
                node = {"type_id": type_id, "kind": "market",
                        "unit_cost": self.data.input_price(type_id),
                        "inputs": []}
        else:
            node = {"type_id": type_id, "kind": "market",
                    "unit_cost": self.data.input_price(type_id),
                    "inputs": []}

        memo[type_id] = node
        return node

    def _produced_node(self, type_id: int, recipe: Recipe,
                       fac: FacilityParams, me: float, memo: dict) -> dict:
        out = recipe.output_per_run
        inputs = []
        mat_cost = 0.0
        for mat, base_qty in recipe.materials:
            child = self.cost_node(mat, fac, me, memo)
            adj_qty = me_adjusted_qty(base_qty, me, fac.material_bonus_pct)
            qty_per_unit = adj_qty / out
            cost = qty_per_unit * child["unit_cost"]
            inputs.append({"node": child, "qty_per_unit": qty_per_unit,
                           "cost": cost})
            mat_cost += cost
        eiv = estimated_item_value(recipe, self.data.adjusted_price)
        job_per_unit = job_install_cost(eiv, fac) / out
        return {"type_id": type_id, "kind": "produced",
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
        batch = max(1, int(batch))

        out = recipe.output_per_run
        units = out * batch

        inputs = []
        material_cost = 0.0  # total over the whole batch
        for mat, base_qty in recipe.materials:
            child = self.cost_node(mat, fac, me, memo)
            run_qty = me_adjusted_qty(base_qty, me, fac.material_bonus_pct)
            total_qty = run_qty * batch
            cost = total_qty * child["unit_cost"]
            inputs.append({"node": child, "run_qty": run_qty,
                           "total_qty": total_qty, "cost": cost})
            material_cost += cost

        eiv = estimated_item_value(recipe, self.data.adjusted_price)
        job_cost = job_install_cost(eiv, fac) * batch

        bpc_cost = max(0.0, blueprint_cost_per_run) * batch
        total_build = material_cost + job_cost + bpc_cost
        unit_cost = total_build / units if units else 0.0

        sell = self.data.sell_info(product_type_id)
        margins = self._margins(unit_cost, sell, fees)

        result = {
            "type_id": product_type_id,
            "me": me,
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
