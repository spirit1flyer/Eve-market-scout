"""Runtime evidence for ESI activity; never fetches data or changes scheduling.

HTTP responses are evidence of transport activity, not proof that a caller
parsed, validated, or saved a snapshot. Existing ESI-Timing events supply those
separate decisions. Reports explicitly leave unvisited routes unverified.
"""

from collections import Counter
from email.utils import parsedate_to_datetime
import hashlib
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit


REVISION = "esi-evidence-2-policy-batch"
ROUTES = (
    "GET /markets/{id}/orders/", "GET /markets/structures/{id}/",
    "GET /markets/{id}/history/", "GET /markets/prices/",
    "GET /industry/systems/", "GET /characters/{id}/wallet/",
    "GET /characters/{id}/wallet/transactions/",
    "GET /characters/{id}/wallet/journal/", "GET /characters/{id}/orders/",
    "GET /characters/{id}/orders/history/", "GET /characters/{id}/skills/",
    "GET /characters/{id}/standings/", "GET /characters/{id}/blueprints/",
    "GET /universe/structures/{id}/", "GET /universe/systems/{id}/",
    "GET /universe/stargates/{id}/", "GET /universe/stations/{id}/",
    "GET /universe/regions/", "GET /universe/regions/{id}/",
    "GET /universe/constellations/{id}/", "GET /universe/types/{id}/",
    "POST /universe/names/", "POST /universe/ids/",
    "GET /contracts/public/{id}/", "GET /contracts/public/items/{id}/",
    "GET /route/{id}/{id}/", "POST /sso/v2/oauth/token/",
    "GET /sso/v2/oauth/verify/",
)
_lock = threading.RLock()
_output_lock = threading.RLock()
_routes = {}
_decisions = {}
_decision_prints = {}
_pending = {}
_expiry = {}
_sequence = 0
_started_at = time.monotonic()
_started = False
_run = f"{os.getpid()}-{int(time.time())}"


def _emit(message):
    with _output_lock:
        print(f"[ESI-Evidence] run={_run} {message}", flush=True)


def _field(headers, name, default=""):
    return next((str(v) for k, v in headers.items()
                 if k.lower() == name.lower()), default)


def _label(value):
    return re.sub(r"[^A-Za-z0-9_./:{}=-]", "_", str(value))[:100]


def _route(method, url):
    parts = urlsplit(str(url))
    path = re.sub(r"^/(latest|legacy|dev|v\d+)(?=/)", "", parts.path)
    path = re.sub(r"/\d+(?=/|$)", "/{id}", path).rstrip("/") + "/"
    if parts.hostname == "login.eveonline.com":
        # Preserve SSO v2: ESI's old /vN prefix rule does not apply to SSO.
        path = "/sso" + parts.path.rstrip("/") + "/"
    return f"{method.upper()} {path}"


def begin_request(method, url, headers):
    """Track a real outgoing request; store only a hash of its private identity."""
    global _sequence
    route = _route(method, url)
    # Separate character, query/page, and region representations without logging
    # authorization, search text, character IDs, or full request URLs.
    identity = hashlib.sha256(
        (method.upper() + str(url) + _field(headers, "Authorization")).encode()
    ).hexdigest()
    now = time.monotonic()
    with _lock:
        _sequence += 1
        token = _sequence
        row = _routes.setdefault(route, {
            "started": 0, "http": Counter(), "network_errors": 0,
            "early_repeats": 0, "last": None, "last_print": 0.0,
        })
        row["started"] += 1
        prior_expiry = _expiry.get(identity, 0)
        early = max(0.0, prior_expiry - time.time()) if method.upper() == "GET" else 0
        if early:
            row["early_repeats"] += 1
        _pending[token] = (route, now, identity)
        first = row["started"] == 1
        announce_early = early and (row["early_repeats"] == 1
                                   or now - row["last_print"] >= 30)
        if announce_early:
            row["last_print"] = now
    if first or announce_early:
        _emit(f"event=REQUEST route='{route}' "
              f"cache={'BEFORE_PREVIOUS_EXPIRY' if early else 'no_early_repeat_observed'} "
              f"previous_expiry_in={early:.0f}s "
              f"user_agent={'present' if _field(headers, 'User-Agent') else 'absent'} "
              f"compatibility_requested={_label(_field(headers, 'X-Compatibility-Date', 'absent'))}")
    return token


