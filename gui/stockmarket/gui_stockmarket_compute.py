"""Pure-compute helpers for the cold-start orchestrator.

Lifts the SQL/loop logic from StockMarketHubPanel's per-hub methods
(`apply_material_filter`, `_run_leading_indicators_background`) into
plain functions the orchestrator can call from its worker thread.

These helpers intentionally duplicate logic from
gui_stockmarket_hub_filters.py rather than refactoring it.  The
existing per-hub flow is the working code path the user relies on
between cold starts; we don't want a shared helper to introduce risk
into that path.  Once the cold-start orchestrator fully owns startup
the duplication can be revisited.

Signatures:

    classify_floor_trend(yearly_stats) -> "low" | "medium" | "high" | "none"

    run_material_filter_compute(
        hub_key, region_id, profiles_manager,
        progress_cb=None,
    ) -> {"analyzed": int, "promoted": int}

    run_leading_indicators_compute(
        hub_key, region_id, profiles_manager,
        progress_cb=None,
    ) -> {"computed": int, "skipped": int, "errored": int}

`progress_cb` is `Callable[[int, int, str], None]` — current, total,
detail.  Called from the worker thread.  Caller is responsible for
marshalling onto the main thread if it touches Tk widgets.
"""

import time
from typing import Callable, Dict, Optional


# =============================================================================
# Pure floor-trend classifier — duplicate of
# StockMarketHubRefreshMixin._get_floor_trend.  Kept in sync manually.
# =============================================================================

def classify_floor_trend(yearly_stats: dict) -> str:
    """Classify floor trend from yearly_stats.

    Returns "low" (stable), "medium" (rising), "high" (declining),
    or "none" (insufficient data / no clear pattern).
    """
    if len(yearly_stats) < 2:
        return "none"

    years = sorted(yearly_stats.keys(), reverse=True)
    floors = [yearly_stats[y].p_low for y in years[:3]]

    if len(floors) < 2:
        return "none"

    declining = all(floors[i] < floors[i + 1] for i in range(len(floors) - 1))
    if declining:
        return "high"

    rising = all(floors[i] > floors[i + 1] for i in range(len(floors) - 1))
    if rising:
        return "medium"

    if len(floors) >= 2:
        avg_floor = sum(floors) / len(floors)
        if avg_floor > 0:
            max_dev = max(
                abs(f - avg_floor) / avg_floor * 100 for f in floors
            )
            if max_dev <= 15:
                return "low"

    return "none"


# =============================================================================
# Material filter compute
# =============================================================================

def run_material_filter_compute(
    hub_key: str,
    region_id: int,
    profiles_manager,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    min_volume: int = 0,
) -> Dict[str, int]:
    """Run material filter compute for a hub.

    Mirrors the body of HubPanel.apply_material_filter's run_filter()
    inner function, minus the per-panel overlay calls and the chain to
    leading indicators.  Marks the 24h tracker complete on success.

    Returns counts: {"analyzed": N, "promoted": M}.
    """
    from analytics.stockmarket_filters import (
        get_material_filter_tracker,
        check_material_risk,
        clear_material_risk_cache_for_region,
    )
    from analytics.material_analysis import prebuild_material_floor_cache
    from sde.sde_industry import get_sde_industry_db
    from history.market_history import get_market_history_db
    from analytics import material_risk_storage

    print(f"[ColdStart-{hub_key}] === Material Filter === "
          f"(region {region_id})")

    clear_material_risk_cache_for_region(region_id, hub_key=hub_key)

    region_profiles = profiles_manager.get_profiles_for_region(region_id)

    if min_volume > 0:
        before = len(region_profiles)
        region_profiles = [
            p for p in region_profiles
            if getattr(p, "avg_daily_volume", 0) >= min_volume
        ]
        print(f"[ColdStart-{hub_key}] MF volume gate: "
              f"{len(region_profiles)}/{before} pass (>= {min_volume}/day)")

    all_stats = profiles_manager.get_all_yearly_stats_for_region(
        region_id, context_label=hub_key
    )

    stable_profiles = []
    for profile in region_profiles:
        yearly_stats = all_stats.get(profile.type_id, {})
        if classify_floor_trend(yearly_stats) == "low":
            stable_profiles.append(profile)

    total = len(stable_profiles)
    print(f"[ColdStart-{hub_key}] {total} stable-floor candidates to analyze")

    if progress_cb:
        progress_cb(0, total, "Loading material prices")

    # Pre-build material floor cache from blueprint inputs.
    industry_db = get_sde_industry_db()
    recent_floors: Dict[int, float] = {}
    baseline_floors: Dict[int, float] = {}
    market_db = get_market_history_db()

    if industry_db.is_available() and stable_profiles:
        unique_materials: set = set()
        for profile in stable_profiles:
            mats = industry_db.get_materials_for_item(profile.type_id)
            if mats:
                for m in mats:
                    unique_materials.add(m.type_id)

        if unique_materials:
            recent_floors, baseline_floors = prebuild_material_floor_cache(
                list(unique_materials),
                market_db,
                context_label=hub_key,
            )

    # Pre-build item price floor cache for the candidates themselves.
    item_recent_floors: Dict[int, float] = {}
    item_baseline_floors: Dict[int, float] = {}
    if stable_profiles:
        stable_type_ids = [p.type_id for p in stable_profiles]
        item_recent_floors, item_baseline_floors = prebuild_material_floor_cache(
            stable_type_ids,
            market_db,
            region_id=region_id,
            context_label=f"{hub_key}-items",
        )

    if progress_cb:
        progress_cb(0, total, "Analyzing materials")

    analyzed = 0
    promoted = 0
    pending_saves = []
    for idx, profile in enumerate(stable_profiles, start=1):
        result = check_material_risk(
            profile.type_id,
            region_id,
            recent_floor_cache=recent_floors,
            baseline_floor_cache=baseline_floors,
            item_floor_recent_cache=item_recent_floors,
            item_floor_baseline_cache=item_baseline_floors,
            hub_key=hub_key,
            persist=False,
        )
        analyzed += 1
        if result == "medium":
            promoted += 1
        pending_saves.append((profile.type_id, region_id, result))

        if progress_cb and (idx % 10 == 0 or idx == total):
            progress_cb(idx, total, f"Analyzing {idx}/{total}")

        time.sleep(0)  # yield GIL so Tk mainloop stays responsive

    if pending_saves:
        if progress_cb:
            progress_cb(total, total, f"Saving {len(pending_saves)} results")
        material_risk_storage.save_batch(pending_saves)

    get_material_filter_tracker().mark_complete(hub_key)
    print(f"[ColdStart-{hub_key}] Material filter: "
          f"{analyzed} analyzed, {promoted} promoted to medium")

    return {"analyzed": analyzed, "promoted": promoted}


