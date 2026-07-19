"""ESI client layer for public contracts (Step 2).

Why this lives apart from api.ESIClient._get: the freshness design depends on
**conditional requests** (`If-None-Match` → a 304 just bumps the stored expiry,
a 200 reprocesses). `_get` raises on non-2xx, hides the response headers, and
takes no custom request headers — so it can't carry an ETag in or read one out.
Rather than reshape `_get` and risk every existing caller (the user has been
burned by backend rewrites), this module adds a SEPARATE fetch path that REUSES
the shared client's machinery — its per-loop `session`, `semaphore`, error-budget
pause (`_await_rate_limit_pause` / `_record_response_headers`) and Expires parser
— but owns its own header + status handling.

Everything is async and expects to run inside the live app's event loop, where
DNS is warm and ESI calls actually succeed (fresh python.exe invocations on the
user's machine hit EDR/DNS issues — so this is driven from the app, never a CLI
harness).

ESI realities handled here:
  - List endpoint `/contracts/public/{region_id}/` is paginated and carries NO
    item contents. We treat page 1's ETag + Expires as the scope marker: ESI
    regenerates all pages on one cache cycle, so a 304 on page 1 means the whole
    region list is unchanged and we can skip the rest.
  - Items endpoint `/contracts/public/items/{contract_id}/` is per-contract and
    immutable. 404 = contract no longer public (treat as gone).
  - 520 "ConStopSpamming" = ESI's contract-search rate clamp. We back off
    exponentially with jitter and retry, so a region crawl throttles itself
    rather than hammering into a ban.
  - Name resolution is `POST /universe/names/` (batched ≤1000).

All diagnostics carry the greppable `[ContractDiag]` tag.
"""

import asyncio
import random
from typing import Optional

import aiohttp

from core.config import ESI_BASE_URL

# Budget for logging 400-with-body lines before suppressing (per process).
_400_log_budget = 5

# 520 ConStopSpamming backoff schedule (seconds, before jitter). ESI's contract
# endpoints are the ones that throw 520 when you crawl too hard; the schedule is
# deliberately generous because a region backfill is a one-time cost and getting
# clamped (or banned) is far worse than being slow.
_520_BACKOFF = [2, 5, 10, 30, 60]
_MAX_NAMES_PER_CALL = 1000

# Per-contract chatter (one line per items fetch) floods the log on a big crawl
# — tens of thousands of lines that can stall a captured stdout (VS debug
# console) and starve the UI. Off by default; the every-N progress line and the
# end-of-fetch counters summary give enough visibility. Flip to True to debug a
# specific contract's fetch.
VERBOSE = False


def _print(msg: str) -> None:
    """Tagged stdout so the flow is visible in eve_scout.log (the user's CLI
    harnesses fail on his machine — debug must surface in the live app)."""
    print(f"[ContractDiag] {msg}")


def _vprint(msg: str) -> None:
    """Per-item chatter, only when VERBOSE (avoids 30k-line log floods)."""
    if VERBOSE:
        print(f"[ContractDiag] {msg}")


