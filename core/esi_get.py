"""ESIClient GET transport and public Industry cache integration."""

import asyncio

from core.config import ESI_BASE_URL
from core.esi_public_cache import PUBLIC_INDUSTRY_ROUTES, get_public_cache


class ESIGetMixin:
    """Uses the client's per-loop session, semaphore and shared rate gate."""

    async def _get_with_headers(self, endpoint: str,
                                params: dict = None) -> tuple[dict | list, dict]:
        """Rate-limited GET returning JSON plus response-header metadata.

        429s get one automatic retry after sleeping for Retry-After;
        other failures raise for the existing callers to handle.
        """
        self._check_halted()
        url = f"{ESI_BASE_URL}{endpoint}"
        await self._await_rate_limit_pause()
        async with self.semaphore:
            for attempt in range(2):
                await self._await_rate_limit_pause()
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
                    return await response.json(), dict(response.headers)
        raise RuntimeError("ESI _get_with_headers fell through retry loop unexpectedly")

    async def _get(self, endpoint: str, params: dict = None) -> dict | list:
        """Rate-limited GET, sharing full public Industry responses."""
        if endpoint in PUBLIC_INDUSTRY_ROUTES and not params:
            snapshot = await self.get_public_industry_snapshot(endpoint)
            return snapshot["payload"]
        data, _headers = await self._get_with_headers(endpoint, params)
        return data

    async def get_public_industry_snapshot(self, endpoint: str) -> dict:
        """Full public response and original acquisition/expiry metadata."""
        return await get_public_cache().get(self, endpoint)
