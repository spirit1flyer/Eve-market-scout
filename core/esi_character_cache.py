"""Shared raw skills/standings snapshots; consumer calculations stay local."""

from datetime import timedelta
import math
import threading

from core.esi_private_cache import PrivateWalletCache, PrivateSnapshotError, private_context
from core.esi_public_cache import _expires


CONSUMER_SECONDS = 3600  # Preserve the existing one-hour non-forced preference.
REPUTATION_SECONDS = 7 * 24 * 3600
_singleton = None
_lock = threading.Lock()


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("invalid character integer field")


def validate_character_payload(resource, payload):
    """Validate every row before acceptance, staging or disk restoration."""
    seen = set()
    if resource == "skills":
        if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
            raise ValueError("full skills object required")
        _integer(payload["total_sp"])
        if "unallocated_sp" in payload:
            _integer(payload["unallocated_sp"])
        for row in payload["skills"]:
            if not isinstance(row, dict):
                raise ValueError("skill row must be an object")
            _integer(row["skill_id"], 1)
            _integer(row["trained_skill_level"], 0, 5)
            _integer(row["active_skill_level"], 0, 5)
            _integer(row["skillpoints_in_skill"])
            if row["skill_id"] in seen:
                raise ValueError("duplicate skill identity")
            seen.add(row["skill_id"])
    elif resource == "standings":
        if not isinstance(payload, list):
            raise ValueError("full standings list required")
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("standing row must be an object")
            _integer(row["from_id"], 1)
            if row["from_type"] not in ("agent", "npc_corp", "faction"):
                raise ValueError("unknown standing category")
            value = row["standing"]
            if type(value) not in (int, float) or not math.isfinite(value) or not -10 <= value <= 10:
                raise ValueError("invalid standing value")
            identity = row["from_type"], row["from_id"]
            if identity in seen:
                raise ValueError("duplicate standing identity")
            seen.add(identity)
    else:
        raise ValueError("unsupported character resource")


class PrivateCharacterCache(PrivateWalletCache):
    """Use the verified private transport, locks, staging and atomic persistence."""

    resource_paths = {"skills": "skills/", "standings": "standings/"}
    fallback_seconds = {"skills": 3600, "standings": 3600}
    single_value_resources = ("skills",)
    _validate_payload = staticmethod(validate_character_payload)

    @staticmethod
    def _item_count(resource, payload):
        return len(payload["skills"]) if resource == "skills" else len(payload)

    @staticmethod
    def _response_expiry(resource, headers, received):
        if resource == "skills" and headers.get("cache-control"):
            # ESI's event-driven skills route makes Cache-Control authoritative.
            controls = {part.strip().lower() for part in headers["cache-control"].split(",")}
            if "no-store" in controls:
                raise PrivateSnapshotError("response_forbids_storage")
            if "no-cache" in controls:
                return received
            return _expires({key: value for key, value in headers.items() if key != "expires"}, received)
        return _expires(headers, received)


class RosterAuth:
    """Expose a roster character through the existing private-context contract."""

    def __init__(self, roster, character_id):
        self.roster, self.character_id = roster, character_id

    @property
    def client_id(self):
        from esi.esi_auth import CLIENT_ID
        return getattr(self.roster, "client_id", CLIENT_ID)

    def get_character(self, slot="industry"):
        return self.roster.get(self.character_id)

    def get_auth_headers(self, slot="industry"):
        return self.roster.get_auth_headers(self.character_id)


def get_character_cache():
    global _singleton
    with _lock:
        if _singleton is None:
            from core.sound_manager import get_data_dir
            _singleton = PrivateCharacterCache(get_data_dir())
        return _singleton


def usable_until(snapshot):
    """Consumer preference never changes the saved acquisition or ESI expiry."""
    seconds = REPUTATION_SECONDS if snapshot["resource"] == "standings" else CONSUMER_SECONDS
    return max(snapshot["expires"], snapshot["acquired_at"] + timedelta(seconds=seconds))


def character_snapshot(auth, resource, slot="seller", *, fetch=False,
                       force=False, cache=None, trigger="character", allow_stale=False):
    """Getters restore/reuse only; explicit fetches may acquire eligible data."""
    store = cache if cache is not None else get_character_cache()
    try:
        context = private_context(auth, slot)
    except PrivateSnapshotError:
        if fetch:
            raise
        return None
    if fetch:
        seconds = REPUTATION_SECONDS if resource == "standings" else CONSUMER_SECONDS
        try:
            snapshot = store.get(auth, context, resource, trigger=trigger,
                                 min_age_seconds=0 if force else seconds)
        except Exception as exc:
            snapshot = store.peek(context, resource) if resource == "standings" else None
            if snapshot is None or private_context(auth, slot) != context:
                raise
            store._event(context, resource, "STALE_REUSE", trigger, snapshot,
                         reason="refresh_failed_previous_rep_retained", status=type(exc).__name__)
            return snapshot
    else:
        snapshot = store.peek(context, resource)
        if snapshot is None or (resource != "standings" and not allow_stale
                                and store.now() >= usable_until(snapshot)):
            return None
    if private_context(auth, slot) != context:
        raise PrivateSnapshotError("authorization_changed")
    if fetch:
        store._event(context, resource, "COMPLETE" if snapshot["durable"] else "MEMORY_ONLY",
                     trigger, snapshot, reason="raw_response_ready",
                     eligible_at=snapshot["expires"] if force else usable_until(snapshot),
                     status="durable" if snapshot["durable"] else "save_failed")
    return snapshot


def trained_levels(snapshot):
    return {row["skill_id"]: row["trained_skill_level"] for row in snapshot["payload"]["skills"]}
