"""ESI API client with async requests, rate limiting, and system data."""

import asyncio
import aiohttp
import re
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from core.config import (
    ESI_BASE_URL, MAX_CONCURRENT_REQUESTS, REQUEST_TIMEOUT,
    JITA_SYSTEM_ID, MIN_SECURITY_STATUS,
    JITA_REGION_ID,
    get_esi_headers,
)
from core.esi_rate_limit import record_response, wait_async, make_trace_config
from esi.esi_supplement import ESISupplementCache
from history.market_history import MarketHistoryDB
from core.ssl_context import make_connector
from core.order_cache import OrderCacheStore
from core.market_snapshot import ESIPaginationChangedError, fetch_market_snapshot
from core.type_name_mixin import TypeNameMixin
from core.esi_get import ESIGetMixin
from core.history_mixin import HistoryMixin
from core.order_policy import JitaReferenceMixin
from core.esi_history_cache import get_history_cache

# Local debug flag - set True to enable diagnostic output
DEBUG_ESI = False


class ESIConnectionHaltedError(RuntimeError):
    """Raised when ESI has been hard-halted after repeated timeouts at
    minimum concurrency (budget already 1 and still failing). The link
    can't move the data, so all external requests stop. Cleared only by
    restarting the app."""


class ESIClient(TypeNameMixin, ESIGetMixin, HistoryMixin, JitaReferenceMixin):
    """Async client for EVE ESI API."""

    def __init__(self):
        # Per-event-loop async primitives. Each worker thread runs its own
        # event loop and gets its own semaphore + session, so concurrent
        # threads can't stomp each other (was the cause of "Semaphore is
        # bound to a different event loop" cross-loop races on cold start).
        # Throwaway-loop owners must call close_loop_state(loop) before
        # loop.close(), or the entry (and its live session) leaks and can be
        # inherited by a future loop that reuses the same id().
        self._per_loop: dict[int, dict] = {}
        self._per_loop_lock = threading.Lock()

        # Staged-retry hard-halt flag (in-memory, resets every launch — never
        # persisted). Order pulls retry timed-out pages at progressively lower
        # concurrency (see _fetch_pages_staged); if pages still time out at
        # concurrency 1 the link can't move the data, so we set _esi_halted,
        # alert the user once, and refuse all further ESI requests until the
        # app is restarted. See _check_halted / _set_halted.
        self._esi_halted = False
        self._adapt_lock = threading.Lock()

        self.type_name_cache: dict[int, str] = {}
        self.system_cache: dict[int, dict] = {}
        self.valid_systems: set[int] = set()

        # ESI cache expiry tracking
        self.market_expires: Optional[datetime] = None

        # Legacy Jita-specific cache (mostly superseded by order_cache)
        self.jita_orders_cache: list[dict] = []
        self.jita_orders_timestamp: Optional[datetime] = None

        # History cache: region_id -> {type_id -> history_list}
        self.history_cache: dict[int, dict[int, list[dict]]] = {}
        self.recent_history = get_history_cache()
        self.recent_history.overlay(self.history_cache)

        # Market history database (set externally, uses SQLite)
        self.market_history: Optional[MarketHistoryDB] = None

        # ESI supplement cache (for items missing from market history db)
        self.supplement = ESISupplementCache()

        # Shared per-region order cache (scanner <-> stock market)
        self.order_cache = OrderCacheStore()

        # Backfill legacy Jita fields from disk-loaded cache so has_jita_cache()
        # returns True on startup without forcing a fresh ESI fetch.
        _jita_entry = self.order_cache._order_cache.get(JITA_REGION_ID)
        if _jita_entry and _jita_entry.get('orders'):
            self.jita_orders_cache = _jita_entry['orders']
            self.jita_orders_timestamp = _jita_entry.get('timestamp')

    def _get_loop_state(self) -> dict:
        """Return the {semaphore, session} dict for the current event loop.

        Lazily creates the entry on first access from a given loop.
        """
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        key = id(loop)
        with self._per_loop_lock:
            state = self._per_loop.get(key)
            # id(loop) is only unique among LIVE loops: once a throwaway loop
            # is GC'd its id can be reused by a fresh loop, resurrecting the
            # dead loop's semaphore/session (both bound to the now-dead loop).
            # Identity-check the stored loop and discard any stale entry.
            if state is not None and state.get("loop") is not loop:
                print("[API] per-loop state: loop id reuse detected — "
                      "discarding stale state")
                state = None
            if state is None:
                state = {
                    "loop": loop,
                    "semaphore": asyncio.Semaphore(MAX_CONCURRENT_REQUESTS),
                    "session": None,
                }
                self._per_loop[key] = state
        return state

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._get_loop_state()["semaphore"]

    @semaphore.setter
    def semaphore(self, value):
        # Setter kept for backward compatibility. None is a no-op (legacy
        # __init__ pattern); other values store into current loop's state.
        if value is None:
            return
        self._get_loop_state()["semaphore"] = value

    @property
    def session(self) -> Optional[aiohttp.ClientSession]:
        return self._get_loop_state().get("session")

    @session.setter
    def session(self, value):
        self._get_loop_state()["session"] = value

    # =========================================================================
    # Staged-retry concurrency — finish the current demand without stacking
    # new requests on a struggling link (in-memory only, resets every launch)
    # =========================================================================

    def _check_halted(self):
        """Raise if ESI has been hard-halted after a min-load failure.

        Called at the entry of every external-request path so that, once
        halted, no further requests go out until the app restarts.
        """
        if self._esi_halted:
            raise ESIConnectionHaltedError(
                "ESI halted after timeouts at minimum concurrency"
            )

    def _set_halted(self, endpoint: str):
        """Flip the hard-halt flag and alert the user once."""
        notify = False
        with self._adapt_lock:
            if not self._esi_halted:
                self._esi_halted = True
                notify = True
        if notify:
            print(
                f"[ESI-Adapt] HALT — {endpoint} still timing out at "
                f"concurrency 1; stopping all ESI requests until restart"
            )
            self._notify_halt()

    async def _fetch_pages_staged(
        self, endpoint: str, pages: list[int], page_loader=None
    ) -> tuple[dict[int, list], dict[int, Optional[str]], set[int]]:
        """Fetch `pages` of one region, retrying timed-out pages at
        progressively lower concurrency: 20 -> 10 -> 5 -> 2 -> 1.

        Each wave runs to completion (every page resolves to data, empty, or
        a timeout) BEFORE the next, narrower wave starts — so we only ever
        re-attempt the same outstanding demand, never stack fresh requests on
        a link that's already struggling. That's the difference from a global
        dial: the in-flight count for the *failing* pages is actually bounded.

        Returns page data, each page's Last-Modified validator, and the set
        of pages that returned 404 because pagination changed mid-pull. If
        pages still time out at concurrency 1, the link can't move the data —
        hard-halt and raise ESIConnectionHaltedError.
        """
        results: dict[int, list] = {}
        validators: dict[int, Optional[str]] = {}
        stale_pages: set[int] = set()
        remaining = list(pages)
        concurrency = MAX_CONCURRENT_REQUESTS

        while remaining:
            self._check_halted()
            sem = asyncio.Semaphore(concurrency)
            cur = concurrency  # bind for the closure / log lines below
            print(
                f"[ESI-Adapt] {endpoint}: fetching {len(remaining)} page(s) "
                f"at concurrency {cur}"
            )

            async def fetch_one(p: int):
                async with sem:
                    try:
                        if page_loader is not None:
                            row = await page_loader(p)
                            return p, row, row["headers"].get("last-modified"), False
                        orders, headers = await self._get_with_headers(
                            endpoint, {"page": p}
                        )
                        last_modified = (
                            headers.get("Last-Modified")
                            or headers.get("last-modified")
                        )
                        return p, orders, last_modified, False
                    except aiohttp.ClientResponseError as e:
                        if e.status == 404:
                            return p, [], None, True
                        if e.status in (500, 502, 503, 504):
                            # Transient ESI/gateway error (500 backend error,
                            # 502/503/504 gateway) — treat like a timeout: mark
                            # for a gentler retry wave instead of aborting the
                            # whole scan. Persistent failures still resolve via
                            # the concurrency-1 hard-halt below.
                            print(
                                f"[ESI] HTTP {e.status} on {endpoint} page {p} "
                                f"(concurrency {cur}) — retrying"
                            )
                            return p, None, None, False
                        raise
                    except (asyncio.TimeoutError, TimeoutError):
                        # Live heartbeat — surface each timeout as it happens
                        # so a slow wave doesn't look like a hang.
                        print(
                            f"[ESI] Timeout on {endpoint} page {p} "
                            f"(concurrency {cur})"
                        )
                        return p, None, None, False

            wave = await asyncio.gather(*[fetch_one(p) for p in remaining], return_exceptions=True)
            for item in wave:
                if isinstance(item, BaseException):
                    raise item

            failed = []
            for p, orders, last_modified, is_stale in wave:
                if orders is None:
                    failed.append(p)
                else:
                    results[p] = orders
                    validators[p] = last_modified
                    if is_stale:
                        stale_pages.add(p)

            print(
                f"[ESI-Adapt] {endpoint}: concurrency {cur} wave done — "
                f"{len(remaining) - len(failed)} ok, {len(failed)} timed out"
            )

            if not failed:
                break

            if concurrency <= 1:
                self._set_halted(endpoint)
                raise ESIConnectionHaltedError(
                    f"{len(failed)} page(s) on {endpoint} still timing out "
                    f"at concurrency 1"
                )

            old = concurrency
            concurrency = max(1, concurrency // 2)
            print(
                f"[ESI-Adapt] {len(failed)} page(s) on {endpoint} timed out "
                f"— retrying just those at concurrency {old} -> {concurrency}"
            )
            remaining = failed

        return results, validators, stale_pages

    def _notify_halt(self):
        """Pop a one-time user-facing error that ESI has been halted."""
        msg = (
            "EVE ESI requests are timing out even at minimum load — your "
            "internet connection appears too slow or throttled to fetch "
            "market data.\n\nAll ESI requests have been stopped. Restart the "
            "app once your connection is back to normal."
        )
        try:
            from core.tk_queue import submit
            import tkinter.messagebox as messagebox
            submit(lambda: messagebox.showerror(
                "Connection Problem — ESI Halted", msg
            ))
        except Exception as e:
            print(f"[ESI-Adapt] could not show halt dialog: {e}")

    def reset_for_new_loop(self):
        """No-op kept for backward compatibility.

        Previously this overwrote a shared semaphore on every new loop,
        which caused cross-loop races between worker threads. Semaphore
        and session are now per-event-loop and created lazily, so callers
        no longer need to reset anything when entering a new loop.
        """
        pass

    def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure a valid aiohttp session exists for the current event loop."""
        state = self._get_loop_state()
        if state["session"] is None or state["session"].closed:
            state["session"] = aiohttp.ClientSession(
                connector=make_connector(),
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                headers=get_esi_headers(), trace_configs=[make_trace_config(pacing=False)],
            )
        return state["session"]

    def close_loop_state(self, loop) -> None:
        """Close the session tied to `loop` and forget its per-loop entry.

        Must run BEFORE loop.close() (the session close is awaited on that
        loop). Skipping this leaks the session's sockets, and worse: a future
        new_event_loop() that lands on the same id() inherits the stale entry —
        the never-closed session passes ensure_session's .closed check and
        requests then die with "Event loop is closed".
        """
        with self._per_loop_lock:
            state = self._per_loop.get(id(loop))
            # Only pop if the stored entry actually belongs to THIS loop — an
            # id(loop) collision could otherwise let us close a live loop's
            # session out from under it.
            if state is not None and state.get("loop") is loop:
                self._per_loop.pop(id(loop), None)
            else:
                state = None
        session = (state or {}).get("session")
        if session is not None and not session.closed:
            try:
                loop.run_until_complete(session.close())
            except Exception as e:
                print(f"[API] close_loop_state: session close failed: {e}")

    def get_history_cache_stats(self) -> str:
        """Get human-readable history cache stats."""
        if self.market_history is not None:
            try:
                stats = self.market_history.get_stats()
                row_count = stats.get('row_count', 0)
                if row_count > 0:
                    return f"{row_count:,} records (SQLite)"
            except Exception:
                pass

        total = sum(len(types) for types in self.history_cache.values())
        return f"{total} items cached (ESI)"

    def clear_history_cache(self):
        """Clear history cache to force refresh."""
        self.history_cache = {}

    # =========================================================================
    # Order cache delegation (backed by OrderCacheStore)
    # =========================================================================

    def cache_orders_for_region(self, region_id: int, orders: list,
                                 expires: Optional[datetime] = None, **metadata):
        self.order_cache.cache_orders_for_region(region_id, orders, expires, **metadata)

    def get_cached_orders(self, region_id: int, max_age_seconds: int = 300) -> Optional[list]:
        return self.order_cache.get_cached_orders(region_id, max_age_seconds)

    def clear_region_disk_cache(self, region_id: int):
        self.order_cache.clear_region_disk_cache(region_id)

    # =========================================================================
    # ESI expiry / refresh timing
    # =========================================================================

    def _apply_earliest_expires(self, expires: Optional[datetime]):
        """Update market_expires using earliest-wins rule.

        Past timestamps are ignored to prevent zeroing the countdown on stale
        cache hits or clock skew.
        """
        if expires is None:
            return
        if expires <= datetime.now(timezone.utc):
            return
        if self.market_expires is None or expires < self.market_expires:
            self.market_expires = expires

    def get_seconds_until_refresh(self) -> float:
        """Get seconds until ESI market cache expires. Returns 0 if unknown or expired."""
        if self.market_expires is None:
            return 0

        now = datetime.now(timezone.utc)
        delta = (self.market_expires - now).total_seconds()

        if delta <= 0:
            return 0

        return delta + 1.0  # 1s buffer so CCP servers have refreshed

    def _parse_expires_header(self, response: aiohttp.ClientResponse) -> Optional[datetime]:
        """Parse cache expiry from ESI response headers (Cache-Control first, Expires fallback)."""
        cache_control = response.headers.get("Cache-Control", "")
        match = re.search(r'max-age=(\d+)', cache_control)
        if match:
            max_age = int(match.group(1))
            return datetime.now(timezone.utc) + timedelta(seconds=max_age)

        expires_str = response.headers.get("Expires")
        if not expires_str:
            return None

        try:
            return datetime.strptime(expires_str, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            connector=make_connector(),
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            headers=get_esi_headers(), trace_configs=[make_trace_config(pacing=False)],
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------ rate limits

    def _record_response_headers(self, response: aiohttp.ClientResponse,
                                 endpoint: str = ""):
        """Feed ESI rate headers into the process-wide pacing gate."""
        record_response(response.headers, response.status, endpoint)

    async def _await_rate_limit_pause(self):
        """Wait for the process-wide legacy/bucket rate gate."""
        await wait_async()

    async def get_system_info(self, system_id: int) -> dict:
        """Get system info including security status and stargates."""
        if system_id in self.system_cache:
            return self.system_cache[system_id]

        try:
            data = await self._get(f"/universe/systems/{system_id}/")

            neighbors = []
            if "stargates" in data:
                stargate_tasks = [
                    self._get(f"/universe/stargates/{sg_id}/")
                    for sg_id in data["stargates"]
                ]
                stargates = await asyncio.gather(*stargate_tasks, return_exceptions=True)
                for sg in stargates:
                    if isinstance(sg, dict) and "destination" in sg:
                        neighbors.append(sg["destination"]["system_id"])

            info = {
                "security": data.get("security_status", 0),
                "name": data.get("name", f"System {system_id}"),
                "neighbors": neighbors
            }
            self.system_cache[system_id] = info
            return info
        except Exception:
            return {"security": 0, "name": "Unknown", "neighbors": []}

    async def build_valid_systems_cache(self, progress_callback=None, system_ids: list[int] = None) -> set[int]:
        """Check which systems are high-sec from a list of system IDs."""
        if not system_ids:
            return self.valid_systems

        def update(text):
            if progress_callback:
                progress_callback(text, 5)

        uncached = [sid for sid in system_ids if sid not in self.system_cache]

        if uncached:
            update(f"Checking {len(uncached)} systems...")
            tasks = [self._get_system_security(sid) for sid in uncached]
            await asyncio.gather(*tasks, return_exceptions=True)

        for system_id in system_ids:
            security = self.system_cache.get(system_id, {}).get("security", 0)
            if security >= MIN_SECURITY_STATUS:
                self.valid_systems.add(system_id)

        return self.valid_systems

    async def _get_system_security(self, system_id: int):
        """Fetch just the security status for a system."""
        if system_id in self.system_cache:
            return
        try:
            data = await self._get(f"/universe/systems/{system_id}/")
            self.system_cache[system_id] = {
                "security": data.get("security_status", 0),
                "name": data.get("name", f"System {system_id}")
            }
        except Exception:
            self.system_cache[system_id] = {"security": 0, "name": "Unknown"}

    async def get_orders_for_hub(
        self, hub_key: str, use_cache: bool = False, force_refresh: bool = False
    ) -> list[dict]:
        """Fetch orders for any registered hub — NPC region or player structure.

        Routes to `/markets/{region_id}/orders/` for NPC hubs (the historical
        path) and to `/markets/structures/{id}/` for player structures via
        the authorization-scoped shared structure provider.
        """
        from core.config import get_hub_config
        hub_config = get_hub_config(hub_key)

        if hub_config.get("type") != "structure":
            return await self.get_market_orders(
                hub_config["region_id"],
                use_cache=use_cache,
                force_refresh=force_refresh,
            )

        structure_id = hub_config["station_id"]

        # Auth-scoped provider owns reuse and in-flight work, including Browse.
        # force_refresh never bypasses a still-unexpired ESI response.
        from esi.esi_auth import ESIAuth
        from core.esi_private_cache import private_context, PrivateSnapshotError
        from esi.esi_structures import (
            fetch_structure_orders, _structure_snapshot,
            StructureAccessError, StructureSnapshotError,
        )
        auth = ESIAuth.singleton()
        try:
            context = private_context(auth)
            orders, expires, metadata = await asyncio.to_thread(
                fetch_structure_orders, structure_id, auth, "seller")
            if private_context(auth) != context:
                raise StructureAccessError(structure_id, 401, "authorization changed")
        except PrivateSnapshotError:
            print(f"[Structure] {hub_key} fetch failed: authorization unavailable")
            return []
        except StructureAccessError as exc:
            print(f"[Structure] {hub_key} fetch failed: {exc}")
            return []
        except StructureSnapshotError as exc:
            # Only the current authorization context may supply a fallback.
            try:
                previous = await asyncio.to_thread(
                    _structure_snapshot, "structure_orders", structure_id,
                    auth, "seller", fetch=False)
                if private_context(auth) != context:
                    previous = None
            except (StructureAccessError, StructureSnapshotError, PrivateSnapshotError):
                previous = None
            stale = previous["payload"] if previous else []
            print(f"[Structure] {hub_key} snapshot rejected: {exc}; kept {len(stale)} prior orders")
            return stale
        self._apply_earliest_expires(expires)
        try:
            from history.structure_history import StructureHistoryDB
            StructureHistoryDB.singleton().record_snapshot(
                structure_id, orders, now_utc=metadata["acquired_at"])
        except Exception as exc:
            print(f"[StructHist] hook skipped for {structure_id}: {exc}")
        return orders

    async def get_market_orders(self, region_id: int, use_cache: bool = False,
                                 force_refresh: bool = False) -> list[dict]:
        """Fetch a complete scanner-current book; flags cannot bypass ESI expiry.

        use_cache remains a compatibility argument. Deliberately older Jita
        reference data is available only through get_jita_reference().
        """
        cached = self.order_cache.get_cached_orders(region_id)
        if cached is not None:
            entry = self.order_cache.get_snapshot_entry(region_id)
            self._apply_earliest_expires(entry.get('expires'))
            return cached

        async with self.order_cache.fetch_guard(region_id):
            # Recheck even for forced callers after another worker completes.
            cached = self.order_cache.get_cached_orders(region_id)
            if cached is not None:
                entry = self.order_cache.get_snapshot_entry(region_id)
                self._apply_earliest_expires(entry.get('expires'))
                return cached

            self._check_halted()
            for snapshot_attempt in range(2):
                try:
                    all_orders, expires, total_pages, metadata = (
                        await fetch_market_snapshot(self, region_id)
                    )
                    break
                except ESIPaginationChangedError as e:
                    decision = "RETRY" if snapshot_attempt == 0 else "REJECT"
                    print(
                        "[ESI-Timing] resource=regional_orders "
                        f"trigger=page_validation region={region_id} "
                        f"decision={decision} reason=representation_changed "
                        f"details={e}"
                    )
                    if snapshot_attempt == 1:
                        raise

            self._apply_earliest_expires(expires)
            print(
                "[ESI-Timing] resource=regional_orders trigger=page_validation "
                f"region={region_id} decision=ACCEPT pages={total_pages} "
                f"orders={len(all_orders)}"
            )

            self.order_cache.cache_orders_for_region(region_id, all_orders, expires, **metadata)

            # Legacy Jita cache (preserve existing behavior)
            if region_id == JITA_REGION_ID:
                self.jita_orders_cache = all_orders
                self.jita_orders_timestamp = metadata.get("acquired_at") or datetime.now(timezone.utc)

            return all_orders

    async def _get_page_safe(self, endpoint: str, page: int) -> list | None:
        """Fetch a page, returning None on 404 (stale pagination). Retries once on timeout."""
        for attempt in range(2):
            try:
                return await self._get(endpoint, {"page": page})
            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    return None
                raise
            except (asyncio.TimeoutError, TimeoutError):
                if attempt == 0:
                    print(f"[ESI] Timeout on {endpoint} page {page}, retrying...")
                else:
                    print(f"[ESI] Timeout on {endpoint} page {page}, skipping page")
                    return None

    def get_system_security(self, system_id: int) -> float:
        """Get cached security status for a system."""
        return self.system_cache.get(system_id, {}).get("security", 0)

    def is_valid_system(self, system_id: int) -> bool:
        """Check if system is within range and high-sec."""
        return system_id in self.valid_systems
