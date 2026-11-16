"""Shared per-region order cache with in-flight guard and disk persistence."""

import asyncio
import gzip
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from core.config import TRADE_HUBS
from core.sound_manager import get_data_dir
from core.esi_diagnostics import decision_event, format_refresh_wait

_REGION_ID_TO_HUB_KEY: dict[int, str] = {}
for _hub_key, _hub_cfg in TRADE_HUBS.items():
    if _hub_cfg.get("type") != "structure":
        # Built-in hubs are inserted first; a custom NPC station in the same
        # region shares that complete regional snapshot and disk file.
        _REGION_ID_TO_HUB_KEY.setdefault(_hub_cfg["region_id"], _hub_key)


class OrderCacheStore:
    """Shared per-region order cache: in-memory, in-flight guard, and disk persistence."""

    def __init__(self):
        self._order_cache: dict[int, dict] = {}
        self._inflight_locks: dict[int, threading.Lock] = {}
        self._inflight_locks_meta = threading.Lock()
        self._load_all_disk_caches()

    def cache_orders_for_region(
        self,
        region_id: int,
        orders: list,
        expires: Optional[datetime] = None,
        *,
        etag: Optional[str] = None,
        last_modified: Optional[str] = None,
        complete: bool = True,
        source: str = "unknown",
    ):
        """Cache orders for a region, with empty-result guard.

        Rejects an empty write when a populated cache already exists, protecting
        against transient ESI hiccups wiping out good data. First-fetch empties
        are still stored so the cache shape stays consistent.
        """
        if not orders:
            existing = self._order_cache.get(region_id)
            if existing and existing.get('orders'):
                print(f"[API] Skipped empty cache write for region "
                      f"{region_id} (kept {len(existing['orders'])} existing)")
                return

        self._order_cache[region_id] = {
            'orders': orders,
            'timestamp': datetime.now(timezone.utc),
            'expires': expires,
            'etag': etag,
            'last_modified': last_modified,
            'complete': complete,
            'source': source,
        }
        print(f"[API] Cached {len(orders)} orders for region {region_id}")
        self._save_region_cache_to_disk(region_id)

    def get_cached_orders(self, region_id: int, max_age_seconds: int = 300) -> Optional[list]:
        """Get cached orders if fresh enough.

        Two independent freshness checks:
        1. Timestamp age must be under max_age_seconds.
        2. Stored ESI Expires must still be in the future — a cache hit on an
           already-expired entry would poison the scanner's countdown timer.
        """
        timing = self.get_cache_timing(region_id, max_age_seconds)
        decision_event(
            "structure_orders" if region_id >= 10**12 else "regional_orders",
            "REUSE" if timing['reusable'] else "MISS", "shared_cache",
            reason=timing['reason'], items=timing['order_count'],
            next_in=f"{timing['expires_in_seconds']:.0f}s"
            if timing['expires_in_seconds'] is not None else "unknown",
        )
        if not timing['reusable']:
            return None

        cached = self._order_cache[region_id]
        age = timing['age_seconds']
        wait = format_refresh_wait(timing['expires_in_seconds'])
        print(f"[API] Using cached orders for region {region_id} "
              f"(age: {age:.0f}s; next scan eligible in: {wait})")
        return cached['orders']

    def get_cache_timing(self, region_id: int,
                         max_age_seconds: int = 300) -> dict:
        """Describe whether a regional order snapshot can be reused.

        This is deliberately metadata-only so UI diagnostics can explain a
        cache decision without inspecting or printing any individual order.
        The decision exactly mirrors :meth:`get_cached_orders`.
        """
        cached = self._order_cache.get(region_id)
        if not cached:
            return {
                'present': False,
                'reusable': False,
                'reason': 'missing',
                'age_seconds': None,
                'expires_in_seconds': None,
                'order_count': 0,
                'complete': False,
                'source': None,
                'etag': None,
                'last_modified': None,
            }

        now = datetime.now(timezone.utc)
        age = max(0.0, (now - cached['timestamp']).total_seconds())
        expires = cached.get('expires')
        expires_in = ((expires - now).total_seconds()
                      if expires is not None else None)

        complete = bool(cached.get('complete', False))
        if not complete:
            reusable = False
            reason = 'incomplete'
        elif age > max_age_seconds:
            reusable = False
            reason = 'age_limit'
        elif expires_in is not None and expires_in <= 0:
            reusable = False
            reason = 'esi_expired'
        else:
            reusable = True
            reason = 'fresh'

        return {
            'present': True,
            'reusable': reusable,
            'reason': reason,
            'age_seconds': age,
            'expires_in_seconds': expires_in,
            'order_count': len(cached.get('orders') or []),
            'complete': complete,
            'source': cached.get('source'),
            'etag': cached.get('etag'),
            'last_modified': cached.get('last_modified'),
        }

    def peek_cached_orders(self, region_id: int,
                           max_age_seconds: int) -> Optional[tuple]:
        """Cached orders for consumers that tolerate ESI-expired data.

        Unlike get_cached_orders, this only checks the timestamp age — the
        stored ESI Expires is ignored, so a dump a few hours past its 5-min
        market expiry is still returned. Use for derived/approximate displays
        (not for the scanner, whose countdown timer the Expires check
        protects). Returns (orders, age_seconds) or None.
        """
        cached = self._order_cache.get(region_id)
        if not cached or not cached.get('complete', False):
            return None
        age = (datetime.now(timezone.utc) - cached['timestamp']).total_seconds()
        if age > max_age_seconds:
            return None
        return cached['orders'], age

    def _disk_cache_path(self, key_id: int) -> Optional[Path]:
        """Resolve on-disk cache path for a region id or structure id.

        Region ids (NPC hubs) hit the precomputed map. Structure ids miss it,
        so we fall back to scanning `TRADE_HUBS` for a structure-typed entry
        with a matching `station_id` — necessary because custom structures
        are registered at runtime after this module's import-time map was
        already snapshotted.
        """
        hub_key = _REGION_ID_TO_HUB_KEY.get(key_id)
        if hub_key is None:
            for k, cfg in TRADE_HUBS.items():
                if cfg.get("type") == "structure" and cfg.get("station_id") == key_id:
                    hub_key = k
                    break
        if hub_key is None:
            return None
        try:
            return get_data_dir() / f"orders_{hub_key}.json.gz"
        except Exception as e:
            print(f"[API] disk cache path unavailable for {key_id}: {e}")
            return None

    def _load_all_disk_caches(self):
        """Load every hub's persisted order cache from disk at startup.

        Covers NPC hub regions plus any custom structures already registered
        in TRADE_HUBS (custom_stations._bootstrap runs before ESIClient init,
        so structures persisted from a prior session are present here).
        """
        for region_id in _REGION_ID_TO_HUB_KEY:
            self._load_region_disk_cache(region_id)
        for cfg in TRADE_HUBS.values():
            if cfg.get("type") == "structure":
                sid = cfg.get("station_id")
                if sid:
                    self._load_region_disk_cache(sid)

    def _load_region_disk_cache(self, region_id: int):
        """Load one region's persisted cache from disk. Silent on missing file."""
        path = self._disk_cache_path(region_id)
        if path is None or not path.exists():
            return
        try:
            with gzip.open(path, 'rt', encoding='utf-8') as f:
                data = json.load(f)
            orders = data.get('orders') or []
            ts_str = data.get('timestamp')
            exp_str = data.get('expires')
            timestamp = (
                datetime.fromisoformat(ts_str) if ts_str
                else datetime.now(timezone.utc)
            )
            expires = datetime.fromisoformat(exp_str) if exp_str else None
            self._order_cache[region_id] = {
                'orders': orders,
                'timestamp': timestamp,
                'expires': expires,
                'etag': data.get('etag'),
                'last_modified': data.get('last_modified'),
                # Legacy cache files lack proof that every page completed.
                # Keep the data available for display, but reacquire before
                # treating it as a reusable acquisition snapshot.
                'complete': bool(data.get('complete', False)),
                'source': data.get('source', 'legacy_disk'),
            }
            hub_key = _REGION_ID_TO_HUB_KEY.get(region_id, str(region_id))
            print(f"[API] Loaded {len(orders)} orders for {hub_key} from disk")
        except Exception as e:
            print(f"[API] Failed to load disk cache for region {region_id}: {e}")

    def _save_region_cache_to_disk(self, region_id: int):
        """Persist one region's cache to disk. No-op for non-hub regions."""
        path = self._disk_cache_path(region_id)
        if path is None:
            return
        entry = self._order_cache.get(region_id)
        if not entry:
            return
        try:
            payload = {
                'region_id': region_id,
                'orders': entry.get('orders') or [],
                'timestamp': entry['timestamp'].isoformat()
                    if entry.get('timestamp') else None,
                'expires': entry['expires'].isoformat()
                    if entry.get('expires') else None,
                'etag': entry.get('etag'),
                'last_modified': entry.get('last_modified'),
                'complete': bool(entry.get('complete', False)),
                'source': entry.get('source'),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, 'wt', encoding='utf-8') as f:
                json.dump(payload, f)
        except Exception as e:
            print(f"[API] Failed to save disk cache for region {region_id}: {e}")

    def clear_region_disk_cache(self, region_id: int):
        """Drop a region's cache from memory and delete its disk file."""
        self._order_cache.pop(region_id, None)
        path = self._disk_cache_path(region_id)
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            print(f"[API] Failed to delete disk cache for region {region_id}: {e}")

    def _get_region_fetch_lock(self, region_id: int) -> threading.Lock:
        """Get (or create) a per-resource fetch lock for in-flight deduplication."""
        with self._inflight_locks_meta:
            if region_id not in self._inflight_locks:
                self._inflight_locks[region_id] = threading.Lock()
            return self._inflight_locks[region_id]

    @asynccontextmanager
    async def fetch_guard(self, cache_key: int):
        """Serialize one acquisition key across worker threads/event loops."""
        fetch_lock = self._get_region_fetch_lock(cache_key)
        acquire_fut = asyncio.get_running_loop().run_in_executor(
            None, fetch_lock.acquire
        )
        try:
            await asyncio.shield(acquire_fut)
        except asyncio.CancelledError:
            # The executor acquire keeps running after coroutine cancellation.
            # Release it when it lands so the resource cannot wedge forever.
            def release_orphaned_acquire(future):
                if not future.cancelled() and future.exception() is None:
                    fetch_lock.release()
            acquire_fut.add_done_callback(release_orphaned_acquire)
            raise
        try:
            yield
        finally:
            fetch_lock.release()
