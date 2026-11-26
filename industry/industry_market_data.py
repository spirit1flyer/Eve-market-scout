"""Industry tab market-data layer + engine provider.

Mirrors drug_api.py's discipline: NO market sync of its own. The scanner
already pulls full region order dumps; this module only READS caches Scout
maintains —
  * material/product prices: order_cache region dumps (peek_cached_orders)
  * 7d/30d history:          market_history.db (everef archive)
The only ESI traffic is the two universe-wide industry endpoints nothing else
fetches — /markets/prices/ (CCP adjusted prices, for EIV) and /industry/systems/
(system cost indices) — via the shared durable public-response provider.

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
from core.esi_diagnostics import format_refresh_wait
from core.esi_public_cache import next_refresh_seconds
from gui.gui_async import run_coro_blocking

from industry.industry_engine import Recipe, SellInfo, FacilityParams, SellFees
from industry.industry_blueprint_pricing import BpcPricing  # Owned-panel compatibility

CACHE_FILE = str(get_data_dir() / "industry_market_cache.json")
SETTINGS_FILE = str(get_data_dir() / "industry_settings.json")

# Region dumps up to a day old are fine — the scanner refreshes them far more
# often; a stale dump beats no data (same call as drug_api).
DUMP_MAX_AGE = 86400
# History fetch window. ~3 months so the manipulation-baseline (below) has a
# stable average to compare against; the 7d/30d figures are calendar subsets.
HISTORY_FETCH_DAYS = 90
# Outlier rejection: drop any trade-day whose average price is under this
# fraction of the item's ~3-month average. Kills manipulation/junk days (e.g. a
# single 0.01-ISK contract-driven row) that would otherwise crater the 7d mean.
OUTLIER_MIN_FRACTION = 0.5

# Industry activities we pull cost indices for. T1 only needs manufacturing;
# the rest are pulled now so later phases (research/invention/reactions) need
# no refetch.
INDUSTRY_ACTIVITIES = (
    "manufacturing", "research_material_efficiency",
    "research_time_efficiency", "copying", "invention",
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
        try:
            # Filter the shared full snapshots every time: newly selected IDs
            # must not wait six hours merely because this view was opened.
            notes.append(self._fetch_industry(wanted, set(system_ids)))
        except Exception as e:
            print(f"[IndustryDiag] industry fetch failed: {e}")
            notes.append("industry: FAILED (cached)")

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

    def _fetch_industry(self, needed_tids: Set[int], wanted_systems: Set[int]):
        """Read full shared snapshots, acquiring only when eligible."""
        client = self.get_client()

        async def _run(api):
            # Drain both before closing the worker loop, including on failure.
            results = await asyncio.gather(
                api.get_public_industry_snapshot("/markets/prices/"),
                api.get_public_industry_snapshot("/industry/systems/"),
                return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            return results

        snapshots = run_coro_blocking(client, _run)
        prices, systems = [snapshot["payload"] for snapshot in snapshots]

        for item in prices or []:
            tid = item.get("type_id")
            if tid in needed_tids and "adjusted_price" in item:
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

        oldest = min(snapshot["acquired_at"] for snapshot in snapshots)
        self.industry_fetched_at = oldest.isoformat()
        now = datetime.now(timezone.utc)
        age = max(0, (now - oldest).total_seconds())
        next_refresh = ", ".join(
            f"{label} in {format_refresh_wait(next_refresh_seconds(snapshot, now))}"
            for label, snapshot in zip(("prices", "indices"), snapshots))
        durability = ("" if all(s["durable"] for s in snapshots)
                      else "; disk save FAILED (memory only)")
        return (f"industry: snapshot {age / 60:.0f} min old; "
                f"next eligible refresh: {next_refresh}{durability}")

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
        cut_90 = (today - timedelta(days=HISTORY_FETCH_DAYS)).isoformat()
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
            rows_90 = [r for r in clean if (r.get("date") or "") >= cut_90]
            vol_7, price_7 = avg(rows_7)
            vol_30, price_30 = avg(rows_30)
            vol_90, price_90 = avg(rows_90)
            out[tid] = {"avg_volume_7d": vol_7, "avg_price_7d": price_7,
                        "avg_volume_30d": vol_30, "avg_price_30d": price_30,
                        "avg_volume_90d": vol_90, "avg_price_90d": price_90,
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
        if not r:
            return None
        return Recipe(output_per_run=r["output_per_run"],
                      materials=r["materials"])

    def input_price(self, type_id: int) -> float:
        px = self.market.station_price(self.buy_station, type_id,
                                       self.input_field)
        if px and px > 0:
            return px
        # No live order in the dump for this material (thin market). Fall back to
        # its recent market-history average so the build cost isn't understated
        # to 0 (e.g. abyssal mutaplasmid inputs). Prefer 7d, then 30d, then 90d.
        if self.buy_region is not None:
            h = self.market.product_history(self.buy_region, type_id)
            return (h.get("avg_price_7d") or h.get("avg_price_30d")
                    or h.get("avg_price_90d") or 0.0)
        return 0.0

    def adjusted_price(self, type_id: int) -> float:
        return self.market.adjusted_price(type_id)

    def cost_index(self, system_id: int) -> float:
        return self.market.cost_index(system_id, "manufacturing")

    def sell_info(self, type_id: int) -> SellInfo:
        h = self.market.product_history(self.sell_region, type_id)
        return SellInfo(
            lowest_sell=self.market.station_price(self.sell_station, type_id, "sell"),
            highest_buy=self.market.station_price(self.sell_station, type_id, "buy"),
            avg_7d=h.get("avg_price_7d", 0.0),
            avg_30d=h.get("avg_price_30d", 0.0),
            avg_90d=h.get("avg_price_90d", 0.0),
            volume=(h.get("avg_volume_7d") or h.get("avg_volume_30d")
                    or h.get("avg_volume_90d") or 0.0),
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
