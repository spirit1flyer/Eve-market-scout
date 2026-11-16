"""Offline acceptance checks for shared skills/standings, run by Ctrl+F12."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
import json
import threading
from types import SimpleNamespace

from core.esi_character_cache import (
    PrivateCharacterCache, RosterAuth, character_snapshot, trained_levels,
)
from core.esi_private_cache import private_context
from esi.esi_skills import ESISkills, ESIStandings
from industry.industry_skills import IndustrySkills
from industry.industry_standings import IndustryStandings
from tools.private_cache_checks import _Auth, _require, _rejected


class _Roster:
    def __init__(self, auth):
        self.auth = auth

    @property
    def client_id(self):
        return self.auth.client_id

    def get(self, character_id):
        return self.auth.char if self.auth.char.character_id == character_id else None

    def get_auth_headers(self, character_id):
        return self.auth.get_auth_headers() if self.get(character_id) else {}


class _Server:
    def __init__(self):
        self.time = datetime.now(timezone.utc).replace(microsecond=0)
        self.calls = []
        self.lifetime = 120
        self.pages = 1
        self.block = None
        self.cache_control = None
        self.fail = False
        self.payloads = {
            "skills": {"total_sp": 12000, "unallocated_sp": 50, "extra_metadata": "retained",
                "skills": [self.skill(3446, 3), self.skill(16622, 4), self.skill(16597, 2),
                           self.skill(3359, 4), self.skill(3357, 2), self.skill(3380, 5),
                           self.skill(999999, 1)]},
            "standings": [{"from_id": 1000125, "from_type": "npc_corp", "standing": 5.0},
                          {"from_id": 500003, "from_type": "faction", "standing": 8.0},
                          {"from_id": 999, "from_type": "agent", "standing": -5.0}],
        }

    @staticmethod
    def skill(identity, level):
        # Existing consumers intentionally use trained levels, not active levels.
        return {"skill_id": identity, "trained_skill_level": level,
                "active_skill_level": max(0, level - 1), "skillpoints_in_skill": 1000}

    def now(self):
        return self.time

    def advance(self, seconds):
        self.time += timedelta(seconds=seconds)

    def request(self, method, url, headers, params, timeout):
        resource = url.rstrip("/").split("/")[-1]
        self.calls.append(resource)
        if self.fail:
            raise OSError("synthetic reputation refresh failure")
        if self.block:
            entered, release = self.block
            entered.set()
            _require(release.wait(5), "synthetic character transport timed out")
        payload = deepcopy(self.payloads[resource])
        metadata = {"Expires": format_datetime(self.time + timedelta(seconds=self.lifetime), usegmt=True),
                    "Last-Modified": "synthetic-character", "X-Pages": str(self.pages)}
        if self.cache_control is not None:
            metadata.update({"Cache-Control": self.cache_control,
                             "Date": format_datetime(self.time, usegmt=True), "Age": "30"})
        return SimpleNamespace(json=lambda: payload, raise_for_status=lambda: None, headers=metadata)


class _Store(PrivateCharacterCache):
    def __init__(self, directory, server):
        super().__init__(directory, request=server.request, now=server.now)
        self.events = []
        self.fail_save = False
        self.waiting = threading.Event()

    def _event(self, context, resource, action, trigger, entry=None, **fields):
        self.events.append((resource, action, fields))
        if action == "WAIT":
            self.waiting.set()

    def _write_atomic(self, path, saved):
        if self.fail_save and saved.get("complete") is True:
            raise OSError("synthetic character save failure")
        super()._write_atomic(path, saved)


def character_sharing_restart(directory):
    server, auth = _Server(), _Auth()
    roster = _Roster(auth)
    store = _Store(directory, server)
    skills = ESISkills(auth, cache=store)
    _require(skills.fetch_skills() is not None, "trading skill fetch failed")
    source = store.peek(private_context(auth), "skills")
    _require(source["payload"] == server.payloads["skills"], "raw full skill response was filtered")
    _require(skills.fetch_skills(slot="buyer").accounting == 4, "matching buyer context did not share")
    industry = IndustrySkills(roster, cache=store)
    _require(industry.fetch(auth.char.character_id)[999999] == 1, "industry lost non-trading skill")
    _require(server.calls == ["skills"], "matching consumers acquired duplicate responses")
    server.advance(30)
    restored = _Store(directory, server)
    restored_skills = ESISkills(auth, cache=restored)
    _require(restored_skills.get_seller_skills().broker_relations == 3, "cache-only trading restore failed")
    view = IndustrySkills(roster, cache=restored)
    _require(view.get_level(auth.char.character_id, "industry") == 5, "cache-only industry restore failed")
    snapshot = restored.peek(private_context(auth), "skills")
    _require(snapshot["acquired_at"] == source["acquired_at"] and snapshot["expires"] == source["expires"],
             "restart reset character acquisition/expiry")
    _require(server.calls == ["skills"], "cache-only getters made requests")


def character_context_isolation(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    skills = ESISkills(auth, cache=store)
    skills.fetch_skills()
    auth.scopes.append("different-scope")
    auth.rotate(nonce=2)
    _require(skills.get_skill_level("accounting") == 0, "scope change retained prior derived skills")
    _require(skills.fetch_skills() is not None and len(server.calls) == 2, "scope context was shared")
    auth.owner = "different-owner"
    auth.rotate(nonce=3)
    _require(skills.get_skill_level("accounting") == 0, "owner change leaked prior data")
    auth.rotate(character=202)
    _require(skills.get_skill_level("accounting") == 0, "character change leaked prior data")
    auth.char.access_token = None
    _require(skills.get_skill_level("accounting") == 0 and skills.fetch_skills() is None,
             "logout exposed prior skills")
    _require(len(server.calls) == 2, "unauthenticated/cache-only read initiated acquisition")


def character_expiry_force(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    original = character_snapshot(auth, "skills", fetch=True, cache=store)
    server.advance(121)
    reused = character_snapshot(auth, "skills", fetch=True, cache=store)
    _require(reused is original and server.calls == ["skills"], "one-hour preference changed")
    _require(any(action == "REUSE" and fields.get("eligible_at") > server.now()
                 for _, action, fields in store.events), "consumer reuse lost its remaining wait")
    server.lifetime = 7200
    fresh = character_snapshot(auth, "skills", fetch=True, force=True, cache=store)
    _require(fresh["acquired_at"] != original["acquired_at"] and len(server.calls) == 2,
             "force failed to request an eligible update")
    server.advance(3601)
    character_snapshot(auth, "skills", fetch=True, force=True, cache=store)
    _require(len(server.calls) == 2, "force bypassed unexpired ESI data")
    server.advance(3600)
    _require(character_snapshot(auth, "skills", cache=store) is None and len(server.calls) == 2,
             "expired cache-only read initiated acquisition")
    server.cache_control, server.lifetime = "private, max-age=180", 99999
    skills = character_snapshot(auth, "skills", fetch=True, force=True, cache=store)
    _require((skills["expires"] - server.now()).total_seconds() == 150,
             "skills ignored Cache-Control/Age in favor of the legacy Expires header")
    _require(skills["page_metadata"][0]["response_expires"] is not None,
             "original server expiry metadata was lost")
    server.advance(151)
    server.cache_control = "no-store"
    _rejected(lambda: character_snapshot(auth, "skills", fetch=True, force=True, cache=store))


def character_validation_failures(directory):
    server, auth = _Server(), _Auth()
    store, context = _Store(directory, server), private_context(auth)
    original = store.get(auth, context, "skills")
    path = store._path("skills", store._identity(context, "skills", None))
    previous_bytes = path.read_bytes()
    server.advance(3601)
    server.payloads["skills"]["skills"][0]["trained_skill_level"] = 6
    _rejected(lambda: store.get(auth, context, "skills"))
    _require(store.peek(context, "skills") is original and path.read_bytes() == previous_bytes,
             "malformed rows replaced a good response")
    server.payloads["skills"]["skills"][0]["trained_skill_level"] = 4
    store.fail_save = True
    memory = character_snapshot(auth, "skills", fetch=True, force=True, cache=store)
    _require(not memory["durable"] and path.read_bytes() == previous_bytes,
             "failed character save destroyed disk or claimed durability")
    _require(store.events[-1][1] == "MEMORY_ONLY", "save failure reported durable completion")
    calls = len(server.calls)
    store.fail_save = False
    character_snapshot(auth, "skills", fetch=True, cache=store)
    _require(memory["durable"] and len(server.calls) == calls, "save retry reacquired character data")
    _require(_Store(directory, server).peek(context, "skills")["payload"] == memory["payload"],
             "character save retry did not survive restart")
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        corrupt = json.load(stream)
    corrupt["payload"]["skills"][0]["active_skill_level"] = 99
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(corrupt, stream)
    _require(_Store(directory, server).peek(context, "skills") is None, "corrupt disk payload restored")
    server.payloads["standings"][0]["standing"] = float("nan")
    _rejected(lambda: store.get(auth, context, "standings"))
    server.payloads["standings"] = []
    _require(store.get(auth, context, "standings")["payload"] == [], "empty standings rejected")
    server.advance(3601)
    server.payloads["skills"] = {"total_sp": 0, "skills": []}
    server.pages = 2
    _rejected(lambda: store.get(auth, context, "skills"))
    _require(store.peek(context, "skills") is memory, "unproven pagination replaced a full snapshot")
    server.advance(121)
    server.pages = 1
    _require(store.get(auth, context, "skills")["payload"]["skills"] == [], "empty skills rejected")


def character_concurrency(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    entered, release = threading.Event(), threading.Event()
    server.block = entered, release
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(character_snapshot, auth, "skills", fetch=True, cache=store)
        try:
            _require(entered.wait(5), "character request never entered transport")
            second = executor.submit(character_snapshot, RosterAuth(_Roster(auth), 101),
                "skills", "industry", fetch=True, cache=store)
            _require(store.waiting.wait(5), "matching roster request did not share the acquisition lock")
        finally:
            release.set()
        _require(first.result()["payload"] == second.result()["payload"], "concurrent consumers differ")
    _require(server.calls == ["skills"], "concurrent consumers duplicated acquisition")


def character_local_views(directory):
    server, auth = _Server(), _Auth()
    store, roster = _Store(directory, server), _Roster(auth)
    skills = ESISkills(auth, cache=store)
    standings = ESIStandings(auth, skills, cache=store)
    industry_skills = IndustrySkills(roster, cache=store)
    industry_standings = IndustryStandings(roster, industry_skills, cache=store)
    _require(standings.fetch_standings()["factions_base"][500003] == 8, "base standing changed")
    industry_standings.fetch(101)
    _require(server.calls == ["skills", "standings"], "standings consumers duplicated raw pulls")
    _require(standings.get_faction_standing(500003) == 8.32 and
             industry_standings.get(101)["factions"][500003] == 8.32,
             "consumer skill modifiers differ")
    _require(standings.get_faction_standing(500003, base=True) == 8, "fee math lost raw standing")
    server.advance(121)
    for row in server.payloads["skills"]["skills"]:
        if row["skill_id"] == 3359:
            row["trained_skill_level"] = 2
    skills.fetch_skills(force_refresh=True)
    _require(standings.get_faction_standing(500003) == 8.16 and
             industry_standings.get(101)["factions"][500003] == 8.16,
             "derived standings failed to follow the newer cached skills")
    _require(server.calls == ["skills", "standings", "skills"], "local recalculation acquired standings")
    restored = _Store(directory, server)
    restored_skills = ESISkills(auth, cache=restored)
    restored_standings = ESIStandings(auth, restored_skills, cache=restored)
    _require(restored_standings.get_faction_standing(500003) == 8.16,
             "restored raw responses did not reconstruct effective standings")
    _require(trained_levels(restored.peek(private_context(auth), "skills"))[3359] == 2,
             "full skill snapshot changed on restoration")
    _require(server.calls == ["skills", "standings", "skills"], "cache-only restart acquired data")


def reputation_retention(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    skills = ESISkills(auth, cache=store)
    standings = ESIStandings(auth, skills, cache=store)
    standings.fetch_standings()
    original = store.peek(private_context(auth), "standings")
    server.advance(8 * 24 * 3600)
    restored = _Store(directory, server)
    restored_skills = ESISkills(auth, cache=restored)
    seller = ESIStandings(auth, restored_skills, cache=restored)
    roster = _Roster(auth)
    industry_skills = IndustrySkills(roster, cache=restored)
    industry = IndustryStandings(roster, industry_skills, cache=restored)
    _require(seller.get_corp_standing(1000125, base=True) == 5.0,
             "saved seller rep disappeared after its update window")
    _require(seller.get_faction_standing(500003) == 8.32 and
             industry.get(101)["factions"][500003] == 8.32,
             "saved social modifiers disappeared with old rep")
    _require(restored_skills.get_skill_level("connections") == 0,
             "rep retention changed the normal skills freshness policy")
    snapshot = restored.peek(private_context(auth), "standings")
    _require(snapshot["acquired_at"] == original["acquired_at"] and server.calls == ["skills", "standings"],
             "old rep read reset age or made a request")
    auth.rotate(character=202)
    _require(seller.get_corp_standing(1000125, base=True) == 0 and industry.get(101)["factions"] == {},
             "retained rep leaked across character contexts")


def reputation_refresh_policy(directory):
    server, auth = _Server(), _Auth()
    store = _Store(directory, server)
    initial = character_snapshot(auth, "standings", fetch=True, cache=store)
    server.advance(30)
    character_snapshot(auth, "standings", fetch=True, force=True, cache=store)
    _require(server.calls == ["standings"], "manual rep refresh bypassed ESI expiry")
    server.advance(91)
    character_snapshot(auth, "standings", fetch=True, cache=store)
    _require(server.calls == ["standings"], "rep refreshed before seven days")
    manual = character_snapshot(auth, "standings", fetch=True, force=True, cache=store)
    _require(manual["acquired_at"] != initial["acquired_at"] and len(server.calls) == 2,
             "manual request could not update eligible rep")
    server.payloads["standings"][0]["standing"] = 6.0
    server.advance(7 * 24 * 3600 - 1)
    character_snapshot(auth, "standings", fetch=True, cache=store)
    _require(len(server.calls) == 2, "rep refreshed before the seven-day boundary")
    server.advance(1)
    fresh = character_snapshot(auth, "standings", fetch=True, cache=store)
    _require(len(server.calls) == 3 and fresh["payload"][0]["standing"] == 6.0,
             "seven-day on-use refresh failed")
    server.advance(7 * 24 * 3600)
    server.fail = True
    completes = sum(action == "COMPLETE" for _, action, _ in store.events)
    retained = character_snapshot(auth, "standings", fetch=True, cache=store)
    _require(retained is fresh and store.events[-1][1] == "STALE_REUSE",
             "failed rep refresh discarded the previous snapshot")
    _require(sum(action == "COMPLETE" for _, action, _ in store.events) == completes,
             "failed rep refresh claimed successful acquisition")
    _require(_Store(directory, server).peek(private_context(auth), "standings")["acquired_at"]
             == fresh["acquired_at"], "failed refresh changed the saved rep clock")
