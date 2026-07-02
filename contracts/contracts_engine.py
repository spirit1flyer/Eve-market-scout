"""Cache/diff engine for the Contracts tab (Step 3).

Sits on top of contracts_db (storage) and contracts_esi (network). One public
sync that the search tab triggers manually and the hourly pull (Step 6) calls
on a timer:

    sync_region(region_id, item_fetch_locations=None, ...)

The flow, in order:
  1. Freshness gate — if `now < stored expiry`, skip ESI entirely (unless the
     items worklist still has work). The list pull is conditional on the stored
     ETag, so even a stale window costs a single 304 most of the time.
  2. Diff by contract_id — `new = fresh - cached`, `gone = cached - fresh`.
     Contract_ids are unique/never-reused, so no timestamps needed. Gone
     contracts (completed/expired) are pruned along with their cached items.
  3. Items fetch — ONLY for contracts at `item_fetch_locations` (the station
     cost lever). `None` means region-wide (the deferred, expensive path).
     Excluded contracts are dropped from the worklist before fetching, so the
     exclusion list also trims fetch load.
  4. Name resolution — issuer/corp ids resolved via ESI and cached. Item names
     are a free local SDE lookup, done at display time (not here).

Freshness is keyed by REGION (`scope_key_for_region`), not by station: the list
pull is always region-wide (you must pull the region list to find a station's
rows), so two stations in one region share one list pull / one ETag. Station
scope only narrows which contracts get their *items* opened.

Driven from the live app's event loop (CLI harnesses fail on the user's
machine). `run_sync_in_thread` is the GUI entry point: worker thread → fresh
loop → `sync_region` → `submit()` the result back to the Tk thread.

All diagnostics carry the greppable `[ContractDiag]` tag.
"""

import asyncio
import threading
from typing import Callable, Optional

from contracts import contracts_esi
from contracts.contracts_db import ContractsDB, scope_key_for_region


def _print(msg: str) -> None:
    print(f"[ContractDiag] {msg}")


class _StoreBuffer:
    """Buffers streamed item results and flushes to the DB in batches.

    `add` is called from the fetch workers as each contract lands; once the
    buffer hits `batch_size` it persists (one transaction) so progress survives
    a mid-crawl shutdown. gone/bad ids are collected for end-of-crawl handling.
    """

    def __init__(self, db, batch_size: int = 200):
        self.db = db
        self.batch_size = batch_size
        self._buf: dict[int, list] = {}
        self.gone: list[int] = []
        self.bad: list[int] = []

    def add(self, contract_id: int, res: dict) -> None:
        if res.get("gone"):
            self.gone.append(int(contract_id))
        elif res.get("status") == 200:
            self._buf[int(contract_id)] = res.get("items") or []
            if len(self._buf) >= self.batch_size:
                self.flush()
        elif res.get("status") == 400:
            self.bad.append(int(contract_id))
        # else: transient (timeout/5xx/429-give-up) — leave unfetched to retry.

    def flush(self) -> None:
        if self._buf:
            self.db.store_items_batch(self._buf)
            self._buf = {}


