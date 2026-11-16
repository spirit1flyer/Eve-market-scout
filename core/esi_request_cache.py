"""Shared exact public contract-page responses across callers and event loops."""

from copy import deepcopy
from datetime import timedelta
from contextlib import closing
import hashlib
import json
import threading
import sqlite3

from core.config import ESI_COMPATIBILITY_DATE
from core.esi_cache_io import async_guard
from core.esi_private_cache import PrivateWalletCache
from core.esi_public_cache import _expires, _utc
from core.sound_manager import get_data_dir

_singleton = None
_lock = threading.Lock()


class PublicRequestCache(PrivateWalletCache):
    def _key(self, endpoint, params):
        descriptor = [ESI_COMPATIBILITY_DATE, endpoint, params or {}]
        return hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()

    def _restore_reply(self, key):
        if key in self.restored:
            return
        self.restored.add(key)
        try:
            path = self.directory / "esi_private_contract_replies.db"
            if not path.exists():
                return
            with closing(sqlite3.connect(path)) as db:
                saved = db.execute("SELECT payload FROM replies WHERE identity = ?", (key,)).fetchone()
            if saved is None:
                return
            row = json.loads(saved[0])
            if row.get("identity") != key or row.get("schema") != 1:
                raise ValueError("unproven contract reply")
            _utc(row["expires"])
            if _utc(row["acquired_at"]) > self.now():
                raise ValueError("future contract reply")
            self._validate_reply(row["status"], row["body"])
            self.entries[key] = {**row, "durable": True}
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[ContractCache] decision=RESTORE_FAILED error_type={type(exc).__name__}")

    @staticmethod
    def _validate_reply(status, body):
        if type(status) is not int:
            raise ValueError("invalid HTTP status")
        if status == 200 and (not isinstance(body, list) or any(not isinstance(row, dict) for row in body)):
            raise ValueError("full contract page list required")
        json.dumps(body, allow_nan=False)

    def _save_reply(self, key, row):
        try:
            # One transactional store avoids tens of thousands of tiny files
            # during a large contract crawl. Connections close on every path.
            self.directory.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.directory / "esi_private_contract_replies.db")) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=NORMAL")
                with db:
                    db.execute("CREATE TABLE IF NOT EXISTS replies (identity TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL)")
                    if "expires" not in {column[1] for column in db.execute("PRAGMA table_info(replies)")}:
                        db.execute("ALTER TABLE replies ADD COLUMN expires REAL")
                    db.execute("CREATE INDEX IF NOT EXISTS reply_expiry ON replies(expires)")
                    deadline = max(_utc(row["expires"]), _utc(row.get("retry_after", row["expires"])))
                    db.execute("INSERT OR REPLACE INTO replies (identity, payload, expires) VALUES (?, ?, ?)",
                               (key, json.dumps(row, allow_nan=False), deadline.timestamp()))
                    if self.now() >= getattr(self, "_prune_after", self.now()):
                        db.execute("DELETE FROM replies WHERE expires < ?", ((self.now()-timedelta(days=7)).timestamp(),))
                        self._prune_after = self.now() + timedelta(hours=1)
            row["durable"] = True
        except Exception as exc:
            row["durable"] = False
            print(f"[ContractCache] decision=SAVE_FAILED previous_disk_retained=True error_type={type(exc).__name__}")

    async def get_reply(self, endpoint, params, producer, request_etag=None):
        key = self._key(endpoint, params)
        async with async_guard(self._lock(key)):
            self._restore_reply(key)
            previous = self.entries.get(key)
            if previous and self.now() < max(_utc(previous["expires"]), _utc(previous.get("retry_after", previous["expires"]))):
                if not previous.get("durable"):
                    self._save_reply(key, previous)
                if previous.get("failure_status"):
                    return previous["failure_status"], deepcopy(previous["headers"]), None
                if previous["status"] == 304 and previous.get("request_etag") != request_etag:
                    # No full representation exists for that other validator.
                    # Do not issue a pre-expiry request to manufacture one.
                    return 503, deepcopy(previous["headers"]), None
                return previous["status"], deepcopy(previous["headers"]), deepcopy(previous["body"])
            try:
                status, headers, body = await producer()
            except Exception as exc:
                print(f"[ContractCache] decision=FETCH_FAILED previous_snapshot_retained=True error_type={type(exc).__name__}")
                status, headers, body = 502, {}, None
            try:
                self._validate_reply(status, body)
            except (ValueError, TypeError):
                status, body = 502, None
            lowered = {name.lower(): value for name, value in headers.items()}
            now = self.now()
            try:
                expiry = _expires(lowered, now)
            except ValueError:
                expiry = now + timedelta(seconds=3600 if status in (200, 304) else 300 if status == 404 else 30)
            if status == 304 and previous and previous["status"] == 200:
                status, body = 200, previous["body"]
                headers = {**previous["headers"], **headers}
            # A failed refresh preserves the previous full disk/memory reply.
            # Separate retry deadline prevents another caller hammering it.
            if status not in (200, 304, 404):
                if previous:
                    previous["retry_after"] = max(expiry, now + timedelta(seconds=30)).isoformat()
                    previous["failure_status"] = status
                    previous["durable"] = False
                    self._save_reply(key, previous)
                else:
                    failed = {"schema": 1, "identity": key, "status": status, "headers": headers,
                              "body": None, "acquired_at": now.isoformat(),
                              "expires": max(expiry, now + timedelta(seconds=30)).isoformat(), "durable": False}
                    self.entries[key] = failed
                    self._save_reply(key, failed)
                return status, headers, body
            row = {"schema": 1, "identity": key, "status": status,
                   "headers": headers, "body": body, "request_etag": request_etag,
                   "acquired_at": now.isoformat(), "expires": expiry.isoformat(), "durable": False}
            self.entries[key] = row
            self._save_reply(key, row)
            return status, deepcopy(headers), deepcopy(body)


def get_request_cache():
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = PublicRequestCache(get_data_dir())
        return _singleton
