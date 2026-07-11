"""Industry tab market-data layer + engine provider.

Mirrors drug_api.py's discipline: NO market sync of its own. The scanner
already pulls full region order dumps; this module only READS caches Scout
maintains —
  * material/product prices: order_cache region dumps (peek_cached_orders)
  * 7d/30d history:          market_history.db (everef archive)
The only ESI traffic is the two universe-wide industry endpoints nothing else
fetches — /markets/prices/ (CCP adjusted prices, for EIV) and /industry/systems/
(system cost indices) — via the shared api.ESIClient, TTL-gated to 6 hours.

Also owns the CCP-tuned job-cost constants (SCC surcharge, NPC facility tax,
alpha clone tax). These are CCP-tuned and have been changed by patches; a stale
constant silently corrupts every job cost, so they live as named, overridable
settings with a verify-date and persist to industry_settings.json.

`IndustryProvider` adapts this data + sde_industry recipes to the pure
industry_engine.MarketDataProvider protocol.
"""

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from core.sound_manager import get_data_dir
from sde.sde_industry import ACTIVITY_REACTION, ACTIVITY_INVENTION

from industry.industry_engine import (
    Recipe, SellInfo, FacilityParams, SellFees,
    Decryptor,
    invention_probability, invention_outcome, science_job_cost,
    invention_cost_per_run, estimated_item_value,
)

CACHE_FILE = str(get_data_dir() / "industry_market_cache.json")
SETTINGS_FILE = str(get_data_dir() / "industry_settings.json")

# Region dumps up to a day old are fine — the scanner refreshes them far more
# often; a stale dump beats no data (same call as drug_api).
DUMP_MAX_AGE = 86400
# Adjusted prices / cost indices update server-side ~daily.
INDUSTRY_TTL = 6 * 3600
# History fetch window. ~3 months so the manipulation-baseline (below) has a
# stable average to compare against; the 7d/30d figures are calendar subsets.
HISTORY_FETCH_DAYS = 90
# Outlier rejection: drop any trade-day whose average price is under this
# fraction of the item's ~3-month average. Kills manipulation/junk days (e.g. a
# single 0.01-ISK contract-driven row) that would otherwise crater the 7d mean.
OUTLIER_MIN_FRACTION = 0.5

# Industry activities we pull cost indices for. T1 only needs manufacturing;
# the rest are pulled now so later phases (research/invention/reactions) need
# no refetch. ⚠ Exact ESI spelling (verified live 2026-07-08 against
# /industry/systems/): "researching_material_efficiency" /
# "researching_time_efficiency" — NOT "research_material_efficiency" /
# "research_time_efficiency". Getting this wrong silently drops both indices
# at fetch time (the Stage 5.4 bug this comment guards against).
INDUSTRY_ACTIVITIES = (
    "manufacturing", "researching_material_efficiency",
    "researching_time_efficiency", "copying", "invention",
    "reaction",
)


# ---------------------------------------------------------------------------
# CCP-tuned job-cost constants (⚠ verify live — they change with patches)
# ---------------------------------------------------------------------------

@dataclass
class JobCostConstants:
    """Global, CCP-tuned job-cost inputs. Overridable + persisted.

    last verified: 2026-06-22 (PLAN_industry_tab.md Stage 0.1) — these MUST be
    re-checked against a live install before any job-cost figure is trusted.
    Sources to check at build time: EVE patch notes, in-game industry window
    "Job cost" breakdown, EVE University Industry/Cost page.
    """
    facility_tax_pct: float = 0.25   # NPC station default; player struct = owner-set
    scc_surcharge_pct: float = 4.0   # ⚠ CCP raises this; was changed July 2025
    alpha_clone_tax_pct: float = 0.0 # Omega = 0; Alpha = 0.25

    @classmethod
    def load(cls) -> "JobCostConstants":
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
                jc = data.get("job_cost_constants", {})
                return cls(**{k: jc[k] for k in jc if k in cls.__annotations__})
            except (json.JSONDecodeError, IOError, TypeError) as e:
                print(f"[IndustryDiag] settings load error: {e}")
        return cls()

    def save(self) -> None:
        try:
            data = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            data["job_cost_constants"] = asdict(self)
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[IndustryDiag] settings save error: {e}")


# ---------------------------------------------------------------------------
# Observed contract-BPC prices (2026-07-11): stale-average fallback
# ---------------------------------------------------------------------------

OBSERVED_BPC_FILE = str(get_data_dir() / "industry_bpc_observed.json")


