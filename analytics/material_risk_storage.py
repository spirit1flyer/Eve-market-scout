"""Persistent storage for material risk filter results.

The material filter (TBC correlation analysis) classifies items as
'low', 'medium', or 'skip' per (type_id, region_id). These results
were previously held only in an in-memory cache and lost on app restart,
forcing a full rerun of the filter on every launch.

This module persists those results to stock_profiles.db with a
computed_date stamp, so:
  - The in-memory cache is repopulated at startup from today's rows
  - MaterialFilterTracker can detect "already ran today" across launches
  - Old rows are pruned after a few days

Schema:
    CREATE TABLE material_risk_cache (
        type_id INTEGER NOT NULL,
        region_id INTEGER NOT NULL,
        classification TEXT NOT NULL,    -- 'low' | 'medium' | 'skip'
        computed_date TEXT NOT NULL,     -- YYYY-MM-DD
        PRIMARY KEY (type_id, region_id)
    );

Connections are short-lived per call. WAL mode is enabled so this
coexists safely with profile reads/writes.
"""

import sqlite3
from datetime import date, timedelta
from typing import Dict, Tuple, Optional

from core.sound_manager import get_data_dir


# Path matches historical_profiles.PROFILES_DB
PROFILES_DB = str(get_data_dir() / "stock_profiles.db")

# Retention window for old cache rows (days)
RETENTION_DAYS = 7


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection with sane defaults."""
    conn = sqlite3.connect(PROFILES_DB, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    """Create material_risk_cache table if it doesn't exist.

    Idempotent — safe to call on every startup.
    """
    try:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS material_risk_cache (
                    type_id INTEGER NOT NULL,
                    region_id INTEGER NOT NULL,
                    classification TEXT NOT NULL,
                    computed_date TEXT NOT NULL,
                    PRIMARY KEY (type_id, region_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mrc_date_region
                ON material_risk_cache(computed_date, region_id)
            """)
            conn.commit()
            print("[MaterialRiskStorage] Table ready")
        finally:
            conn.close()
    except Exception as e:
        print(f"[MaterialRiskStorage] init_table error: {e}")


def save_entry(type_id: int, region_id: int, classification: str) -> None:
    """Save (or replace) a single classification entry with today's date."""
    today = date.today().strftime("%Y-%m-%d")
    try:
        conn = _connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO material_risk_cache
                    (type_id, region_id, classification, computed_date)
                VALUES (?, ?, ?, ?)
            """, (type_id, region_id, classification, today))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[MaterialRiskStorage] save_entry error "
              f"(type={type_id}, region={region_id}): {e}")


def save_batch(entries) -> int:
    """Save many (type_id, region_id, classification) tuples in one
    transaction. Returns count saved.

    Mirrors leading_indicators_storage.save_batch — one connection,
    one BEGIN/COMMIT, executemany. Avoids per-row connection-open and
    per-row commit overhead, which is significant on Windows WAL.
    """
    today = date.today().strftime("%Y-%m-%d")
    if not entries:
        return 0
    rows = [(type_id, region_id, classification, today)
            for (type_id, region_id, classification) in entries]
    try:
        conn = _connect()
        try:
            with conn:
                conn.executemany("""
                    INSERT OR REPLACE INTO material_risk_cache
                        (type_id, region_id, classification, computed_date)
                    VALUES (?, ?, ?, ?)
                """, rows)
        finally:
            conn.close()
        print(f"[MaterialRiskStorage] Saved {len(rows)} entries for {today}")
        return len(rows)
    except Exception as e:
        print(f"[MaterialRiskStorage] save_batch error: {e}")
        return 0


def _fresh_cutoff() -> str:
    # Today OR yesterday — matches the tracker's 24h rolling window across
    # midnight rollover. PK is (type_id, region_id) so there's only ever one
    # row per item; cutoff just bounds how stale it can be.
    return (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")


def has_today_data(region_id: int) -> bool:
    """Return True if recent (<=1 day old) cached rows exist for region."""
    cutoff = _fresh_cutoff()
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                SELECT 1 FROM material_risk_cache
                WHERE region_id = ? AND computed_date >= ?
                LIMIT 1
            """, (region_id, cutoff))
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        print(f"[MaterialRiskStorage] has_today_data error "
              f"(region={region_id}): {e}")
        return False


def load_all_today() -> Dict[Tuple[int, int], str]:
    """Load recent (<=1 day old) rows.

    Returns dict shaped like _material_risk_cache:
        {(type_id, region_id): classification}
    """
    cutoff = _fresh_cutoff()
    result: Dict[Tuple[int, int], str] = {}
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                SELECT type_id, region_id, classification
                FROM material_risk_cache
                WHERE computed_date >= ?
            """, (cutoff,))
            for row in cur:
                result[(row["type_id"], row["region_id"])] = row["classification"]
        finally:
            conn.close()
        print(f"[MaterialRiskStorage] Loaded {len(result)} entries (since {cutoff})")
    except Exception as e:
        print(f"[MaterialRiskStorage] load_all_today error: {e}")
    return result


def delete_today_for_region(region_id: int) -> int:
    """Delete recent (<=1 day old) rows for a specific region.

    Called when the in-memory cache for a region is being cleared so
    the next has_today_data() check correctly reports False.

    Returns number of rows deleted.
    """
    cutoff = _fresh_cutoff()
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                DELETE FROM material_risk_cache
                WHERE region_id = ? AND computed_date >= ?
            """, (region_id, cutoff))
            conn.commit()
            deleted = cur.rowcount
            print(f"[MaterialRiskStorage] Deleted {deleted} rows for "
                  f"region {region_id} (since {cutoff})")
            return deleted
        finally:
            conn.close()
    except Exception as e:
        print(f"[MaterialRiskStorage] delete_today_for_region error "
              f"(region={region_id}): {e}")
        return 0


def purge_before(cutoff: Optional[date] = None) -> int:
    """Delete rows older than cutoff date (default: today - RETENTION_DAYS).

    Returns number of rows deleted.
    """
    if cutoff is None:
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                DELETE FROM material_risk_cache
                WHERE computed_date < ?
            """, (cutoff_str,))
            conn.commit()
            deleted = cur.rowcount
            if deleted > 0:
                print(f"[MaterialRiskStorage] Purged {deleted} rows "
                      f"older than {cutoff_str}")
            return deleted
        finally:
            conn.close()
    except Exception as e:
        print(f"[MaterialRiskStorage] purge_before error: {e}")
        return 0
