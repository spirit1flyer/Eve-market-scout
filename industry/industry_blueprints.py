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
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from core.sound_manager import get_data_dir

logger = logging.getLogger(__name__)

DB_FILENAME = "industry_blueprints.db"
BASE_URL = "https://esi.evetech.net/latest"

# Slow-pace safety: pause between blueprint pages so a heavy roster pull never
# hammers ESI (most characters are 1 page; large asset hangars can be several).
PAGE_PAUSE_SECONDS = 0.3

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
                count         INTEGER NOT NULL DEFAULT 0
            );
        """)
        c.commit()
        logger.debug("[IndustryBP] schema ensured at %s", self.db_path)

    # =========================================================================
    # Writes
    # =========================================================================

    def replace_for_character(self, character_id: int, rows: list[dict]) -> int:
        """Replace ALL owned blueprints for one character with `rows`.

        A blueprint pull is authoritative for that character: anything not in the
        new payload was sold/consumed/moved, so we delete the character's old
        rows and insert the fresh set in one transaction (resume-safe: a crash
        mid-write leaves the previous full set intact). Returns rows written.

        Each row is a raw ESI blueprint dict (item_id, type_id, location_id,
        location_flag, quantity, material_efficiency, time_efficiency, runs).
        """
        character_id = int(character_id)
        now = datetime.now(timezone.utc).isoformat()
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
                    "INSERT INTO pull_meta (character_id, last_pulled, count) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(character_id) DO UPDATE SET "
                    "  last_pulled = excluded.last_pulled, "
                    "  count = excluded.count",
                    (character_id, now, len(prepared)),
                )
            logger.debug("[IndustryBP] replaced %d blueprints for character %s",
                         len(prepared), character_id)
            return len(prepared)
        except Exception:
            logger.exception("[IndustryBP] replace_for_character failed for %s",
                             character_id)
            return 0

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
        """{last_pulled, count} for a character, or None if never pulled."""
        c = self._conn()
        row = c.execute(
            "SELECT last_pulled, count FROM pull_meta WHERE character_id = ?",
            (int(character_id),),
        ).fetchone()
        return dict(row) if row is not None else None

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

    def __init__(self, roster, db: Optional[IndustryBlueprintsDB] = None):
        self.roster = roster
        self.db = db or IndustryBlueprintsDB.singleton()

    def pull(self, character_id: int) -> tuple[bool, str]:
        """Fetch + store one character's blueprints. Returns (ok, message)."""
        character_id = int(character_id)
        headers = self.roster.get_auth_headers(character_id)
        if not headers:
            msg = f"not authenticated for character {character_id}"
            print(f"[IndustryBP] {msg}")
            return False, msg

        rows = self._fetch_all_pages(character_id, headers)
        if rows is None:
            return False, "ESI fetch failed (see log)"

        written = self.db.replace_for_character(character_id, rows)
        msg = f"stored {written} blueprints"
        print(f"[IndustryBP] character {character_id}: {msg}")
        return True, msg

    def _fetch_all_pages(self, character_id: int, headers: dict
                         ) -> Optional[list[dict]]:
        """Walk every X-Pages page; None on hard failure, [] is valid (no BPs)."""
        all_rows: list[dict] = []
        url = f"{BASE_URL}/characters/{character_id}/blueprints/"
        try:
            first = requests.get(
                url, headers=headers,
                params={"datasource": "tranquility", "page": 1},
                timeout=30,
            )
            first.raise_for_status()
            all_rows.extend(first.json() or [])
            total_pages = int(first.headers.get("X-Pages", "1"))
            print(f"[IndustryBP] character {character_id}: X-Pages={total_pages}")
        except (requests.RequestException, ValueError) as e:
            print(f"[IndustryBP] character {character_id} page 1 error: {e}")
            return None

        for page in range(2, total_pages + 1):
            time.sleep(PAGE_PAUSE_SECONDS)  # slow-pace safety
            try:
                resp = requests.get(
                    url, headers=headers,
                    params={"datasource": "tranquility", "page": page},
                    timeout=30,
                )
                resp.raise_for_status()
                all_rows.extend(resp.json() or [])
            except (requests.RequestException, ValueError) as e:
                print(f"[IndustryBP] character {character_id} page {page} "
                      f"error: {e}")
                return None
        return all_rows


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


def fetch_structure_meta(structure_id: int, headers: dict) -> Optional[dict]:
    """Resolve a player structure's {name, system_id} via the roster's auth.

    `esi_structures.fetch_structure_info` takes the trading `ESIAuth`; the
    Industry roster uses its own per-character headers (it holds the
    `esi-universe.read_structures.v1` scope), so Phase 3.5 needs this small
    header-based variant. Returns None on any auth/HTTP failure (the character
    may have lost docking access to the structure).
    """
    if not headers:
        return None
    try:
        resp = requests.get(
            f"{BASE_URL}/universe/structures/{int(structure_id)}/",
            headers=headers, timeout=30,
        )
        if resp.status_code != 200:
            print(f"[IndustryBP] structure {structure_id} meta HTTP "
                  f"{resp.status_code}")
            return None
        d = resp.json()
        return {"name": d.get("name", f"Structure {structure_id}"),
                "system_id": d.get("solar_system_id")}
    except (requests.RequestException, ValueError) as e:
        print(f"[IndustryBP] structure {structure_id} meta error: {e}")
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