class ContractsEngine:
    """Orchestrates list pull → diff → items fetch → name resolution."""

    def __init__(self, get_client: Callable,
                 db: Optional[ContractsDB] = None,
                 exclude_ids_provider: Optional[Callable[[], set]] = None):
        """
        get_client: callable returning the shared ESIClient.
        exclude_ids_provider: callable returning a set of excluded contract_ids
            (wired in Step 5; defaults to none excluded).
        """
        self.get_client = get_client
        self.db = db or ContractsDB.singleton()
        self.exclude_ids_provider = exclude_ids_provider

    def _excludes(self) -> set:
        if self.exclude_ids_provider is None:
            return set()
        try:
            return set(self.exclude_ids_provider() or set())
        except Exception:
            _print("exclude provider raised — treating as empty")
            return set()

    # =========================================================================
    # Core async sync
    # =========================================================================

    async def sync_region(self, region_id: int,
                          item_fetch_locations: Optional[set] = None,
                          force: bool = False,
                          progress_cb: Optional[Callable] = None,
                          max_items: Optional[int] = None) -> dict:
        """Sync one region's list + the in-scope contracts' items.

        item_fetch_locations: set of start_location_id (stations) to open items
            for. None = whole region (the deferred expensive path). An empty set
            = refresh the list only, fetch no items.
        force: ignore the freshness gate and always hit ESI (still conditional
            on the ETag, so a 304 is cheap).
        max_items: cap how many item fetches to do this call (the rest stay
            pending for a later call). None = no cap (full crawl). Lets the
            background scheduler drain a huge backlog gradually instead of
            blocking on a 30k pull in one cycle.

        Returns a summary dict for the caller/log.
        """
        client = self.get_client() if self.get_client else None
        if client is None:
            _print(f"sync_region {region_id} aborted — no ESI client")
            return {"ok": False, "reason": "no_client"}

        region_id = int(region_id)
        scope_key = scope_key_for_region(region_id)
        excludes = self._excludes()

        summary = {
            "ok": True, "region_id": region_id, "list_status": None,
            "new": 0, "gone": 0, "items_fetched": 0, "items_gone": 0,
            "names_resolved": 0, "skipped_fresh": False,
        }

        # --- 1. Freshness gate -------------------------------------------------
        fresh = self.db.is_scope_fresh(scope_key)
        pending_items = self._pending_items_count(region_id, item_fetch_locations,
                                                  excludes)
        if fresh and not force and pending_items == 0:
            _print(f"region {region_id} fresh and no pending items — skip ESI")
            summary["skipped_fresh"] = True
            return summary

        # --- 2. List pull (conditional) + diff --------------------------------
        rec = self.db.get_scope_freshness(scope_key)
        etag = rec.get("etag") if rec else None

        if not fresh or force:
            result = await contracts_esi.fetch_region_contract_list(
                client, region_id, etag=etag
            )
            summary["list_status"] = result["status"]

            if result["status"] == 304:
                self.db.bump_scope_expiry(scope_key, result["expires"], 304)
            elif result["status"] == 200:
                contracts = result["contracts"] or []
                fresh_ids = {int(c["contract_id"]) for c in contracts
                             if c.get("contract_id") is not None}
                cached_ids = self.db.get_contract_ids_for_region(region_id)
                new_ids = fresh_ids - cached_ids
                gone_ids = cached_ids - fresh_ids

                self.db.upsert_list_rows(region_id, contracts)
                if gone_ids:
                    self.db.prune_contracts(gone_ids)
                self.db.set_scope_freshness(
                    scope_key, region_id, result["expires"], result["etag"], 200
                )
                summary["new"] = len(new_ids)
                summary["gone"] = len(gone_ids)
                _print(f"region {region_id} diff — {len(new_ids)} new, "
                       f"{len(gone_ids)} gone, {len(fresh_ids)} live")

                await self._resolve_issuer_names(client, contracts, summary)
            else:
                _print(f"region {region_id} list pull failed "
                       f"(status {result['status']}) — keeping stale cache")
        else:
            # Fresh window but we still have pending items below — reuse cache.
            _print(f"region {region_id} fresh; skipping list pull, "
                   f"draining {pending_items} pending item fetches")

        # --- 3. Items fetch for in-scope, non-excluded contracts --------------
        await self._fetch_scope_items(client, region_id, item_fetch_locations,
                                      excludes, summary, progress_cb, max_items)

        _print(f"region {region_id} sync done — {summary}")
        return summary

    def pending_station_count(self, region_id: int, station_id: int) -> int:
        """How many contracts at this station still need their items fetched
        (excluding user-excluded ones). Lets the UI warn before a big crawl."""
        excludes = self._excludes()
        ids = self.db.get_unfetched_contract_ids(int(region_id), int(station_id))
        return len([c for c in ids if c not in excludes])

    def _pending_items_count(self, region_id, item_fetch_locations, excludes) -> int:
        worklist = self._items_worklist(region_id, item_fetch_locations, excludes)
        return len(worklist)

    def _items_worklist(self, region_id, item_fetch_locations, excludes) -> list[int]:
        """Unfetched, in-scope, non-excluded contract_ids to open."""
        if item_fetch_locations is None:
            ids = self.db.get_unfetched_contract_ids(region_id)
        else:
            ids = []
            for loc in item_fetch_locations:
                ids.extend(self.db.get_unfetched_contract_ids(region_id, int(loc)))
        if excludes:
            ids = [c for c in ids if c not in excludes]
        return ids

    async def _fetch_scope_items(self, client, region_id, item_fetch_locations,
                                 excludes, summary, progress_cb,
                                 max_items: Optional[int] = None) -> None:
        worklist = self._items_worklist(region_id, item_fetch_locations, excludes)
        total_pending = len(worklist)
        if max_items is not None and max_items >= 0:
            worklist = worklist[:max_items]
        if not worklist:
            summary["items_remaining"] = total_pending
            return
        _print(f"region {region_id} items worklist — fetching {len(worklist)} "
               f"of {total_pending} pending contracts")

        # Stream results into DB batches so a mid-crawl shutdown only loses the
        # last partial batch (resume-safe). Gone/bad are applied at the end.
        buf = _StoreBuffer(self.db, batch_size=200)
        counters = await contracts_esi.fetch_items_for_contracts(
            client, worklist, on_result=buf.add, progress_cb=progress_cb
        )
        buf.flush()
        if buf.gone:
            self.db.prune_contracts(buf.gone)
        if buf.bad:
            # Hard rejections (400) — flag so they're never retried.
            self.db.mark_items_unavailable(buf.bad)

        summary["items_fetched"] += counters.get("fetched", 0)
        summary["items_gone"] += counters.get("gone", 0)
        summary["items_bad"] = summary.get("items_bad", 0) + counters.get("bad", 0)
        summary["items_remaining"] = max(0, total_pending - len(worklist))

    async def _resolve_issuer_names(self, client, contracts, summary) -> None:
        """Resolve any new issuer / corp ids to names (cached ~permanently)."""
        ids: set[int] = set()
        for c in contracts:
            for key in ("issuer_id", "issuer_corporation_id"):
                v = c.get(key)
                if v:
                    ids.add(int(v))
        unresolved = self.db.get_unresolved_ids(ids)
        if not unresolved:
            return
        mapping = await contracts_esi.resolve_names(client, unresolved)
        if mapping:
            self.db.store_names(mapping)
            summary["names_resolved"] += len(mapping)

    # =========================================================================
    # GUI driver (worker thread → fresh loop → submit result back)
    # =========================================================================

    def run_sync_in_thread(self, region_id: int,
                           item_fetch_locations: Optional[set] = None,
                           force: bool = False,
                           progress_cb: Optional[Callable] = None,
                           done_cb: Optional[Callable] = None) -> None:
        """Fire-and-forget a sync off the Tk thread.

        `progress_cb(done, total)` and `done_cb(summary)` are invoked via
        tk_queue.submit so they run on the Tk thread (safe for widget updates).
        """
        def _worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            summary = {"ok": False, "reason": "unstarted"}
            try:
                wrapped_progress = self._wrap_cb(progress_cb)
                summary = loop.run_until_complete(
                    self.sync_region(region_id, item_fetch_locations,
                                     force=force, progress_cb=wrapped_progress)
                )
            except Exception as e:
                _print(f"run_sync_in_thread region {region_id} crashed: {e}")
                summary = {"ok": False, "reason": str(e)}
            finally:
                loop.close()
            if done_cb is not None:
                self._submit(lambda: done_cb(summary))

        threading.Thread(target=_worker, daemon=True).start()

    @staticmethod
    def _submit(fn) -> None:
        try:
            from core.tk_queue import submit
            submit(fn)
        except Exception:
            # No Tk loop (headless/test) — run inline.
            try:
                fn()
            except Exception:
                pass

    def _wrap_cb(self, cb):
        if cb is None:
            return None

        def _wrapped(done, total):
            self._submit(lambda: cb(done, total))
        return _wrapped

    # =========================================================================
    # Search (reads cache only — manual Search button drives the sync first)
    # =========================================================================

    def search_cached(self, type_id: int, region_id: int,
                      start_location_id: Optional[int] = None) -> list[dict]:
        """Return cached contracts (full list rows) offering `type_id` in scope.

        Pure cache read — the tab calls sync_region first, then this. Excluded
        contracts are filtered out. Issuer names attached from the name cache.
        """
        excludes = self._excludes()
        ids = self.db.find_contracts_with_type(type_id, region_id,
                                               start_location_id)
        ids = [c for c in ids if c not in excludes]
        rows = []
        issuer_ids = set()
        for cid in ids:
            row = self.db.get_list_row(cid)
            if row is None:
                continue
            # Attach the quantity of the searched item in this contract.
            # BPCs don't stack (each record raw_quantity -2), so sum across
            # records; raw_quantity is negative for blueprints, so fall back to
            # `quantity` and finally count records when both are absent.
            matched_qty = 0
            for it in self.db.get_items(cid):
                if int(it.get("type_id") or 0) != int(type_id):
                    continue
                q = it.get("quantity")
                if q is None or q <= 0:
                    q = 1
                matched_qty += int(q)
            row["matched_qty"] = matched_qty
            rows.append(row)
            if row.get("issuer_id"):
                issuer_ids.add(int(row["issuer_id"]))
        names = self.db.get_names(issuer_ids)
        for row in rows:
            row["issuer_name"] = names.get(int(row.get("issuer_id") or 0),
                                           "Unknown")
        _print(f"search type {type_id} region {region_id} "
               f"station {start_location_id} — {len(rows)} cached hits")
        return rows
