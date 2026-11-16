"""Durable exact-resource wallet snapshots, isolated by authorization context.

JWT claims are used only to partition an already authorized app session. This
does not replace the existing SSO verification or validate token signatures.
No credentials, concrete character IDs or private payloads enter diagnostics.
"""

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from core.config import ESI_COMPATIBILITY_DATE
from core.esi_diagnostics import decision_event
from core.esi_public_cache import _expires, _utc
from core.esi_rate_limit import esi_request
from core.sound_manager import get_data_dir
from esi.esi_wallet_models import parse_wallet_resource


BASE_URL = "https://esi.evetech.net/latest"
RESOURCE_PATHS = {
    "balance": "wallet/", "transactions": "wallet/transactions/",
    "journal": "wallet/journal/", "orders": "orders/",
    "order_history": "orders/history/",
}
FALLBACK_SECONDS = {"balance": 60, "transactions": 3600, "journal": 3600,
                    "orders": 1200, "order_history": 3600}
_singleton = None
_singleton_lock = threading.Lock()


class PrivateSnapshotError(RuntimeError):
    """A snapshot could not be safely acquired or applied."""


@dataclass(frozen=True)
class PrivateContext:
    character_id: int
    partition: str
    slot: str


def private_context(auth, slot="seller"):
    """Stable owner/client/scope partition, independent of normal token rotation."""
    if auth is None:
        raise PrivateSnapshotError("not_authenticated")
    char = auth.get_character(slot)
    if not char or not char.character_id or not char.access_token:
        raise PrivateSnapshotError("not_authenticated")
    token = char.access_token
    try:
        encoded = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    except (ValueError, TypeError, IndexError, UnicodeError):
        claims = {}
    if not isinstance(claims, dict):
        claims = {}
    subject = claims.get("sub")
    if subject is not None and subject != f"CHARACTER:EVE:{char.character_id}":
        raise PrivateSnapshotError("character_context_mismatch")
    scopes = claims.get("scp", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or any(not isinstance(scope, str) for scope in scopes):
        raise PrivateSnapshotError("invalid_scope_context")
    # Opaque/older tokens get their own conservative partition. They never
    # inherit a known owner's data; rotation may cause an extra cache miss.
    owner = claims.get("owner")
    if not isinstance(owner, str) or not owner:
        owner = "opaque:" + hashlib.sha256(token.encode()).hexdigest()
    identity = {
        "character": char.character_id, "owner": owner,
        "client": getattr(auth, "client_id", "trading"),
        "authorized_party": claims.get("azp"),
        "scopes": sorted(set(scopes)), "datasource": "tranquility",
        "compatibility": ESI_COMPATIBILITY_DATE,
    }
    partition = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return PrivateContext(char.character_id, partition, slot)


def get_private_cache():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = PrivateWalletCache(get_data_dir())
        return _singleton


class PrivateWalletCache:
    """One full accepted representation per exact resource and auth context."""

    resource_paths = RESOURCE_PATHS
    fallback_seconds = FALLBACK_SECONDS
    single_value_resources = ("balance",)
    paginated_resources = ("journal", "orders", "order_history")

    def _request_url(self, context, resource, query):
        return f"{BASE_URL}/characters/{context.character_id}/{self.resource_paths[resource]}"

    def _request_query(self, params):
        return {"datasource": "tranquility", **(params or {})}

    def _wire_query(self, query):
        return dict(query)

    def _validate_request_payload(self, resource, payload, params):
        self._validate_payload(resource, payload)

    @staticmethod
    def _validate_payload(resource, payload):
        parse_wallet_resource(resource, payload)

    @staticmethod
    def _item_count(resource, payload):
        return 1 if resource == "balance" else len(payload)

    @staticmethod
    def _response_expiry(resource, headers, received):
        return _expires(headers, received)

    def __init__(self, directory, request=esi_request, now=None):
        self.directory = Path(directory)
        self.request = request
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.entries = {}
        self.staged_pages = {}
        self.restored = set()
        self.locks = {}
        self.meta_lock = threading.Lock()

    def _identity(self, context, resource, params):
        if resource not in self.resource_paths:
            raise ValueError("unsupported private resource")
        descriptor = {"partition": context.partition, "resource": resource,
                      "params": params or {}}
        return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()

    def _lock(self, key):
        with self.meta_lock:
            return self.locks.setdefault(key, threading.Lock())

    def _path(self, resource, key):
        return self.directory / f"esi_private_{resource}_{key}.json.gz"

    def _event(self, context, resource, action, trigger, entry=None, **fields):
        if entry is not None:
            now = self.now()
            eligible_at = fields.pop("eligible_at", entry["expires"])
            fields.update(
                age=f"{max(0, (now - entry['acquired_at']).total_seconds()):.0f}s",
                next_in=f"{max(0, (eligible_at - now).total_seconds()):.0f}s",
                expires=entry["expires"].isoformat(), complete=True,
                items=self._item_count(resource, entry["payload"]),
                pages=len(entry["page_metadata"]), source="esi_private_full")
        decision_event(resource, action, trigger, slot=context.slot,
                       context=context.partition[:12], **fields)

    def _restore(self, context, resource, key):
        if key in self.restored:
            return
        self.restored.add(key)
        try:
            with gzip.open(self._path(resource, key), "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if (saved.get("schema") != 1 or saved.get("identity") != key
                    or saved.get("partition") != context.partition
                    or saved.get("resource") != resource
                    or saved.get("complete") is not True
                    or saved.get("source") != "esi_private_full"):
                raise PrivateSnapshotError("unproven_snapshot")
            if self._identity(context, resource, saved.get("params")) != key:
                raise PrivateSnapshotError("mismatched_request")
            self._validate_request_payload(resource, saved["payload"], saved.get("params"))
            json.dumps(saved["payload"], allow_nan=False)
            acquired = _utc(saved["acquired_at"])
            expires = _utc(saved["expires"])
            metadata = saved["page_metadata"]
            if not isinstance(metadata, list) or not metadata:
                raise PrivateSnapshotError("missing_page_metadata")
            if acquired > self.now():
                raise PrivateSnapshotError("future_acquisition")
            for page in metadata:
                _utc(page["expires"])
            if expires != max(_utc(page["expires"]) for page in metadata):
                raise PrivateSnapshotError("inconsistent_expiry_metadata")
            first_page = saved.get("params", {}).get("page", 1)
            if [page["page"] for page in metadata] != list(range(first_page, first_page + len(metadata))):
                raise PrivateSnapshotError("incomplete_page_sequence")
            if any(page["total_pages"] != first_page + len(metadata) - 1 for page in metadata):
                raise PrivateSnapshotError("incomplete_page_count")
            if len(metadata) > 1:
                validators = {page.get("last_modified") for page in metadata}
                if len(validators) != 1 or None in validators or "" in validators:
                    raise PrivateSnapshotError("inconsistent_page_validators")
            entry = {**saved, "acquired_at": acquired, "expires": expires, "durable": True}
            self.entries[key] = entry
            self._event(context, resource, "RESTORE", "startup", entry, reason="disk_complete")
        except FileNotFoundError:
            self._event(context, resource, "MISS", "startup", reason="no_disk_snapshot")
        except Exception as exc:
            self._event(context, resource, "RESTORE_FAILED", "startup",
                        reason="previous_disk_retained", status=type(exc).__name__)

    def peek(self, context, resource, params=None):
        """Restore payload and original timing without making any requests."""
        key = self._identity(context, resource, params)
        with self._lock(key):
            self._restore(context, resource, key)
            return self.entries.get(key)

    def _write_atomic(self, path, saved):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory,
                    prefix=path.name + ".", suffix=".tmp",
                    delete=False) as raw:
                temporary = Path(raw.name)
                with gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
                    compressed.write(json.dumps(saved, allow_nan=False).encode("utf-8"))
                raw.flush()
                os.fsync(raw.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"[ESI-PrivateCache] temporary cleanup failed: {type(exc).__name__}")

    def _save(self, context, resource, key, entry):
        try:
            saved = {key: value for key, value in entry.items() if key != "durable"}
            for field in ("acquired_at", "expires"):
                saved[field] = entry[field].isoformat()
            self._write_atomic(self._path(resource, key), saved)
            entry["durable"] = True
            self._event(context, resource, "PERSIST", "disk", entry, reason="atomic_replace")
        except Exception as exc:
            self._event(context, resource, "SAVE_FAILED", "disk", entry,
                        reason="previous_disk_retained", status=type(exc).__name__)

    def _stage(self, context, resource, key):
        """Successful pages can be reused even when a later page failed."""
        if key in self.staged_pages:
            return self.staged_pages[key]
        pages = {}
        try:
            with gzip.open(self._path(resource + "_pages", key), "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if (saved.get("schema") != 1 or saved.get("identity") != key
                    or saved.get("partition") != context.partition
                    or saved.get("resource") != resource or saved.get("complete") is not False):
                raise PrivateSnapshotError("invalid_page_stage")
            for page, row in saved["pages"].items():
                self._validate_payload(resource, row["payload"])
                json.dumps(row["payload"], allow_nan=False)
                _utc(row["expires"])
                _utc(row["acquired_at"])
                if not isinstance(row["headers"], dict) or int(page) < 1:
                    raise PrivateSnapshotError("invalid_staged_page")
            pages = saved["pages"]
            self._event(context, resource, "PAGE_RESTORE", "startup",
                        reason="partial_stage_not_full_snapshot", pages=len(pages))
        except FileNotFoundError:
            pass
        except Exception as exc:
            self._event(context, resource, "PAGE_RESTORE_FAILED", "startup",
                        reason="stage_unusable", status=type(exc).__name__)
        self.staged_pages[key] = pages
        return pages

    def _page(self, auth, context, resource, key, page, headers, query, retry_server_errors):
        pages = self._stage(context, resource, key)
        previous = pages.get(str(page))
        if previous and self.now() < _utc(previous["expires"]):
            self._validate_request_payload(resource, previous["payload"], query)
            self._event(context, resource, "PAGE_REUSE", "retry",
                        reason="esi_unexpired", pages=1,
                        next_in=f"{(_utc(previous['expires']) - self.now()).total_seconds():.0f}s")
            return previous
        for attempt in range(2 if retry_server_errors else 1):
            self._check_context(auth, context)
            response = self.request("GET",
                self._request_url(context, resource, query),
                headers=headers, params=self._wire_query(query), timeout=30)
            if getattr(response, "status_code", 200) < 500 or attempt == 1:
                break
            # Retain structure discovery's existing one retry on 5xx.
        response.raise_for_status()
        self._check_context(auth, context)
        payload = response.json()
        self._validate_request_payload(resource, payload, query)
        json.dumps(payload, allow_nan=False)
        received = self.now()
        metadata = {name.lower(): value for name, value in response.headers.items()
                    if name.lower() in ("etag", "last-modified", "expires", "cache-control",
                                        "date", "age", "x-pages")}
        try:
            expires = self._response_expiry(resource, metadata, received)
            expiry_source = "esi"
        except ValueError:
            expires = received + timedelta(seconds=self.fallback_seconds[resource])
            expiry_source = "fallback"
        row = {"payload": payload, "headers": metadata, "expires": expires.isoformat(),
               "acquired_at": received.isoformat(), "expiry_source": expiry_source}
        pages[str(page)] = row
        try:
            self._write_atomic(self._path(resource + "_pages", key), {
                "schema": 1, "identity": key, "partition": context.partition,
                "resource": resource, "complete": False, "pages": pages})
        except Exception as exc:
            self._event(context, resource, "PAGE_SAVE_FAILED", "disk",
                        reason="previous_stage_retained", status=type(exc).__name__)
        return row

    @staticmethod
    def _check_context(auth, context):
        if private_context(auth, context.slot) != context:
            raise PrivateSnapshotError("authorization_changed")

    def _acquire(self, auth, context, resource, params, key, retry_server_errors=False):
        headers = auth.get_auth_headers(context.slot)
        if not headers:
            raise PrivateSnapshotError("not_authenticated")
        self._check_context(auth, context)
        paginated = resource in self.paginated_resources
        start = (params or {}).get("page", 1)
        if type(start) is not int or not 1 <= start <= 9999:
            raise PrivateSnapshotError("invalid_start_page")
        query = self._request_query(params)
        payload = []
        metadata = []
        first_validator = None
        total = start
        for page in range(start, 10000):
            self._check_context(auth, context)
            if paginated and (resource != "orders" or page > 1):
                query["page"] = page
            row = self._page(auth, context, resource, key, page, headers, query, retry_server_errors)
            data, response_headers = row["payload"], row["headers"]
            validator = response_headers.get("last-modified")
            reported_pages = int(response_headers.get("x-pages", "1"))
            if not 1 <= reported_pages <= 9999:
                raise PrivateSnapshotError("invalid_page_count")
            if page == start:
                total = reported_pages if paginated else 1
                first_validator = validator
                if resource == "journal":
                    self._event(context, resource, "PAGES", "pagination",
                                reason="will_fetch_all", pages=total - start + 1)
            elif reported_pages != total or validator != first_validator:
                raise PrivateSnapshotError("representation_changed")
            if paginated and (start > total or (total > 1 and not validator)):
                raise PrivateSnapshotError("pagination_not_proven")
            if not paginated and reported_pages != 1:
                raise PrivateSnapshotError("unexpected_pagination")
            self._validate_payload(resource, data)
            metadata.append({"page": page, "total_pages": total, "expires": row["expires"],
                "acquired_at": row["acquired_at"],
                "etag": response_headers.get("etag"), "last_modified": validator,
                "response_expires": response_headers.get("expires"),
                "cache_control": response_headers.get("cache-control"),
                "date": response_headers.get("date"), "age": response_headers.get("age"),
                "expiry_source": row["expiry_source"]})
            if resource in self.single_value_resources:
                payload = data
            else:
                payload.extend(data)
            if not paginated or page >= total:
                break
        self._check_context(auth, context)
        self._validate_request_payload(resource, payload, params)
        json.dumps(payload, allow_nan=False)
        return {"payload": payload, "page_metadata": metadata,
                "acquired_at": self.now(),
                # Wait for every page to expire before reacquiring the full book.
                "expires": max(_utc(page["expires"]) for page in metadata)}

    def get(self, auth, context, resource, params=None, trigger="trade", min_age_seconds=0):
        key = self._identity(context, resource, params)
        lock = self._lock(key)
        if lock.locked():
            self._event(context, resource, "WAIT", trigger, reason="same_resource_inflight")
        with lock:
            self._check_context(auth, context)
            self._restore(context, resource, key)
            entry = self.entries.get(key)
            if entry and (self.now() < entry["expires"] or
                          (self.now() - entry["acquired_at"]).total_seconds() < min_age_seconds):
                reason = "esi_unexpired" if self.now() < entry["expires"] else "consumer_age"
                self._event(context, resource, "REUSE", trigger, entry, reason=reason,
                            eligible_at=max(entry["expires"], entry["acquired_at"] +
                                            timedelta(seconds=min_age_seconds)))
                if not entry["durable"]:
                    self._save(context, resource, key, entry)
                return entry
            self._event(context, resource, "FETCH", trigger, entry,
                        reason="missing" if entry is None else "eligible")
            try:
                acquired = self._acquire(auth, context, resource, params, key,
                                         retry_server_errors=trigger == "structure_discovery")
            except Exception as exc:
                reason = (f"{exc}_previous_snapshot_retained"
                          if isinstance(exc, PrivateSnapshotError) else "previous_snapshot_retained")
                self._event(context, resource, "FETCH_FAILED", trigger, entry,
                            reason=reason, status=type(exc).__name__)
                raise
            accepted = {**acquired, "schema": 1, "identity": key,
                        "partition": context.partition, "resource": resource,
                        "params": params or {}, "complete": True,
                        "source": "esi_private_full", "durable": False}
            self.entries[key] = accepted
            self._event(context, resource, "ACCEPT", trigger, accepted,
                        reason="validated_full_response",
                        status="fallback_expiry" if any(page["expiry_source"] == "fallback"
                            for page in accepted["page_metadata"]) else "esi_expiry")
            self._save(context, resource, key, accepted)
            return accepted
