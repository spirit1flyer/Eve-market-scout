"""Durable Jita blueprint asking-price snapshots and persistent overrides.

Separate from the live contract cache: pruning a vanished contract never
deletes its historical observations. No network or Tk calls live here.
"""

import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path

from core.config import JITA_REGION_ID, JITA_STATION_ID
from core.sound_manager import get_data_dir

DAY = 86400
_singleton = None
_singleton_lock = threading.Lock()


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def read_cached_offers(contracts_db, blueprint_ids, excluded_ids=()):
    """Read one consistent cache view; reject mixed bundles and item trades."""
    uri = Path(contracts_db.db_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        rows = conn.execute("""
            SELECT cl.contract_id, cl.price, cl.date_expired,
                   cl.start_location_id AS location_id, cl.region_id,
                   ci.type_id AS blueprint_id, ci.is_blueprint_copy AS is_copy,
                   ci.quantity, ci.runs, ci.material_efficiency AS me,
                   ci.time_efficiency AS te
            FROM contract_list cl JOIN contract_items ci USING(contract_id)
            WHERE cl.region_id=? AND cl.start_location_id=?
              AND cl.type='item_exchange' AND cl.items_fetched=1 AND cl.price>0
              AND ci.is_included=1
              AND (SELECT COUNT(*) FROM contract_items i
                   WHERE i.contract_id=cl.contract_id AND i.is_included=1)=1
              AND NOT EXISTS(SELECT 1 FROM contract_items i
                   WHERE i.contract_id=cl.contract_id AND i.is_included=0)
        """, (JITA_REGION_ID, JITA_STATION_ID)).fetchall()
        fresh = conn.execute(
            "SELECT last_fetched FROM scope_freshness WHERE scope_key=?",
            (f"region:{JITA_REGION_ID}",)).fetchone()
    checked = timestamp(fresh[0]) if fresh else None
    excluded_ids = set(excluded_ids)
    return [dict(row) for row in rows if row["blueprint_id"] in blueprint_ids
            and row["contract_id"] not in excluded_ids], checked


class BlueprintPriceStore:
    def __init__(self, path=None, clock=time.time):
        self.path = Path(path) if path else get_data_dir() / "industry_blueprint_prices.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self._lock = threading.RLock()
        self._listeners = []
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, observed_at REAL NOT NULL,
                    source_checked_at REAL, complete INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS offers (
                    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
                    contract_id INTEGER NOT NULL, blueprint_id INTEGER NOT NULL,
                    is_copy INTEGER NOT NULL, price REAL NOT NULL,
                    quantity INTEGER NOT NULL, runs INTEGER, me INTEGER, te INTEGER,
                    location_id INTEGER NOT NULL, region_id INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY(snapshot_id, contract_id));
                CREATE INDEX IF NOT EXISTS offer_blueprint
                    ON offers(snapshot_id, blueprint_id, is_copy);
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS overrides (
                    blueprint_id INTEGER NOT NULL, is_copy INTEGER NOT NULL,
                    price REAL NOT NULL, runs INTEGER, me INTEGER, te INTEGER,
                    PRIMARY KEY(blueprint_id, is_copy));
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=20)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def subscribe(self, callback):
        self._listeners.append(callback)

    def notify(self):
        for callback in tuple(self._listeners):
            try:
                callback()
            except Exception as exc:
                print(f"[BlueprintPrices] notification failed: {exc}")

    def setting(self, key, default=None):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self._lock, self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def retention(self):
        return self.setting("retention_days", 30)

    def set_retention(self, days):
        if days is not None and (isinstance(days, bool) or int(days) < 7):
            raise ValueError("Keep at least seven days, or keep forever")
        self.set_setting("retention_days", None if days is None else int(days))
        self.prune()

    def prune(self):
        days = self.retention()
        if days is None:
            return
        with self._lock, self.connect() as conn:
            # Keep the last usable snapshot even if it is older than retention.
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM snapshots WHERE observed_at<? AND id!=(SELECT MAX(id) FROM snapshots)",
                (self.clock() - days * DAY,))]
            for sid in ids:
                conn.execute("DELETE FROM offers WHERE snapshot_id=?", (sid,))
                conn.execute("DELETE FROM snapshots WHERE id=?", (sid,))

    def latest(self):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def due(self):
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(observed_at) FROM snapshots WHERE complete=1").fetchone()
        return ((row[0] is None or now - row[0] >= DAY)
                and now >= self.setting("retry_after", 0))

    def save_snapshot(self, rows, source_checked_at, complete=False):
        now = self.clock()
        clean = []
        for row in rows:
            expires = timestamp(row.get("date_expired"))
            try:
                price = float(row.get("price") or 0)
                quantity = int(row.get("quantity") or 0)
            except (ValueError, TypeError, OverflowError):
                continue
            if not expires or expires <= now or not math.isfinite(price) or price <= 0 or quantity <= 0:
                continue
            if row["is_copy"] and quantity != 1:
                continue
            me, te = row.get("me"), row.get("te")
            me = me if isinstance(me, (int, float)) and 0 <= me <= 10 else None
            te = te if isinstance(te, (int, float)) and 0 <= te <= 20 else None
            clean.append((int(row["contract_id"]), int(row["blueprint_id"]),
                          int(bool(row["is_copy"])), price, quantity, row.get("runs"),
                          me, te, int(row["location_id"]),
                          int(row["region_id"]), expires))
        with self._lock, self.connect() as conn:
            sid = conn.execute(
                "INSERT INTO snapshots(observed_at,source_checked_at,complete) VALUES (?,?,?)",
                (now, source_checked_at, int(complete))).lastrowid
            conn.executemany("INSERT INTO offers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             [(sid,) + row for row in clean])
        try:
            self.prune()
        except sqlite3.Error as exc:
            print(f"[BlueprintPrices] retention cleanup failed; snapshot remains saved: {exc}")
        print(f"[BlueprintPrices] snapshot id={sid} offers={len(clean)} "
              f"rejected={len(rows) - len(clean)} complete={complete} "
              f"source_checked_at={source_checked_at} retention={self.retention()}")
        self.notify()
        return sid

    def offers(self, blueprint_id=None, is_copy=None):
        clauses = ["o.snapshot_id=(SELECT MAX(id) FROM snapshots)", "o.expires_at>?"]
        params = [self.clock()]
        if blueprint_id is not None:
            clauses.append("o.blueprint_id=?")
            params.append(int(blueprint_id))
        if is_copy is not None:
            clauses.append("o.is_copy=?")
            params.append(int(is_copy))
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT o.*, s.observed_at,s.source_checked_at FROM offers o "
                "JOIN snapshots s ON s.id=o.snapshot_id WHERE " + " AND ".join(clauses), params).fetchall()
        return [dict(row) for row in rows]

    def overrides(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM overrides")]

    def set_override(self, blueprint_id, price, runs=None, me=None, te=None, is_copy=True):
        if not math.isfinite(float(price)) or price < 0:
            raise ValueError("Price must be a finite nonnegative number")
        if price > 0 and is_copy and (runs is None or int(runs) < 1):
            raise ValueError("A copy override needs a positive run count")
        if me is not None and not 0 <= me <= 10:
            raise ValueError("ME must be between 0 and 10")
        if te is not None and not 0 <= te <= 20:
            raise ValueError("TE must be between 0 and 20")
        with self._lock, self.connect() as conn:
            key = (int(blueprint_id), int(is_copy))
            if price == 0:
                conn.execute("DELETE FROM overrides WHERE blueprint_id=? AND is_copy=?", key)
            else:
                conn.execute("INSERT OR REPLACE INTO overrides VALUES (?,?,?,?,?,?)",
                             key + (float(price), runs, me, te))
        self.notify()

    def migrate_overrides(self, settings_file):
        with self._lock:
            if self.setting("legacy_overrides_imported", False):
                return
            path = Path(settings_file)
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                if not isinstance(data, dict) or not isinstance(data.get("bpc_prices", {}), dict):
                    raise ValueError("invalid legacy settings shape")
            except (OSError, ValueError) as exc:
                print(f"[BlueprintPrices] legacy overrides not imported: {exc}")
                return
            # Import once: clearing an override must not resurrect its legacy entry.
            with self.connect() as conn:
                for bid, value in data.get("bpc_prices", {}).items():
                    try:
                        bid, price, runs = int(bid), float(value["price"]), int(value["runs"])
                    except (ValueError, TypeError, KeyError, OverflowError):
                        print("[BlueprintPrices] skipped invalid legacy override")
                        continue
                    if math.isfinite(price) and price > 0 and runs > 0:
                        conn.execute("INSERT OR IGNORE INTO overrides VALUES (?,?,?,?,?,?)",
                                     (int(bid), 1, price, runs, None, None))
                conn.execute("INSERT OR REPLACE INTO settings VALUES (?,?)",
                             ("legacy_overrides_imported", "true"))


def get_blueprint_price_store():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = BlueprintPriceStore()
        return _singleton
