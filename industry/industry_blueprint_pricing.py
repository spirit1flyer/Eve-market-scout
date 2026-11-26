"""Blueprint acquisition estimates; manual overrides beat saved Jita offers."""

import math
from collections import defaultdict

from core.sound_manager import get_data_dir
from industry.industry_contract_prices import get_blueprint_price_store, read_cached_offers
from industry.industry_diagnostics import _zero_input_count


class BpcPricing:
    """Compatible Owned-panel resolver plus material-aware browser costing."""

    def __init__(self, store=None, settings_file=None, excluded_ids_provider=None):
        self.store = store or get_blueprint_price_store()
        self._excluded_ids = excluded_ids_provider or (lambda: set())
        self.store.migrate_overrides(settings_file or get_data_dir() / "industry_settings.json")

    def get_writein(self, blueprint_type_id, is_copy=True):
        return next((r for r in self.store.overrides()
                     if r["blueprint_id"] == int(blueprint_type_id)
                     and bool(r["is_copy"]) == is_copy), None)

    def set_writein(self, blueprint_type_id, price, runs, me=None, te=None, is_copy=True):
        if price > 0 and (me is None or te is None):
            previous = self.get_writein(blueprint_type_id, is_copy) or {}
            me = previous.get("me") if me is None else me
            te = previous.get("te") if te is None else te
        self.store.set_override(blueprint_type_id, price, runs, me, te, is_copy)

    def bootstrap(self, contracts_db, blueprint_ids):
        if contracts_db is not None and self.store.latest() is None:
            try:
                rows, checked = read_cached_offers(contracts_db, blueprint_ids, self._excluded_ids())
                if rows:
                    self.store.save_snapshot(rows, checked, complete=False)
            except Exception as exc:
                print(f"[BlueprintPrices] cached-offer seed failed; daily refresh remains due: {exc}")

    def view(self):
        """One plain snapshot per compute pass, not one SQLite read per item."""
        offers = defaultdict(list)
        excluded = set(self._excluded_ids())
        for row in self.store.offers():
            if row["contract_id"] in excluded:
                continue
            offers[(row["blueprint_id"], bool(row["is_copy"]))].append(row)
        overrides = {(r["blueprint_id"], bool(r["is_copy"])): r
                     for r in self.store.overrides()}
        return offers, overrides

    @staticmethod
    def _base(source="unset", **values):
        return dict({"source": source, "per_run": 0.0, "price": 0.0,
                     "runs": 0, "offer_count": 0, "sufficient": False}, **values)

    def resolve(self, blueprint_type_id, regions=None, contracts_db=None, *,
                view=None, batch=1, material_cost=None, is_copy=True,
                comparable_me=None, comparable_te=None):
        """Choose by complete build cost when ME/materials are known.

        material_cost(me) returns build cost/run without the BPC, or None when
        inputs are incomplete. Without a complete comparison, use cheapest
        known ISK/run. Missing/zero runs are never invented as one run.
        """
        offers, overrides = view if view is not None else self.view()
        if blueprint_type_id is None:
            return self._base()
        key = (int(blueprint_type_id), bool(is_copy))
        override = overrides.get(key)
        if override:
            runs = int(override.get("runs") or 0)
            return self._base("write-in", price=override["price"], runs=runs,
                              per_run=override["price"] / runs if is_copy else 0.0,
                              me=override.get("me"), te=override.get("te"),
                              sufficient=not is_copy or runs >= batch)
        candidates = list(offers.get(key, []))
        if not is_copy:
            if not candidates:
                return self._base()
            best = min(candidates, key=lambda r: (r["price"] / r["quantity"], r["contract_id"]))
            return self._base("contract", **self._offer_fields(best),
                              price=best["price"] / best["quantity"],
                              offer_count=len(candidates), sufficient=True)
        usable = [r for r in candidates if isinstance(r.get("runs"), int)
                  and r["runs"] >= batch and r["runs"] > 0]
        # Owned copies keep their actual ME/TE; price against like research if possible.
        comparable = [r for r in usable
                      if (comparable_me is None or r["me"] == comparable_me)
                      and (comparable_te is None or r["te"] == comparable_te)]
        if comparable_me is not None and comparable:
            usable = comparable
        if not usable:
            return self._base("insufficient" if candidates else "unset",
                              offer_count=len(candidates))
        costs = {}
        complete = material_cost is not None
        if complete:
            for row in usable:
                me = row.get("me")
                if me is None or not 0 <= me <= 10:
                    complete = False
                    break
                if me not in costs:
                    costs[me] = material_cost(me)
                if costs[me] is None or not math.isfinite(costs[me]):
                    complete = False
                    break
        def score(row):
            per_run = row["price"] / row["runs"]
            return (per_run + (costs[row["me"]] if complete else 0),
                    per_run, row["contract_id"])
        best = min(usable, key=score)
        return self._base("contract", **self._offer_fields(best), price=best["price"],
                          per_run=best["price"] / best["runs"], offer_count=len(usable),
                          sufficient=True, comparison="build cost" if complete else "ISK/run")

    @staticmethod
    def _offer_fields(row):
        return {k: row[k] for k in ("contract_id", "runs", "me", "te", "observed_at",
                                    "source_checked_at", "location_id", "expires_at")}

    def cost_build(self, calc, fac, fees, tid, blueprint_id, has_bpo, params, view):
        batch = params["batch"]
        def material_cost(me):
            result = calc.calc_full(tid, fac, fees, me=me, batch=batch)
            if not result or _zero_input_count(result.get("inputs") or []):
                return None
            return result["total_build"] / batch
        quote = self.resolve(blueprint_id, view=view, batch=batch,
                             material_cost=material_cost, is_copy=not has_bpo)
        me = params["me"]
        if not has_bpo and quote.get("me") is not None:
            me = quote["me"]
        result = calc.calc_full(tid, fac, fees, me=me, batch=batch,
                                blueprint_cost_per_run=quote["per_run"])
        if result:
            result["_blueprint"] = dict(quote, blueprint_id=blueprint_id, is_copy=not has_bpo)
            result["te"] = (quote.get("te") if not has_bpo and quote.get("te") is not None
                            else params["te"])
            if not has_bpo and not quote["sufficient"]:
                # A missing copy cost must not produce an apparently complete profit.
                for name in result:
                    if name.endswith(("_profit", "_margin")):
                        result[name] = None
        return result
