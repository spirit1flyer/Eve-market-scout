"""Durable successful regional pages, independent of whole-book acceptance."""

from datetime import timedelta
import gzip
import json
import threading

from core.config import ESI_COMPATIBILITY_DATE
from core.esi_private_cache import PrivateWalletCache
from core.esi_public_cache import _expires, _utc
from core.esi_route_cache import validate_route_payload
from core.sound_manager import get_data_dir

_singleton = None
_lock = threading.Lock()


class RegionalPageStore(PrivateWalletCache):
    def _page_path(self, region, page):
        return self.directory / f"esi_private_regional_page_{region}_{page}.json.gz"

    def _validate(self, region, page, row):
        if (row.get("schema") != 1 or row.get("region") != region or row.get("page") != page
                or row.get("compatibility") != ESI_COMPATIBILITY_DATE):
            raise ValueError("mismatched regional page")
        validate_route_payload("type_orders", row["payload"])
        if _utc(row["acquired_at"]) > self.now():
            raise ValueError("future regional acquisition")
        _utc(row["expires"])
        count = int(row["headers"].get("x-pages", "1"))
        if not page <= count <= 9999:
            raise ValueError("invalid regional page count")

    def peek_page(self, region, page):
        key = region, page
        with self._lock(key):
            if key not in self.restored:
                self.restored.add(key)
                try:
                    with gzip.open(self._page_path(region, page), "rt", encoding="utf-8") as stream:
                        row = json.load(stream)
                    self._validate(region, page, row)
                    self.entries[key] = {**row, "durable": True}
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    print(f"[RegionalPages] decision=RESTORE_FAILED region={region} page={page} error_type={type(exc).__name__}")
            row = self.entries.get(key)
            if row is None or self.now() >= _utc(row["expires"]):
                return None
            if not row.get("durable"):
                self._persist_page(region, page, row)
            return row

    def _persist_page(self, region, page, row):
        try:
            self._write_atomic(self._page_path(region, page), row)
            row["durable"] = True
        except Exception as exc:
            row["durable"] = False
            print(f"[RegionalPages] decision=SAVE_FAILED region={region} page={page} previous_disk_retained=True error_type={type(exc).__name__}")

    def accept_page(self, region, page, payload, headers):
        lowered = {key.lower(): value for key, value in headers.items()}
        received = self.now()
        try:
            expires = _expires(lowered, received)
        except ValueError:
            expires = received + timedelta(seconds=300)
        row = {"schema": 1, "region": region, "page": page,
               "compatibility": ESI_COMPATIBILITY_DATE, "payload": payload,
               "headers": lowered, "acquired_at": received.isoformat(),
               "expires": expires.isoformat(), "durable": False}
        self._validate(region, page, row)
        with self._lock((region, page)):
            self.entries[region, page] = row
            self.restored.add((region, page))
            self._persist_page(region, page, row)
        return row


def get_regional_pages():
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = RegionalPageStore(get_data_dir())
        return _singleton
