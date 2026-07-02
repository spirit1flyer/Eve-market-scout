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
    ESI_USER_AGENT, ERROR_LIMIT_PAUSE_THRESHOLD,
)
from esi.esi_supplement import ESISupplementCache
from history.market_history import MarketHistoryDB
from core.ssl_context import make_connector
from core.order_cache import OrderCacheStore
from core.type_name_mixin import TypeNameMixin

# Local debug flag - set True to enable diagnostic output
DEBUG_ESI = False


class ESIConnectionHaltedError(RuntimeError):
    """Raised when ESI has been hard-halted after repeated timeouts at
    minimum concurrency (budget already 1 and still failing). The link
    can't move the data, so all external requests stop. Cleared only by
    restarting the app."""


class ESIClient(TypeNameMixin):
    """Async client for EVE ESI API."""

    def __init__(self):
        # Per-event-loop async primitives. Each worker thread runs its own
        # event loop and gets its own semaphore + session, so concurrent
        # threads can't stomp each other (was the cause of "Semaphore is
        # bound to a different event loop" cross-loop races on cold start).
        # Entries for closed loops leak in this dict but the loops themselves
        # are GC'd; cleanup is a TODO for later.
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

        # Market history database (set externally, uses SQLite)
        self.market_history: Optional[MarketHistoryDB] = None

        # ESI supplement cache (for items missing from market history db)
        self.supplement = ESISupplementCache()

        # Shared per-region order cache (scanner <-> stock market)
        self.order_cache = OrderCacheStore()

        # Error-budget pause. Set when X-ESI-Error-Limit-Remain dips below
        # ERROR_LIMIT_PAUSE_THRESHOLD; we sleep until the window resets so
        # we don't trigger a 420 (which would block ALL our requests for the
        # remainder of the window and put us on CCP's radar for bans).
        # Plain attribute — write contention across loops is harmless here,
        # extending the pause is always safe.
        self._error_limit_pause_until: Optional[datetime] = None

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
            if state is None:
                state = {
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
        self, endpoint: str, pages: list[int]
    ) -> dict[int, list]:
        """Fetch `pages` of one region, retrying timed-out pages at
        progressively lower concurrency: 20 -> 10 -> 5 -> 2 -> 1.

        Each wave runs to completion (every page resolves to data, empty, or
        a timeout) BEFORE the next, narrower wave starts — so we only ever
        re-attempt the same outstanding demand, never stack fresh requests on
        a link that's already struggling. That's the difference from a global
        dial: the in-flight count for the *failing* pages is actually bounded.

        Returns {page -> orders}. 404 (stale pagination) counts as an empty,
        resolved page. If pages still time out at concurrency 1, the link
        can't move the data — hard-halt and raise ESIConnectionHaltedError.
        """
        results: dict[int, list] = {}
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
                        orders = await self._get(endpoint, {"page": p})
                        return p, orders
                    except aiohttp.ClientResponseError as e:
                        if e.status == 404:
                            return p, []  # page gone (stale pagination)
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
                            return p, None
                        raise
                    except (asyncio.TimeoutError, TimeoutError):
                        # Live heartbeat — surface each timeout as it happens
                        # so a slow wave doesn't look like a hang.
                        print(
                            f"[ESI] Timeout on {endpoint} page {p} "
                            f"(concurrency {cur})"
                        )
                        return p, None  # mark for a gentler retry wave

            wave = await asyncio.gather(*[fetch_one(p) for p in remaining])

            failed = []
            for p, orders in wave:
                if orders is None:
                    failed.append(p)
                else:
                    results[p] = orders

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

        return results

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
                headers={"User-Agent": ESI_USER_AGENT},
            )
        return state["session"]

    def clear_jita_cache(self):
        """Clear Jita orders cache to force refresh on next scan."""
        self.jita_orders_cache = []
        self.jita_orders_timestamp = None

    def has_jita_cache(self) -> bool:
        """Check if Jita orders are cached."""
        return len(self.jita_orders_cache) > 0

    def get_jita_cache_age(self) -> str:
        """Get human-readable age of Jita cache."""
        if self.jita_orders_timestamp is None:
            return "No cache"

        age = datetime.now(timezone.utc) - self.jita_orders_timestamp
        minutes = int(age.total_seconds() / 60)

        if minutes < 1:
            return "< 1 min"
        elif minutes < 60:
            return f"{minutes} min"
        else:
            hours = minutes // 60
            return f"{hours}h {minutes % 60}m"

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
                                 expires: Optional[datetime] = None):
        self.order_cache.cache_orders_for_region(region_id, orders, expires)

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
            headers={"User-Agent": ESI_USER_AGENT},
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    # ------------------------------------------------------------ rate limits

    def _record_response_headers(self, response: aiohttp.ClientResponse,
                                 endpoint: str = ""):
        """Inspect ESI's error-budget headers and arm a pause if we're close.

        `X-ESI-Error-Limit-Remain` counts non-2xx/3xx responses left in the
        current 60s window (limit is 100). When it dips low we set
        `_error_limit_pause_until` to the window's reset time so subsequent
        requests park themselves instead of triggering a 420.

        Logs (every line carries a logger timestamp from main.py's
        basicConfig, so the times line up with the rest of eve_scout.log):
          [ESI-RL] error budget X remaining on <endpoint>           (warn zone)
          [ESI-RL] pausing N s until window resets at <UTC ISO>     (gate arm)
        """
        remain_raw = response.headers.get("X-Esi-Error-Limit-Remain")
        reset_raw = response.headers.get("X-Esi-Error-Limit-Reset")
        if not remain_raw or not reset_raw:
            return
        try:
            remain = int(remain_raw)
            reset = int(reset_raw)
        except (ValueError, TypeError):
            return

        # Warning zone — budget is dipping but we're not throttling yet. Lets
        # the user see WHICH endpoint started eating the budget before the
        # pause-armed line ever fires.
        if (
            remain <= ERROR_LIMIT_PAUSE_THRESHOLD * 3
            and remain > ERROR_LIMIT_PAUSE_THRESHOLD
            and response.status >= 400
        ):
            print(
                f"[ESI-RL] error budget low: {remain}/100 remaining after "
                f"HTTP {response.status} on {endpoint or '?'} (resets in {reset}s)"
            )

        if remain > ERROR_LIMIT_PAUSE_THRESHOLD:
            return

        pause_until = datetime.now(timezone.utc) + timedelta(seconds=reset + 1)
        prior = self._error_limit_pause_until
        if prior is None or pause_until > prior:
            self._error_limit_pause_until = pause_until
            print(
                f"[ESI-RL] THROTTLE ARMED: budget {remain}/100 after HTTP "
                f"{response.status} on {endpoint or '?'} — pausing all ESI "
                f"requests for {reset + 1}s, resumes at "
                f"{pause_until.isoformat(timespec='seconds')}"
            )

    async def _await_rate_limit_pause(self):
        """If a pause is armed, sleep until it expires before proceeding.

        First waiter to wake clears the pause and logs THROTTLE RELEASED so
        the log shows a clean start/end pair per throttle event. Concurrent
        waiters that lose the race just see pause_until cleared and proceed
        silently.
        """
        pause_until = self._error_limit_pause_until
        if pause_until is None:
            return
        delay = (pause_until - datetime.now(timezone.utc)).total_seconds()
        if delay <= 0:
            return
        await asyncio.sleep(delay)
        # GIL-atomic check-and-clear — only the first waker logs the release.
        if self._error_limit_pause_until == pause_until:
            self._error_limit_pause_until = None
            print(
                f"[ESI-RL] THROTTLE RELEASED at "
                f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} "
                f"— resuming ESI requests"
            )

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Rate-limited GET. Honors X-ESI-Error-Limit-* and 429 Retry-After.

        429s get one automatic retry after sleeping for `Retry-After` seconds;
        any other 4xx/5xx falls through to `raise_for_status()` as before so
        existing callers (e.g. `_get_page_safe` swallowing 404) keep working.
        """
        self._check_halted()
        url = f"{ESI_BASE_URL}{endpoint}"
        await self._await_rate_limit_pause()
        async with self.semaphore:
            for attempt in range(2):
                async with self.session.get(url, params=params) as response:
                    self._record_response_headers(response, endpoint)
                    if response.status == 429 and attempt == 0:
                        retry_after = max(
                            1, int(response.headers.get("Retry-After", "1") or "1")
                        )
                        print(
                            f"[ESI-RL] HTTP 429 on {endpoint} — Retry-After "
                            f"{retry_after}s, sleeping then retrying once"
                        )
                        await asyncio.sleep(retry_after)
                        continue
                    if response.status == 429:
                        print(
                            f"[ESI-RL] HTTP 429 on {endpoint} AGAIN after "
                            f"retry — giving up, raising"
                        )
                    response.raise_for_status()
                    return await response.json()
        # Unreachable — the loop always returns or raises — but kept for type
        # checkers that can't prove that.
        raise RuntimeError("ESI _get fell through retry loop unexpectedly")

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
        the auth'd `esi_structures.fetch_structure_orders`. Structures don't
        have a regional market, so we cache them under the structure_id —
        which is >= 1T and can't collide with any region_id.
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

        if not force_refresh:
            cached = self.order_cache.get_cached_orders(structure_id)
            if cached is not None:
                cache_entry = self.order_cache._order_cache.get(structure_id, {})
                self._apply_earliest_expires(cache_entry.get('expires'))
                return cached

        from esi.esi_auth import ESIAuth
        from esi.esi_structures import fetch_structure_orders, StructureAccessError

        auth = ESIAuth()
        loop = asyncio.get_event_loop()

        def _blocking():
            return fetch_structure_orders(structure_id, auth, slot="seller")

        try:
            orders, expires = await loop.run_in_executor(None, _blocking)
        except StructureAccessError as e:
            print(f"[Structure] {hub_key} fetch failed: {e}")
            return []

        self._apply_earliest_expires(expires)
        self.order_cache.cache_orders_for_region(structure_id, orders, expires=expires)

        # Phase 1: silent observed-history collection. record_snapshot
        # swallows its own exceptions; the outer try guards the import path
        # so a broken module can never block the scanner.
        try:
            from history.structure_history import StructureHistoryDB
            StructureHistoryDB.singleton().record_snapshot(structure_id, orders)
        except Exception as e:
            print(f"[StructHist] hook skipped for {structure_id}: {e}")

        return orders

    async def get_market_orders(self, region_id: int, use_cache: bool = False,
                                 force_refresh: bool = False) -> list[dict]:
        """Fetch all market orders for a region (handles pagination).

        Cache layering (when force_refresh is False):
        - First: shared per-region cache (5 min freshness + ESI expires check)
        - Then: legacy Jita-specific cache (if use_cache=True and Jita region)
        - Otherwise: fetch from ESI

        Concurrent calls for the same region are serialized via a per-region
        lock so a second caller reads from the populated cache instead of
        starting a duplicate fetch.
        """
        if not force_refresh:
            cached = self.order_cache.get_cached_orders(region_id)
            if cached is not None:
                cache_entry = self.order_cache._order_cache.get(region_id, {})
                self._apply_earliest_expires(cache_entry.get('expires'))
                return cached

            if use_cache and region_id == JITA_REGION_ID and self.jita_orders_cache:
                return self.jita_orders_cache

        fetch_lock = self.order_cache._get_region_fetch_lock(region_id)
        fetch_lock.acquire()
        try:
            # Re-check after acquiring lock — another thread may have populated cache
            if not force_refresh:
                cached = self.order_cache.get_cached_orders(region_id)
                if cached is not None:
                    cache_entry = self.order_cache._order_cache.get(region_id, {})
                    self._apply_earliest_expires(cache_entry.get('expires'))
                    return cached

            self._check_halted()
            all_orders = []

            url = f"{ESI_BASE_URL}/markets/{region_id}/orders/"
            orders_endpoint = f"/markets/{region_id}/orders/"
            await self._await_rate_limit_pause()
            async with self.semaphore:
                # Mirror _get's 429 retry but keep the response object live so
                # we can read X-Pages / Cache-Control before the body parse.
                for attempt in range(2):
                    async with self.session.get(url, params={"page": 1}) as response:
                        self._record_response_headers(response, orders_endpoint)
                        if response.status == 429 and attempt == 0:
                            retry_after = max(
                                1, int(response.headers.get("Retry-After", "1") or "1")
                            )
                            print(
                                f"[ESI-RL] HTTP 429 on {orders_endpoint} — "
                                f"Retry-After {retry_after}s, sleeping then retrying once"
                            )
                            await asyncio.sleep(retry_after)
                            continue
                        if response.status == 429:
                            print(
                                f"[ESI-RL] HTTP 429 on {orders_endpoint} AGAIN "
                                f"after retry — giving up, raising"
                            )
                        response.raise_for_status()
                        total_pages = int(response.headers.get("X-Pages", 1))
                        expires = self._parse_expires_header(response)
                        self._apply_earliest_expires(expires)
                        all_orders.extend(await response.json())
                        break

            if total_pages > 1:
                # Staged retry: pages 2..N as one unit of demand, timed-out
                # pages re-attempted at progressively lower concurrency rather
                # than stacking fresh requests on a struggling link.
                page_results = await self._fetch_pages_staged(
                    orders_endpoint, list(range(2, total_pages + 1))
                )
                for page_orders in page_results.values():
                    if page_orders:
                        all_orders.extend(page_orders)

            self.order_cache.cache_orders_for_region(region_id, all_orders, expires=expires)

            # Legacy Jita cache (preserve existing behavior)
            if region_id == JITA_REGION_ID:
                self.jita_orders_cache = all_orders
                self.jita_orders_timestamp = datetime.now(timezone.utc)

            return all_orders
        finally:
            fetch_lock.release()

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

    async def get_market_history(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history for a specific item in a region."""
        try:
            return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})
        except Exception:
            return []

    async def get_market_history_bulk(self, region_id: int, type_ids: list[int],
                                       use_cache: bool = True) -> dict[int, list[dict]]:
        """Fetch market history for multiple items.

        Priority:
        1. Market history database (SQLite) - instant indexed lookups
        2. ESI supplement cache (for items missing from db)
        3. In-memory ESI cache
        4. ESI API calls for missing items
        """
        if self.market_history is not None:
            result = self.market_history.get_history_bulk(region_id, type_ids, days=30)

            missing_from_db = [tid for tid in type_ids if not result.get(tid)]
            has_data = [tid for tid in type_ids if result.get(tid)]

            if DEBUG_ESI:
                print(f"[DIAG] Market history DB for region {region_id}:")
                print(f"[DIAG]   Requested: {len(type_ids)} items")
                print(f"[DIAG]   Have data: {len(has_data)} items")
                print(f"[DIAG]   Missing (empty): {len(missing_from_db)} items")

            if missing_from_db:
                still_missing = []
                supplement_hits = 0

                for tid in missing_from_db:
                    cached = self.supplement.get_if_fresh(region_id, tid)
                    if cached is not None:
                        result[tid] = cached
                        supplement_hits += 1
                    else:
                        still_missing.append(tid)

                if supplement_hits > 0 and DEBUG_ESI:
                    print(f"[DIAG]   ESI supplement cache: {supplement_hits} items")

                if still_missing:
                    known_bad = [tid for tid in still_missing
                                 if self.supplement.is_known_bad(region_id, tid)]
                    to_fetch = [tid for tid in still_missing if tid not in known_bad]

                    if known_bad and DEBUG_ESI:
                        print(f"[DIAG]   Skipping {len(known_bad)} known-bad items (2+ errors)")
                    for tid in known_bad:
                        result[tid] = []

                    if to_fetch:
                        if DEBUG_ESI:
                            print(f"[DIAG]   Still need ESI fetch: {len(to_fetch)} items")
                            print(f"[DIAG]   Missing type_ids (first 20): {to_fetch[:20]}")

                        esi_results, esi_errors = await self._fetch_history_from_esi(
                            region_id, to_fetch, track_errors=True)

                        esi_got_data = esi_empty = esi_error = 0
                        for tid in to_fetch:
                            if tid in esi_errors:
                                esi_error += 1
                                self.supplement.store(region_id, tid, [], is_error=True)
                                result[tid] = []
                            else:
                                data = esi_results.get(tid, [])
                                if data:
                                    esi_got_data += 1
                                    self.supplement.store(region_id, tid, data)
                                else:
                                    esi_empty += 1
                                    self.supplement.store(region_id, tid, [])
                                result[tid] = data

                        if DEBUG_ESI:
                            print(f"[DIAG]   ESI fallback: {esi_got_data} got data, "
                                  f"{esi_empty} empty, {esi_error} errors")

            final_with_data = sum(1 for tid in type_ids if result.get(tid))
            final_empty = sum(1 for tid in type_ids if not result.get(tid))
            if DEBUG_ESI:
                print(f"[DIAG]   Final: {final_with_data} with data, {final_empty} empty")

            if region_id not in self.history_cache:
                self.history_cache[region_id] = {}
            self.history_cache[region_id].update(result)

            return result

        # === FALLBACK: Original ESI-based approach ===
        if region_id not in self.history_cache:
            self.history_cache[region_id] = {}

        region_cache = self.history_cache[region_id]
        uncached = [tid for tid in type_ids if tid not in region_cache] if use_cache else list(type_ids)

        if type_ids and DEBUG_ESI:
            cached_count = len(type_ids) - len(uncached)
            if uncached:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, "
                      f"fetching {len(uncached)} from ESI")
            else:
                print(f"[DIAG] History cache: {cached_count}/{len(type_ids)} cached, no ESI calls needed")

        if uncached:
            esi_results, _ = await self._fetch_history_from_esi(region_id, uncached, track_errors=False)
            region_cache.update(esi_results)

        return {tid: region_cache.get(tid, []) for tid in type_ids}

    async def _fetch_history_from_esi(self, region_id: int, type_ids: list[int],
                                       track_errors: bool = False) -> tuple[dict[int, list[dict]], set[int]]:
        """Fetch history for multiple items from ESI API."""
        result = {}
        errors = set()

        success_count = error_count = empty_count = 0
        errors_seen = {}

        tasks = [self._get_market_history_raw(region_id, tid) for tid in type_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for tid, history in zip(type_ids, results):
            if isinstance(history, Exception):
                error_count += 1
                err_type = type(history).__name__
                errors_seen[err_type] = errors_seen.get(err_type, 0) + 1
                if track_errors:
                    errors.add(tid)
                else:
                    result[tid] = []
            elif isinstance(history, list):
                if history:
                    success_count += 1
                else:
                    empty_count += 1
                result[tid] = history
            else:
                error_count += 1
                if track_errors:
                    errors.add(tid)
                else:
                    result[tid] = []

        if DEBUG_ESI:
            print(f"[DIAG] ESI fetch for {len(type_ids)} items: "
                  f"{success_count} success, {empty_count} empty, {error_count} errors")
            if errors_seen:
                print(f"[DIAG]   Error types: {errors_seen}")

        return result, errors

    async def _get_market_history_raw(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history with exceptions not caught (for diagnostics)."""
        return await self._get(f"/markets/{region_id}/history/", {"type_id": type_id})

    async def freshen_history_for_items(self, region_id: int,
                                         type_ids: list[int]) -> None:
        """Force-fetch ESI history for tracked items and merge into history_cache.

        Unlike get_market_history_bulk (which short-circuits on SQLite hits
        and never calls ESI for items everef has), this always calls ESI.
        Used by the stock market holdings UI to close the 1-4 day recency
        gap on user-tracked items: SQLite gives the long tail, ESI fills
        the most recent few days that haven't been imported yet.

        Merge rule: keep all existing dates (SQLite usually has more
        history), add any ESI dates not already present, sort newest-first.
        """
        if not type_ids:
            return
        esi_results, _ = await self._fetch_history_from_esi(
            region_id, list(type_ids), track_errors=False
        )
        if region_id not in self.history_cache:
            self.history_cache[region_id] = {}
        region_cache = self.history_cache[region_id]
        for tid, fresh in esi_results.items():
            if not fresh:
                continue
            existing = region_cache.get(tid, [])
            if not existing:
                region_cache[tid] = fresh
                continue
            existing_dates = {r.get("date") for r in existing}
            merged = list(existing) + [
                r for r in fresh if r.get("date") not in existing_dates
            ]
            merged.sort(key=lambda r: r.get("date") or "", reverse=True)
            region_cache[tid] = merged
