"""Per-structure observed market history (Phase 1 — silent collection).

CCP has no `/markets/structures/{id}/history/` endpoint. We snapshot the
order book on every structure scan and infer daily activity from snapshot
deltas so structure hubs can eventually feed the same safety checks NPC
hubs feed (velocity gates, ceiling caps, market-crashing flags).

Phase 1 scope: storage + collection only. No consumer reads `structure_daily`
yet — that arrives in later phases. The derived rollup is still built
incrementally on every snapshot so it's queryable when consumers wire in.

See `PLAN_structure_history.md` for the full design and phasing.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from core.sound_manager import get_data_dir

logger = logging.getLogger(__name__)

DB_FILENAME = "structure_history.db"
RETENTION_DAYS = 730  # 2 years

_SINGLETON: Optional["StructureHistoryDB"] = None
_SINGLETON_LOCK = threading.Lock()


class StructureHistoryDB:
    """SQLite singleton for per-structure observed history.

    Tables:
      structure_snapshots — one row per (structure, snapshot_at, order_id).
        The raw observation. Re-derivable rollups should always be rebuildable
        from this table.
      structure_daily — incremental per-day rollup. Columns map to the same
        shape `MarketHistoryDB.get_history` returns so a future dispatcher can
        feed the existing scanner safety checks with no per-call-site changes.
    """

    def __init__(self, db_path: Optional[Path] = None):
        if db_path is None:
            db_path = get_data_dir() / DB_FILENAME
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._ensure_schema()

    @classmethod
    def singleton(cls) -> "StructureHistoryDB":
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
            CREATE TABLE IF NOT EXISTS structure_snapshots (
                structure_id  INTEGER NOT NULL,
                snapshot_at   TEXT    NOT NULL,
                order_id      INTEGER NOT NULL,
                type_id       INTEGER NOT NULL,
                is_buy        INTEGER NOT NULL,
                price         REAL    NOT NULL,
                volume_remain INTEGER NOT NULL,
                issued        TEXT    NOT NULL,
                duration      INTEGER NOT NULL,
                PRIMARY KEY (structure_id, snapshot_at, order_id)
            );
            CREATE INDEX IF NOT EXISTS idx_struct_type_time
                ON structure_snapshots (structure_id, type_id, snapshot_at);
            CREATE INDEX IF NOT EXISTS idx_struct_order
                ON structure_snapshots (structure_id, order_id, snapshot_at);

            CREATE TABLE IF NOT EXISTS structure_daily (
                structure_id     INTEGER NOT NULL,
                type_id          INTEGER NOT NULL,
                day_utc          TEXT    NOT NULL,
                volume_sold      INTEGER NOT NULL DEFAULT 0,
                sales_count      INTEGER NOT NULL DEFAULT 0,
                price_min        REAL,
                price_max        REAL,
                price_qty_sum    REAL    NOT NULL DEFAULT 0,
                snapshots_in_day INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (structure_id, type_id, day_utc)
            );
        """)
        c.commit()

    # =========================================================================
    # Public write entry point
    # =========================================================================

    def record_snapshot(self, structure_id: int, orders: list[dict],
                        now_utc: Optional[datetime] = None) -> None:
        """Persist a snapshot and incrementally update the daily rollup.

        Empty `orders` is skipped — treated as no-data / no-access rather than
        a genuine empty market. Better to lose one rare all-clear day than to
        attribute false fills on every 403.

        All exceptions are caught and logged; collection must never break the
        scanner's order-fetch path.
        """
        if not orders:
            return
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        try:
            self._record_snapshot_inner(structure_id, orders, now_utc)
        except Exception:
            logger.exception("[StructHist] record_snapshot failed for %s",
                             structure_id)

    def _record_snapshot_inner(self, structure_id: int, orders: list[dict],
                               now_utc: datetime) -> None:
        snapshot_at = now_utc.isoformat()
        conn = self._conn()

        prior_at = conn.execute(
            "SELECT MAX(snapshot_at) FROM structure_snapshots "
            "WHERE structure_id = ? AND snapshot_at < ?",
            (structure_id, snapshot_at),
        ).fetchone()[0]

        rows = []
        for o in orders:
            if "order_id" not in o or "type_id" not in o:
                continue
            try:
                rows.append((
                    structure_id,
                    snapshot_at,
                    int(o["order_id"]),
                    int(o["type_id"]),
                    1 if o.get("is_buy_order") else 0,
                    float(o["price"]),
                    int(o["volume_remain"]),
                    str(o.get("issued", "")),
                    int(o.get("duration", 0)),
                ))
            except (TypeError, ValueError):
                continue

        if not rows:
            return

        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO structure_snapshots "
                "(structure_id, snapshot_at, order_id, type_id, is_buy, "
                "price, volume_remain, issued, duration) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

            if prior_at is not None:
                self._infer_and_apply(conn, structure_id, prior_at, snapshot_at)

            cutoff_ts = (now_utc - timedelta(days=RETENTION_DAYS)).isoformat()
            cutoff_day = (now_utc - timedelta(days=RETENTION_DAYS)).date().isoformat()
            conn.execute(
                "DELETE FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at < ?",
                (structure_id, cutoff_ts),
            )
            conn.execute(
                "DELETE FROM structure_daily "
                "WHERE structure_id = ? AND day_utc < ?",
                (structure_id, cutoff_day),
            )

    # =========================================================================
    # Inference
    # =========================================================================

    def _infer_and_apply(self, conn: sqlite3.Connection, structure_id: int,
                         prior_at: str, current_at: str) -> None:
        """Compare prior↔current snapshots; apply inferred fills to daily.

        Rules (per `(structure_id, order_id)`):
          - volume_remain dropped by X → partial fill of X at order's price.
          - Present in prior, absent in current, BEFORE issued+duration →
            likely full fill of last-known volume_remain.
          - Present in prior, absent in current, AT/AFTER expiry → ignored
            (expired or cancelled — indistinguishable; not a fill).
          - Absent in prior, present in current → new listing (not a fill).
        """
        prior = {
            r["order_id"]: r
            for r in conn.execute(
                "SELECT order_id, type_id, price, volume_remain, issued, duration "
                "FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, prior_at),
            )
        }
        current_order_ids = {
            r[0]
            for r in conn.execute(
                "SELECT order_id FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, current_at),
            )
        }
        current_remain = {
            r["order_id"]: r["volume_remain"]
            for r in conn.execute(
                "SELECT order_id, volume_remain FROM structure_snapshots "
                "WHERE structure_id = ? AND snapshot_at = ?",
                (structure_id, current_at),
            )
        }

        try:
            current_dt = datetime.fromisoformat(current_at)
        except ValueError:
            return
        day_utc = current_dt.date().isoformat()

        touched_types: set[int] = set()

        for order_id, prior_row in prior.items():
            type_id = prior_row["type_id"]
            price = prior_row["price"]

            if order_id in current_order_ids:
                delta = prior_row["volume_remain"] - current_remain[order_id]
                if delta > 0:
                    self._apply_fill(conn, structure_id, type_id, day_utc,
                                     qty=delta, price=price)
                    touched_types.add(type_id)
            else:
                expiry = _parse_expiry(prior_row["issued"], prior_row["duration"])
                if expiry is not None and current_dt < expiry:
                    qty = prior_row["volume_remain"]
                    if qty > 0:
                        self._apply_fill(conn, structure_id, type_id, day_utc,
                                         qty=qty, price=price)
                        touched_types.add(type_id)
                # else: expired/ambiguous — don't count as a fill.

        for type_id in touched_types:
            conn.execute(
                "UPDATE structure_daily "
                "SET snapshots_in_day = snapshots_in_day + 1 "
                "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
                (structure_id, type_id, day_utc),
            )

    def _apply_fill(self, conn: sqlite3.Connection, structure_id: int,
                    type_id: int, day_utc: str, qty: int, price: float) -> None:
        existing = conn.execute(
            "SELECT price_min, price_max FROM structure_daily "
            "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
            (structure_id, type_id, day_utc),
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO structure_daily "
                "(structure_id, type_id, day_utc, volume_sold, sales_count, "
                " price_min, price_max, price_qty_sum, snapshots_in_day) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, 0)",
                (structure_id, type_id, day_utc, qty, price, price, qty * price),
            )
            return

        new_min = price if existing["price_min"] is None else min(existing["price_min"], price)
        new_max = price if existing["price_max"] is None else max(existing["price_max"], price)
        conn.execute(
            "UPDATE structure_daily SET "
            "  volume_sold = volume_sold + ?, "
            "  sales_count = sales_count + 1, "
            "  price_min = ?, "
            "  price_max = ?, "
            "  price_qty_sum = price_qty_sum + ? "
            "WHERE structure_id = ? AND type_id = ? AND day_utc = ?",
            (qty, new_min, new_max, qty * price,
             structure_id, type_id, day_utc),
        )


    # =========================================================================
    # Reader API (Phase 2 — for Browse Orders' history view)
    # =========================================================================

    def snapshot_count(self, structure_id: int) -> int:
        c = self._conn()
        row = c.execute(
            "SELECT COUNT(DISTINCT snapshot_at) FROM structure_snapshots "
            "WHERE structure_id = ?",
            (structure_id,),
        ).fetchone()
        return int(row[0] or 0)

    def days_observed(self, structure_id: int) -> int:
        """Distinct UTC days on which we have at least one snapshot.

        This is the gate for "do we have enough data to trust observed history
        over the regional proxy" — distinct from `days_with_fills` (the count
        of days that produced any inferred fill). A 30-day-old structure with
        a healthy 30 days of snapshots but only 4 days that traded should pass
        the gate; the scanner's existing 7d/30d math handles the thin-volume
        case naturally (calendar-day divide → low safe_velocity → filtered).
        """
        c = self._conn()
        row = c.execute(
            "SELECT COUNT(DISTINCT substr(snapshot_at, 1, 10)) "
            "FROM structure_snapshots WHERE structure_id = ?",
            (structure_id,),
        ).fetchone()
        return int(row[0] or 0)

    def get_structure_summary(self, structure_id: int) -> dict:
        """Top-of-tab header: snapshots, types seen, first/last observation,
        days with at least one inferred fill."""
        c = self._conn()
        row = c.execute(
            "SELECT COUNT(DISTINCT snapshot_at) AS snapshots, "
            "       COUNT(DISTINCT type_id)    AS types, "
            "       MIN(snapshot_at)           AS first_at, "
            "       MAX(snapshot_at)           AS last_at "
            "FROM structure_snapshots WHERE structure_id = ?",
            (structure_id,),
        ).fetchone()
        days = c.execute(
            "SELECT COUNT(DISTINCT day_utc) FROM structure_daily "
            "WHERE structure_id = ?",
            (structure_id,),
        ).fetchone()[0]
        return {
            "snapshots": int(row["snapshots"] or 0),
            "types_observed": int(row["types"] or 0),
            "first_at": row["first_at"],
            "last_at": row["last_at"],
            "days_with_fills": int(days or 0),
        }

    def get_history(self, structure_id: int, type_id: int,
                    days: int = 30) -> list[dict]:
        """Return per-day history rows for one item at one structure.

        Shape matches `MarketHistoryDB.get_history`:
          [{date, average, lowest, highest, volume, order_count}, ...]
        Newest-first. Only days with at least one inferred fill appear —
        matching NPC regional semantics (where `daily_history` likewise only
        carries rows for days that traded). The scanner's calendar-day
        averaging then handles thin-volume structures correctly.
        """
        c = self._conn()
        cutoff = (datetime.now(timezone.utc).date()
                  - timedelta(days=days)).isoformat()
        rows = c.execute(
            "SELECT day_utc, volume_sold, sales_count, price_min, price_max, "
            "       price_qty_sum "
            "FROM structure_daily "
            "WHERE structure_id = ? AND type_id = ? AND day_utc >= ? "
            "ORDER BY day_utc DESC",
            (structure_id, type_id, cutoff),
        ).fetchall()
        return [_daily_row_to_history_dict(r) for r in rows]

    def get_history_bulk(self, structure_id: int, type_ids: list[int],
                         days: int = 30) -> dict[int, list[dict]]:
        """Bulk variant of `get_history`. Missing items get empty lists,
        identical to `MarketHistoryDB.get_history_bulk` semantics so the
        downstream pipeline can't tell which source produced it.
        """
        if not type_ids:
            return {}
        c = self._conn()
        cutoff = (datetime.now(timezone.utc).date()
                  - timedelta(days=days)).isoformat()
        result: dict[int, list[dict]] = {int(tid): [] for tid in type_ids}
        placeholders = ",".join("?" * len(type_ids))
        rows = c.execute(
            f"SELECT type_id, day_utc, volume_sold, sales_count, price_min, "
            f"       price_max, price_qty_sum "
            f"FROM structure_daily "
            f"WHERE structure_id = ? AND type_id IN ({placeholders}) "
            f"AND day_utc >= ? "
            f"ORDER BY type_id, day_utc DESC",
            [structure_id, *type_ids, cutoff],
        ).fetchall()
        for r in rows:
            tid = int(r["type_id"])
            result[tid].append(_daily_row_to_history_dict(r))
        return result

    def get_items_observed(self, structure_id: int) -> list[dict]:
        """One entry per type_id ever seen at this structure.

        Includes items with zero inferred fills (presence-only) so the History
        tab can show "we saw this item but never inferred a sale" cases too.
        """
        c = self._conn()
        presence = {
            r["type_id"]: r["orders_seen"]
            for r in c.execute(
                "SELECT type_id, COUNT(DISTINCT order_id) AS orders_seen "
                "FROM structure_snapshots WHERE structure_id = ? "
                "GROUP BY type_id",
                (structure_id,),
            )
        }
        daily = {
            r["type_id"]: r
            for r in c.execute(
                "SELECT type_id, "
                "       COALESCE(SUM(volume_sold), 0) AS volume_sold, "
                "       COALESCE(SUM(sales_count), 0) AS sales_count, "
                "       MIN(price_min)                AS price_min, "
                "       MAX(price_max)                AS price_max, "
                "       COALESCE(SUM(price_qty_sum), 0) AS price_qty_sum, "
                "       COUNT(*) AS days_with_fills "
                "FROM structure_daily WHERE structure_id = ? "
                "GROUP BY type_id",
                (structure_id,),
            )
        }

        out: list[dict] = []
        for type_id, orders_seen in presence.items():
            d = daily.get(type_id)
            vol = int(d["volume_sold"]) if d else 0
            avg = (d["price_qty_sum"] / vol) if (d and vol) else None
            out.append({
                "type_id": int(type_id),
                "orders_seen": int(orders_seen),
                "days_with_fills": int(d["days_with_fills"]) if d else 0,
                "volume_sold": vol,
                "sales_count": int(d["sales_count"]) if d else 0,
                "price_min": d["price_min"] if d else None,
                "price_max": d["price_max"] if d else None,
                "avg_price": avg,
            })
        out.sort(key=lambda x: (-x["sales_count"], -x["volume_sold"], x["type_id"]))
        return out

    def get_event_trail(self, structure_id: int, type_id: int) -> list[dict]:
        """Replay snapshots for one item; return events newest-first.

        Each event: {at, kind, order_id, qty, price, issued}.
        Kinds: "listing" | "partial_fill" | "full_fill" | "expire".

        The first snapshot in the window emits no events (every order in it
        is just "first seen" — we don't know whether it was new or pre-existing).
        Inference rules match `_infer_and_apply` so the trail and the daily
        rollup never disagree.
        """
        c = self._conn()
        rows = c.execute(
            "SELECT snapshot_at, order_id, price, volume_remain, issued, duration "
            "FROM structure_snapshots "
            "WHERE structure_id = ? AND type_id = ? "
            "ORDER BY snapshot_at",
            (structure_id, type_id),
        ).fetchall()

        by_snap: dict[str, dict[int, sqlite3.Row]] = {}
        for r in rows:
            by_snap.setdefault(r["snapshot_at"], {})[r["order_id"]] = r

        snap_times = sorted(by_snap.keys())
        events: list[dict] = []

        for i, t in enumerate(snap_times):
            if i == 0:
                continue
            try:
                cur_dt = datetime.fromisoformat(t)
            except ValueError:
                continue
            prev = by_snap[snap_times[i - 1]]
            cur = by_snap[t]

            for oid, row in cur.items():
                if oid not in prev:
                    events.append({
                        "at": t,
                        "kind": "listing",
                        "order_id": int(oid),
                        "qty": int(row["volume_remain"]),
                        "price": float(row["price"]),
                        "issued": row["issued"],
                    })

            for oid, prev_row in prev.items():
                if oid in cur:
                    delta = prev_row["volume_remain"] - cur[oid]["volume_remain"]
                    if delta > 0:
                        events.append({
                            "at": t,
                            "kind": "partial_fill",
                            "order_id": int(oid),
                            "qty": int(delta),
                            "price": float(prev_row["price"]),
                            "issued": prev_row["issued"],
                        })
                else:
                    expiry = _parse_expiry(prev_row["issued"], prev_row["duration"])
                    if expiry is not None and cur_dt < expiry:
                        events.append({
                            "at": t,
                            "kind": "full_fill",
                            "order_id": int(oid),
                            "qty": int(prev_row["volume_remain"]),
                            "price": float(prev_row["price"]),
                            "issued": prev_row["issued"],
                        })
                    else:
                        events.append({
                            "at": t,
                            "kind": "expire",
                            "order_id": int(oid),
                            "qty": int(prev_row["volume_remain"]),
                            "price": float(prev_row["price"]),
                            "issued": prev_row["issued"],
                        })

        events.reverse()
        return events


def _daily_row_to_history_dict(r: sqlite3.Row) -> dict:
    """Translate a `structure_daily` row into MarketHistoryDB's get_history
    dict shape so the scanner's history pipeline can't tell sources apart."""
    vol = int(r["volume_sold"] or 0)
    qty_sum = float(r["price_qty_sum"] or 0.0)
    avg = (qty_sum / vol) if vol else 0.0
    return {
        "date": r["day_utc"],
        "average": avg,
        "lowest": float(r["price_min"]) if r["price_min"] is not None else 0.0,
        "highest": float(r["price_max"]) if r["price_max"] is not None else 0.0,
        "volume": vol,
        "order_count": int(r["sales_count"] or 0),
    }


def _parse_expiry(issued: str, duration: int) -> Optional[datetime]:
    """Return UTC expiry datetime, or None if unparseable."""
    if not issued:
        return None
    try:
        s = issued.replace("Z", "+00:00")
        issued_dt = datetime.fromisoformat(s)
        if issued_dt.tzinfo is None:
            issued_dt = issued_dt.replace(tzinfo=timezone.utc)
        return issued_dt + timedelta(days=int(duration))
    except (TypeError, ValueError):
        return None
