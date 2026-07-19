"""Storage layer for the Contracts tab (Step 1 — schema + freshness store).

Public ESI contract search is a manual, specific-item lens (see the Contracts
tab design). ESI has no server-side contract search: `/contracts/public/{region}/`
returns list rows WITHOUT contents, so finding a named item means opening every
contract in scope via `/contracts/public/items/{contract_id}/`. Contract
contents are immutable, so we cache them forever and detect new contracts by a
set-difference of contract_ids. Station scope is the cost lever (only a few
hundred contracts per station vs tens of thousands region-wide).

This module is the storage primitives only — schema, the per-scope freshness
record (absolute expiry + ETag, so rapid app start/close cycles don't spam ESI),
and CRUD for list rows, the immutable items cache, and the id->name cache. The
ESI client (Step 2) and the diff/pull engine (Step 3) sit on top of this and own
the network + orchestration. Nothing here touches existing code or the network,
so the app launches unchanged with this module present.

Convention notes: mirrors structure_history.StructureHistoryDB (SQLite singleton,
thread-local WAL connections, writes swallow-and-log so a storage hiccup never
breaks a caller). All diagnostics carry a greppable `[ContractDiag]` tag so the
whole flow is observable in eve_scout.log (the user's CLI harnesses fail on his
machine, so debug must be visible in the live app).
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.sound_manager import get_data_dir

logger = logging.getLogger(__name__)

DB_FILENAME = "contracts.db"

_SINGLETON: Optional["ContractsDB"] = None
_SINGLETON_LOCK = threading.Lock()


def scope_key_for_region(region_id: int) -> str:
    """Canonical scope key for a whole-region pull."""
    return f"region:{int(region_id)}"


def scope_key_for_station(station_id: int) -> str:
    """Canonical scope key for a station-scoped pull.

    Station scope still pulls the region *list* (to find the station's rows)
    but only fetches *items* for that station's contracts — the cheap default.
    """
    return f"station:{int(station_id)}"


class ContractsDB:
    """SQLite singleton backing the Contracts tab.

    Tables:
      contract_list   — one row per public contract seen in a region. Mirrors
        the `/contracts/public/{region_id}/` list payload (which has NO item
        contents). `items_fetched` flags whether we've pulled this contract's
        contents into `contract_items` yet (a contract can legitimately have
        zero items — e.g. courier — so presence in contract_items is not a
        reliable "fetched" signal on its own).
      contract_items  — immutable contents cache, `contract_id -> [records]`.
        Cache-forever: contract contents never change. BPCs don't stack, so a
        bundle of 12 BPCs is 12 records (each raw_quantity -2); we keep
        record_id to preserve that.
      id_names        — id -> name cache for issuers/corporations resolved via
        `POST /universe/names/`. Effectively permanent.
      scope_freshness — per-scope absolute expiry + ETag (from ESI
        Cache-Control/Expires + ETag headers). On boot: if now < expires skip
        ESI entirely; else a conditional If-None-Match request → 304 just bumps
        expiry, 200 reprocesses. Survives rapid start/close without spamming.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_data_dir() / DB_FILENAME
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()

    @classmethod
    def singleton(cls) -> "ContractsDB":
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
            CREATE TABLE IF NOT EXISTS contract_list (
                contract_id            INTEGER PRIMARY KEY,
                region_id              INTEGER NOT NULL,
                type                   TEXT,
                price                  REAL,
                reward                 REAL,
                collateral             REAL,
                buyout                 REAL,
                volume                 REAL,
                start_location_id      INTEGER,
                end_location_id        INTEGER,
                issuer_id              INTEGER,
                issuer_corporation_id  INTEGER,
                for_corporation        INTEGER NOT NULL DEFAULT 0,
                days_to_complete       INTEGER,
                title                  TEXT,
                date_issued            TEXT,
                date_expired           TEXT,
                first_seen             TEXT NOT NULL,
                items_fetched          INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_clist_region
                ON contract_list (region_id);
            CREATE INDEX IF NOT EXISTS idx_clist_location
                ON contract_list (start_location_id);
            CREATE INDEX IF NOT EXISTS idx_clist_region_fetched
                ON contract_list (region_id, items_fetched);

            CREATE TABLE IF NOT EXISTS contract_items (
                contract_id          INTEGER NOT NULL,
                record_id            INTEGER NOT NULL,
                type_id              INTEGER NOT NULL,
                quantity             INTEGER,
                raw_quantity         INTEGER,
                is_included          INTEGER NOT NULL DEFAULT 1,
                is_blueprint_copy    INTEGER NOT NULL DEFAULT 0,
                runs                 INTEGER,
                material_efficiency  INTEGER,
                time_efficiency      INTEGER,
                PRIMARY KEY (contract_id, record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_citems_type
                ON contract_items (type_id);
            CREATE INDEX IF NOT EXISTS idx_citems_contract
                ON contract_items (contract_id);

            CREATE TABLE IF NOT EXISTS id_names (
                id           INTEGER PRIMARY KEY,
                name         TEXT NOT NULL,
                category     TEXT,
                resolved_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scope_freshness (
                scope_key     TEXT PRIMARY KEY,
                region_id     INTEGER NOT NULL,
                expires       TEXT,
                etag          TEXT,
                last_fetched  TEXT,
                last_status   INTEGER
            );
        """)
        c.commit()
        logger.debug("[ContractDiag] schema ensured at %s", self.db_path)

    # =========================================================================
    # Freshness store (per-scope absolute expiry + ETag)
    # =========================================================================

    def get_scope_freshness(self, scope_key: str) -> Optional[dict]:
        """Return {region_id, expires, etag, last_fetched, last_status} or None."""
        c = self._conn()
        row = c.execute(
            "SELECT region_id, expires, etag, last_fetched, last_status "
            "FROM scope_freshness WHERE scope_key = ?",
            (scope_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "scope_key": scope_key,
            "region_id": int(row["region_id"]),
            "expires": row["expires"],
            "etag": row["etag"],
            "last_fetched": row["last_fetched"],
            "last_status": row["last_status"],
        }

    def set_scope_freshness(self, scope_key: str, region_id: int,
                            expires: Optional[str] = None,
                            etag: Optional[str] = None,
                            last_status: Optional[int] = None) -> None:
        """Upsert a scope's freshness record.

        `expires` is an absolute ISO timestamp (not a countdown) so it survives
        app restarts. Pass the values the ESI client read off the response
        headers. A None field overwrites with NULL by design — callers pass the
        full picture from the latest response.
        """
        now = datetime.now(timezone.utc).isoformat()
        try:
            c = self._conn()
            with c:
                c.execute(
                    "INSERT INTO scope_freshness "
                    "(scope_key, region_id, expires, etag, last_fetched, last_status) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(scope_key) DO UPDATE SET "
                    "  region_id = excluded.region_id, "
                    "  expires = excluded.expires, "
                    "  etag = excluded.etag, "
                    "  last_fetched = excluded.last_fetched, "
                    "  last_status = excluded.last_status",
                    (scope_key, int(region_id), expires, etag, now, last_status),
                )
            logger.debug(
                "[ContractDiag] freshness set scope=%s expires=%s etag=%s status=%s",
                scope_key, expires, etag, last_status,
            )
        except Exception:
            logger.exception("[ContractDiag] set_scope_freshness failed for %s",
                             scope_key)

    def bump_scope_expiry(self, scope_key: str, expires: Optional[str],
                          last_status: int = 304) -> None:
        """Lightweight update for the 304 path — refresh expiry/last_fetched
        without disturbing the stored ETag (which is still valid on a 304)."""
        now = datetime.now(timezone.utc).isoformat()
        try:
            c = self._conn()
            with c:
                c.execute(
                    "UPDATE scope_freshness SET expires = ?, last_fetched = ?, "
                    "last_status = ? WHERE scope_key = ?",
                    (expires, now, last_status, scope_key),
                )
            logger.debug("[ContractDiag] freshness bumped scope=%s expires=%s "
                         "(status %s)", scope_key, expires, last_status)
        except Exception:
            logger.exception("[ContractDiag] bump_scope_expiry failed for %s",
                             scope_key)

    def is_scope_fresh(self, scope_key: str,
                       now: Optional[datetime] = None) -> bool:
        """True if now < stored expiry — meaning we can skip ESI entirely.

        Unparseable/missing expiry → not fresh (forces a conditional request,
        which is cheap thanks to the ETag).
        """
        rec = self.get_scope_freshness(scope_key)
        if not rec or not rec.get("expires"):
            return False
        if now is None:
            now = datetime.now(timezone.utc)
        exp = _parse_iso(rec["expires"])
        if exp is None:
            return False
        fresh = now < exp
        logger.debug("[ContractDiag] freshness check scope=%s fresh=%s "
                     "(expires=%s)", scope_key, fresh, rec["expires"])
        return fresh

    def list_tracked_scopes(self) -> list[dict]:
        """All scopes we've ever pulled — the set the hourly engine refreshes."""
        c = self._conn()
        rows = c.execute(
            "SELECT scope_key, region_id, expires, etag, last_fetched, last_status "
            "FROM scope_freshness ORDER BY scope_key"
        ).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # Contract list rows
    # =========================================================================

    def get_contract_ids_for_region(self, region_id: int) -> set[int]:
        """All cached contract_ids for a region — the left side of the diff."""
        c = self._conn()
        rows = c.execute(
            "SELECT contract_id FROM contract_list WHERE region_id = ?",
            (region_id,),
        ).fetchall()
        return {int(r[0]) for r in rows}

    def upsert_list_rows(self, region_id: int, rows: list[dict]) -> int:
        """Insert/update list rows for a region. Returns rows written.

        `first_seen` is preserved on conflict (so a row's age survives
        re-pulls); everything else refreshes from the latest list payload.
        Note: list rows carry NO item contents — that's a separate fetch.
        """
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        prepared = []
        for r in rows:
            cid = r.get("contract_id")
            if cid is None:
                continue
            prepared.append((
                int(cid),
                int(region_id),
                r.get("type"),
                _as_float(r.get("price")),
                _as_float(r.get("reward")),
                _as_float(r.get("collateral")),
                _as_float(r.get("buyout")),
                _as_float(r.get("volume")),
                _as_int(r.get("start_location_id")),
                _as_int(r.get("end_location_id")),
                _as_int(r.get("issuer_id")),
                _as_int(r.get("issuer_corporation_id")),
                1 if r.get("for_corporation") else 0,
                _as_int(r.get("days_to_complete")),
                r.get("title"),
                r.get("date_issued"),
                r.get("date_expired"),
                now,
            ))
        if not prepared:
            return 0
        try:
            c = self._conn()
            with c:
                c.executemany(
                    "INSERT INTO contract_list "
                    "(contract_id, region_id, type, price, reward, collateral, "
                    " buyout, volume, start_location_id, end_location_id, "
                    " issuer_id, issuer_corporation_id, for_corporation, "
                    " days_to_complete, title, date_issued, date_expired, first_seen) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(contract_id) DO UPDATE SET "
                    "  region_id = excluded.region_id, "
                    "  type = excluded.type, "
                    "  price = excluded.price, "
                    "  reward = excluded.reward, "
                    "  collateral = excluded.collateral, "
                    "  buyout = excluded.buyout, "
                    "  volume = excluded.volume, "
                    "  start_location_id = excluded.start_location_id, "
                    "  end_location_id = excluded.end_location_id, "
                    "  issuer_id = excluded.issuer_id, "
                    "  issuer_corporation_id = excluded.issuer_corporation_id, "
                    "  for_corporation = excluded.for_corporation, "
                    "  days_to_complete = excluded.days_to_complete, "
                    "  title = excluded.title, "
                    "  date_issued = excluded.date_issued, "
                    "  date_expired = excluded.date_expired",
                    prepared,
                )
            logger.debug("[ContractDiag] upserted %d list rows for region %s",
                         len(prepared), region_id)
            return len(prepared)
        except Exception:
            logger.exception("[ContractDiag] upsert_list_rows failed for region %s",
                             region_id)
            return 0

    def prune_contracts(self, contract_ids) -> int:
        """Delete contracts that vanished from the live list (completed/expired).

        Contract_ids are unique and never reused, so a gone id is gone for good.
        Also clears their cached items — they can never be searched again. The
        immutable-contents guarantee is about contracts that still EXIST; a
        pruned id will never come back, so dropping its items reclaims space
        without risking a re-fetch.
        """
        ids = [int(x) for x in contract_ids]
        if not ids:
            return 0
        try:
            c = self._conn()
            with c:
                c.executemany("DELETE FROM contract_items WHERE contract_id = ?",
                              [(i,) for i in ids])
                c.executemany("DELETE FROM contract_list WHERE contract_id = ?",
                              [(i,) for i in ids])
            logger.debug("[ContractDiag] pruned %d gone contracts", len(ids))
            return len(ids)
        except Exception:
            logger.exception("[ContractDiag] prune_contracts failed")
            return 0

    def get_list_row(self, contract_id: int) -> Optional[dict]:
        c = self._conn()
        row = c.execute(
            "SELECT * FROM contract_list WHERE contract_id = ?",
            (int(contract_id),),
        ).fetchone()
        return dict(row) if row is not None else None

    def get_unfetched_contract_ids(self, region_id: int,
                                   start_location_id: Optional[int] = None
                                   ) -> list[int]:
        """Contracts whose items we haven't pulled yet, optionally narrowed to
        a station (the cheap default scope). This is the items-fetch worklist.
        """
        c = self._conn()
        if start_location_id is None:
            rows = c.execute(
                "SELECT contract_id FROM contract_list "
                "WHERE region_id = ? AND items_fetched = 0",
                (int(region_id),),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT contract_id FROM contract_list "
                "WHERE region_id = ? AND start_location_id = ? AND items_fetched = 0",
                (int(region_id), int(start_location_id)),
            ).fetchall()
        return [int(r[0]) for r in rows]

    # =========================================================================
    # Contract items (immutable contents cache)
    # =========================================================================

    @staticmethod
    def _prepare_item_rows(contract_id: int, items: list[dict]) -> list[tuple]:
        cid = int(contract_id)
        prepared = []
        for it in items or []:
            rid = it.get("record_id")
            tid = it.get("type_id")
            if rid is None or tid is None:
                continue
            prepared.append((
                cid,
                int(rid),
                int(tid),
                _as_int(it.get("quantity")),
                _as_int(it.get("raw_quantity")),
                1 if it.get("is_included", True) else 0,
                1 if it.get("is_blueprint_copy") else 0,
                _as_int(it.get("runs")),
                _as_int(it.get("material_efficiency")),
                _as_int(it.get("time_efficiency")),
            ))
        return prepared

    _ITEMS_INSERT_SQL = (
        "INSERT OR REPLACE INTO contract_items "
        "(contract_id, record_id, type_id, quantity, raw_quantity, "
        " is_included, is_blueprint_copy, runs, "
        " material_efficiency, time_efficiency) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def store_items(self, contract_id: int, items: list[dict]) -> None:
        """Cache a contract's contents forever and flag it items_fetched.

        Idempotent: re-storing the same contract is a no-op replace. An empty
        `items` list is valid (courier/loan contracts have no items) — we still
        set items_fetched so we don't keep re-opening it.
        """
        self.store_items_batch({int(contract_id): items})

    def store_items_batch(self, items_by_contract: dict) -> int:
        """Store many contracts' contents in ONE transaction.

        This is the resume-safe write path: items are persisted incrementally in
        batches as they're fetched, so a mid-crawl shutdown only loses the items
        in flight since the last batch (not the whole crawl). Returns the number
        of contracts written. Each contract is flagged items_fetched=1.
        """
        if not items_by_contract:
            return 0
        try:
            c = self._conn()
            with c:
                for cid, items in items_by_contract.items():
                    cid = int(cid)
                    prepared = self._prepare_item_rows(cid, items)
                    c.execute("DELETE FROM contract_items WHERE contract_id = ?",
                              (cid,))
                    if prepared:
                        c.executemany(self._ITEMS_INSERT_SQL, prepared)
                    c.execute(
                        "UPDATE contract_list SET items_fetched = 1 "
                        "WHERE contract_id = ?", (cid,))
            logger.debug("[ContractDiag] stored items for %d contracts (batch)",
                         len(items_by_contract))
            return len(items_by_contract)
        except Exception:
            logger.exception("[ContractDiag] store_items_batch failed")
            return 0

    def mark_items_unavailable(self, contract_ids) -> int:
        """Flag contracts whose items endpoint returned a hard error (e.g. 400)
        as items_fetched=2 so they're excluded from the worklist and never
        retried. Distinct from 1 (fetched OK) so the state is debuggable.
        """
        ids = [int(x) for x in contract_ids]
        if not ids:
            return 0
        try:
            c = self._conn()
            with c:
                c.executemany(
                    "UPDATE contract_list SET items_fetched = 2 "
                    "WHERE contract_id = ?", [(i,) for i in ids])
            logger.debug("[ContractDiag] marked %d contracts items-unavailable",
                         len(ids))
            return len(ids)
        except Exception:
            logger.exception("[ContractDiag] mark_items_unavailable failed")
            return 0

    def has_items(self, contract_id: int) -> bool:
        """Whether this contract's contents are already cached (items_fetched).

        Uses the flag rather than COUNT(contract_items) so legitimately-empty
        contracts (courier) read as fetched.
        """
        c = self._conn()
        row = c.execute(
            "SELECT items_fetched FROM contract_list WHERE contract_id = ?",
            (int(contract_id),),
        ).fetchone()
        return bool(row and row[0])

    def get_items(self, contract_id: int) -> list[dict]:
        c = self._conn()
        rows = c.execute(
            "SELECT record_id, type_id, quantity, raw_quantity, is_included, "
            "       is_blueprint_copy, runs, material_efficiency, time_efficiency "
            "FROM contract_items WHERE contract_id = ? ORDER BY record_id",
            (int(contract_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    def find_contracts_with_type(self, type_id: int, region_id: int,
                                 start_location_id: Optional[int] = None,
                                 included_only: bool = True) -> list[int]:
        """Contract_ids in scope whose contents include `type_id`.

        This is the search query the tab runs after a manual Search. Scope is
        region or station-in-region only (no "everywhere"). `included_only`
        keeps it to items being offered (is_included = 1), not requested.
        """
        c = self._conn()
        clauses = ["cl.region_id = ?", "ci.type_id = ?"]
        params: list = [int(region_id), int(type_id)]
        if start_location_id is not None:
            clauses.append("cl.start_location_id = ?")
            params.append(int(start_location_id))
        if included_only:
            clauses.append("ci.is_included = 1")
        where = " AND ".join(clauses)
        rows = c.execute(
            f"SELECT DISTINCT cl.contract_id "
            f"FROM contract_list cl "
            f"JOIN contract_items ci ON ci.contract_id = cl.contract_id "
            f"WHERE {where}",
            params,
        ).fetchall()
        return [int(r[0]) for r in rows]

    def find_bpc_offers(self, blueprint_type_id: int,
                        region_id: int) -> list[dict]:
        """Clean blueprint-copy offers for one blueprint type in a region.

        Returns item-exchange contracts whose ONLY included item is a single
        BPC of `blueprint_type_id` (no junk bundles, so price == BPC price),
        not yet expired, cheapest per run first. Each row: {contract_id,
        price, runs, material_efficiency, time_efficiency,
        start_location_id, date_expired}.
        """
        c = self._conn()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = c.execute(
            """
            SELECT cl.contract_id, cl.price,
                   COALESCE(ci.runs, 1) AS runs,
                   ci.material_efficiency, ci.time_efficiency,
                   cl.start_location_id, cl.date_expired
            FROM contract_list cl
            JOIN contract_items ci ON ci.contract_id = cl.contract_id
            WHERE cl.region_id = ?
              AND cl.type = 'item_exchange'
              AND cl.price > 0
              AND ci.type_id = ?
              AND ci.is_included = 1
              AND ci.is_blueprint_copy = 1
              AND (cl.date_expired IS NULL OR cl.date_expired > ?)
              AND (SELECT COUNT(*) FROM contract_items ci2
                   WHERE ci2.contract_id = cl.contract_id
                     AND ci2.is_included = 1) = 1
            ORDER BY cl.price / COALESCE(ci.runs, 1)
            """,
            (int(region_id), int(blueprint_type_id), now_iso),
        ).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # id -> name cache (issuers / corporations via /universe/names/)
    # =========================================================================

    def get_names(self, ids) -> dict[int, str]:
        """Bulk id -> name lookup; missing ids simply absent from the result."""
        ids = [int(x) for x in ids]
        if not ids:
            return {}
        c = self._conn()
        placeholders = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT id, name FROM id_names WHERE id IN ({placeholders})", ids
        ).fetchall()
        return {int(r["id"]): r["name"] for r in rows}

    def store_names(self, mapping: dict[int, str],
                    category: Optional[str] = None) -> None:
        """Cache id -> name resolutions (effectively permanent)."""
        if not mapping:
            return
        now = datetime.now(timezone.utc).isoformat()
        prepared = [(int(i), str(n), category, now) for i, n in mapping.items() if n]
        if not prepared:
            return
        try:
            c = self._conn()
            with c:
                c.executemany(
                    "INSERT INTO id_names (id, name, category, resolved_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET "
                    "  name = excluded.name, "
                    "  category = excluded.category, "
                    "  resolved_at = excluded.resolved_at",
                    prepared,
                )
            logger.debug("[ContractDiag] cached %d id->name resolutions",
                         len(prepared))
        except Exception:
            logger.exception("[ContractDiag] store_names failed")

    def get_unresolved_ids(self, ids) -> list[int]:
        """Subset of `ids` not yet in the name cache — the resolve worklist."""
        ids = list({int(x) for x in ids})
        if not ids:
            return []
        known = self.get_names(ids)
        return [i for i in ids if i not in known]

    # =========================================================================
    # Stats (for the [ContractDiag] header / debug surfaces)
    # =========================================================================

    def get_stats(self) -> dict:
        c = self._conn()
        contracts = c.execute("SELECT COUNT(*) FROM contract_list").fetchone()[0]
        fetched = c.execute(
            "SELECT COUNT(*) FROM contract_list WHERE items_fetched = 1"
        ).fetchone()[0]
        item_rows = c.execute("SELECT COUNT(*) FROM contract_items").fetchone()[0]
        names = c.execute("SELECT COUNT(*) FROM id_names").fetchone()[0]
        scopes = c.execute("SELECT COUNT(*) FROM scope_freshness").fetchone()[0]
        return {
            "contracts": int(contracts or 0),
            "items_fetched": int(fetched or 0),
            "item_records": int(item_rows or 0),
            "names_cached": int(names or 0),
            "scopes_tracked": int(scopes or 0),
        }


# =============================================================================
# Small parsing helpers (mirror structure_history's defensive coercion)
# =============================================================================

def _as_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _as_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO timestamp to an aware UTC datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None