def end_request(token, status=None, headers=None, error=None):
    """Record HTTP headers or a transport failure, never response bodies."""
    headers = headers or {}
    now = time.monotonic()
    expires = None
    try:
        expires = parsedate_to_datetime(_field(headers, "Expires")).timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    with _lock:
        request = _pending.pop(token, None)
        if request is None:
            return
        route, began, identity = request
        row = _routes[route]
        if error is not None:
            announce = row["network_errors"] == 0 or now - row["last_print"] >= 30
            row["network_errors"] += 1
            outcome = "NETWORK_ERROR"
        else:
            announce = row["http"][status] == 0 or now - row["last_print"] >= 30
            row["http"][status] += 1
            outcome = f"HTTP_{status}"
            if expires is not None:
                # Bounded metadata only. Eviction cannot alter the real cache.
                if len(_expiry) >= 20000 and identity not in _expiry:
                    _expiry.pop(next(iter(_expiry)))
                _expiry[identity] = expires
        row["last"] = now
        if announce:
            row["last_print"] = now
    if announce:
        expiry_text = (f"{max(0, expires - time.time()):.0f}s"
                       if expires is not None else "unknown")
        _emit(f"event=RESPONSE route='{route}' result={outcome} "
              f"header_wait={now - began:.2f}s "
              f"expires_in={expiry_text} "
              f"validator={'present' if _field(headers, 'Last-Modified') else 'absent'} "
              f"etag={'present' if _field(headers, 'ETag') else 'absent'} "
              f"compatibility={_label(_field(headers, 'X-Compatibility-Date', 'unknown'))} "
              f"bucket={_label(_field(headers, 'X-Ratelimit-Group', 'unknown'))} "
              f"remaining={_label(_field(headers, 'X-Ratelimit-Remaining', 'unknown'))} "
              f"error_type={type(error).__name__ if error is not None else 'none'}")