# =============================================================================
# Leading indicators compute
# =============================================================================

def run_leading_indicators_compute(
    hub_key: str,
    region_id: int,
    profiles_manager,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    min_volume: int = 0,
) -> Dict[str, int]:
    """Run leading indicators compute for a hub.

    Mirrors HubPanel._run_leading_indicators_background, minus the
    per-panel overlay calls and the chained refresh.  Marks the 24h
    tracker complete on success.

    Returns counts: {"computed": N, "skipped": M, "errored": K}.
    Skipped = no-history items.
    """
    from analytics.leading_indicators_batch import (
        compute_leading_indicators,
        get_reference_date,
    )
    from analytics.leading_indicators_tracker import get_leading_indicators_tracker
    from history.market_history import get_market_history_db
    from analytics import leading_indicators_storage

    region_profiles = profiles_manager.get_profiles_for_region(region_id)

    if min_volume > 0:
        before = len(region_profiles)
        region_profiles = [
            p for p in region_profiles
            if getattr(p, "avg_daily_volume", 0) >= min_volume
        ]
        print(f"[ColdStart-{hub_key}] LI volume gate: "
              f"{len(region_profiles)}/{before} pass (>= {min_volume}/day)")

    total = len(region_profiles)
    print(f"[ColdStart-{hub_key}] === Leading Indicators === "
          f"({total} items, region {region_id})")

    market_db = get_market_history_db()
    type_ids_list = [p.type_id for p in region_profiles]
    all_history = market_db.get_full_history_bulk(
        region_id, type_ids_list, years=3
    )
    history_rows = sum(len(v) for v in all_history.values())
    print(f"[ColdStart-{hub_key}] LI history pre-fetched ({history_rows} rows)")

    # Anchor trend windows at the DB's latest date, not the wall clock —
    # the everef archive lags 1-4 days (review finding 6-2).
    reference_date = get_reference_date(market_db)
    print(f"[ColdStart-{hub_key}] LI window anchor: "
          f"{reference_date or 'wall clock (DB empty)'}")

    if progress_cb:
        progress_cb(0, total, "Computing indicators")

    results = []
    computed = 0
    skipped = 0
    errored = 0

    for idx, profile in enumerate(region_profiles, start=1):
        try:
            result = compute_leading_indicators(
                profile.type_id, region_id,
                history=all_history.get(profile.type_id, []),
                reference_date=reference_date,
            )
            if result is not None:
                results.append(result)
                computed += 1
            else:
                skipped += 1
        except Exception as e:
            errored += 1
            print(f"[ColdStart-{hub_key}] LI error type={profile.type_id}: {e}")

        if progress_cb and (idx % 25 == 0 or idx == total):
            progress_cb(idx, total, f"Computing {idx}/{total}")

        if idx % 100 == 0 or idx == total:
            print(f"[ColdStart-{hub_key}] LI progress: {idx}/{total} "
                  f"(computed={computed}, no-history={skipped}, "
                  f"errored={errored})")

        time.sleep(0)  # yield GIL so Tk mainloop stays responsive

    if results:
        if progress_cb:
            progress_cb(total, total, f"Saving {len(results)} results")
        leading_indicators_storage.save_batch(results)

    get_leading_indicators_tracker().mark_complete(hub_key)
    print(f"[ColdStart-{hub_key}] Leading indicators: "
          f"{computed} computed, {skipped} no-history, {errored} errored")

    return {"computed": computed, "skipped": skipped, "errored": errored}
