"""Stock Market cold-start orchestrator for EVE Market Scout.

Single dedicated worker thread + asyncio loop that drives the
Stock Market tab through 7 sequential phases on launch:

    0. Detect      — what's missing
    1. Archive     — observe background_import (everef CSVs)
    2. DB import   — observe background_import (CSV -> market_history.db)
    3. Profiles    — extract per-region from market_history.db
    4. ESI burst   — pull stale hub orders from ESI
    5. MF          — material filter per hub (24h gate)
    6. LI          — leading indicators per hub (24h gate)
    7. Unlock      — hide locked overlay

Scanner runs untouched on its own thread/loop.  Step 1's per-loop
semaphore (api.ESIClient._per_loop) keeps the two threads from
stomping each other.

This file currently implements only phase 0 (detection + logging).
It runs alongside the existing _startup_refresh path so we can
verify state detection without changing behavior.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

from core.config import TRADE_HUBS


class _ColdStartCancelled(Exception):
    """Raised internally to unwind the cold-start worker when the Stock Market
    master switch is turned off (or a newer run supersedes this one)."""


@dataclass
class PhaseState:
    """Shared state read by the locked overlay each poll tick.

    Filled in by the cold-start worker thread; read on the main
    (Tk) thread.  Plain dataclass with simple types — no locking
    needed because writes are atomic for these field types and
    readers tolerate transient inconsistency between fields.
    """

    current_phase: int = 0           # 0..7
    phase_name: str = "Detecting state"
    current: int = 0                  # progress within current phase
    total: int = 0                    # 0 = indeterminate
    detail: str = ""                  # free-text sub-status
    done: bool = False                # all phases complete -> unlock
    error: Optional[str] = None       # fatal phase error if any


@dataclass
class DetectedState:
    """Output of phase 0 — what's missing across all phases.

    Used by later phases to decide whether to skip themselves.
    """

    archive_missing_by_year: dict = field(default_factory=dict)  # {year: count}
    db_row_count: int = 0
    db_items_per_region: dict = field(default_factory=dict)      # {region_id: count}
    profiles_per_region: dict = field(default_factory=dict)      # {region_id: count}
    stale_hubs: list = field(default_factory=list)               # [(hub_key, region_id, name)]
    mf_pending_hubs: list = field(default_factory=list)          # [hub_key]
    li_pending_hubs: list = field(default_factory=list)          # [hub_key]
    sde_industry_available: bool = True                          # blueprint DB present?


class StockMarketColdStartMixin:
    """Mixin providing the cold-start orchestrator.

    Expected parent attributes:
        frame            — ttk.Frame, the Stock Market tab frame
        downloader       — ArchiveDownloader instance
        profiles         — ProfileManager instance
        get_client       — callable returning ESIClient (may return None)
    """

    def _init_cold_start(self):
        """Initialise cold-start state.  Call from __init__."""
        self.phase_state = PhaseState()
        self._cold_start_thread: Optional[threading.Thread] = None
        # Monotonic token, bumped on every master on/off flip (see
        # set_enabled). The running worker captures it as _cold_start_run_token
        # at start; a mismatch — or the master switch going off — makes
        # _cold_start_check_cancel bail, aborting an in-progress load.
        self._cold_start_token = 0
        self._cold_start_run_token = 0

    def _start_cold_start_worker(self):
        """Spawn the worker thread.  Idempotent.

        If a previous (now-cancelled) worker is still winding down — e.g. it
        was switched off mid-load and is finishing its current ESI call — we
        retry shortly instead of skipping, so a quick off→on doesn't leave the
        tab stuck on the lock overlay with no worker behind it.
        """
        if self._cold_start_thread and self._cold_start_thread.is_alive():
            if self._stock_market_off():
                return  # toggled back off in the meantime; nothing to start
            self.frame.after(500, self._start_cold_start_worker)
            return

        self._cold_start_thread = threading.Thread(
            target=self._cold_start_run,
            daemon=True,
            name="StockMarketColdStart",
        )
        self._cold_start_thread.start()

    def _cold_start_check_cancel(self):
        """Raise _ColdStartCancelled if the master switch was turned off or a
        newer cold-start run superseded this one. Called at phase boundaries
        and inside the long per-hub/per-region loops so switching Stock Market
        off aborts an in-progress load promptly."""
        if (self._stock_market_off()
                or self._cold_start_token != self._cold_start_run_token):
            raise _ColdStartCancelled()

    def _cold_start_run(self):
        """Worker entry point.  Runs phases sequentially."""
        self._cold_start_run_token = self._cold_start_token
        try:
            print("[ColdStart] === Worker thread started ===")
            detected = self._cold_start_phase0_detect()
            self._cold_start_log_detected(detected)

            self._cold_start_check_cancel()
            self._cold_start_phase0_sde_industry(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase12_bg_import_observer(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase3_profiles(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase4_esi_burst(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase5_material_filter(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase6_leading_indicators(detected)
            self._cold_start_check_cancel()
            self._cold_start_phase7_unlock(detected)

            print("[ColdStart] === Cold start complete ===")
            self.phase_state.done = True
        except _ColdStartCancelled:
            print("[ColdStart] === Cancelled (Stock Market switched off) ===")
            # Leave phase_state.done False; the off overlay is already showing.
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.phase_state.error = str(e)
            print(f"[ColdStart] Worker crashed: {e}")

    # =========================================================================
    # Phase 0 — detect what's missing
    # =========================================================================

    def _cold_start_phase0_detect(self) -> DetectedState:
        """Inspect filesystem + DB + trackers; return DetectedState.

        Pure read-only.  Does not touch ESI or do any computation.
        """
        self.phase_state.current_phase = 0
        self.phase_state.phase_name = "Detecting state"
        self.phase_state.detail = ""

        state = DetectedState()

        # --- archive ---
        try:
            for year in self.downloader.get_years_to_download():
                missing = self.downloader.get_missing_dates(year)
                if missing:
                    state.archive_missing_by_year[year] = len(missing)
        except Exception as e:
            print(f"[ColdStart] archive detection failed: {e}")

        # --- market_history.db ---
        try:
            from history.market_history import get_market_history_db
            db = get_market_history_db()
            stats = db.get_stats()
            state.db_row_count = stats.get("row_count", 0)
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                region_id = config["region_id"]
                items = db.get_items_in_region(region_id)
                state.db_items_per_region[region_id] = len(items)
        except Exception as e:
            print(f"[ColdStart] db detection failed: {e}")

        # --- profiles per region ---
        try:
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                region_id = config["region_id"]
                profiles = self.profiles.get_profiles_for_region(region_id)
                state.profiles_per_region[region_id] = len(profiles) if profiles else 0
        except Exception as e:
            print(f"[ColdStart] profile detection failed: {e}")

        # --- stale hub order caches ---
        try:
            client = self.get_client() if self.get_client else None
            if client:
                state.stale_hubs = self._get_stale_hubs(client)
        except Exception as e:
            print(f"[ColdStart] stale-hub detection failed: {e}")

        # --- SDE industry DB (blueprint material data, used by phase 5) ---
        try:
            from sde.sde_industry import get_sde_industry_db
            state.sde_industry_available = get_sde_industry_db().is_available()
        except Exception as e:
            print(f"[ColdStart] sde_industry detection failed: {e}")
            state.sde_industry_available = False

        # --- MF / LI 24h trackers ---
        try:
            from analytics.stockmarket_filters import MaterialFilterTracker
            from analytics.leading_indicators_tracker import LeadingIndicatorsTracker
            mf = MaterialFilterTracker()
            li = LeadingIndicatorsTracker()
            for hub_key, config in TRADE_HUBS.items():
                if not config.get("enabled", True):
                    continue
                if mf.should_run(hub_key):
                    state.mf_pending_hubs.append(hub_key)
                if li.should_run(hub_key):
                    state.li_pending_hubs.append(hub_key)
        except Exception as e:
            print(f"[ColdStart] tracker detection failed: {e}")

        return state

    # =========================================================================
    # Phase 0 — SDE industry DB download if missing
    # =========================================================================

    def _cold_start_phase0_sde_industry(self, detected: DetectedState):
        """Download the SDE industry blueprint DB if missing.

        Phase 5 (material filter) reads this DB to look up blueprint
        materials per item.  Without it, every per-item check fails the
        is_available() guard and MF silently no-ops — masking results
        and flooding the log with "Industry data not available" lines.

        Treated as part of phase-0 preparation: current_phase stays 0
        so the locked overlay shows the title without a numbered tag.
        """
        if detected.sde_industry_available:
            return

        print("[ColdStart] === Phase 0: SDE industry DB missing — downloading ===")

        self.phase_state.current_phase = 0
        self.phase_state.phase_name = "Downloading SDE industry data"
        self.phase_state.current = 0
        self.phase_state.total = 100
        self.phase_state.detail = ""

        from sde.sde_industry import get_sde_industry_db
        industry_db = get_sde_industry_db()

        def progress_cb(msg: str, pct: int):
            self.phase_state.current = pct
            self.phase_state.total = 100
            self.phase_state.detail = msg

        try:
            ok = industry_db.refresh(progress_callback=progress_cb)
            if ok:
                print("[ColdStart] SDE industry download complete")
                detected.sde_industry_available = True
            else:
                print("[ColdStart] SDE industry download returned False — "
                      "phase 5 will skip blueprint checks")
        except Exception as e:
            print(f"[ColdStart] SDE industry download error: {e}")

    # =========================================================================
    # Phases 1+2 — observe background_import (archive download + DB import)
    # =========================================================================

    def _cold_start_phase12_bg_import_observer(self, detected: DetectedState):
        """Surface background_import progress via phase_state.

        The migration system (gui_migration.py) is responsible for
        kicking off background_import — the orchestrator only observes.
        When bg-import isn't running this phase exits immediately.

        While bg-import runs, this method blocks the worker thread,
        polling its status once a second and translating the status
        message into phase 1 (download) or phase 2 (import) so the
        locked overlay shows meaningful progress.
        """
        from history.background_import import get_background_import_status
        import time

        status = get_background_import_status()
        if not status.get("running"):
            return

        print("[ColdStart] === Phases 1+2: observing background import ===")

        while True:
            status = get_background_import_status()
            if not status.get("running"):
                break

            msg = status.get("status", "")
            msg_lower = msg.lower()
            if "download" in msg_lower:
                self.phase_state.current_phase = 1
                self.phase_state.phase_name = (
                    "Downloading market history archive"
                )
            elif "import" in msg_lower:
                self.phase_state.current_phase = 2
                self.phase_state.phase_name = (
                    "Importing market history to database"
                )
            else:
                self.phase_state.current_phase = 1
                self.phase_state.phase_name = "Preparing market history"

            self.phase_state.current = status.get("current", 0)
            self.phase_state.total = status.get("total", 0)
            self.phase_state.detail = msg

            time.sleep(1)

        print("[ColdStart] === Phases 1+2 complete (bg-import finished) ===")

    # =========================================================================
    # Phase 3 — sequential profile build for all 5 regions
    # =========================================================================

    def _cold_start_phase3_profiles(self, detected: DetectedState):
        """Build profiles for any region that has DB history but no profiles.

        Sync work — uses ProfileManager.extract_all_from_db on the worker
        thread.  Updates phase_state so the locked overlay reflects
        progress: title shows current hub, progress bar shows item
        extraction within that region, detail shows region X of N.
        """
        regions_to_build = []
        for hub_key, config in TRADE_HUBS.items():
            if not config.get("enabled", True):
                continue
            region_id = config["region_id"]
            existing = detected.profiles_per_region.get(region_id, 0)
            if existing > 0:
                print(f"[ColdStart] phase 3: skipping {hub_key} "
                      f"(already has {existing:,} profiles)")
                continue
            db_items = detected.db_items_per_region.get(region_id, 0)
            if db_items == 0:
                print(f"[ColdStart] phase 3: skipping {hub_key} "
                      f"(no DB history for region)")
                continue
            regions_to_build.append((hub_key, region_id, config["name"]))

        if not regions_to_build:
            print("[ColdStart] phase 3: nothing to build, skipping")
            return

        # User-stated priority: Jita first, others in their natural
        # TRADE_HUBS order after that.  Stable sort preserves the
        # remaining order.
        regions_to_build.sort(key=lambda t: 0 if t[0] == "jita" else 1)

        print(f"[ColdStart] === Phase 3: building profiles for "
              f"{len(regions_to_build)} regions ===")

        from history.market_history import get_market_history_db
        db = get_market_history_db()

        total_regions = len(regions_to_build)
        for region_idx, (hub_key, region_id, hub_name) in enumerate(
            regions_to_build, start=1
        ):
            self._cold_start_check_cancel()
            print(f"[ColdStart] phase 3 [{region_idx}/{total_regions}]: "
                  f"{hub_name} (region {region_id})")

            self.phase_state.current_phase = 3
            self.phase_state.phase_name = f"Building profiles for {hub_name}"
            self.phase_state.current = 0
            self.phase_state.total = 0
            self.phase_state.detail = f"region {region_idx} of {total_regions}"

            def progress_cb(
                msg: str, current: int, total: int,
                _ridx=region_idx, _rtot=total_regions,
            ):
                # Throttle phase_state writes — extract_all_from_db calls
                # this on every item.  200 matches the existing
                # _build_profiles_for_hub cadence.
                if current % 200 == 0 or current == total:
                    self.phase_state.current = current
                    self.phase_state.total = total
                    self.phase_state.detail = (
                        f"region {_ridx} of {_rtot} — {msg}"
                    )

            try:
                success, failed = self.profiles.extract_all_from_db(
                    region_id=region_id,
                    market_db=db,
                    progress_callback=progress_cb,
                )
                print(f"[ColdStart] phase 3: {hub_name} done — "
                      f"{success:,} ok, {failed} failed")
            except _ColdStartCancelled:
                raise  # cancellation must unwind, not be swallowed below
            except Exception as e:
                print(f"[ColdStart] phase 3: {hub_name} failed: {e}")
                # Continue with remaining regions instead of bailing the
                # whole orchestrator — partial success is better than none.

        print("[ColdStart] === Phase 3 complete ===")

    # =========================================================================
    # Phase 4 — ESI burst on the worker thread's asyncio loop
    # =========================================================================

    def _cold_start_phase4_esi_burst(self, detected: DetectedState):
        """Pull orders for stale hubs from ESI on the worker thread.

        Uses a fresh asyncio loop bound to the worker thread; combined
        with step 1's per-loop semaphore in api.ESIClient, this can run
        concurrently with the scanner's own loop without cross-loop
        races.

        Skips entirely when no hubs are stale (cache age < 24h).
        """
        if not detected.stale_hubs:
            print("[ColdStart] phase 4: no stale hubs, skipping")
            return

        client = self.get_client() if self.get_client else None
        if client is None:
            print("[ColdStart] phase 4: no ESI client, skipping")
            return

        # User priority: Jita first, then the rest in their natural order.
        stale = sorted(
            detected.stale_hubs,
            key=lambda t: 0 if t[0] == "jita" else 1,
        )
        total_hubs = len(stale)

        print(f"[ColdStart] === Phase 4: pulling orders for "
              f"{total_hubs} stale hubs ===")

        import asyncio
        from analytics.stockmarket_filters import get_hub_burst_tracker

        self.phase_state.current_phase = 4
        self.phase_state.phase_name = "Pulling fresh hub orders from ESI"
        self.phase_state.current = 0
        self.phase_state.total = total_hubs
        self.phase_state.detail = ""

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            tracker = get_hub_burst_tracker()

            async def pull_all():
                client.ensure_session()
                client.reset_for_new_loop()
                for idx, (hub_key, region_id, hub_name) in enumerate(
                    stale, start=1
                ):
                    self._cold_start_check_cancel()
                    self.phase_state.phase_name = (
                        f"Pulling orders for {hub_name}"
                    )
                    self.phase_state.detail = (
                        f"hub {idx} of {total_hubs}"
                    )
                    print(f"[ColdStart] phase 4 [{idx}/{total_hubs}]: "
                          f"{hub_name} (region {region_id})")
                    try:
                        await client.get_market_orders(region_id)
                        tracker.mark_complete(hub_key)
                        print(f"[ColdStart] phase 4: {hub_name} done")
                    except _ColdStartCancelled:
                        raise
                    except Exception as e:
                        print(f"[ColdStart] phase 4: {hub_name} failed: {e}")
                    self.phase_state.current = idx

            loop.run_until_complete(pull_all())
        finally:
            loop.close()

        print("[ColdStart] === Phase 4 complete ===")

    # =========================================================================
    # Phase 5 — material filter compute, all hubs (Jita first)
    # =========================================================================

    def _cold_start_phase5_material_filter(self, detected: DetectedState):
        """Run material filter compute for every hub whose 24h tracker
        says it's due.  Sequential, Jita first.
        """
        from gui.stockmarket.gui_stockmarket_compute import run_material_filter_compute

        hubs_to_run = [h for h in detected.mf_pending_hubs]
        if not hubs_to_run:
            print("[ColdStart] phase 5: nothing pending, skipping")
            return

        hubs_to_run.sort(key=lambda h: 0 if h == "jita" else 1)
        total = len(hubs_to_run)

        print(f"[ColdStart] === Phase 5: material filter for {total} hubs ===")

        for idx, hub_key in enumerate(hubs_to_run, start=1):
            config = TRADE_HUBS.get(hub_key, {})
            region_id = config.get("region_id")
            hub_name = config.get("name", hub_key)
            if region_id is None:
                continue

            self._cold_start_check_cancel()
            self.phase_state.current_phase = 5
            self.phase_state.phase_name = f"Material filter for {hub_name}"
            self.phase_state.current = 0
            self.phase_state.total = 0
            self.phase_state.detail = f"hub {idx} of {total}"

            def progress_cb(
                current: int, total: int, detail: str,
                _idx=idx, _tot=total,
            ):
                self.phase_state.current = current
                self.phase_state.total = total
                self.phase_state.detail = (
                    f"hub {_idx} of {_tot} — {detail}" if detail
                    else f"hub {_idx} of {_tot}"
                )

            try:
                run_material_filter_compute(
                    hub_key, region_id, self.profiles,
                    progress_cb=progress_cb,
                    min_volume=getattr(
                        getattr(self, "settings", None),
                        "min_daily_volume", 0,
                    ),
                )
            except _ColdStartCancelled:
                raise
            except Exception as e:
                print(f"[ColdStart] phase 5: {hub_name} failed: {e}")

        print("[ColdStart] === Phase 5 complete ===")

    # =========================================================================
    # Phase 6 — leading indicators compute, all hubs (Jita first)
    # =========================================================================

    def _cold_start_phase6_leading_indicators(self, detected: DetectedState):
        """Run leading indicators compute for every hub whose 24h tracker
        says it's due.  Sequential, Jita first.
        """
        from gui.stockmarket.gui_stockmarket_compute import run_leading_indicators_compute

        hubs_to_run = [h for h in detected.li_pending_hubs]
        if not hubs_to_run:
            print("[ColdStart] phase 6: nothing pending, skipping")
            return

        hubs_to_run.sort(key=lambda h: 0 if h == "jita" else 1)
        total = len(hubs_to_run)

        print(f"[ColdStart] === Phase 6: leading indicators for {total} hubs ===")

        for idx, hub_key in enumerate(hubs_to_run, start=1):
            config = TRADE_HUBS.get(hub_key, {})
            region_id = config.get("region_id")
            hub_name = config.get("name", hub_key)
            if region_id is None:
                continue

            self._cold_start_check_cancel()
            self.phase_state.current_phase = 6
            self.phase_state.phase_name = f"Leading indicators for {hub_name}"
            self.phase_state.current = 0
            self.phase_state.total = 0
            self.phase_state.detail = f"hub {idx} of {total}"

            def progress_cb(
                current: int, total: int, detail: str,
                _idx=idx, _tot=total,
            ):
                self.phase_state.current = current
                self.phase_state.total = total
                self.phase_state.detail = (
                    f"hub {_idx} of {_tot} — {detail}" if detail
                    else f"hub {_idx} of {_tot}"
                )

            try:
                run_leading_indicators_compute(
                    hub_key, region_id, self.profiles,
                    progress_cb=progress_cb,
                    min_volume=getattr(
                        getattr(self, "settings", None),
                        "min_daily_volume", 0,
                    ),
                )
            except _ColdStartCancelled:
                raise
            except Exception as e:
                print(f"[ColdStart] phase 6: {hub_name} failed: {e}")

        print("[ColdStart] === Phase 6 complete ===")

    # =========================================================================
    # Phase 7 — unlock: refresh all panels so the new MF/LI results render
    # =========================================================================

    def _cold_start_phase7_unlock(self, detected: DetectedState):
        """Mark cold-start complete so the locked overlay can hide.

        Panel rendering is lazy — driven by _on_hub_tab_changed in
        gui_stockmarket.py. Cold-start only acquires data; the first
        time a hub tab is shown the panel runs refresh_display_async
        itself.
        """
        self.phase_state.current_phase = 7
        self.phase_state.phase_name = "Refreshing displays"
        self.phase_state.current = 0
        self.phase_state.total = 0
        self.phase_state.detail = ""

        print("[ColdStart] === Phase 7 complete (lazy render on tab change) ===")

    # =========================================================================
    # Logging helpers
    # =========================================================================

    def _cold_start_log_detected(self, s: DetectedState):
        """Pretty-print detected state for visibility during scaffold phase."""
        print("[ColdStart] --- Detected state ---")
        if s.archive_missing_by_year:
            for year, n in sorted(s.archive_missing_by_year.items()):
                print(f"[ColdStart]   archive {year}: {n} files missing")
        else:
            print("[ColdStart]   archive: complete")
        print(f"[ColdStart]   db rows: {s.db_row_count:,}")
        for region_id, n in s.db_items_per_region.items():
            print(f"[ColdStart]   db items in region {region_id}: {n:,}")
        for region_id, n in s.profiles_per_region.items():
            print(f"[ColdStart]   profiles in region {region_id}: {n:,}")
        if s.stale_hubs:
            names = ", ".join(h[0] for h in s.stale_hubs)
            print(f"[ColdStart]   stale hubs: {names}")
        else:
            print("[ColdStart]   stale hubs: none (all caches fresh)")
        print(f"[ColdStart]   MF pending: {s.mf_pending_hubs or 'none'}")
        print(f"[ColdStart]   LI pending: {s.li_pending_hubs or 'none'}")
        print(f"[ColdStart]   SDE industry available: {s.sde_industry_available}")
        print("[ColdStart] -----------------------")
