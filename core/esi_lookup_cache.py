"""Shared public name/ID resolution with isolated negatives and atomic storage."""

from copy import deepcopy
from datetime import timedelta
import gzip
import json
import threading

from core.config import get_esi_headers, ESI_COMPATIBILITY_DATE
from core.esi_private_cache import BASE_URL, PrivateWalletCache
from core.esi_public_cache import _expires, _utc
from core.sound_manager import get_data_dir

_singleton = None
_singleton_lock = threading.Lock()


class LookupCache(PrivateWalletCache):
    def __init__(self, directory, **kwargs):
        super().__init__(directory, **kwargs)
        self.records = {"names": {}, "ids": {}}
        self.loaded = set()
        self.dirty = set()

    def _file(self, kind):
        return self.directory / f"esi_private_public_{kind}.json.gz"

    def _load(self, kind):
        if kind in self.loaded:
            return
        self.loaded.add(kind)
        try:
            with gzip.open(self._file(kind), "rt", encoding="utf-8") as stream:
                saved = json.load(stream)
            if saved.get("compatibility") != ESI_COMPATIBILITY_DATE:
                return
            for key, row in saved["records"].items():
                _utc(row["expires"])
                _utc(row["acquired_at"])
                if kind == "names" and row["value"] is not None and not isinstance(row["value"], str):
                    raise ValueError("invalid saved name")
                if kind == "ids" and row["value"] is not None and not isinstance(row["value"], dict):
                    raise ValueError("invalid saved ID result")
            self.records[kind] = saved["records"]
        except FileNotFoundError:
            pass
        except Exception as exc:
            print(f"[ESI-Lookup] decision=RESTORE_FAILED resource={kind} error_type={type(exc).__name__}")

    def _save_records(self, kind):
        try:
            self._write_atomic(self._file(kind), {"compatibility": ESI_COMPATIBILITY_DATE,
                                                 "records": self.records[kind]})
            self.dirty.discard(kind)
        except Exception as exc:
            print(f"[ESI-Lookup] decision=SAVE_FAILED resource={kind} previous_disk_retained=True error_type={type(exc).__name__}")

    def _accept(self, kind, key, value, headers, transient=False):
        now = self.now()
        ttl = 30 if transient else 300 if value is None else 7 * 86400
        expiry = now + timedelta(seconds=ttl)
        try:
            expiry = max(expiry, _expires(headers, now))
        except ValueError:
            pass
        self.dirty.add(kind)
        previous = self.records[kind].get(str(key))
        if value is None and previous and previous["value"] is not None:
            previous["retry_after"] = expiry.isoformat()
            return  # Keep known names/IDs and their original acquisition time.
        self.records[kind][str(key)] = {"value": value, "acquired_at": now.isoformat(),
                                        "expires": expiry.isoformat()}

    def _fetch(self, kind, batch):
        response = self.request("POST", f"{BASE_URL}/universe/{kind}/",
                                json=batch, headers=get_esi_headers(), timeout=30)
        status = response.status_code
        if status in (400, 404):
            if len(batch) > 1:
                middle = len(batch) // 2
                self._fetch(kind, batch[:middle])
                self._fetch(kind, batch[middle:])
            else:
                self._accept(kind, batch[0], None, response.headers)
            return
        if status != 200:
            for key in batch:
                self._accept(kind, key, None, response.headers, transient=True)
            print(f"[ESI-Lookup] decision=FAILED resource={kind} status={status} items={len(batch)}")
            return
        data = response.json()
        if kind == "names":
            if not isinstance(data, list):
                raise ValueError("name list required")
            resolved = {}
            for row in data:
                if (not isinstance(row, dict) or type(row.get("id")) is not int
                        or row["id"] not in batch or not isinstance(row.get("name"), str)
                        or not row["name"] or row["id"] in resolved):
                    raise ValueError("invalid name response")
                resolved[row["id"]] = row["name"]
            for key in batch:
                self._accept(kind, key, resolved.get(key), response.headers)
        else:
            if not isinstance(data, dict):
                raise ValueError("ID categories required")
            by_name = {key: {} for key in batch}
            for category, rows in data.items():
                if not isinstance(rows, list):
                    raise ValueError("ID category list required")
                for row in rows:
                    if (not isinstance(row, dict) or type(row.get("id")) is not int
                            or row["id"] <= 0 or not isinstance(row.get("name"), str)):
                        raise ValueError("invalid ID result")
                    key = row["name"].strip().casefold()
                    if key in by_name:
                        by_name[key].setdefault(category, []).append(row)
            for key, value in by_name.items():
                self._accept(kind, key, value or None, response.headers)

    def _resolve(self, kind, keys):
        with self._lock(kind):
            self._load(kind)
            missing = [key for key in keys if str(key) not in self.records[kind]
                       or self.now() >= max(_utc(self.records[kind][str(key)]["expires"]),
                           _utc(self.records[kind][str(key)].get("retry_after", self.records[kind][str(key)]["expires"])))]
            for start in range(0, len(missing), 500):
                batch = missing[start:start + 500]
                try:
                    self._fetch(kind, batch)
                except Exception as exc:
                    for key in batch:
                        if str(key) not in self.records[kind] or self.now() >= _utc(self.records[kind][str(key)]["expires"]):
                            self._accept(kind, key, None, {}, transient=True)
                    print(f"[ESI-Lookup] decision=FAILED resource={kind} error_type={type(exc).__name__}")
            if kind in self.dirty:
                self._save_records(kind)
            return {key: deepcopy(self.records[kind][str(key)]["value"])
                    for key in keys if str(key) in self.records[kind]
                    and self.records[kind][str(key)]["value"] is not None}

    def names(self, ids):
        keys = sorted({int(value) for value in ids if int(value) > 0})
        return self._resolve("names", keys) if keys else {}

    def ids(self, terms):
        keys = sorted({term.strip().casefold() for term in terms if term.strip()})
        return self._resolve("ids", keys) if keys else {}


def get_lookup_cache():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = LookupCache(get_data_dir())
        return _singleton
