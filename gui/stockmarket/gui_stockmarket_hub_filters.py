"""Filter phase mixin for StockMarketHubPanel.

Bundles the lock-overlay management and the two background filter
phases (material filter, then leading indicators) into a single mixin.
Extracted from gui_stockmarket_hub.py to keep that file under the
700-line target.

The two filters share one lifecycle:
  1. Show lock overlay
  2. Material filter background phase (if needed today)
  3. Leading indicators background phase (if needed today)
  4. Refresh display
  5. Hide overlay

Each phase checks its own once-per-day-per-hub tracker, so any
combination of "fresh today" / "already ran" works correctly across
launches.

Required parent (StockMarketHubPanel) attributes:
    self.hub_key: str             - "amarr" / "jita" / etc.
    self.region_id: int
    self.profiles                 - ProfileManager instance
    self.frame: ttk.Frame         - the panel root frame
    self._overlay_frame           - initialized to None in __init__
    self._overlay_status_var      - initialized to None in __init__
    self._overlay_progress        - initialized to None in __init__

Required parent methods:
    self.refresh_display_async(after=callback)
    self._get_floor_trend(yearly_stats) -> str
"""

import tkinter as tk
from tkinter import ttk
import threading
from typing import Dict, Optional

from core.tk_queue import submit


# Imported here so the mixin file can do thread-safety checks the same
# way the host file does. Defined as module-private to avoid clashing
# with the host file's _check_thread.
def _check_thread(context: str):
    """Log a warning + stack trace if called from a non-main thread.

    Mirror of the function in gui_stockmarket_hub.py - this mixin uses
    its own copy so it doesn't depend on import-from-host.
    """
    import traceback
    current = threading.current_thread()
    if current is not threading.main_thread():
        print(f"[THREAD WARNING] {context} called from non-main thread "
              f"'{current.name}'")
        for line in traceback.format_stack(limit=8):
            print(line.rstrip())