def format_refresh_wait(seconds):
    """Readable remaining time for logs/status; does not schedule a request."""
    try:
        remaining = max(0, math.ceil(float(str(seconds).removesuffix("s"))))
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    hours, remainder = divmod(remaining, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def observe_line(line):
    """Summarize existing decision logs using only an allowlist of safe fields."""
    if not line.startswith("[ESI-Timing]"):
        return
    fields = dict(re.findall(r"\b(\w+)=([^\s]+)", line))
    if "resource" not in fields or "decision" not in fields:
        return
    key = tuple(_label(fields.get(name, "unknown"))
                for name in ("resource", "trigger", "slot", "context"))
    decision = _label(fields["decision"])
    detail = " ".join(f"{name}={_label(fields[name])}" for name in
                      ("decision", "reason", "status", "items", "pages", "next_in",
                       "age", "expires", "complete", "source", "next_refresh_in", "context")
                      if name in fields)
    with _lock:
        row = _decisions.setdefault(key, {"counts": Counter(), "detail": ""})
        row["counts"][decision] += 1
        row["detail"] = detail


def decision_event(resource, decision, trigger, **fields):
    """Count provider decisions; print the first and then at most every 30s."""
    if decision == "REUSE":
        fields["next_refresh_in"] = format_refresh_wait(fields.get("next_in"))
    safe = {name: _label(value) for name, value in fields.items()
            if name in ("reason", "status", "items", "pages", "next_in", "slot",
                        "age", "expires", "complete", "source", "next_refresh_in", "context")}
    line = (f"[ESI-Timing] resource={_label(resource)} decision={_label(decision)} "
            f"trigger={_label(trigger)} "
            + " ".join(f"{k}={v}" for k, v in safe.items()))
    observe_line(line)
    key = (resource, trigger, decision, safe.get("reason"), safe.get("slot"), safe.get("context"))
    now = time.monotonic()
    with _lock:
        last = _decision_prints.get(key)
        announce = last is None or now - last >= 30
        if announce:
            _decision_prints[key] = now
    if announce:
        # ESI-Evidence avoids counting this line a second time through TeeStream.
        _emit("event=DECISION " + line.removeprefix("[ESI-Timing] "))


def print_report(trigger="manual", detailed=True):
    """Print session evidence; absent traffic never becomes a passed check."""
    now = time.monotonic()
    with _lock:
        rows = {k: {**v, "http": dict(v["http"])} for k, v in _routes.items()}
        decisions = {k: {**v, "counts": dict(v["counts"])}
                     for k, v in _decisions.items()}
        pending = list(_pending.values())
    http = sum(sum(v["http"].values()) for v in rows.values())
    errors = sum(v["network_errors"] + sum(n for code, n in v["http"].items()
                                          if code >= 400) for v in rows.values())
    repeats = sum(v["early_repeats"] for v in rows.values())
    oldest = max((now - began for _, began, _ in pending), default=0)
    with _output_lock:
        _emit(f"event={'REPORT' if detailed else 'HEARTBEAT'} trigger={trigger} "
              f"uptime={now - _started_at:.0f}s http_responses={http} "
              f"errors={errors} pending_headers={len(pending)} oldest_wait={oldest:.0f}s "
              f"early_repeats={repeats} routes_observed={len(rows)} "
              "compliance=NOT_CERTIFIED")
        if not detailed:
            return
        for route in sorted(set(ROUTES) | set(rows)):
            row = rows.get(route)
            if row is None:
                _emit(f"route='{route}' state=NOT_EXERCISED")
                continue
            codes = ",".join(f"{code}:{n}" for code, n in sorted(row["http"].items())) or "none"
            _emit(f"route='{route}' state=OBSERVED started={row['started']} "
                  f"http={codes} network_errors={row['network_errors']} "
                  f"early_repeats={row['early_repeats']}")
        for (resource, source, slot, context), row in sorted(decisions.items()):
            counts = ",".join(f"{k}:{n}" for k, n in sorted(row["counts"].items()))
            _emit(f"resource={resource} trigger={source} slot={slot} context={context} "
                  f"decisions={counts} latest='{row['detail']}'")
        _emit("event=REPORT_END meaning=HTTP_is_transport_only "
              "snapshot_acceptance_requires_consumer_COMPLETE_or_ACCEPT "
              "early_repeats_are_observations_not_a_compliance_verdict "
              "report_issued_no_network_requests=true")


def start(entry_file, app_version, compatibility_date, log_file):
    """Identify this exact run and emit a quiet, recurring liveness summary."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    source = Path(__file__).resolve()
    try:
        code = source.read_bytes()
    except OSError:
        code = begin_request.__code__.co_code  # Source is absent in frozen builds.
    fingerprint = hashlib.sha256(code).hexdigest()[:12]
    entry = sys.executable if getattr(sys, "frozen", False) else entry_file
    _emit(f"event=START revision={REVISION} code={fingerprint} "
          f"version='{app_version}' compatibility={compatibility_date} "
          f"entry='{Path(entry).resolve()}' log='{log_file}' "
          "heartbeat=30s report_button=ESI_Report")
    _emit("event=LEGEND REQUEST=actual_network_attempt RESPONSE=HTTP_headers_received "
          "REUSE=consumer_used_cache COMPLETE=consumer_finished "
          "NOT_EXERCISED=no_runtime_evidence_yet")

    def heartbeat():
        ticks = 0
        while True:
            time.sleep(30)
            ticks += 1
            # A full startup report appears without needing to find a button.
            print_report(trigger="timer", detailed=ticks == 2 or ticks % 10 == 0)

    threading.Thread(target=heartbeat, name="esi-evidence", daemon=True).start()


def traced_request(method, url, **kwargs):
    """Observe requests without changing its arguments, retries, or exceptions."""
    import requests
    token = begin_request(method, requests.Request(
        method, url, params=kwargs.get("params")).prepare().url,
        kwargs.get("headers") or {})
    try:
        response = requests.request(method, url, **kwargs)
    except BaseException as error:
        end_request(token, error=error)
        raise
    end_request(token, response.status_code, response.headers)
    return response


def attach_trace(trace):
    """Observe aiohttp attempts, including connection errors hidden by callers."""
    async def on_start(_session, context, params):
        context.evidence_token = begin_request(params.method, params.url, params.headers)

    async def on_end(_session, context, params):
        end_request(getattr(context, "evidence_token", None),
                    params.response.status, params.response.headers)

    async def on_error(_session, context, params):
        end_request(getattr(context, "evidence_token", None), error=params.exception)

    trace.on_request_start.append(on_start)
    trace.on_request_end.append(on_end)
    trace.on_request_exception.append(on_error)
