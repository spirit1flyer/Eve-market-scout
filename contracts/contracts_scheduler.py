"""Hourly passive contract pull + watchlist matching (Steps 6 + 7).

The design has NO separate watchlist poller — the hourly contract pull IS the
engine. Each cycle:
  1. Refresh every cached trade-hub scope (station-scoped at the hub's main
     station) regardless of what the user has selected, so a hub watched from
     another screen stays live. The freshness gate + ETag make a no-change
     cycle cheap (mostly 304s).
  2. After a region is refreshed, diff its watchlist entries' cache hits against
     already-seen ids — new hits are the alerts, surfaced to the sub-tab.
  3. Drain any regions the user opted into for region-wide coverage (Step 7),
     one per cycle, throttled, with progress — the deferred backfill path.

Runs on a daemon thread with a long sleep; the first cycle is delayed after
launch so we don't slam ESI at startup (cold-start caution). Matches are handed
back via `tk_queue.submit` so the UI callback runs on the Tk thread.

All diagnostics carry the greppable `[ContractDiag]` tag.
"""

import threading
import time
from typing import Callable, Optional

from core.config import get_enabled_hubs, get_hub_config
from contracts.contracts_lists import ContractWatchlist


# Hourly cadence; first run delayed so startup isn't ESI-heavy. ESI caches the
# public contract list ~30 min, so an hourly sweep never wastes a pull.
PULL_INTERVAL_SECONDS = 3600
FIRST_RUN_DELAY_SECONDS = 180

# Max item fetches per region per cycle. A busy station (Jita IV-4 ~30k) must
# NOT be crawled in one blocking shot from the background — that's what froze
# the UI. With a watchlist present we drain the backlog gradually over cycles;
# the search tab does the fast full crawl on explicit, warned user action.
ITEMS_PER_CYCLE = 1500


def _print(msg: str) -> None:
    print(f"[ContractDiag] {msg}")


