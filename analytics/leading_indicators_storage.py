"""Persistent storage for leading indicators results.

The leading indicators batch (volume/order/price/spread/compression
trend analysis) classifies items per (type_id, region_id) into a set
of divergence flags. These results are persisted to stock_profiles.db
so the heavy compute runs once per day per region, not per app launch.

Mirrors material_risk_storage.py architecture exactly.

Schema:
    CREATE TABLE leading_indicators (
        type_id INTEGER NOT NULL,
        region_id INTEGER NOT NULL,
        flags TEXT NOT NULL,              -- comma-joined, e.g. "UNDERCUT SPIRAL,STEALTH BLEED"
        primary_verdict TEXT NOT NULL,    -- e.g. "HEALTHY" or "UNDERCUT SPIRAL"
        is_warning INTEGER NOT NULL,      -- 0 or 1
        is_promotion INTEGER NOT NULL,    -- 0 or 1 (auto-promote tier?)
        price_label TEXT,
        volume_label TEXT,
        order_count_label TEXT,
        spread_label TEXT,
        compression_label TEXT,
        computed_date TEXT NOT NULL,      -- YYYY-MM-DD
        PRIMARY KEY (type_id, region_id)
    );
"""

import sqlite3
from datetime import date, timedelta
from typing import Dict, Optional, Tuple

from core.sound_manager import get_data_dir
from analytics.leading_indicators_batch import LeadingIndicatorResult


PROFILES_DB = str(get_data_dir() / "stock_profiles.db")
RETENTION_DAYS = 7


def _connect() -> sqlite3.Connection:
    """Open a short-lived connection with sane defaults."""
    conn = sqlite3.connect(PROFILES_DB, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_table() -> None:
    """Create leading_indicators table if it doesn't exist.

    Idempotent - safe to call on every startup.
    """
    try:
        conn = _connect()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leading_indicators (
                    type_id INTEGER NOT NULL,
                    region_id INTEGER NOT NULL,
                    flags TEXT NOT NULL,
                    primary_verdict TEXT NOT NULL,
                    is_warning INTEGER NOT NULL,
                    is_promotion INTEGER NOT NULL,
                    price_label TEXT,
                    volume_label TEXT,
                    order_count_label TEXT,
                    spread_label TEXT,
                    compression_label TEXT,
                    computed_date TEXT NOT NULL,
                    PRIMARY KEY (type_id, region_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_li_date_region
                ON leading_indicators(computed_date, region_id)
            """)
            conn.commit()
            print("[LeadingIndicatorsStorage] Table ready")
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] init_table error: {e}")


def save_entry(result: LeadingIndicatorResult) -> None:
    """Save (or replace) one result with today's date."""
    today = date.today().strftime("%Y-%m-%d")
    d = result.to_storage_dict()
    try:
        conn = _connect()
        try:
            conn.execute("""
                INSERT OR REPLACE INTO leading_indicators
                    (type_id, region_id, flags, primary_verdict,
                     is_warning, is_promotion,
                     price_label, volume_label, order_count_label,
                     spread_label, compression_label,
                     computed_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                d["type_id"], d["region_id"], d["flags"],
                d["primary_verdict"], d["is_warning"], d["is_promotion"],
                d["price_label"], d["volume_label"], d["order_count_label"],
                d["spread_label"], d["compression_label"],
                today,
            ))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] save_entry error "
              f"(type={result.type_id}, region={result.region_id}): {e}")


def save_batch(results) -> int:
    """Save a list of results in one transaction. Returns count saved."""
    today = date.today().strftime("%Y-%m-%d")
    saved = 0
    try:
        conn = _connect()
        try:
            with conn:
                for result in results:
                    d = result.to_storage_dict()
                    conn.execute("""
                        INSERT OR REPLACE INTO leading_indicators
                            (type_id, region_id, flags, primary_verdict,
                             is_warning, is_promotion,
                             price_label, volume_label, order_count_label,
                             spread_label, compression_label,
                             computed_date)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        d["type_id"], d["region_id"], d["flags"],
                        d["primary_verdict"], d["is_warning"],
                        d["is_promotion"],
                        d["price_label"], d["volume_label"],
                        d["order_count_label"],
                        d["spread_label"], d["compression_label"],
                        today,
                    ))
                    saved += 1
        finally:
            conn.close()
        print(f"[LeadingIndicatorsStorage] Saved {saved} entries for {today}")
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] save_batch error: {e}")
    return saved


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
                SELECT 1 FROM leading_indicators
                WHERE region_id = ? AND computed_date >= ?
                LIMIT 1
            """, (region_id, cutoff))
            return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] has_today_data error "
              f"(region={region_id}): {e}")
        return False


def load_all_today() -> Dict[Tuple[int, int], LeadingIndicatorResult]:
    """Load all rows from the last day (today or yesterday) as a dict."""
    cutoff = _fresh_cutoff()
    result: Dict[Tuple[int, int], LeadingIndicatorResult] = {}
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                SELECT type_id, region_id, flags, primary_verdict,
                       is_warning, is_promotion,
                       price_label, volume_label, order_count_label,
                       spread_label, compression_label
                FROM leading_indicators
                WHERE computed_date >= ?
            """, (cutoff,))
            for row in cur:
                key = (row["type_id"], row["region_id"])
                result[key] = LeadingIndicatorResult.from_storage_row(row)
        finally:
            conn.close()
        print(f"[LeadingIndicatorsStorage] Loaded {len(result)} entries "
              f"(since {cutoff})")
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] load_all_today error: {e}")
    return result


def load_for_region(region_id: int) -> Dict[int, LeadingIndicatorResult]:
    """Load recent (<=1 day old) rows for a region keyed by type_id."""
    cutoff = _fresh_cutoff()
    result: Dict[int, LeadingIndicatorResult] = {}
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                SELECT type_id, region_id, flags, primary_verdict,
                       is_warning, is_promotion,
                       price_label, volume_label, order_count_label,
                       spread_label, compression_label
                FROM leading_indicators
                WHERE region_id = ? AND computed_date >= ?
            """, (region_id, cutoff))
            for row in cur:
                result[row["type_id"]] = (
                    LeadingIndicatorResult.from_storage_row(row)
                )
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] load_for_region error "
              f"(region={region_id}): {e}")
    return result


def delete_today_for_region(region_id: int) -> int:
    """Delete recent (<=1 day old) rows for a region. Returns rows deleted."""
    cutoff = _fresh_cutoff()
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                DELETE FROM leading_indicators
                WHERE region_id = ? AND computed_date >= ?
            """, (region_id, cutoff))
            conn.commit()
            deleted = cur.rowcount
            print(f"[LeadingIndicatorsStorage] Deleted {deleted} rows for "
                  f"region {region_id} (since {cutoff})")
            return deleted
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] delete_today_for_region error "
              f"(region={region_id}): {e}")
        return 0


def purge_before(cutoff: Optional[date] = None) -> int:
    """Delete rows older than cutoff (default: today - RETENTION_DAYS)."""
    if cutoff is None:
        cutoff = date.today() - timedelta(days=RETENTION_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    try:
        conn = _connect()
        try:
            cur = conn.execute("""
                DELETE FROM leading_indicators
                WHERE computed_date < ?
            """, (cutoff_str,))
            conn.commit()
            deleted = cur.rowcount
            if deleted > 0:
                print(f"[LeadingIndicatorsStorage] Purged {deleted} rows "
                      f"older than {cutoff_str}")
            return deleted
        finally:
            conn.close()
    except Exception as e:
        print(f"[LeadingIndicatorsStorage] purge_before error: {e}")
        return 0
