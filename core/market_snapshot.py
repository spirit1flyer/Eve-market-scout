"""Consistent multi-page regional market snapshot acquisition."""

import asyncio
from datetime import datetime
from typing import Optional

from core.config import ESI_BASE_URL


class ESIPaginationChangedError(RuntimeError):
    """Raised when pages came from different ESI cached representations."""


async def fetch_market_snapshot(
    client, region_id: int
) -> tuple[list[dict], Optional[datetime], int, dict]:
    """Fetch one complete, internally consistent regional order snapshot."""
    url = f"{ESI_BASE_URL}/markets/{region_id}/orders/"
    endpoint = f"/markets/{region_id}/orders/"
    await client._await_rate_limit_pause()

    async with client.semaphore:
        for attempt in range(2):
            await client._await_rate_limit_pause()
            async with client.session.get(url, params={"page": 1}) as response:
                client._record_response_headers(response, endpoint)
                if response.status == 429 and attempt == 0:
                    retry_after = max(
                        1, int(response.headers.get("Retry-After", "1") or "1")
                    )
                    print(
                        f"[ESI-RL] HTTP 429 on {endpoint} - Retry-After "
                        f"{retry_after}s, sleeping then retrying once"
                    )
                    await asyncio.sleep(retry_after)
                    continue
                if response.status == 429:
                    print(
                        f"[ESI-RL] HTTP 429 on {endpoint} AGAIN after "
                        "retry - giving up, raising"
                    )
                response.raise_for_status()
                total_pages = int(response.headers.get("X-Pages", 1))
                expires = client._parse_expires_header(response)
                page_one_validator = response.headers.get("Last-Modified")
                etag = response.headers.get("ETag")
                all_orders = list(await response.json())
                break

    if total_pages > 1:
        page_results, validators, stale_pages = await client._fetch_pages_staged(
            endpoint, list(range(2, total_pages + 1))
        )
        changed_pages = sorted(
            page for page, validator in validators.items()
            if validator != page_one_validator
        )
        if stale_pages or changed_pages:
            details = []
            if stale_pages:
                details.append(f"404_pages={sorted(stale_pages)}")
            if changed_pages:
                details.append(f"validator_pages={changed_pages}")
            raise ESIPaginationChangedError(" ".join(details))

        if not page_one_validator:
            print(
                "[ESI-Timing] resource=regional_orders "
                f"trigger=page_validation region={region_id} "
                "decision=UNVALIDATED reason=no_last_modified "
                f"pages={total_pages}"
            )
        for page in sorted(page_results):
            all_orders.extend(page_results[page])

    return all_orders, expires, total_pages, {
        "etag": etag,
        "last_modified": page_one_validator,
        "complete": True,
        "source": "esi_region_orders",
    }