class ObservedBpcPrices:
    """Rolling per-blueprint record of contract BPC sightings.

    Contract offers expire out of the local cache, so a blueprint that HAD
    clean offers last week can read "none cached" today even though its going
    rate is known. Whenever the GUI finds live offers it calls `record()`,
    snapshotting the average + best per-run price, offer count and a
    timestamp; `get()` serves that snapshot later as an explicitly-stale
    fallback (the caller renders the disclaimer/age — a few days old is
    acceptable per Caleb 2026-07-11). Persistence mirrors BpcPricing:
    swallow-and-log, one JSON in the data dir (industry_bpc_observed.json),
    keyed by blueprint type_id.
    """

    def __init__(self):
        self.seen: Dict[int, dict] = {}
        self._load()

    def _load(self):
        if not os.path.exists(OBSERVED_BPC_FILE):
            return
        try:
            with open(OBSERVED_BPC_FILE, "r") as f:
                raw = json.load(f)
            self.seen = {int(k): v for k, v in raw.items()}
        except (json.JSONDecodeError, IOError, ValueError, TypeError) as e:
            print(f"[IndustryDiag] observed-bpc load error: {e}")

    def _save(self):
        try:
            with open(OBSERVED_BPC_FILE, "w") as f:
                json.dump({str(k): v for k, v in self.seen.items()}, f,
                          indent=2)
        except IOError as e:
            print(f"[IndustryDiag] observed-bpc save error: {e}")

    def record(self, blueprint_type_id: int, offers, save: bool = True) -> None:
        """Snapshot the current live offers (find_bpc_offers rows) for later
        stale fallback. Empty offer lists are ignored — they'd erase the very
        knowledge this store exists to keep."""
        per_runs = [o["price"] / max(1, o["runs"]) for o in offers
                    if o.get("price", 0) > 0]
        if not per_runs:
            return
        self.seen[int(blueprint_type_id)] = {
            "avg_per_run": sum(per_runs) / len(per_runs),
            "best_per_run": min(per_runs),
            "offers": len(per_runs),
            "seen": datetime.now(timezone.utc).isoformat(),
        }
        if save:
            self._save()

    def record_many(self, sightings: Dict[int, list]) -> None:
        """Batch variant for sweep passes (the Invention sub-tab records every
        blueprint with live offers in one pass — one file write, not hundreds)."""
        for bid, offers in sightings.items():
            self.record(bid, offers, save=False)
        self._save()

    def get(self, blueprint_type_id: int) -> Optional[dict]:
        """Last snapshot for a blueprint ({avg_per_run, best_per_run, offers,
        seen}) or None if it was never observed with live offers."""
        return self.seen.get(int(blueprint_type_id))


# ---------------------------------------------------------------------------
# BPC pricing (Phase 3.4): amortized blueprint-copy cost per run
# ---------------------------------------------------------------------------

