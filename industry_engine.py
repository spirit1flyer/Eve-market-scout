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
