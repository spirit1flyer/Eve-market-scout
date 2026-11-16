"""History access and recent-overlay integration for ESIClient."""

import asyncio
from core.esi_history_cache import merge_history


class HistoryMixin:
    async def get_market_history(self, region_id: int, type_id: int) -> list[dict]:
        """Fetch market history for a specific item in a region."""
        try:
            return await self._get_market_history_raw(region_id, type_id)
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
        from core.api import DEBUG_ESI
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
            for tid in type_ids:
                result[tid] = merge_history(result.get(tid, []),
                    self.recent_history.entries.get((region_id, tid), {}).get('payload', []))
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
        from core.api import DEBUG_ESI
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
                errors.add(tid)
            elif isinstance(history, list):
                if history:
                    success_count += 1
                else:
                    empty_count += 1
                result[tid] = history
            else:
                error_count += 1
                errors.add(tid)

        if DEBUG_ESI:
            print(f"[DIAG] ESI fetch for {len(type_ids)} items: "
                  f"{success_count} success, {empty_count} empty, {error_count} errors")
            if errors_seen:
                print(f"[DIAG]   Error types: {errors_seen}")

        return result, errors

    async def _get_market_history_raw(self, region_id: int, type_id: int) -> list[dict]:
        """Use the same durable provider for direct and bulk history callers."""
        row, _fetched = await self.recent_history.get(self, region_id, type_id)
        cache = self.history_cache.setdefault(region_id, {})
        cache[type_id] = merge_history(cache.get(type_id, []), row['payload'])
        return row['payload']

    async def freshen_history_for_items(self, region_id: int, type_ids: list[int]) -> dict:
        """Return per-item acceptance/durability; failures never advance success."""
        ids = list(dict.fromkeys(type_ids))
        async def one(tid):
            return await self.recent_history.get(self, region_id, tid, min_age_seconds=3600)
        replies = await asyncio.gather(*(one(tid) for tid in ids), return_exceptions=True)
        result = dict(accepted=[], durable=[], failed=[], fetched=[], reused=[])
        cache = self.history_cache.setdefault(region_id, {})
        for tid, reply in zip(ids, replies):
            if isinstance(reply, BaseException):
                result['failed'].append(tid)
                continue
            row, fetched = reply
            cache[tid] = merge_history(cache.get(tid, []), row['payload'])
            result['accepted'].append(tid)
            result['fetched' if fetched else 'reused'].append(tid)
            if row['durable']:
                result['durable'].append(tid)
        result['complete'] = len(result['durable']) == len(ids)
        print(f"[HistoryOverlay] region={region_id} requested={len(ids)} "
              f"accepted={len(result['accepted'])} durable={len(result['durable'])} "
              f"failed={len(result['failed'])} fetched={len(result['fetched'])} "
              f"reused={len(result['reused'])} complete={result['complete']}")
        return result
