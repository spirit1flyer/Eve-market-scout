"""Type name lookup and item search mixin for ESIClient."""

import asyncio
import aiohttp
from core.config import ESI_BASE_URL

DEBUG_ESI = False

_sde_manager = None


def _get_sde():
    """Get SDE manager instance (lazy load)."""
    global _sde_manager
    if _sde_manager is None:
        try:
            from sde.sde_manager import get_sde_manager
            _sde_manager = get_sde_manager()
        except ImportError:
            _sde_manager = False
    return _sde_manager if _sde_manager else None


class TypeNameMixin:
    """Type name lookup and item search for ESIClient.

    Expects self.session, self.semaphore, self.type_name_cache from ESIClient.
    """

    async def get_type_name(self, type_id: int) -> str:
        """Get item name from type ID, with SDE lookup and ESI fallback."""
        if type_id in self.type_name_cache:
            return self.type_name_cache[type_id]

        sde = _get_sde()
        if sde and sde.is_available():
            name = sde.get_type_name(type_id)
            if name:
                self.type_name_cache[type_id] = name
                return name

        try:
            data = await self._get(f"/universe/types/{type_id}/")
            name = data.get("name", f"Unknown ({type_id})")
            self.type_name_cache[type_id] = name
            return name
        except Exception:
            return f"Unknown ({type_id})"

    async def get_type_names_bulk(self, type_ids: list[int]) -> dict[int, str]:
        """Fetch multiple type names with SDE lookup and ESI fallback."""
        result = {}
        uncached = []

        for tid in type_ids:
            if tid in self.type_name_cache:
                result[tid] = self.type_name_cache[tid]
            else:
                uncached.append(tid)

        if not uncached:
            return result

        sde = _get_sde()
        if sde and sde.is_available():
            sde_names = sde.get_type_names_bulk(uncached)
            for tid, name in sde_names.items():
                result[tid] = name
                self.type_name_cache[tid] = name
            uncached = [tid for tid in uncached if tid not in sde_names]

        if uncached:
            await self._fetch_type_names_from_esi(uncached, result)

        for tid in type_ids:
            if tid not in result:
                result[tid] = f"Unknown ({tid})"

        return result

    async def _fetch_type_names_from_esi(self, type_ids: list[int], result: dict[int, str]):
        """Fetch type names from ESI in batches, updating result dict in place."""
        BATCH_SIZE = 500

        for i in range(0, len(type_ids), BATCH_SIZE):
            batch = type_ids[i:i + BATCH_SIZE]
            try:
                async with self.semaphore:
                    url = f"{ESI_BASE_URL}/universe/names/"
                    async with self.session.post(url, json=batch) as response:
                        if response.status == 200:
                            data = await response.json()
                            for item in data:
                                result[item["id"]] = item["name"]
                                self.type_name_cache[item["id"]] = item["name"]
                        else:
                            await self._fetch_type_names_individual(batch, result)
            except Exception:
                await self._fetch_type_names_individual(batch, result)

    async def _fetch_type_names_individual(self, type_ids: list[int], result: dict[int, str]):
        """Fetch type names individually (fallback for batch failures)."""
        tasks = [self.get_type_name(tid) for tid in type_ids]
        names = await asyncio.gather(*tasks, return_exceptions=True)
        for tid, name in zip(type_ids, names):
            if isinstance(name, str):
                result[tid] = name

    async def search_item_by_name(self, search_term: str) -> list[dict]:
        """Search for items by name via ESI universe/ids, then local cache for partials.

        Returns list of {type_id, name} dicts, up to 20 results.
        """
        results = []

        try:
            async with self.semaphore:
                url = f"{ESI_BASE_URL}/universe/ids/"
                async with self.session.post(url, json=[search_term]) as response:
                    if response.status == 200:
                        data = await response.json()
                        for item in data.get("inventory_types", []):
                            results.append({"type_id": item["id"], "name": item["name"]})
                            self.type_name_cache[item["id"]] = item["name"]
        except Exception as e:
            if DEBUG_ESI:
                print(f"[ESI] universe/ids error: {e}")

        search_lower = search_term.lower()
        for type_id, name in self.type_name_cache.items():
            if search_lower in name.lower():
                if not any(r["type_id"] == type_id for r in results):
                    results.append({"type_id": type_id, "name": name})

        def sort_key(item):
            name_lower = item["name"].lower()
            if name_lower == search_lower:
                return (0, name_lower)
            elif name_lower.startswith(search_lower):
                return (1, name_lower)
            else:
                return (2, name_lower)

        results.sort(key=sort_key)
        return results[:20]

    def search_cached_items(self, search_term: str) -> list[dict]:
        """Search local type_name_cache for items (instant, no API call).

        Returns list of {type_id, name} dicts, up to 20 results.
        """
        search_lower = search_term.lower()
        results = [
            {"type_id": type_id, "name": name}
            for type_id, name in self.type_name_cache.items()
            if search_lower in name.lower()
        ]
        results.sort(key=lambda x: x["name"])
        return results[:20]