class HubFilterPhaseMixin:
    """Mixin providing the filter overlay + material/leading-indicators
    phase logic for StockMarketHubPanel.

    See module docstring for required parent attributes and methods.
    """

    # =========================================================
    # Lock overlay
    # =========================================================

    def _show_filter_overlay(self, message: str, total: int = 0):
        """Show the lock overlay covering the hub panel.

        MUST be called on the main thread. Background threads should
        route through tk_queue.submit().
        """
        _check_thread(f"HubPanel._show_filter_overlay({self.hub_key})")

        if self._overlay_frame is None:
            self._overlay_status_var = tk.StringVar(value=message)

            self._overlay_frame = ttk.Frame(
                self.frame,
                relief="solid",
                borderwidth=1,
            )

            # Center content vertically
            inner = ttk.Frame(self._overlay_frame)
            inner.place(relx=0.5, rely=0.5, anchor="center")

            ttk.Label(
                inner,
                text="Stock Market is locked",
                font=("Segoe UI", 12, "bold"),
            ).pack(pady=(0, 10))

            ttk.Label(
                inner,
                textvariable=self._overlay_status_var,
                font=("Segoe UI", 10),
            ).pack(pady=(0, 10))

            self._overlay_progress = ttk.Progressbar(
                inner,
                mode="determinate",
                length=320,
            )
            self._overlay_progress.pack(pady=(0, 4))
        else:
            self._overlay_status_var.set(message)

        # Configure progress bar range
        if total > 0:
            self._overlay_progress.configure(
                mode="determinate", maximum=total
            )
            self._overlay_progress["value"] = 0
        else:
            # Indeterminate for unknown-length phases (like refresh)
            self._overlay_progress.configure(mode="indeterminate")
            try:
                self._overlay_progress.start(80)
            except Exception:
                pass

        # Place overlay covering the whole frame (above notebook + ticker)
        self._overlay_frame.place(
            relx=0, rely=0, relwidth=1, relheight=1,
        )
        try:
            self._overlay_frame.lift()
        except Exception:
            pass

    def _update_filter_overlay(self, current: int,
                               message: Optional[str] = None):
        """Update overlay progress and status. Main thread only."""
        _check_thread(f"HubPanel._update_filter_overlay({self.hub_key})")

        if self._overlay_frame is None:
            return

        if message is not None and self._overlay_status_var is not None:
            self._overlay_status_var.set(message)

        if self._overlay_progress is not None:
            try:
                self._overlay_progress["value"] = current
            except Exception:
                pass

    def _hide_filter_overlay(self):
        """Remove the lock overlay. Main thread only."""
        _check_thread(f"HubPanel._hide_filter_overlay({self.hub_key})")

        if self._overlay_frame is None:
            return

        if self._overlay_progress is not None:
            try:
                self._overlay_progress.stop()
            except Exception:
                pass

        try:
            self._overlay_frame.place_forget()
        except Exception:
            pass

    # =========================================================
    # Material filter phase
    # =========================================================

    def apply_material_filter(self):
        """Run material filter pass after profiles are fully built.

        This is the SINGLE entry-point for the material filter.
        - Shows the lock overlay so the panel is visibly busy
        - Checks the once-per-hub-per-day tracker
        - If filter is due: pre-builds material floor cache, walks all
          stable-floor profiles, populates the material risk cache
        - Marks the tracker complete
        - Chains to leading indicators phase (which then refreshes)
        - Hides the overlay (via the LI phase's after= callback)

        If the filter already ran today, falls straight to the LI phase
        which itself decides whether to compute or skip to refresh.
        """
        from analytics.stockmarket_filters import get_material_filter_tracker

        tracker = get_material_filter_tracker()
        already_ran = not tracker.should_run(self.hub_key)

        if already_ran:
            # Material filter done. Now check if leading indicators
            # need to run. _run_leading_indicators_phase() handles both
            # cases (run or skip) and ends with the refresh.
            self._show_filter_overlay(
                "Checking indicators...", total=0
            )
            self._run_leading_indicators_phase()
            return

        # Show overlay before kicking off background work
        self._show_filter_overlay(
            "Preparing material filter...", total=0
        )

        def run_filter():
            """Background thread entry: any unhandled exception must still
            chain to the LI phase, or the hub stays locked behind the
            filter overlay until restart."""
            try:
                run_filter_inner()
            except Exception:
                import traceback
                print(f"[StockMarket-{self.hub_key}] Material filter FAILED "
                      f"— skipping to leading indicators:")
                traceback.print_exc()
                # Deliberately NOT tracker.mark_complete: let the filter
                # retry on the next scan instead of skipping until tomorrow.
                submit(lambda: self._run_leading_indicators_phase())

        def run_filter_inner():
            """Background thread: prebuild, analyze, mark complete."""
            from analytics.stockmarket_filters import (
                check_material_risk,
                clear_material_risk_cache_for_region,
            )
            from analytics.material_analysis import prebuild_material_floor_cache
            from sde.sde_industry import get_sde_industry_db
            from history.market_history import get_market_history_db

            print(f"[StockMarket-{self.hub_key}] "
                  f"=== PHASE: Material Filter === "
                  f"(region {self.region_id})")

            # Flush stale 'skip' results from incomplete data
            clear_material_risk_cache_for_region(
                self.region_id, hub_key=self.hub_key
            )

            # Walk profiles to find stable-floor candidates
            region_profiles = self.profiles.get_profiles_for_region(self.region_id)

            # Compute-time volume gate: skip low-volume items entirely.
            min_volume = getattr(self.settings, "min_daily_volume", 0)
            if min_volume > 0:
                before = len(region_profiles)
                region_profiles = [
                    p for p in region_profiles
                    if getattr(p, "avg_daily_volume", 0) >= min_volume
                ]
                print(f"[StockMarket-{self.hub_key}] Volume gate: "
                      f"{len(region_profiles)}/{before} pass "
                      f"(>= {min_volume}/day)")

            # Batched fetch - one query instead of N connections
            all_stats = self.profiles.get_all_yearly_stats_for_region(
                self.region_id, context_label=self.hub_key
            )

            stable_profiles = []
            for profile in region_profiles:
                yearly_stats = all_stats.get(profile.type_id, {})
                trend = self._get_floor_trend(yearly_stats)
                if trend == "low":
                    stable_profiles.append(profile)

            total = len(stable_profiles)
            print(f"[StockMarket-{self.hub_key}] "
                  f"{total} stable-floor candidates to analyze")

            # ---------------------------------------------------------
            # Pre-build material floor cache: collect every unique
            # material across all stable candidates' blueprints, then
            # run two batched SQL queries (recent + baseline) to get
            # all Jita floors at once. This collapses what was N*M*2
            # per-material queries into 2 queries total.
            # ---------------------------------------------------------
            industry_db = get_sde_industry_db()
            recent_floors: Dict[int, float] = {}
            baseline_floors: Dict[int, float] = {}
            market_db = get_market_history_db()

            if industry_db.is_available() and stable_profiles:
                unique_materials: set = set()
                for profile in stable_profiles:
                    mats = industry_db.get_materials_for_item(
                        profile.type_id
                    )
                    if mats:
                        for m in mats:
                            unique_materials.add(m.type_id)

                if unique_materials:
                    submit(
                        lambda c=len(unique_materials):
                        self._update_filter_overlay(
                            0,
                            message=f"Loading material prices "
                                    f"({c} unique inputs)..."
                        )
                    )
                    recent_floors, baseline_floors = (
                        prebuild_material_floor_cache(
                            list(unique_materials),
                            market_db,
                            context_label=self.hub_key,
                        )
                    )

            # ---------------------------------------------------------
            # Pre-build item price floor cache for all stable candidates.
            # Two aggregated SQL queries instead of 2*N per-item queries.
            # ---------------------------------------------------------
            item_recent_floors: Dict[int, float] = {}
            item_baseline_floors: Dict[int, float] = {}
            if stable_profiles:
                stable_type_ids = [p.type_id for p in stable_profiles]
                item_recent_floors, item_baseline_floors = (
                    prebuild_material_floor_cache(
                        stable_type_ids,
                        market_db,
                        region_id=self.region_id,
                        context_label=f"{self.hub_key}-items",
                    )
                )

            # ---------------------------------------------------------
            # Iterate stable candidates, populate material risk cache.
            # Throttle progress updates to every 10 items to keep the
            # main-thread queue light.
            # ---------------------------------------------------------
            # Switch overlay to determinate progress
            submit(lambda t=total: self._show_filter_overlay(
                f"Analyzing {t} items...", total=t
            ))

            analyzed = 0
            promoted = 0
            for idx, profile in enumerate(stable_profiles, start=1):
                result = check_material_risk(
                    profile.type_id,
                    self.region_id,
                    recent_floor_cache=recent_floors,
                    baseline_floor_cache=baseline_floors,
                    item_floor_recent_cache=item_recent_floors,
                    item_floor_baseline_cache=item_baseline_floors,
                    hub_key=self.hub_key,
                )
                analyzed += 1
                if result == 'medium':
                    promoted += 1

                if idx % 10 == 0 or idx == total:
                    submit(
                        lambda i=idx, t=total:
                        self._update_filter_overlay(
                            i,
                            message=f"Analyzing materials... "
                                    f"({i} / {t})"
                        )
                    )

            tracker.mark_complete(self.hub_key)
            print(f"[StockMarket-{self.hub_key}] Material filter: "
                  f"{analyzed} analyzed, {promoted} promoted to medium")

            # Chain into leading indicators phase (which then refreshes).
            # _run_leading_indicators_phase() must run on main thread.
            submit(lambda: self._run_leading_indicators_phase())

        threading.Thread(
            target=run_filter, daemon=True,
            name=f"MaterialFilter-{self.hub_key}"
        ).start()

    # =========================================================
    # Leading indicators phase
    # =========================================================

    def _run_leading_indicators_phase(self):
        """Phase 2 of the post-scan flow.

        Called after the material filter completes (or was skipped because
        it already ran today). Checks the leading indicators tracker:
          - If LI should run: kicks off background thread to compute and
            save batch, then chains to refresh.
          - If LI ran already today: skips compute and goes straight to
            refresh.

        Either way, ends with refresh_display_async + overlay hide.
        Must be called from the main thread.
        """
        _check_thread(
            f"HubPanel._run_leading_indicators_phase({self.hub_key})"
        )

        from analytics.leading_indicators_tracker import (
            get_leading_indicators_tracker,
        )

        li_tracker = get_leading_indicators_tracker()
        if not li_tracker.should_run(self.hub_key):
            # LI already done today - go straight to refresh
            print(f"[StockMarket-{self.hub_key}] Leading indicators: "
                  f"skipping (already ran today)")
            print(f"[StockMarket-{self.hub_key}] === PHASE: Refresh ===")
            self._show_filter_overlay("Refreshing display...", total=0)
            self.refresh_display_async(after=self._hide_filter_overlay)
            return

        # Kick off LI compute in background
        self._show_filter_overlay(
            "Preparing leading indicators...", total=0
        )
        threading.Thread(
            target=self._run_leading_indicators_background,
            daemon=True,
            name=f"LeadingIndicators-{self.hub_key}"
        ).start()

    def _run_leading_indicators_background(self):
        """Background thread: compute leading indicators for all profiles
        in the region, save as one batch, then refresh.

        Iterates ALL region profiles (not just stable-floor ones) so that
        Med Risk and High Risk panels also get their indicator column.
        Items with no history return None and are skipped.
        """
        from analytics.leading_indicators_batch import (
            compute_leading_indicators,
            get_reference_date,
        )
        from analytics.leading_indicators_tracker import (
            get_leading_indicators_tracker,
        )
        from analytics import leading_indicators_storage

        li_tracker = get_leading_indicators_tracker()

        try:
            from history.market_history import get_market_history_db
            region_profiles = self.profiles.get_profiles_for_region(self.region_id)

            # Compute-time volume gate: skip low-volume items entirely.
            min_volume = getattr(self.settings, "min_daily_volume", 0)
            if min_volume > 0:
                before = len(region_profiles)
                region_profiles = [
                    p for p in region_profiles
                    if getattr(p, "avg_daily_volume", 0) >= min_volume
                ]
                print(f"[StockMarket-{self.hub_key}] LI volume gate: "
                      f"{len(region_profiles)}/{before} pass "
                      f"(>= {min_volume}/day)")

            total = len(region_profiles)
            print(f"[StockMarket-{self.hub_key}] "
                  f"=== PHASE: Leading Indicators === "
                  f"({total} items, region {self.region_id})")

            # Pre-fetch 3yr history for all region items in one pass so
            # compute_leading_indicators skips its per-item DB call.
            market_db = get_market_history_db()
            type_ids_list = [p.type_id for p in region_profiles]
            all_history = market_db.get_full_history_bulk(
                self.region_id, type_ids_list, years=3
            )
            print(f"[StockMarket-{self.hub_key}] LI history pre-fetched "
                  f"({sum(len(v) for v in all_history.values())} rows)")

            # Anchor trend windows at the DB's latest date, not the wall
            # clock — the everef archive lags 1-4 days (review finding 6-2).
            reference_date = get_reference_date(market_db)
            print(f"[StockMarket-{self.hub_key}] LI window anchor: "
                  f"{reference_date or 'wall clock (DB empty)'}")

            # Switch overlay to determinate progress
            submit(lambda t=total: self._show_filter_overlay(
                f"Computing indicators ({t} items)...", total=t
            ))

            results = []
            computed = 0
            skipped = 0
            errored = 0

            # Console checkpoint cadence: every 100 items so we can see
            # which hub is making progress when several run in parallel.
            checkpoint_every = 100

            for idx, profile in enumerate(region_profiles, start=1):
                try:
                    result = compute_leading_indicators(
                        profile.type_id, self.region_id,
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
                    print(f"[LeadingIndicators:{self.hub_key}] error type="
                          f"{profile.type_id}: {e}")

                # Throttle UI updates - every 25 items keeps queue light
                if idx % 25 == 0 or idx == total:
                    submit(lambda i=idx, t=total:
                           self._update_filter_overlay(
                               i,
                               message=f"Computing indicators... "
                                       f"({i} / {t})"
                           ))

                # Console checkpoint - lets us see progress per hub
                # when multiple hubs run LI concurrently.
                if idx % checkpoint_every == 0 or idx == total:
                    print(f"[StockMarket-{self.hub_key}] "
                          f"LI progress: {idx}/{total} "
                          f"(computed={computed}, "
                          f"no-history={skipped}, "
                          f"errored={errored})")

            # Save batch in one transaction
            if results:
                submit(
                    lambda n=len(results): self._update_filter_overlay(
                        total,
                        message=f"Saving {n} indicator results..."
                    )
                )
                leading_indicators_storage.save_batch(results)

            li_tracker.mark_complete(self.hub_key)
            print(f"[StockMarket-{self.hub_key}] Leading indicators: "
                  f"{computed} computed, {skipped} no-history, "
                  f"{errored} errored")
        except Exception as e:
            print(f"[StockMarket-{self.hub_key}] Leading indicators "
                  f"phase failed: {e}")
            # Don't mark tracker complete - let it retry next launch

        # Always proceed to refresh, even if LI failed
        print(f"[StockMarket-{self.hub_key}] === PHASE: Refresh ===")
        submit(lambda: self._show_filter_overlay(
            "Refreshing display...", total=0
        ))
        submit(lambda: self.refresh_display_async(
            after=self._hide_filter_overlay
        ))
