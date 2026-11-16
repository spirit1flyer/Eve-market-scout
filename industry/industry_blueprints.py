"""Owned BPO/BPC store + pull (Phase 3 of the Industry tab).

Phase 3 turns the Top Profit lens around: instead of "every T1 item, what would
it cost to build," it answers "of the blueprints I actually own, what are they
worth to build right now." That needs the character's owned blueprints, with
their researched ME/TE and (for copies) remaining runs.

This module has two halves:

  * `IndustryBlueprintsDB` — a SQLite singleton owned-blueprint store keyed by
    character_id, mirroring `contracts_db.ContractsDB` conventions (thread-local
    WAL connections, writes swallow-and-log, a greppable `[IndustryBP]` tag).
    Stage 3.1.
  * `BlueprintPuller` — the ESI fetch (`GET /characters/{id}/blueprints/`,
    paginated via X-Pages) over the Industry roster's auth headers, with a
    per-page slow-pace pause, writing the result into the DB as a full
    per-character replace. Stage 3.2.

ESI blueprint semantics (verified against the EVE Swagger spec):
  * `quantity == -1`  → a single ORIGINAL (a BPO).
  * `quantity == -2`  → a COPY (a BPC). BPCs do not stack, so each is one row.
  * `runs == -1`      → unlimited runs (only ever true for a BPO).
  * `runs >= 0`       → runs remaining (BPCs; a hard batch cap in Phase 3.4).
  * `material_efficiency` / `time_efficiency` → researched ME/TE percent (0–10
    / 0–20).
`item_id` is the unique asset id of the blueprint stack and is stable, so it is
the natural primary key.

The roster scope set already includes `esi-characters.read_blueprints.v1`
(see `industry_characters.INDUSTRY_SCOPES`), so no scope change is needed.
"""

import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import requests

from core.sound_manager import get_data_dir
from core.esi_rate_limit import esi_request

logger = logging.getLogger(__name__)

DB_FILENAME = "industry_blueprints.db"
BASE_URL = "https://esi.evetech.net/latest"

# Slow-pace safety: pause between blueprint pages so a heavy roster pull never
# hammers ESI (most characters are 1 page; large asset hangars can be several).
PAGE_PAUSE_SECONDS = 0.3
BLUEPRINT_MIN_REFRESH_SECONDS = 60 * 60

_SINGLETON: Optional["IndustryBlueprintsDB"] = None
_SINGLETON_LOCK = threading.Lock()


