"""Authorization-scoped blueprint snapshots and serialized database application."""

import threading
import time

from core.esi_private_cache import PrivateWalletCache
from core.esi_public_cache import _utc
from core.sound_manager import get_data_dir

_singleton = None
_lock = threading.Lock()
_apply_locks = {}


def validate_blueprints(resource, rows):
    if not isinstance(rows, list):
        raise ValueError("blueprint list required")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("blueprint object required")
        for field in ("item_id", "type_id", "location_id"):
            if type(row[field]) is not int or row[field] <= 0:
                raise ValueError("invalid blueprint identity")
        if row["item_id"] in seen:
            raise ValueError("duplicate blueprint item")
        seen.add(row["item_id"])
        if not isinstance(row["location_flag"], str) or not row["location_flag"]:
            raise ValueError("invalid blueprint location flag")
        for field, minimum, maximum in (("material_efficiency", 0, 10),
                                         ("time_efficiency", 0, 20),
                                         ("runs", -1, None), ("quantity", -2, None)):
            value = row[field]
            if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
                raise ValueError("invalid blueprint field")
        if row["quantity"] == 0 or (row["quantity"] == -2 and row["runs"] < 0):
            raise ValueError("invalid blueprint copy quantity/runs")


class PrivateBlueprintCache(PrivateWalletCache):
    resource_paths = {"blueprints": "blueprints/"}
    fallback_seconds = {"blueprints": 3600}
    single_value_resources = ()
    paginated_resources = ("blueprints",)
    _validate_payload = staticmethod(validate_blueprints)

    def _page(self, auth, context, resource, key, page, headers, query, retry_server_errors):
        staged = self._stage(context, resource, key).get(str(page))
        if page > 1 and (not staged or self.now() >= _utc(staged["expires"])):
            time.sleep(0.3)  # Preserve the existing slow-page safety.
        return super()._page(auth, context, resource, key, page, headers, query, retry_server_errors)


def get_blueprint_cache():
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = PrivateBlueprintCache(get_data_dir())
        return _singleton


def blueprint_apply_lock(db, character_id):
    key = str(db.db_path.resolve()), int(character_id)
    with _lock:
        return _apply_locks.setdefault(key, threading.Lock())