class ContractsScheduler:
    """Background loop that drives the hourly pull + watchlist matching."""

    def __init__(self, engine, on_matches: Optional[Callable] = None,
                 on_backfill_progress: Optional[Callable] = None):
        """
        engine: a ContractsEngine.
        on_matches(list_of_match_dicts): called (Tk thread) when new contracts
            match a watchlist entry.
        on_backfill_progress(region_id, done, total): regional backfill ticks.
        """
        self.engine = engine
        self.on_matches = on_matches
        self.on_backfill_progress = on_backfill_progress

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()

        # Regions the user opted into for region-wide coverage (Step 7). Drained
        # one-per-cycle in the background; cheap forever after the first crawl.
        self._regional_optins: set[int] = set()
        self._optin_lock = threading.Lock()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ContractsScheduler")
        self._thread.start()
        _print("scheduler started")

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger_now(self) -> None:
        """Wake the loop for an immediate cycle (e.g. user forced a refresh)."""
        self._wake.set()

    # --- regional opt-in (Step 7) ------------------------------------------

    def queue_regional_optin(self, region_id: int) -> None:
        """Queue a region for the deferred region-wide backfill. Does NOT crawl
        live — picked up on the next cycle."""
        with self._optin_lock:
            self._regional_optins.add(int(region_id))
        _print(f"region {region_id} queued for region-wide backfill at next sync")

    def pending_optins(self) -> set[int]:
        with self._optin_lock:
            return set(self._regional_optins)

    # --- main loop ----------------------------------------------------------

    def _run(self) -> None:
        # Delayed first cycle.
        if self._wait(FIRST_RUN_DELAY_SECONDS):
            return
        while not self._stop.is_set():
            try:
                self._cycle()
            except Exception as e:
                _print(f"scheduler cycle crashed: {e}")
            if self._wait(PULL_INTERVAL_SECONDS):
                return

    def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`, returning early if woken. True => stop."""
        self._wake.wait(timeout=seconds)
        self._wake.clear()
        return self._stop.is_set()

    def _cycle(self) -> None:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._cycle_async())
        finally:
            # Release the shared client's session for this throwaway loop
            # before closing it — otherwise the session leaks every cycle and
            # its stale _per_loop entry can be picked up by a later loop with
            # the same id() ("Event loop is closed" errors).
            try:
                client = self.engine.get_client() if self.engine.get_client else None
                if client is not None:
                    client.close_loop_state(loop)
            except Exception as e:
                _print(f"loop-state cleanup failed: {e}")
            loop.close()

    async def _cycle_async(self) -> None:
        _print("hourly cycle begin")
        # 1+2. Trade-hub scopes + watchlist match per region.
        #
        # Item fetching is the expensive part (one call per contract; Jita is
        # ~30k). We ONLY fetch items for a hub region that actually has a
        # watchlist entry — otherwise there's nothing to match, so we just
        # refresh the LIST (cheap, conditional/ETag) and move on. Even with a
        # watchlist we cap item fetches per cycle so a fresh 30k backlog drains
        # gradually in the background instead of blocking on one giant pull.
        from contracts.contracts_lists import ContractWatchlist

        hub_regions: set[int] = set()
        for key, _name in get_enabled_hubs():
            cfg = get_hub_config(key)
            if cfg.get("type") == "structure":
                continue  # public contract list is NPC-region only
            region_id = cfg["region_id"]
            station_id = cfg["station_id"]
            hub_regions.add(region_id)
            has_watchlist = bool(ContractWatchlist.for_region(region_id).entries())
            try:
                if has_watchlist:
                    await self.engine.sync_region(
                        region_id, item_fetch_locations={station_id},
                        max_items=ITEMS_PER_CYCLE)
                else:
                    # List-only refresh — no items, no backlog crawl.
                    await self.engine.sync_region(
                        region_id, item_fetch_locations=set())
            except Exception as e:
                _print(f"hub sync failed region {region_id}: {e}")

        self._match_regions(hub_regions)

        # 3. One queued regional backfill per cycle (Step 7).
        await self._drain_one_regional_optin()
        _print("hourly cycle end")

    def _match_regions(self, region_ids) -> None:
        all_matches: list[dict] = []
        for region_id in region_ids:
            try:
                wl = ContractWatchlist.for_region(region_id)
                matches = wl.match_region(self.engine.db)
                all_matches.extend(matches)
            except Exception as e:
                _print(f"watchlist match failed region {region_id}: {e}")
        if all_matches and self.on_matches is not None:
            self._submit(lambda: self.on_matches(all_matches))

    async def _drain_one_regional_optin(self) -> None:
        with self._optin_lock:
            if not self._regional_optins:
                return
            region_id = next(iter(self._regional_optins))
            self._regional_optins.discard(region_id)

        _print(f"region {region_id} region-wide backfill begin (deferred opt-in)")

        def _progress(done, total):
            if self.on_backfill_progress is not None:
                self._submit(
                    lambda: self.on_backfill_progress(region_id, done, total))

        try:
            # item_fetch_locations=None → whole region (the expensive path),
            # throttled inside contracts_esi and capped per cycle so a region
            # backfill (tens of thousands) drains over many cycles, not one.
            summary = await self.engine.sync_region(
                region_id, item_fetch_locations=None, progress_cb=_progress,
                max_items=ITEMS_PER_CYCLE)
            # Re-match against whatever items now exist.
            self._match_regions({region_id})
            remaining = (summary or {}).get("items_remaining", 0)
            if remaining > 0:
                # More to crawl — re-queue for the next cycle.
                with self._optin_lock:
                    self._regional_optins.add(region_id)
                _print(f"region {region_id} backfill chunk done — "
                       f"{remaining} contracts remaining, re-queued")
            else:
                _print(f"region {region_id} region-wide backfill complete")
        except Exception as e:
            _print(f"region {region_id} backfill failed: {e}")

    @staticmethod
    def _submit(fn) -> None:
        try:
            from core.tk_queue import submit
            submit(fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass
