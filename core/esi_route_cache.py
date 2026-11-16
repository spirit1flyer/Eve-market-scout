"""Exact structure/public route snapshots using the shared durable transport.

Private books and metadata never enter the unscoped regional cache. Public
type-filtered orders remain distinct from complete regional order books.
"""

from copy import deepcopy
import math
import threading
import gzip
import json
from datetime import timedelta
import requests

from core.config import ESI_COMPATIBILITY_DATE, get_esi_headers
from core.esi_private_cache import (
    BASE_URL, PrivateContext, PrivateSnapshotError, PrivateWalletCache, private_context,
)
from core.sound_manager import get_data_dir
from core.esi_public_cache import _expires, _utc


PUBLIC_CONTEXT = PrivateContext(0, "public-routes-" + ESI_COMPATIBILITY_DATE, "public")
_singleton = None
_lock = threading.Lock()


class _PublicAuth:
    def get_auth_headers(self, slot):
        return get_esi_headers()


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError("positive integer required")


def validate_route_payload(resource, payload):
    if resource == "structure_search":
        if not isinstance(payload, dict) or not isinstance(payload.get("structure", []), list):
            raise ValueError("structure search result required")
        for value in payload.get("structure", []):
            _positive(value)
        return
    if resource in ("region_ids", "route"):
        if not isinstance(payload, list) or not payload:
            raise ValueError("nonempty ID list required")
        for value in payload:
            _positive(value)
        return
    if resource in ("structure_orders", "type_orders"):
        if not isinstance(payload, list):
            raise ValueError("order list required")
        seen = set()
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("order object required")
            for field in ("order_id", "type_id", "location_id", "volume_total", "duration"):
                _positive(row[field])
            if type(row["volume_remain"]) is not int or not 0 <= row["volume_remain"] <= row["volume_total"]:
                raise ValueError("invalid remaining volume")
            if (type(row["is_buy_order"]) is not bool or type(row["price"]) not in (int, float)
                    or not math.isfinite(row["price"]) or row["price"] < 0):
                raise ValueError("invalid order side/price")
            if row["order_id"] in seen:
                raise ValueError("duplicate order identity")
            seen.add(row["order_id"])
            _utc(row["issued"])
    else:
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str) or not payload["name"]:
            raise ValueError("named metadata object required")
        fields = {"structure_info": ("solar_system_id", "type_id"),
                  "station_info": ("system_id", "type_id", "station_id"),
                  "system_info": ("constellation_id",),
                  "constellation_info": ("region_id",),
                  "region_info": (), "type_info": (), "stargate_info": ("system_id",)}[resource]
        for field in fields:
            _positive(payload[field])
        for field in ("owner", "owner_id"):
            if field in payload:
                _positive(payload[field])
        if resource == "stargate_info":
            _positive(payload["destination"]["system_id"])