class IndustryBlueprintsDB:
    """SQLite singleton backing the Owned BPO/BPC master list.

    Tables:
      blueprints  — one row per owned blueprint asset (`item_id` PK), tagged
        with the owning character_id. BPCs don't stack so a bundle is N rows.
        Stores the researched ME/TE, runs (-1 = BPO infinite), and a derived
        `is_copy` flag for cheap filtering.
      pull_meta   — per-character pull bookkeeping (last_pulled, count) so the
        UI can show "last updated" and gate a manual re-pull.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_data_dir() / DB_FILENAME
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()

    @classmethod
    def singleton(cls) -> "IndustryBlueprintsDB":
        global _SINGLETON
        if _SINGLETON is None:
            with _SINGLETON_LOCK:
                if _SINGLETON is None:
                    _SINGLETON = cls()
        return _SINGLETON

    # =========================================================================
    # Connection
    # =========================================================================

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            c = sqlite3.connect(str(self.db_path))
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return self._local.conn

    def _ensure_schema(self) -> None:
        c = self._conn()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS blueprints (
                item_id              INTEGER PRIMARY KEY,
                character_id         INTEGER NOT NULL,
                type_id              INTEGER NOT NULL,
                location_id          INTEGER,
                location_flag        TEXT,
                quantity             INTEGER,
                material_efficiency  INTEGER NOT NULL DEFAULT 0,
                time_efficiency      INTEGER NOT NULL DEFAULT 0,
                runs                 INTEGER NOT NULL DEFAULT -1,
                is_copy              INTEGER NOT NULL DEFAULT 0,
                pulled_at            TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bp_character
                ON blueprints (character_id);
            CREATE INDEX IF NOT EXISTS idx_bp_type
                ON blueprints (type_id);

            CREATE TABLE IF NOT EXISTS pull_meta (
                character_id  INTEGER PRIMARY KEY,
                last_pulled   TEXT,
                expires_at    TEXT,
                count         INTEGER NOT NULL DEFAULT 0
            );
        """)
        columns = {
            row[1] for row in c.execute("PRAGMA table_info(pull_meta)").fetchall()
        }
        if "expires_at" not in columns:
            c.execute("ALTER TABLE pull_meta ADD COLUMN expires_at TEXT")
        if "authorization_context" not in columns:
            c.execute("ALTER TABLE pull_meta ADD COLUMN authorization_context TEXT")
        c.commit()
        logger.debug("[IndustryBP] schema ensured at %s", self.db_path)

    # =========================================================================
    # Writes
    # =========================================================================

    def replace_for_character(self, character_id: int, rows: list[dict],
                              expires_at: Optional[datetime] = None, *, acquired_at=None,
                              authorization_context=None, validate_context=None) -> Optional[int]:
        """Replace ALL owned blueprints for one character with `rows`.

        A blueprint pull is authoritative for that character: anything not in the
        new payload was sold/consumed/moved, so we delete the character's old
        rows and insert the fresh set in one transaction (resume-safe: a crash
        mid-write leaves the previous full set intact). Returns rows written.

        Each row is a raw ESI blueprint dict (item_id, type_id, location_id,
        location_flag, quantity, material_efficiency, time_efficiency, runs).
        """
        character_id = int(character_id)
        from industry.industry_blueprint_cache import validate_blueprints
        validate_blueprints("blueprints", rows)
        now = (acquired_at or datetime.now(timezone.utc)).isoformat()
        expires_text = expires_at.isoformat() if expires_at else None
        prepared = []
        for r in rows or []:
            iid = r.get("item_id")
            tid = r.get("type_id")
            if iid is None or tid is None:
                continue
            quantity = _as_int(r.get("quantity"))
            runs = _as_int(r.get("runs"))
            if runs is None:
                runs = -1
            is_copy = 1 if (quantity == -2 or runs >= 0) else 0
            prepared.append((
                int(iid),
                character_id,
                int(tid),
                _as_int(r.get("location_id")),
                r.get("location_flag"),
                quantity,
                _as_int(r.get("material_efficiency")) or 0,
                _as_int(r.get("time_efficiency")) or 0,
                runs,
                is_copy,
                now,
            ))
        try:
            c = self._conn()
            with c:
                if validate_context:
                    validate_context()
                c.execute("DELETE FROM blueprints WHERE character_id = ?",
                          (character_id,))
                if prepared:
                    c.executemany(
                        "INSERT OR REPLACE INTO blueprints "
                        "(item_id, character_id, type_id, location_id, "
                        " location_flag, quantity, material_efficiency, "
                        " time_efficiency, runs, is_copy, pulled_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        prepared,
                    )
                c.execute(
                    "INSERT INTO pull_meta "
                    "(character_id, last_pulled, expires_at, count, authorization_context) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(character_id) DO UPDATE SET "
                    "  last_pulled = excluded.last_pulled, "
                    "  expires_at = excluded.expires_at, "
                    "  count = excluded.count, authorization_context = excluded.authorization_context",
                    (character_id, now, expires_text, len(prepared), authorization_context),
                )
                if validate_context:
                    validate_context()
            logger.debug("[IndustryBP] replaced %d blueprints for character %s",
                         len(prepared), character_id)
            return len(prepared)
        except Exception:
            logger.exception("[IndustryBP] replace_for_character failed for %s",
                             character_id)
            return None

    def clear_character(self, character_id: int) -> None:
        """Drop a character's blueprints + pull record (on roster removal)."""
        character_id = int(character_id)
        try:
            c = self._conn()
            with c:
                c.execute("DELETE FROM blueprints WHERE character_id = ?",
                          (character_id,))
                c.execute("DELETE FROM pull_meta WHERE character_id = ?",
                          (character_id,))
            logger.debug("[IndustryBP] cleared character %s", character_id)
        except Exception:
            logger.exception("[IndustryBP] clear_character failed for %s",
                             character_id)

    # =========================================================================
    # Reads
    # =========================================================================

    def get_blueprints(self, character_id: Optional[int] = None) -> list[dict]:
        """Owned blueprints, all characters or one. Sorted by type_id."""
        c = self._conn()
        if character_id is None:
            rows = c.execute(
                "SELECT * FROM blueprints ORDER BY type_id"
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM blueprints WHERE character_id = ? ORDER BY type_id",
                (int(character_id),),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_blueprints_for_type(self, type_id: int) -> list[dict]:
        """All owned copies of one blueprint type (across characters)."""
        c = self._conn()
        rows = c.execute(
            "SELECT * FROM blueprints WHERE type_id = ? ORDER BY is_copy, runs DESC",
            (int(type_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_owned_type_ids(self) -> set[int]:
        """Distinct blueprint type_ids owned (for 'you own this' marking)."""
        c = self._conn()
        rows = c.execute("SELECT DISTINCT type_id FROM blueprints").fetchall()
        return {int(r[0]) for r in rows}

    def get_structure_locations(self) -> dict:
        """Distinct owned-blueprint location_ids that are player STRUCTURES
        (≥ 1T) the blueprint sits *directly in the hangar* of, each mapped to one
        owning character_id (for auth). Phase 3.5 uses this to offer registering
        those structures as industry hubs.

        The ≥1T floor alone is not enough: a blueprint inside a container or ship
        carries that *container/ship's* item_id as its location_id (also ≥1T),
        and those 404 on the structure-meta lookup. ESI only reports a true
        structure_id as the location_id when the blueprint is loose in a hangar,
        which it flags with a hangar `location_flag` — so we whitelist those.
        """
        c = self._conn()
        placeholders = ",".join("?" * len(HANGAR_LOCATION_FLAGS))
        rows = c.execute(
            "SELECT location_id, MIN(character_id) AS cid FROM blueprints "
            f"WHERE location_id >= ? AND location_flag IN ({placeholders}) "
            "GROUP BY location_id",
            (STRUCTURE_ID_FLOOR, *HANGAR_LOCATION_FLAGS),
        ).fetchall()
        return {int(r["location_id"]): int(r["cid"]) for r in rows}

    def get_pull_meta(self, character_id: int) -> Optional[dict]:
        """Pull timing/count metadata for a character, or None if never pulled."""
        c = self._conn()
        row = c.execute(
            "SELECT last_pulled, expires_at, count, authorization_context FROM pull_meta "
            "WHERE character_id = ?",
            (int(character_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_cache_status(self, character_id: int) -> tuple[bool, int]:
        """Return (can_fetch, seconds_remaining) for a manual blueprint pull."""
        meta = self.get_pull_meta(character_id)
        expires_raw = meta.get("expires_at") if meta else None
        if not expires_raw:
            return True, 0
        try:
            expires_at = datetime.fromisoformat(expires_raw)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return True, 0
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            return True, 0
        return False, max(0, int(remaining))

    def get_stats(self) -> dict:
        c = self._conn()
        total = c.execute("SELECT COUNT(*) FROM blueprints").fetchone()[0]
        bpos = c.execute(
            "SELECT COUNT(*) FROM blueprints WHERE is_copy = 0"
        ).fetchone()[0]
        bpcs = c.execute(
            "SELECT COUNT(*) FROM blueprints WHERE is_copy = 1"
        ).fetchone()[0]
        chars = c.execute(
            "SELECT COUNT(*) FROM pull_meta"
        ).fetchone()[0]
        return {
            "blueprints": int(total or 0),
            "bpos": int(bpos or 0),
            "bpcs": int(bpcs or 0),
            "characters_pulled": int(chars or 0),
        }


class BlueprintPuller:
    """Pulls owned blueprints for roster characters via ESI into the DB.

    `roster` is an `IndustryRoster` (provides `get_auth_headers(character_id)`).
    One-time + manual re-pull: each `pull` is a full per-character replace, so
    re-pulling after building/selling blueprints converges to the current set.
    """

    def __init__(self, roster, db: Optional[IndustryBlueprintsDB] = None, cache=None):
        self.roster = roster
        self.db = db or IndustryBlueprintsDB.singleton()
        self.cache = cache

    def get_cache_status(self, character_id):
        from core.esi_character_cache import RosterAuth
        from core.esi_private_cache import private_context
        from industry.industry_blueprint_cache import get_blueprint_cache
        try:
            auth = RosterAuth(self.roster, int(character_id))
            context = private_context(auth, "industry")
            meta = self.db.get_pull_meta(character_id)
            if not meta or meta.get("authorization_context") != context.partition:
                return True, 0
            store = self.cache if self.cache is not None else get_blueprint_cache()
            snapshot = store.peek(context, "blueprints")
            if snapshot is None or not snapshot["durable"]:
                return True, 0
            if (meta.get("last_pulled") != snapshot["acquired_at"].isoformat()
                    or meta.get("count") != len(snapshot["payload"])):
                return True, 0  # A retained raw snapshot still needs DB application.
            store._check_context(auth, context)
            eligible = max(snapshot["expires"], snapshot["acquired_at"] + timedelta(seconds=3600))
            seconds = max(0, int((eligible - store.now()).total_seconds()))
            return seconds == 0, seconds
        except Exception:
            return True, 0

    def pull(self, character_id: int) -> tuple[bool, str]:
        from core.esi_character_cache import RosterAuth
        from core.esi_private_cache import private_context
        from industry.industry_blueprint_cache import get_blueprint_cache, blueprint_apply_lock
        character_id = int(character_id)
        auth = RosterAuth(self.roster, character_id)
        store = self.cache if self.cache is not None else get_blueprint_cache()
        try:
            with blueprint_apply_lock(self.db, character_id):
                context = private_context(auth, "industry")
                snapshot = store.get(auth, context, "blueprints", trigger="industry_blueprints",
                                     min_age_seconds=BLUEPRINT_MIN_REFRESH_SECONDS)
                def check_context():
                    store._check_context(auth, context)
                check_context()
                meta = self.db.get_pull_meta(character_id)
                acquired = snapshot["acquired_at"]
                eligible = max(snapshot["expires"], acquired + timedelta(seconds=BLUEPRINT_MIN_REFRESH_SECONDS))
                if (meta and meta.get("authorization_context") == context.partition
                        and meta.get("last_pulled") == acquired.isoformat()
                        and meta.get("count") == len(snapshot["payload"])):
                    written = meta["count"]
                else:
                    written = self.db.replace_for_character(character_id, snapshot["payload"],
                        expires_at=eligible, acquired_at=acquired,
                        authorization_context=context.partition, validate_context=check_context)
                if written is None:
                    store._event(context, "blueprints", "SAVE_FAILED", "industry_blueprints",
                                 snapshot, reason="database_previous_rows_retained")
                    return False, "blueprint database write failed; previous data retained"
                if not snapshot["durable"]:
                    return False, "blueprints stored in database; raw snapshot save failed"
                check_context()
                store._event(context, "blueprints", "COMPLETE", "industry_blueprints",
                             snapshot, reason="database_and_snapshot_durable", status="durable",
                             eligible_at=eligible)
                msg = f"stored {written} blueprints; next update in {_format_duration((eligible-store.now()).total_seconds())}"
                print(f"[IndustryBP] character {character_id}: {msg}")
                return True, msg
        except Exception as exc:
            print(f"[IndustryBP] pull failed; previous data retained: {type(exc).__name__}")
            return False, "blueprint acquisition failed; previous data retained (see log)"


def _response_expiry(response: requests.Response) -> datetime:
    """Honor ESI expiry, with a one-hour local minimum between blueprint pulls."""
    now = datetime.now(timezone.utc)
    local_floor = now + timedelta(seconds=BLUEPRINT_MIN_REFRESH_SECONDS)
    server_expiry = None

    cache_control = response.headers.get("Cache-Control", "")
    match = re.search(r"max-age=(\d+)", cache_control)
    if match:
        server_expiry = now + timedelta(seconds=int(match.group(1)))
    else:
        expires_raw = response.headers.get("Expires")
        if expires_raw:
            try:
                server_expiry = parsedate_to_datetime(expires_raw)
                if server_expiry.tzinfo is None:
                    server_expiry = server_expiry.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                server_expiry = None

    return max(local_floor, server_expiry) if server_expiry else local_floor


def _format_duration(seconds: int) -> str:
    minutes, secs = divmod(max(0, int(seconds)), 60)
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


STRUCTURE_ID_FLOOR = 1_000_000_000_000  # player structures live above 1T

# ESI `location_flag` values that mean "loose in a hangar at this location" —
# i.e. the blueprint's location_id is the STRUCTURE itself, not a container or
# ship sitting inside it. Anything else (Cargo/DroneBay/Locked/Unlocked/None/…)
# means location_id points at an asset (container/ship), which 404s on the
# structure-meta lookup, so we exclude it from structure registration.
# Character blueprints normally use "Hangar"; the corp/deliveries variants are
# whitelisted defensively in case a pull ever surfaces them.
HANGAR_LOCATION_FLAGS = (
    "Hangar", "Deliveries", "CorpDeliveries",
    "CorpSAG1", "CorpSAG2", "CorpSAG3", "CorpSAG4",
    "CorpSAG5", "CorpSAG6", "CorpSAG7",
)


def fetch_structure_meta(structure_id: int, auth, slot="industry", *, cache=None) -> Optional[dict]:
    """Resolve through the same context-scoped metadata provider as discovery."""
    from esi.esi_structures import fetch_structure_info, StructureAccessError, StructureSnapshotError
    try:
        info = fetch_structure_info(structure_id, auth, slot, cache=cache)
        return {"name": info.name, "system_id": info.solar_system_id}
    except (StructureAccessError, StructureSnapshotError) as exc:
        print(f"[IndustryBP] structure meta failed: {type(exc).__name__}")
        return None


# =============================================================================
# Small parsing helper (mirrors contracts_db's defensive coercion)
# =============================================================================

def _as_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
