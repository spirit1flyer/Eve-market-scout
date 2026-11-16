"""Shared, durable full responses for the two public Industry resources.

Only explicitly listed, parameter-free public routes belong here. Private
resources require authorization-aware keys and are deliberately excluded.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import gzip
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from core.esi_diagnostics import decision_event
from core.sound_manager import get_data_dir


PUBLIC_INDUSTRY_ROUTES = {
    "/markets/prices/": "adjusted_prices",
    "/industry/systems/": "industry_systems",
}
# Preserve the existing consumer cadence; ESI expiry always wins if later.
INDUSTRY_MAX_AGE = 6 * 3600
_singleton = None
_singleton_lock = threading.Lock()


def get_public_cache():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PublicIndustryCache(get_data_dir())
        return _singleton


def _utc(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


def next_refresh_seconds(snapshot, now=None):
    """Time until both the consumer age and ESI expiry permit acquisition."""
    now = now or datetime.now(timezone.utc)
    eligible = max(snapshot["expires"], snapshot["acquired_at"]
                   + timedelta(seconds=INDUSTRY_MAX_AGE))
    return max(0, (eligible - now).total_seconds())


def _expires(headers, now):
    """Prefer absolute Expires; never restart a cached response's max-age."""
    lowered = {key.lower(): value for key, value in headers.items()}
    try:
        expires = parsedate_to_datetime(lowered.get("expires", ""))
        if expires.tzinfo is not None:
            return expires.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)",
                      lowered.get("cache-control", ""), re.I)
    if not match:
        raise ValueError("response has no usable cache expiry")
    age = max(0, int(lowered.get("age", "0")))
    remaining = int(match.group(1)) - age
    expires = now + timedelta(seconds=remaining)
    try:
        date = parsedate_to_datetime(lowered.get("date", ""))
        if date.tzinfo is not None:
            expires = min(expires, date + timedelta(seconds=int(match.group(1))))
    except (TypeError, ValueError, OverflowError):
        pass
    return expires