class RouteSnapshotCache(PrivateWalletCache):
    resource_paths = {
        "structure_orders": "markets/structures/{resource_id}/",
        "structure_info": "universe/structures/{resource_id}/",
        "structure_search": "characters/{resource_id}/search/",
        "type_orders": "markets/{resource_id}/orders/",
        "station_info": "universe/stations/{resource_id}/",
        "system_info": "universe/systems/{resource_id}/",
        "constellation_info": "universe/constellations/{resource_id}/",
        "region_ids": "universe/regions/", "region_info": "universe/regions/{resource_id}/",
        "type_info": "universe/types/{resource_id}/",
        "stargate_info": "universe/stargates/{resource_id}/",
        "route": "route/{resource_id}/{destination}/",
    }
    fallback_seconds = {"structure_orders": 300, "structure_info": 3600,
                        "type_orders": 300, "station_info": 3600,
                        "system_info": 3600, "constellation_info": 3600}
    fallback_seconds.update({"region_ids": 86400, "region_info": 86400,
                             "type_info": 86400, "stargate_info": 86400, "route": 86400,
                             "structure_search": 300})
    single_value_resources = ("structure_info", "station_info", "system_info", "constellation_info",
                              "region_ids", "region_info", "type_info", "stargate_info", "route", "structure_search")
    paginated_resources = ("structure_orders", "type_orders")
    _validate_payload = staticmethod(validate_route_payload)

    def __init__(self, directory, **kwargs):
        super().__init__(directory, **kwargs)
        self.public_negatives = {}

    def get(self, auth, context, resource, params=None, trigger="route", min_age_seconds=0):
        # A missing page does not establish that a whole order book is absent.
        # Keep successful staged pages and allow the failed page to recover.
        if context != PUBLIC_CONTEXT or resource not in self.single_value_resources:
            return super().get(auth, context, resource, params, trigger, min_age_seconds)
        key = self._identity(context, resource, params)
        # A separate lock protects negative metadata/route responses as well as
        # the inherited positive snapshot. No private authorization is cached.
        with self._lock(("public-negative", key)):
            path = self._path(resource + "_negative", key)
            try:
                negative = self.public_negatives.get(key)
                if negative is None:
                    with gzip.open(path, "rt", encoding="utf-8") as stream:
                        negative = json.load(stream)
                    self.public_negatives[key] = negative
                if negative.get("identity") == key and self.now() < _utc(negative["expires"]):
                    raise PrivateSnapshotError("cached_public_not_found")
            except (FileNotFoundError, ValueError, KeyError, OSError):
                pass
            try:
                return super().get(auth, context, resource, params, trigger, min_age_seconds)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    received = self.now()
                    try:
                        expiry = max(received + timedelta(seconds=300), _expires(exc.response.headers, received))
                    except ValueError:
                        expiry = received + timedelta(seconds=300)
                    try:
                        negative = {"identity": key, "expires": expiry.isoformat()}
                        self.public_negatives[key] = negative
                        self._write_atomic(path, negative)
                    except OSError:
                        pass
                raise

    @staticmethod
    def _item_count(resource, payload):
        return len(payload) if resource in RouteSnapshotCache.paginated_resources else 1

    def _identity(self, context, resource, params):
        if (resource.startswith("structure_") == (context == PUBLIC_CONTEXT)):
            raise PrivateSnapshotError("wrong_route_authorization")
        _positive((params or {}).get("resource_id"))
        if resource == "type_orders":
            _positive(params.get("type_id"))
            if params.get("order_type") != "sell":
                raise ValueError("this route cache supports sell-order fallback only")
        if resource == "route":
            _positive(params.get("destination"))
        return super()._identity(context, resource, params)

    def _request_url(self, context, resource, query):
        return BASE_URL + "/" + self.resource_paths[resource].format(**query)

    def _wire_query(self, query):
        return {key: value for key, value in query.items() if key not in ("resource_id", "destination")}

    def _acquire(self, auth, context, resource, params, key, retry_server_errors=False):
        return super()._acquire(auth, context, resource, params, key,
                                retry_server_errors=resource.startswith("structure_"))

    def _validate_request_payload(self, resource, payload, params):
        self._validate_payload(resource, payload)
        if resource == "type_orders" and any(
                row["type_id"] != params["type_id"] or row["is_buy_order"]
                for row in payload):
            raise PrivateSnapshotError("mismatched_filtered_orders")
        if resource == "structure_orders" and any(
                row["location_id"] != params["resource_id"] for row in payload):
            raise PrivateSnapshotError("mismatched_structure_orders")
        if resource == "station_info" and payload["station_id"] != params["resource_id"]:
            raise PrivateSnapshotError("mismatched_station")
        if resource == "route" and (payload[0] != params["resource_id"] or payload[-1] != params["destination"]):
            raise PrivateSnapshotError("mismatched_route_endpoints")

    @staticmethod
    def _check_context(auth, context):
        if context != PUBLIC_CONTEXT:
            PrivateWalletCache._check_context(auth, context)


def get_route_cache():
    global _singleton
    with _lock:
        if _singleton is None:
            _singleton = RouteSnapshotCache(get_data_dir())
        return _singleton


def route_snapshot(resource, resource_id, *, auth=None, slot="seller", params=None,
                   cache=None, fetch=True, trigger="route"):
    store = cache if cache is not None else get_route_cache()
    private = resource.startswith("structure_")
    context = private_context(auth, slot) if private else PUBLIC_CONTEXT
    query = {**(params or {}), "resource_id": int(resource_id)}
    snapshot = (store.get(auth if private else _PublicAuth(), context, resource, query,
                          trigger=trigger) if fetch else store.peek(context, resource, query))
    if private:
        store._check_context(auth, context)
    if fetch:
        store._event(context, resource, "COMPLETE" if snapshot["durable"] else "MEMORY_ONLY",
                     trigger, snapshot, reason="route_snapshot_ready",
                     status="durable" if snapshot["durable"] else "save_failed")
    # Callers may sort/filter their view without changing accepted raw data.
    return deepcopy(snapshot)
