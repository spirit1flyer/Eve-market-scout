"""Process-wide pacing for ESI legacy and bucket rate limits."""

import asyncio
import re
import threading
import time
from urllib.parse import urlparse

import aiohttp
import requests

from core.config import ERROR_LIMIT_PAUSE_THRESHOLD
from core.esi_diagnostics import attach_trace, traced_request


_lock = threading.Lock()
_pause_until = 0.0
_pause_reason = ""


def _header(headers, name: str):
    value = headers.get(name)
    if value is not None:
        return value
    target = name.lower()
    return next((v for k, v in headers.items() if k.lower() == target), None)


def _safe_endpoint(url_or_endpoint: str) -> str:
    """Strip IDs and query data from a URL before diagnostic logging."""
    path = urlparse(url_or_endpoint).path or url_or_endpoint.split("?", 1)[0]
    return re.sub(r"/\d+(?=/|$)", "/{id}", path)


def _arm_pause(delay: float, reason: str) -> bool:
    """Extend the global pause; return True when a new gate was armed."""
    global _pause_until, _pause_reason
    if delay <= 0:
        return False
    candidate = time.monotonic() + delay
    with _lock:
        newly_armed = _pause_until <= time.monotonic()
        if candidate > _pause_until:
            _pause_until = candidate
            _pause_reason = reason
        return newly_armed


def seconds_until_allowed() -> float:
    with _lock:
        return max(0.0, _pause_until - time.monotonic())


def wait_sync():
    """Block a synchronous ESI caller until the shared gate opens."""
    while True:
        delay = seconds_until_allowed()
        if delay <= 0:
            return
        time.sleep(delay)


async def wait_async():
    """Park an asynchronous ESI caller until the shared gate opens."""
    while True:
        delay = seconds_until_allowed()
        if delay <= 0:
            return
        await asyncio.sleep(delay)


def record_response(headers, status: int, endpoint: str = ""):
    """Update global pacing from either generation of ESI limit headers."""
    safe_endpoint = _safe_endpoint(endpoint or "?")
    retry_raw = _header(headers, "Retry-After")
    if status == 429:
        try:
            delay = max(1.0, float(retry_raw)) + 1.0
        except (TypeError, ValueError):
            delay = 2.0
        if _arm_pause(delay, "429"):
            print(
                f"[ESI-RL] decision=PAUSE reason=429 next_in={delay:.0f}s "
                f"endpoint={safe_endpoint}"
            )
        return

    legacy_remain = _header(headers, "X-ESI-Error-Limit-Remain")
    legacy_reset = _header(headers, "X-ESI-Error-Limit-Reset")
    if legacy_remain is not None and legacy_reset is not None:
        try:
            remain = int(legacy_remain)
            reset = int(legacy_reset)
        except (TypeError, ValueError):
            return
        if remain <= ERROR_LIMIT_PAUSE_THRESHOLD:
            delay = reset + 1.0
            if _arm_pause(delay, "legacy_error_limit"):
                print(
                    "[ESI-RL] decision=PAUSE reason=legacy_error_limit "
                    f"remaining={remain} next_in={delay:.0f}s "
                    f"endpoint={safe_endpoint}"
                )
        return

    group = _header(headers, "X-Ratelimit-Group")
    limit_raw = _header(headers, "X-Ratelimit-Limit")
    remain_raw = _header(headers, "X-Ratelimit-Remaining")
    used_raw = _header(headers, "X-Ratelimit-Used")
    if not group or not limit_raw or remain_raw is None:
        return

    match = re.fullmatch(r"(\d+)/(\d+)([smh])", str(limit_raw).strip())
    if not match:
        return
    total, window, unit = int(match.group(1)), int(match.group(2)), match.group(3)
    window_seconds = window * {"s": 1, "m": 60, "h": 3600}[unit]
    try:
        remaining = int(remain_raw)
        used = max(1, int(used_raw or 1))
    except (TypeError, ValueError):
        return

    threshold = max(10, total // 10)
    if remaining <= threshold:
        delay = max(1.0, window_seconds * used / max(1, total))
        if _arm_pause(delay, f"bucket:{group}"):
            print(
                "[ESI-Timing] resource=rate_limit decision=PACE "
                f"group={group} remaining={remaining}/{total} "
                f"next_in={delay:.0f}s endpoint={safe_endpoint}"
            )


def esi_request(method: str, url: str, **kwargs) -> requests.Response:
    """Send a synchronous ESI request through the shared pacing gate."""
    wait_sync()
    response = traced_request(method, url, **kwargs)
    record_response(response.headers, response.status_code, url)
    return response


def make_trace_config(pacing: bool = True) -> aiohttp.TraceConfig:
    """Observe all requests; optionally pace direct callers outside ESIClient."""
    trace = aiohttp.TraceConfig()

    async def on_request_start(_session, _context, _params):
        await wait_async()

    async def on_request_end(_session, _context, params):
        record_response(
            params.response.headers,
            params.response.status,
            str(params.url),
        )

    if pacing:
        trace.on_request_start.append(on_request_start)
        trace.on_request_end.append(on_request_end)
    attach_trace(trace)
    return trace