async def _raw_request(client, method: str, endpoint: str,
                       params: Optional[dict] = None,
                       json_body=None,
                       extra_headers: Optional[dict] = None
                       ) -> tuple[int, dict, object]:
    """One rate-limited request that EXPOSES status + headers (unlike _get).

    Returns (status, headers, parsed_json_or_None). Honors the shared client's
    error-budget pause and 429 Retry-After, and retries 520 ConStopSpamming on
    its own backoff schedule. Non-retryable non-2xx (e.g. 404) return their
    status with body None — callers decide what that means.
    """
    url = f"{ESI_BASE_URL}{endpoint}"
    session = client.ensure_session()
    headers = dict(extra_headers or {})

    attempt_520 = 0
    while True:
        await client._await_rate_limit_pause()
        async with client.semaphore:
            async with session.request(
                method, url, params=params, json=json_body, headers=headers
            ) as response:
                client._record_response_headers(response, endpoint)
                status = response.status

                # 304 — caller's conditional matched; no body.
                if status == 304:
                    return status, dict(response.headers), None

                # 429 — respect Retry-After once via the shared pause logic.
                if status == 429:
                    retry_after = max(
                        1, int(response.headers.get("Retry-After", "1") or "1")
                    )
                    _print(f"HTTP 429 on {endpoint} — sleeping {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue

                # 520 ConStopSpamming — back off and retry on our own schedule.
                if status == 520:
                    if attempt_520 >= len(_520_BACKOFF):
                        _print(f"520 ConStopSpamming on {endpoint} — backoff "
                               f"exhausted ({attempt_520} tries), giving up")
                        return status, dict(response.headers), None
                    base = _520_BACKOFF[attempt_520]
                    delay = base + random.uniform(0, base * 0.5)
                    attempt_520 += 1
                    _print(f"520 ConStopSpamming on {endpoint} — backoff "
                           f"{delay:.1f}s (try {attempt_520}/{len(_520_BACKOFF)})")
                    await asyncio.sleep(delay)
                    continue

                if status >= 400:
                    # Surface but don't raise — callers branch on status.
                    # Capture a short body snippet on the FIRST few 400s to
                    # diagnose why ESI rejects certain contracts' items, then go
                    # quiet so a crawl full of 400s can't flood the log.
                    global _400_log_budget
                    if status == 400 and _400_log_budget > 0:
                        _400_log_budget -= 1
                        try:
                            snippet = (await response.text())[:160]
                        except Exception:
                            snippet = ""
                        _print(f"HTTP 400 on {endpoint}"
                               + (f" — {snippet}" if snippet else "")
                               + (f"  [further 400s suppressed]"
                                  if _400_log_budget == 0 else ""))
                    elif status != 400:
                        _vprint(f"HTTP {status} on {endpoint}")
                    return status, dict(response.headers), None

                try:
                    body = await response.json()
                except Exception:
                    body = None
                return status, dict(response.headers), body


# =============================================================================
# Contract list (paginated, conditional via page-1 ETag)
# =============================================================================

async def fetch_region_contract_list(client, region_id: int,
                                     etag: Optional[str] = None) -> dict:
    """Pull a region's public contract list, conditional on `etag`.

    Returns a dict:
      {status, expires, etag, contracts}
    where:
      - status 304  → list unchanged; `contracts` is None, `etag` echoes back
        the one we sent, `expires` is the fresh cache window to store.
      - status 200  → `contracts` is the full multi-page list, `etag`/`expires`
        are page 1's (the scope marker).
      - status >=400 → `contracts` is None; caller should keep stale cache.

    NOTE: list rows carry NO item contents (contract_id/price/location/dates/
    issuer/title/type only). Contents are a separate per-contract fetch.
    """
    endpoint = f"/contracts/public/{int(region_id)}/"
    extra = {"If-None-Match": etag} if etag else None

    status, headers, body = await _raw_request(
        client, "GET", endpoint, params={"page": 1}, extra_headers=extra
    )
    # ESIClient._parse_expires_header takes a live response object; we only kept
    # the header dict, so recompute the absolute expiry from the header string.
    expires = _expires_from_headers(headers)
    new_etag = headers.get("ETag") or headers.get("Etag")

    if status == 304:
        _print(f"region {region_id} list 304 (unchanged) — expires={expires}")
        return {"status": 304, "expires": expires, "etag": etag, "contracts": None}

    if status != 200 or body is None:
        _print(f"region {region_id} list fetch failed status={status}")
        return {"status": status, "expires": expires, "etag": new_etag,
                "contracts": None}

    contracts = list(body)
    try:
        total_pages = int(headers.get("X-Pages", 1))
    except (TypeError, ValueError):
        total_pages = 1
    _print(f"region {region_id} list 200 — page 1/{total_pages}, "
           f"{len(contracts)} rows, etag={new_etag}")

    if total_pages > 1:
        tasks = [
            _fetch_list_page(client, endpoint, p)
            for p in range(2, total_pages + 1)
        ]
        results = await asyncio.gather(*tasks)
        for page_rows in results:
            if page_rows:
                contracts.extend(page_rows)

    _print(f"region {region_id} list assembled — {len(contracts)} total rows "
           f"across {total_pages} pages")
    return {"status": 200, "expires": expires, "etag": new_etag,
            "contracts": contracts}


async def _fetch_list_page(client, endpoint: str, page: int) -> Optional[list]:
    """Fetch one extra list page; None on failure (logged, non-fatal)."""
    status, _headers, body = await _raw_request(
        client, "GET", endpoint, params={"page": page}
    )
    if status == 200 and isinstance(body, list):
        return body
    _print(f"list page {page} of {endpoint} failed status={status}")
    return None


# =============================================================================
# Contract items (per-contract, immutable)
# =============================================================================

async def fetch_contract_items(client, contract_id: int) -> dict:
    """Fetch one contract's contents.

    Returns {status, items, gone}:
      - status 200 → `items` is the contents list (may be empty for courier).
      - status 404 → `gone` True; contract no longer public (prune it).
      - other      → `items` None; leave it unfetched to retry next pass.
    """
    cid = int(contract_id)
    endpoint = f"/contracts/public/items/{cid}/"

    status, headers, body = await _raw_request(
        client, "GET", endpoint, params={"page": 1}
    )

    if status == 404:
        _vprint(f"contract {cid} items 404 — gone")
        return {"status": 404, "items": None, "gone": True}

    if status != 200 or body is None:
        _vprint(f"contract {cid} items fetch failed status={status}")
        return {"status": status, "items": None, "gone": False}

    items = list(body)
    try:
        total_pages = int(headers.get("X-Pages", 1))
    except (TypeError, ValueError):
        total_pages = 1
    if total_pages > 1:
        tasks = [
            _fetch_items_page(client, endpoint, p)
            for p in range(2, total_pages + 1)
        ]
        for page_items in await asyncio.gather(*tasks):
            if page_items:
                items.extend(page_items)

    _vprint(f"contract {cid} items 200 — {len(items)} records")
    return {"status": 200, "items": items, "gone": False}


async def _fetch_items_page(client, endpoint: str, page: int) -> Optional[list]:
    status, _headers, body = await _raw_request(
        client, "GET", endpoint, params={"page": page}
    )
    if status == 200 and isinstance(body, list):
        return body
    return None


async def fetch_items_for_contracts(client, contract_ids: list[int],
                                    concurrency: int = 8,
                                    on_result=None,
                                    progress_cb=None) -> dict:
    """Fetch contents for many contracts with a FIXED-SIZE worker pool.

    A big crawl (Jita IV-4 alone is ~30k contracts) must not (a) spawn 30k
    coroutines at once, (b) hold every result in memory, or (c) log/flush
    per item. So:
      - A bounded set of `concurrency` workers drains a queue. The cap is below
        the client's global semaphore to leave headroom for the rest of the app
        and to stay clear of 520 ConStopSpamming.
      - Results are STREAMED to `on_result(contract_id, result)` as they land
        (the engine persists them in DB batches → resume-safe), so we never hold
        the whole crawl in memory.
      - `progress_cb(done, total)` and the progress log fire only every N items.

    Returns a counters dict: {total, done, fetched, gone, bad}.
    """
    ids = [int(c) for c in contract_ids]
    total = len(ids)
    counters = {"total": total, "done": 0, "fetched": 0, "gone": 0, "bad": 0}
    if not ids:
        return counters

    queue: asyncio.Queue = asyncio.Queue()
    for c in ids:
        queue.put_nowait(c)

    lock = asyncio.Lock()
    # Throttle progress to ~1% (min every 25, so small crawls still tick).
    progress_every = max(25, total // 100)

    _print(f"fetching items for {total} contracts "
           f"(pool={min(concurrency, total)})")

    async def _worker():
        while True:
            try:
                cid = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            res = await fetch_contract_items(client, cid)
            if on_result is not None:
                try:
                    on_result(cid, res)
                except Exception:
                    pass
            async with lock:
                counters["done"] += 1
                if res.get("gone"):
                    counters["gone"] += 1
                elif res.get("status") == 200:
                    counters["fetched"] += 1
                else:
                    counters["bad"] += 1
                done = counters["done"]
            if progress_cb is not None and (done % progress_every == 0
                                            or done == total):
                try:
                    progress_cb(done, total)
                except Exception:
                    pass
            if done % progress_every == 0 or done == total:
                _print(f"items progress {done}/{total} "
                       f"(ok={counters['fetched']} gone={counters['gone']} "
                       f"bad={counters['bad']})")

    workers = [asyncio.create_task(_worker())
               for _ in range(min(max(1, concurrency), total))]
    await asyncio.gather(*workers)
    _print(f"items fetch complete — {counters}")
    return counters


# =============================================================================
# Name resolution (issuers / corporations)
# =============================================================================

async def resolve_names(client, ids) -> dict[int, str]:
    """Resolve ids → names via POST /universe/names/ (batched ≤1000).

    Used for issuer_id / issuer_corporation_id. Caller is expected to have
    already filtered to ids missing from the cache. Item names come from the
    local SDE, NOT this endpoint.
    """
    ids = list({int(x) for x in ids if x})
    if not ids:
        return {}

    out: dict[int, str] = {}
    for start in range(0, len(ids), _MAX_NAMES_PER_CALL):
        batch = ids[start:start + _MAX_NAMES_PER_CALL]
        status, _headers, body = await _raw_request(
            client, "POST", "/universe/names/", json_body=batch
        )
        if status != 200 or not isinstance(body, list):
            _print(f"name resolve failed for {len(batch)} ids status={status}")
            continue
        for entry in body:
            try:
                out[int(entry["id"])] = entry["name"]
            except (KeyError, TypeError, ValueError):
                continue
    _print(f"resolved {len(out)}/{len(ids)} names")
    return out


# =============================================================================
# Helpers
# =============================================================================

def _expires_from_headers(headers: dict) -> Optional[str]:
    """Absolute ISO expiry from Cache-Control max-age / Expires header.

    Mirrors ESIClient._parse_expires_header but works off a plain header dict
    (we don't keep the response object alive) and returns an ISO string for the
    freshness store (which persists absolute timestamps, not countdowns).
    """
    import re
    from datetime import datetime, timezone, timedelta

    cache_control = headers.get("Cache-Control", "") or ""
    m = re.search(r"max-age=(\d+)", cache_control)
    if m:
        secs = int(m.group(1))
        return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()

    expires_str = headers.get("Expires")
    if not expires_str:
        return None
    try:
        dt = datetime.strptime(expires_str, "%a, %d %b %Y %H:%M:%S %Z")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None