class BpcPricing:
    """Resolves a blueprint's amortized cost per run for the engine.

    Priority (mirrors the Boosters tab's BPC logic, but committed/public):
      1. user **write-in** (a price + run count, persisted) — authoritative;
      2. cheapest cached **contract offer** (`contracts_db.find_bpc_offers`
         across the supplied hub regions) — price ÷ runs, cheapest per-run wins;
      3. **unset** — per-run 0, flagged so the GUI can mark the margin overstated.

    A BPO amortizes to ~0 over its unlimited runs, so callers simply don't ask
    for one (per-run 0). Write-ins persist to industry_settings.json key
    `bpc_prices` ({blueprint_type_id: {price, runs}}).
    """

    def __init__(self):
        self.writeins: Dict[int, dict] = {}
        self._load()

    def _load(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            raw = data.get("bpc_prices", {})
            self.writeins = {int(k): v for k, v in raw.items()}
        except (json.JSONDecodeError, IOError, ValueError, TypeError) as e:
            print(f"[IndustryDiag] bpc_prices load error: {e}")

    def save(self):
        try:
            data = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            data["bpc_prices"] = {str(k): v for k, v in self.writeins.items()}
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[IndustryDiag] bpc_prices save error: {e}")

    def get_writein(self, blueprint_type_id: int) -> Optional[dict]:
        return self.writeins.get(int(blueprint_type_id))

    def set_writein(self, blueprint_type_id: int, price: float, runs: int):
        """Set/update a write-in BPC price+runs; price<=0 clears it."""
        bid = int(blueprint_type_id)
        if price and price > 0:
            self.writeins[bid] = {"price": float(price),
                                  "runs": max(1, int(runs or 1))}
        else:
            self.writeins.pop(bid, None)
        self.save()

    def resolve(self, blueprint_type_id: int, regions,
                contracts_db=None) -> dict:
        """Return {per_run, source, price, runs, offer_count} for a blueprint.

        source ∈ {"write-in", "contract", "unset"}. `regions` is an iterable of
        region_ids to scan for contract offers (e.g. the buy + sell hub regions).
        """
        bid = int(blueprint_type_id)
        wi = self.writeins.get(bid)
        if wi and wi.get("price", 0) > 0:
            runs = max(1, int(wi.get("runs", 1)))
            return {"per_run": wi["price"] / runs, "source": "write-in",
                    "price": float(wi["price"]), "runs": runs, "offer_count": 0}

        if contracts_db is not None:
            best = None
            count = 0
            for region in set(regions or []):
                try:
                    offers = contracts_db.find_bpc_offers(bid, region)
                except Exception as e:
                    print(f"[IndustryDiag] find_bpc_offers failed: {e}")
                    offers = []
                count += len(offers)
                for o in offers:
                    runs = max(1, int(o.get("runs") or 1))
                    per = o["price"] / runs
                    if best is None or per < best["per_run"]:
                        best = {"per_run": per, "source": "contract",
                                "price": float(o["price"]), "runs": runs}
            if best:
                best["offer_count"] = count
                return best

        return {"per_run": 0.0, "source": "unset", "price": 0.0,
                "runs": 0, "offer_count": 0}


# ---------------------------------------------------------------------------
# Invention pricing (Stage 5.4): amortized invented-BPC cost/run for T2
# ---------------------------------------------------------------------------

class InventionPricing:
    """Resolves a T2/T3 product's amortized invented-BPC cost/run + facts.

    Combines the Stage 5.1 SDE readers (get_invention / get_invention_sources /
    get_required_skills), the Stage 5.2 engine math (invention_probability /
    invention_outcome / science_job_cost / invention_cost_per_run), and this
    module's prices (adjusted_price for EIV; the caller's input_price_fn for
    datacores/decryptor — the GUI passes IndustryProvider.input_price so those
    materials get the same peek + history-fallback path as every other
    material). Mirrors BpcPricing's style: swallow-and-log, "[IndustryDiag]"
    prefix, persisted settings.

    `assumed_invention_skill_level` (default 4) is used whenever a real
    character skill level isn't supplied (no Built-by character, or the caller
    can't resolve that particular skill) — persisted to
    industry_settings.json key "assumed_invention_skill" (mirrors
    JobCostConstants/BpcPricing's settings-file discipline).
    """

    DEFAULT_ASSUMED_LEVEL = 4

    def __init__(self, market: "IndustryMarketData", sde_industry):
        self.market = market
        self.sde = sde_industry
        self.assumed_level = self.DEFAULT_ASSUMED_LEVEL
        self._load()

    def _load(self):
        if not os.path.exists(SETTINGS_FILE):
            return
        try:
            with open(SETTINGS_FILE, "r") as f:
                data = json.load(f)
            level = data.get("assumed_invention_skill")
            if level is not None:
                self.assumed_level = int(level)
        except (json.JSONDecodeError, IOError, ValueError, TypeError) as e:
            print(f"[IndustryDiag] assumed_invention_skill load error: {e}")

    def set_assumed_level(self, level: int) -> None:
        self.assumed_level = max(0, int(level))
        try:
            data = {}
            if os.path.exists(SETTINGS_FILE):
                with open(SETTINGS_FILE, "r") as f:
                    data = json.load(f)
            data["assumed_invention_skill"] = self.assumed_level
            with open(SETTINGS_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except (json.JSONDecodeError, IOError) as e:
            print(f"[IndustryDiag] assumed_invention_skill save error: {e}")

    def _skill_level(self, skill_id: Optional[int], skill_level_fn) -> int:
        """Real level via skill_level_fn(skill_id) if available, else the
        write-in assumed level."""
        if skill_id is not None and skill_level_fn is not None:
            lvl = skill_level_fn(skill_id)
            if lvl is not None:
                return int(lvl)
        return self.assumed_level

    def resolve(self, product_type_id: int, *, input_price_fn,
                skill_level_fn=None,
                decryptor: Optional[Decryptor] = None,
                invention_fac: Optional[FacilityParams] = None,
                invention_system: Optional[int] = None,
                source_bp: Optional[int] = None) -> Optional[dict]:
        """Amortized invented-BPC cost/run + display facts for a T2/T3 product.

        `input_price_fn(tid) -> float` prices datacores/decryptor/relics
        (caller's material-pricing callable). `skill_level_fn(skill_type_id) ->
        Optional[int]` returns a real character's level for a skill, or None
        to fall back to the assumed level (also used when skill_level_fn
        itself is None). `invention_fac` supplies cost-bonus/tax/SCC/alpha for
        both the invention and copying jobs — its `system_cost_index` field is
        IGNORED here; the per-activity indices come from
        `self.market.cost_index(invention_system, "invention"/"copying")`
        instead (science jobs use activity-specific indices, not the
        manufacturing one a bare FacilityParams might carry).

        A T2 blueprint has ONE source (its T1 blueprint); a T3 blueprint has
        THREE relic sources (Intact/Malfunctioning/Wrecked), each with its own
        base probability and base_runs (Phase 6). Every source is costed;
        `source_bp` picks one explicitly (the detail panel's relic-quality
        selector), otherwise the CHEAPEST VIABLE source wins (all consumables
        priced; degrades to computable-then-anything so the GUI always gets
        facts + its unpriced warning instead of a blank section). A relic is a
        CONSUMED input — priced into attempt_materials_cost — and has no copy
        step (copy_job_cost 0); a T1 source instead pays the per-attempt 1-run
        copy job. The result carries `relic` ({"type_id", "price"} or None)
        and `source_options` (per-source facts for the selector).

        Returns None if there's no invention path for this product (no
        blueprint, no invention sources, no invention data for any source, no
        matching invented-product row, or no T2/T3 manufacturing recipe to
        value EIV against) — never raises.
        """
        try:
            t2_bp = self.sde.get_blueprint_for_item(product_type_id)
            if t2_bp is None:
                return None
            sources = self.sde.get_invention_sources(t2_bp)
            if not sources:
                return None

            # EIV for the science jobs = the INVENTED (T2/T3) blueprint's
            # MANUFACTURING recipe base quantities (PLAN §1b) — NOT the
            # source's materials and NOT the datacores. Shared by every
            # source, so valued once.
            t2_recipe = self.sde.get_recipe(product_type_id)
            if not t2_recipe:
                return None
            t2_recipe_obj = Recipe(output_per_run=t2_recipe["output_per_run"],
                                   materials=t2_recipe["materials"])
            eiv = estimated_item_value(t2_recipe_obj, self.market.adjusted_price)

            fac = invention_fac if invention_fac is not None else FacilityParams()
            inv_index = (self.market.cost_index(invention_system, "invention")
                        if invention_system is not None else 0.0)
            copy_index = (self.market.cost_index(invention_system, "copying")
                         if invention_system is not None else 0.0)

            candidates = []
            for src in sources:
                c = self._resolve_source(t2_bp, src, eiv, fac, inv_index,
                                         copy_index, input_price_fn,
                                         skill_level_fn, decryptor)
                if c is not None:
                    candidates.append(c)
            if not candidates:
                return None

            chosen = None
            if source_bp is not None:
                chosen = next((c for c in candidates
                               if c["source_bp"] == source_bp), None)
            if chosen is None:
                pool = ([c for c in candidates
                         if c["cost_per_run"] > 0 and not c["unpriced"]]
                        or [c for c in candidates if c["cost_per_run"] > 0]
                        or candidates)
                chosen = min(pool, key=lambda c: c["cost_per_run"])

            chosen["source_blueprint_ids"] = sources
            chosen["source_options"] = [
                {"blueprint_id": c["source_bp"],
                 "cost_per_run": c["cost_per_run"],
                 "probability": c["probability"],
                 "runs_per_copy": c["runs_per_copy"],
                 "relic_price": c["relic"]["price"] if c["relic"] else None}
                for c in candidates]
            return chosen
        except Exception as e:
            print(f"[IndustryDiag] InventionPricing.resolve failed for "
                 f"{product_type_id}: {e}")
            return None

    def _resolve_source(self, invented_bp: int, source_bp: int, eiv: float,
                        fac: FacilityParams, inv_index: float,
                        copy_index: float, input_price_fn, skill_level_fn,
                        decryptor: Optional[Decryptor]) -> Optional[dict]:
        """Full invention costing for ONE source (a T1 blueprint or a T3
        relic). Returns the resolve() result dict minus the multi-source keys
        (`source_blueprint_ids`/`source_options`), or None if this source has
        no activity-8 row producing `invented_bp`."""
        inv = self.sde.get_invention(source_bp)
        if not inv:
            return None
        product_entry = next(
            (p for p in inv["products"] if p["blueprint_id"] == invented_bp),
            None)
        if product_entry is None:
            return None
        base_prob = product_entry["probability"]
        base_runs = product_entry["base_runs"]

        # Skill classification: the encryption skill is identifiable by
        # name ("* Encryption Methods" — incl. "Sleeper Encryption Methods"
        # on T3 relics); the other two are datacore sciences. If skills are
        # missing (old SDE / skills CSV not imported) or name resolution
        # fails (main SDE absent), fall back to the assumed level for all
        # three — classification only matters when real per-skill levels
        # differ.
        reqs = self.sde.get_required_skills(source_bp, ACTIVITY_INVENTION)
        enc_id = sci1_id = sci2_id = None
        if reqs:
            try:
                from sde.sde_manager import get_sde_manager
                mgr = get_sde_manager()
                sci_ids = []
                for sid, _lvl in reqs:
                    name = mgr.get_type_name(sid) or ""
                    if "encryption methods" in name.lower():
                        enc_id = sid
                    else:
                        sci_ids.append(sid)
                if len(sci_ids) >= 1:
                    sci1_id = sci_ids[0]
                if len(sci_ids) >= 2:
                    sci2_id = sci_ids[1]
            except Exception as e:
                print(f"[IndustryDiag] invention skill-name resolve "
                     f"failed (using assumed level): {e}")
                enc_id = sci1_id = sci2_id = None

        sci1 = self._skill_level(sci1_id, skill_level_fn)
        sci2 = self._skill_level(sci2_id, skill_level_fn)
        enc = self._skill_level(enc_id, skill_level_fn)

        decryptor_mult = decryptor.probability_mult if decryptor else 1.0
        probability = invention_probability(base_prob, sci1, sci2, enc,
                                            decryptor_mult)
        outcome = invention_outcome(base_runs, decryptor)

        unpriced: List[int] = []
        attempt_materials_cost = 0.0
        datacores = []
        for tid, qty in inv["datacores"]:
            unit_px = input_price_fn(tid)
            if not unit_px or unit_px <= 0:
                unpriced.append(tid)
                unit_px = 0.0
            attempt_materials_cost += qty * unit_px
            datacores.append((tid, qty, unit_px))

        decryptor_type_id = None
        if decryptor is not None:
            decryptor_type_id = decryptor.type_id
            dec_px = input_price_fn(decryptor.type_id)
            if not dec_px or dec_px <= 0:
                unpriced.append(decryptor.type_id)
                dec_px = 0.0
            attempt_materials_cost += dec_px

        # Relic vs T1-copy source: a T3 relic manufactures nothing of its
        # own, so get_product_for_blueprint is None. The relic itself is
        # CONSUMED per attempt (priced like any material — thin-market
        # history fallback comes with input_price_fn) and there is no copy
        # step. A T1 source instead pays a per-attempt 1-run copy job
        # (best-effort match of eve-ref's CopyingCalculator — ⚠ verify
        # in-game) whose EIV is the T1 BP's own manufacturing materials.
        relic = None
        copy_cost_estimated = False
        t1_product = self.sde.get_product_for_blueprint(source_bp)
        if t1_product is None:
            relic_px = input_price_fn(source_bp)
            if not relic_px or relic_px <= 0:
                unpriced.append(source_bp)
                relic_px = 0.0
            attempt_materials_cost += relic_px
            relic = {"type_id": source_bp, "price": relic_px}
            copy_job_cost = 0.0
        else:
            t1_recipe = self.sde.get_recipe(t1_product)
            if t1_recipe:
                t1_recipe_obj = Recipe(output_per_run=t1_recipe["output_per_run"],
                                       materials=t1_recipe["materials"])
                eiv_t1 = estimated_item_value(t1_recipe_obj,
                                              self.market.adjusted_price)
            else:
                eiv_t1 = 0.0
                copy_cost_estimated = True
            copy_job_cost = science_job_cost(eiv_t1, copy_index, fac)

        invention_job_cost = science_job_cost(eiv, inv_index, fac)

        cost_per_run = invention_cost_per_run(
            attempt_materials_cost, invention_job_cost, copy_job_cost,
            probability, outcome["runs"])

        return {
            "cost_per_run": cost_per_run,
            "probability": probability,
            "me": outcome["me"],
            "te": outcome["te"],
            "runs_per_copy": outcome["runs"],
            "expected_attempts_per_copy": (
                1.0 / probability if probability > 0 else 0.0),
            "datacores": datacores,
            "decryptor_type_id": decryptor_type_id,
            "attempt_materials_cost": attempt_materials_cost,
            "invention_job_cost": invention_job_cost,
            "copy_job_cost": copy_job_cost,
            "unpriced": unpriced,
            "copy_cost_estimated": copy_cost_estimated,
            "source_bp": source_bp,
            "relic": relic,
        }


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------

class IndustryMarketData:
    """Reads Scout's caches; ESI only for the TTL'd industry endpoints."""

    def __init__(self, get_client):
        self.get_client = get_client  # () -> api.ESIClient (shared instance)

        self.adjusted_prices: Dict[int, float] = {}          # tid -> adjusted px
        self.cost_indices: Dict[int, Dict[str, float]] = {}  # sid -> {activity: idx}
        self.station_prices: Dict[int, Dict[int, dict]] = {} # station_id -> {tid: {buy,sell}}
        self.history: Dict[int, Dict[int, dict]] = {}        # region_id -> {tid: summary}

        self.last_update: Optional[str] = None
        self.industry_fetched_at: Optional[str] = None
        self.freshness_note: str = ""
        self._load_cache()

    # -- cache I/O ----------------------------------------------------------

    def _load_cache(self):
        if not os.path.exists(CACHE_FILE):
            return
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            self.adjusted_prices = {int(k): v for k, v in
                                    data.get("adjusted_prices", {}).items()}
            self.cost_indices = {int(k): v for k, v in
                                 data.get("cost_indices", {}).items()}
            self.station_prices = {
                int(s): {int(t): px for t, px in tids.items()}
                for s, tids in data.get("station_prices", {}).items()}
            self.history = {
                int(r): {int(t): h for t, h in tids.items()}
                for r, tids in data.get("history", {}).items()}
            self.last_update = data.get("last_update")
            self.industry_fetched_at = data.get("industry_fetched_at")
        except (json.JSONDecodeError, IOError, ValueError) as e:
            print(f"[IndustryDiag] cache load error: {e}")

    def save_cache(self):
        try:
            self.last_update = datetime.now().isoformat()
            data = {
                "adjusted_prices": {str(k): v for k, v in self.adjusted_prices.items()},
                "cost_indices": {str(k): v for k, v in self.cost_indices.items()},
                "station_prices": {
                    str(s): {str(t): px for t, px in tids.items()}
                    for s, tids in self.station_prices.items()},
                "history": {
                    str(r): {str(t): h for t, h in tids.items()}
                    for r, tids in self.history.items()},
                "last_update": self.last_update,
                "industry_fetched_at": self.industry_fetched_at,
            }
            with open(CACHE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            print(f"[IndustryDiag] cache save error: {e}")

    # -- refresh ------------------------------------------------------------

    def refresh_all(self, type_ids: List[int], system_ids: List[int],
                    hubs: List[Tuple[int, int]],
                    product_type_ids: Optional[List[int]] = None,
                    callback=None) -> str:
        """Refresh from caches. Sync — call from a worker thread.

        type_ids:   all materials + products needing prices/adjusted prices.
        system_ids: facility systems needing cost indices.
        hubs:       list of (region_id, station_id) to read order dumps for.
        product_type_ids: finished products needing 7d/30d history.
        Returns a freshness note (also on self.freshness_note).
        """
        notes = []
        wanted = set(type_ids)

        if callback:
            callback("Industry data...", 5, 100)
        if self._industry_stale():
            try:
                self._fetch_industry(wanted, set(system_ids))
                notes.append("industry: fresh")
            except Exception as e:
                print(f"[IndustryDiag] industry fetch failed: {e}")
                notes.append("industry: FAILED (cached)")
        else:
            notes.append("industry: cached")

        if callback:
            callback("Reading order dumps...", 35, 100)
        client = self.get_client()
        seen_regions = set()
        for region_id, station_id in hubs:
            if region_id in seen_regions:
                # already have the dump in memory; just re-price this station
                pass
            orders, age_note = self._read_dump(client, region_id)
            seen_regions.add(region_id)
            if orders is None:
                notes.append(f"{station_id}: no dump (run a scan)")
                continue
            self.station_prices.setdefault(station_id, {}).update(
                self._best_station_prices(orders, station_id, wanted))
            notes.append(f"region {region_id}: dump {age_note}")

        if callback:
            callback("Reading history DB...", 75, 100)
        # History for the FULL wanted set (products AND materials), per hub
        # region: products need it for sell-side margins, materials for the
        # input-price fallback (a thin material with no live order falls back to
        # its market-history average instead of pricing at 0).
        hist_ids = list(wanted | set(product_type_ids or []))
        if hist_ids:
            try:
                from history.market_history import get_market_history_db
                db = get_market_history_db()
                for region_id in {r for r, _ in hubs}:
                    self.history.setdefault(region_id, {}).update(
                        self._summarize_bulk(db.get_history_bulk(
                            region_id, hist_ids, days=HISTORY_FETCH_DAYS)))
                notes.append("history: db")
            except Exception as e:
                print(f"[IndustryDiag] history read failed: {e}")
                notes.append("history: FAILED (cached)")

        self.save_cache()
        if callback:
            callback("Complete!", 100, 100)
        self.freshness_note = " | ".join(notes)
        print(f"[IndustryDiag] Refresh: {self.freshness_note}")
        return self.freshness_note

    def _industry_stale(self) -> bool:
        if not self.industry_fetched_at:
            return True
        try:
            fetched = datetime.fromisoformat(self.industry_fetched_at)
        except ValueError:
            return True
        return (datetime.now() - fetched).total_seconds() > INDUSTRY_TTL

    def _fetch_industry(self, needed_tids: Set[int], wanted_systems: Set[int]):
        """Adjusted prices + cost indices via the shared client (2 calls)."""
        client = self.get_client()

        async def _run():
            # Shared client's session is per-event-loop and lazily created; on
            # this fresh loop we must create it ourselves or _get crashes on
            # session=None (the drug "industry: FAILED" bug).
            session = client.ensure_session()
            try:
                return await asyncio.gather(
                    client._get("/markets/prices/"),
                    client._get("/industry/systems/"))
            finally:
                await session.close()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            prices, systems = loop.run_until_complete(_run())
        finally:
            loop.close()

        for item in prices or []:
            tid = item.get("type_id")
            if tid in needed_tids and item.get("adjusted_price"):
                self.adjusted_prices[tid] = item["adjusted_price"]

        for entry in systems or []:
            sid = entry.get("solar_system_id")
            if sid not in wanted_systems:
                continue
            indices = {}
            for ci in entry.get("cost_indices", []):
                if ci.get("activity") in INDUSTRY_ACTIVITIES:
                    indices[ci["activity"]] = ci.get("cost_index", 0)
            self.cost_indices[sid] = indices

        self.industry_fetched_at = datetime.now().isoformat()

    def _read_dump(self, client, region_id: int):
        """(orders, age_note) from existing caches, or (None, '').

        peek_cached_orders (age-only), NOT get_cached_orders (which also rejects
        on the ~5-min ESI Expires of market orders) — for build-cost math a
        stale dump is fine.
        """
        peeked = client.order_cache.peek_cached_orders(
            region_id, max_age_seconds=DUMP_MAX_AGE)
        if peeked is not None:
            orders, age = peeked
            if age < 5400:
                age_note = f"{age / 60:.0f} min old"
            else:
                age_note = f"{age / 3600:.1f} h old"
            return orders, age_note

        if region_id == 10000002 and getattr(client, "jita_orders_cache", None):
            return client.jita_orders_cache, client.get_jita_cache_age()
        return None, ""

    @staticmethod
    def _best_station_prices(orders: list, station_id: int,
                             wanted: Set[int]) -> Dict[int, dict]:
        """Best buy/sell per wanted type at one station from a region dump."""
        best = {tid: {"buy": 0.0, "sell": float("inf")} for tid in wanted}
        for order in orders:
            tid = order.get("type_id")
            if tid not in wanted or order.get("location_id") != station_id:
                continue
            price = order["price"]
            if order["is_buy_order"]:
                if price > best[tid]["buy"]:
                    best[tid]["buy"] = price
            elif price < best[tid]["sell"]:
                best[tid]["sell"] = price
        return {tid: {"buy": b["buy"],
                      "sell": 0 if b["sell"] == float("inf") else b["sell"]}
                for tid, b in best.items()}

    @staticmethod
    def _summarize_bulk(bulk: Dict[int, list]) -> Dict[int, dict]:
        """7d/30d summaries from market_history rows (newest first).

        The windows are CALENDAR windows: the 7-day average is the mean over the
        trades whose `date` falls in the last 7 days, the 30-day over the last 30
        — NOT the 7/30 most recent trade-day rows. Source is the LOCAL
        market_history.db (everef archive — no ESI), which only stores rows for
        days an item actually traded, so a thin item might have just 2 rows in
        the 7-day window and 6 in the 30-day window; averaging by row index
        instead would pull months-old prices into the "7-day" figure.

        Outlier rejection: each day's average is compared to the item's ~3-month
        average (the full `bulk` window, expected ~HISTORY_FETCH_DAYS) and any day
        under OUTLIER_MIN_FRACTION of it is dropped before windowing — this kills
        manipulation/junk days (e.g. a one-off 0.01-ISK contract row) that would
        otherwise tank the 7-day mean.

        `days` = clean trade-day count in the 30-day window (display only).
        """
        from datetime import date, timedelta
        today = date.today()
        cut_7 = (today - timedelta(days=7)).isoformat()
        cut_30 = (today - timedelta(days=30)).isoformat()
        out = {}
        for tid, rows in bulk.items():
            def avg(entries):
                if not entries:
                    return 0, 0
                vol = sum(r.get("volume", 0) for r in entries) / len(entries)
                price = sum(r.get("average", 0) for r in entries) / len(entries)
                return round(vol, 1), round(price, 2)

            # 3-month baseline (priced days only) → discard sub-50% days.
            priced = [r.get("average", 0) for r in rows if r.get("average", 0) > 0]
            baseline = sum(priced) / len(priced) if priced else 0.0
            threshold = baseline * OUTLIER_MIN_FRACTION
            clean = [r for r in rows
                     if r.get("average", 0) > 0
                     and (baseline <= 0 or r["average"] >= threshold)]

            rows_7 = [r for r in clean if (r.get("date") or "") >= cut_7]
            rows_30 = [r for r in clean if (r.get("date") or "") >= cut_30]
            vol_7, price_7 = avg(rows_7)
            vol_30, price_30 = avg(rows_30)
            out[tid] = {"avg_volume_7d": vol_7, "avg_price_7d": price_7,
                        "avg_volume_30d": vol_30, "avg_price_30d": price_30,
                        "days": len(rows_30)}
        return out

    # -- lookups (sync, read cache) -----------------------------------------

    def adjusted_price(self, type_id: int) -> float:
        return self.adjusted_prices.get(type_id, 0.0)

    def station_price(self, station_id: int, type_id: int,
                      side: str = "sell") -> float:
        return self.station_prices.get(station_id, {}).get(
            type_id, {}).get(side, 0.0)

    def cost_index(self, system_id: int, activity: str = "manufacturing") -> float:
        return self.cost_indices.get(system_id, {}).get(activity, 0.0)

    def product_history(self, region_id: int, type_id: int) -> dict:
        return self.history.get(region_id, {}).get(type_id, {})

    def get_last_update(self) -> str:
        if not self.last_update:
            return "Never"
        try:
            return datetime.fromisoformat(self.last_update).strftime(
                "%Y-%m-%d %H:%M")
        except ValueError:
            return self.last_update


# ---------------------------------------------------------------------------
# Engine provider — adapts market data + SDE recipes to MarketDataProvider
# ---------------------------------------------------------------------------

class IndustryProvider:
    """Implements industry_engine.MarketDataProvider for one scan context:
    a buy hub (material input prices), a sell hub (product output prices), a
    facility system (cost index), and the patient/impatient input side.

    Tier-1: pair with IndustryCalculator(buildable=lambda tid: False) so all
    materials resolve to terminal market buys.
    """

    def __init__(self, market: IndustryMarketData, sde_industry,
                 *, buy_station: int, sell_station: int, sell_region: int,
                 facility_system: int, input_side: str = "patient",
                 buy_region: Optional[int] = None):
        self.market = market
        self.sde = sde_industry
        self.buy_station = buy_station
        self.buy_region = buy_region
        self.sell_station = sell_station
        self.sell_region = sell_region
        self.facility_system = facility_system
        # patient input = highest buy order; impatient = lowest sell order
        self.input_field = "buy" if input_side == "patient" else "sell"

    def recipe(self, type_id: int) -> Optional[Recipe]:
        r = self.sde.get_recipe(type_id)
        if r:
            # Manufacturing path unchanged byte-for-byte (activity left at its
            # dataclass default "manufacturing") — pre-5.4 T1 behavior is
            # untouched.
            return Recipe(output_per_run=r["output_per_run"],
                          materials=r["materials"])
        # No manufacturing recipe — try a reaction formula (Stage 5.4). Old
        # (pre-5.1) SDEs return None from get_recipe_for_activity, so this
        # degrades to None exactly like before for every existing install.
        r = self.sde.get_recipe_for_activity(type_id, ACTIVITY_REACTION)
        if r:
            return Recipe(output_per_run=r["output_per_run"],
                          materials=r["materials"], activity="reaction")
        return None

    def input_price(self, type_id: int) -> float:
        px = self.market.station_price(self.buy_station, type_id,
                                       self.input_field)
        if px and px > 0:
            return px
        # No live order in the dump for this material (thin market). Fall back to
        # its recent market-history average so the build cost isn't understated
        # to 0 (e.g. abyssal mutaplasmid inputs). 7d preferred, else 30d.
        if self.buy_region is not None:
            h = self.market.product_history(self.buy_region, type_id)
            return h.get("avg_price_7d") or h.get("avg_price_30d") or 0.0
        return 0.0

    def adjusted_price(self, type_id: int) -> float:
        return self.market.adjusted_price(type_id)

    def cost_index(self, system_id: int, activity: str = "manufacturing") -> float:
        return self.market.cost_index(system_id, activity)

    def sell_info(self, type_id: int) -> SellInfo:
        h = self.market.product_history(self.sell_region, type_id)
        return SellInfo(
            lowest_sell=self.market.station_price(self.sell_station, type_id, "sell"),
            highest_buy=self.market.station_price(self.sell_station, type_id, "buy"),
            avg_7d=h.get("avg_price_7d", 0.0),
            avg_30d=h.get("avg_price_30d", 0.0),
            volume=h.get("avg_volume_7d", 0.0),
            history_days=h.get("days", 0),
        )


def make_facility(constants: JobCostConstants, system_cost_index: float,
                  *, cost_bonus_pct: float = 0.0, material_bonus_pct: float = 0.0,
                  facility_tax_pct: Optional[float] = None) -> FacilityParams:
    """Build a FacilityParams from the global constants + per-facility inputs.
    facility_tax_pct override lets a player structure supply its owner-set tax.
    """
    return FacilityParams(
        system_cost_index=system_cost_index,
        cost_bonus_pct=cost_bonus_pct,
        material_bonus_pct=material_bonus_pct,
        facility_tax_pct=(constants.facility_tax_pct if facility_tax_pct is None
                          else facility_tax_pct),
        scc_surcharge_pct=constants.scc_surcharge_pct,
        alpha_clone_tax_pct=constants.alpha_clone_tax_pct,
    )