class PublicIndustryCache:
    """Process-wide ownership, including workers using different event loops."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.entries = {}
        self.locks = {route: threading.Lock() for route in PUBLIC_INDUSTRY_ROUTES}
        for route in PUBLIC_INDUSTRY_ROUTES:
            self._restore(route)

    def _path(self, route):
        return self.directory / f"esi_public_{PUBLIC_INDUSTRY_ROUTES[route]}.json.gz"

    @staticmethod
    def _validate(route, payload):
        if not isinstance(payload, list) or not payload:
            raise ValueError("expected nonempty full-response list")
        # Applies to disk restoration as well as network acceptance.
        json.dumps(payload, allow_nan=False)
        identity = "type_id" if route == "/markets/prices/" else "solar_system_id"
        seen = set()
        for row in payload:
            if not isinstance(row, dict) or type(row.get(identity)) is not int:
                raise ValueError("invalid resource row identity")
            if row[identity] in seen:
                raise ValueError("duplicate resource row identity")
            seen.add(row[identity])
            if identity == "type_id":
                for key in ("adjusted_price", "average_price"):
                    if key in row and type(row[key]) not in (int, float):
                        raise ValueError("invalid price field")
            else:
                indices = row.get("cost_indices")
                if not isinstance(indices, list) or any(
                    not isinstance(ci, dict)
                    or not isinstance(ci.get("activity"), str)
                    or type(ci.get("cost_index")) not in (int, float)
                    for ci in indices
                ):
                    raise ValueError("invalid system cost indices")

    def _event(self, route, action, trigger, entry=None, **fields):
        entry = entry or self.entries.get(route)
        now = datetime.now(timezone.utc)
        if entry:
            fields.update(
                items=len(entry["payload"]), complete=True, pages=1,
                age=f"{max(0, (now - entry['acquired_at']).total_seconds()):.0f}s",
                expires=entry["expires"].isoformat(),
                next_in=f"{next_refresh_seconds(entry, now):.0f}s",
                source=entry["source"],
            )
        decision_event(PUBLIC_INDUSTRY_ROUTES[route], action, trigger, **fields)

    def _restore(self, route):
        path = self._path(route)
        try:
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if (saved.get("schema") != 1 or saved.get("route") != route
                    or saved.get("complete") is not True
                    or saved.get("pages") != 1
                    or saved.get("source") != "esi_public_full"):
                raise ValueError("unproven or mismatched full response")
            self._validate(route, saved["payload"])
            entry = {
                **saved, "acquired_at": _utc(saved["acquired_at"]),
                "expires": _utc(saved["expires"]), "durable": True,
            }
            if entry["acquired_at"] > datetime.now(timezone.utc):
                raise ValueError("snapshot acquisition time is in the future")
            self.entries[route] = entry
            self._event(route, "RESTORE", "startup", reason="disk_complete")
        except FileNotFoundError:
            self._event(route, "MISS", "startup", reason="no_disk_snapshot")
        except Exception as exc:
            self._event(route, "RESTORE_FAILED", "startup", reason=type(exc).__name__)
            print(f"[ESI-PublicCache] {route} restore failed: {exc}")

    def _save(self, route, entry):
        """Replace only after serialization, gzip close and file sync succeed."""
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            payload = {key: value for key, value in entry.items() if key != "durable"}
            for key in ("acquired_at", "expires"):
                payload[key] = entry[key].isoformat()
            with tempfile.NamedTemporaryFile(
                dir=self.directory, prefix=self._path(route).name + ".",
                suffix=".tmp", delete=False,
            ) as raw:
                temporary = Path(raw.name)
                with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                    compressed.write(json.dumps(payload, allow_nan=False).encode("utf-8"))
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, self._path(route))
            entry["durable"] = True
            self._event(route, "PERSIST", "disk", entry, reason="atomic_replace")
        except Exception as exc:
            self._event(route, "SAVE_FAILED", "disk", entry,
                        reason="previous_disk_retained", status=type(exc).__name__)
            print(f"[ESI-PublicCache] {route} save failed; previous disk snapshot retained: {exc}")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"[ESI-PublicCache] temporary cleanup failed: {exc}")

    @asynccontextmanager
    async def _guard(self, route, trigger):
        lock = self.locks[route]
        if lock.locked():
            self._event(route, "WAIT", trigger, reason="same_resource_inflight")
        future = asyncio.get_running_loop().run_in_executor(None, lock.acquire)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            # The executor still owns its pending acquire after cancellation.
            def release_orphan(done):
                if not done.cancelled() and done.exception() is None:
                    lock.release()
            future.add_done_callback(release_orphan)
            raise
        try:
            yield
        finally:
            lock.release()

    async def get(self, client, route, trigger="industry"):
        if route not in PUBLIC_INDUSTRY_ROUTES:
            raise ValueError("route is not a supported public Industry resource")
        async with self._guard(route, trigger):
            entry = self.entries.get(route)
            now = datetime.now(timezone.utc)
            if entry:
                age = (now - entry["acquired_at"]).total_seconds()
                unexpired = now < entry["expires"]
                if unexpired or age < INDUSTRY_MAX_AGE:
                    self._event(route, "REUSE", trigger,
                                reason="esi_unexpired" if unexpired else "consumer_six_hour_age")
                    # Retry persistence without another network acquisition.
                    if not entry["durable"]:
                        self._save(route, entry)
                    return entry
            self._event(route, "FETCH", trigger, reason="missing" if not entry else "eligible")
            try:
                payload, headers = await client._get_with_headers(route)
                self._validate(route, payload)
                lowered = {key.lower(): value for key, value in headers.items()}
                if int(lowered.get("x-pages", "1")) != 1:
                    raise ValueError("unexpected pagination; full response not proven")
                now = datetime.now(timezone.utc)
                accepted = {
                    "schema": 1, "route": route, "payload": payload,
                    "acquired_at": now, "expires": _expires(headers, now),
                    "etag": lowered.get("etag"),
                    "last_modified": lowered.get("last-modified"),
                    "complete": True, "pages": 1,
                    "source": "esi_public_full", "durable": False,
                }
            except Exception as exc:
                self._event(route, "FETCH_FAILED", trigger,
                            reason="previous_snapshot_retained", status=type(exc).__name__)
                raise
            self.entries[route] = accepted
            self._event(route, "ACCEPT", trigger, accepted, reason="validated_full_response", pages=1)
            self._save(route, accepted)
            return accepted
