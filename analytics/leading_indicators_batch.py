"""Batch leading indicators computation for Stock Market tab.

Stripped-down version of the audit-tool's leading_indicators.py that
returns just the verdict data (no log output) for use in the Stock
Market tab's risk panels.

Mirrors the same thresholds and flag logic so the audit tool and the
Stock Market column always agree.

Public API:
    compute_leading_indicators(type_id, region_id) -> LeadingIndicatorResult
        Run the full analysis for a single item. Returns a result with
        flag list, primary verdict, and is_warning boolean.

    is_promotion_warning(flags) -> bool
        Return True if any flag in the list triggers tier promotion
        (UNDERCUT SPIRAL or LIQUIDITY DRAIN).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import mean
from typing import List, Optional


# =============================================================================
# Tunable thresholds (mirror leading_indicators.py exactly)
# =============================================================================

TREND_FLAT_PCT = 5.0
TREND_STRONG_PCT = 20.0

SPREAD_NARROWING_RATIO = 0.85
SPREAD_WIDENING_RATIO = 1.15

COMPRESSION_TIGHT_RATIO = 0.7
COMPRESSION_LOOSE_RATIO = 1.3

# Volume regime gating - prevents LIQUIDITY_DRAIN and STEALTH_BLEED
# from firing on high-volume commodities like Tritanium where a 28%
# volume dip is a quiet weekend, not a market crisis. Compares last 7d
# total volume against rolling 7d windows from the last year.
REGIME_WINDOW_DAYS = 7
REGIME_LOOKBACK_DAYS = 365
REGIME_MIN_WINDOWS = 30
REGIME_CRITICAL_PERCENTILE = 10  # only fire drain flags below this

RECENT_WINDOW_DAYS = 30
PRIOR_WINDOW_DAYS = 30
SPREAD_BASELINE_DAYS = 60
COMPRESSION_RECENT_DAYS = 90
COMPRESSION_YEARLY_DAYS = 365

MIN_SPREAD_RECENT = 5
MIN_SPREAD_BASELINE = 10
MIN_COMPRESSION_RECENT = 20
MIN_COMPRESSION_YEARLY = 60

# The everef archive normally lags 1-4 days behind the wall clock. Beyond
# this many days the daily update is presumed broken and we log an error.
STALE_DB_ERROR_DAYS = 7


# Flags that trigger tier promotion (Low -> Med, Med -> High)
PROMOTION_FLAGS = {"UNDERCUT SPIRAL", "LIQUIDITY DRAIN"}

# All flags considered warnings (red/yellow indicator in UI)
WARNING_FLAGS = {
    "UNDERCUT SPIRAL",
    "LIQUIDITY DRAIN",
    "STEALTH BLEED",
    "DISTRIBUTION",
    "CAPITULATION",
}


# =============================================================================
# Result data class
# =============================================================================

@dataclass
class LeadingIndicatorResult:
    """Result of a leading indicators computation for one item."""
    type_id: int
    region_id: int
    flags: List[str] = field(default_factory=list)
    primary_verdict: str = "HEALTHY"
    is_warning: bool = False
    is_promotion: bool = False
    # Underlying labels (useful for debugging / future extension)
    price_label: str = "UNKNOWN"
    volume_label: str = "UNKNOWN"
    order_count_label: str = "UNKNOWN"
    spread_label: str = "UNKNOWN"
    compression_label: str = "UNKNOWN"

    def to_storage_dict(self) -> dict:
        """Serialize for storage in the leading_indicators DB table."""
        return {
            "type_id": self.type_id,
            "region_id": self.region_id,
            "flags": ",".join(self.flags),  # comma-joined, simpler than JSON
            "primary_verdict": self.primary_verdict,
            "is_warning": 1 if self.is_warning else 0,
            "is_promotion": 1 if self.is_promotion else 0,
            "price_label": self.price_label,
            "volume_label": self.volume_label,
            "order_count_label": self.order_count_label,
            "spread_label": self.spread_label,
            "compression_label": self.compression_label,
        }

    @classmethod
    def from_storage_row(cls, row) -> "LeadingIndicatorResult":
        """Build a result from a sqlite3.Row.

        flags column stored as comma-joined string. Empty string => no flags.
        """
        flags_str = row["flags"] or ""
        flags = [f for f in flags_str.split(",") if f]
        return cls(
            type_id=row["type_id"],
            region_id=row["region_id"],
            flags=flags,
            primary_verdict=row["primary_verdict"] or "HEALTHY",
            is_warning=bool(row["is_warning"]),
            is_promotion=bool(row["is_promotion"]),
            price_label=row["price_label"] or "UNKNOWN",
            volume_label=row["volume_label"] or "UNKNOWN",
            order_count_label=row["order_count_label"] or "UNKNOWN",
            spread_label=row["spread_label"] or "UNKNOWN",
            compression_label=row["compression_label"] or "UNKNOWN",
        )


# =============================================================================
# Public API
# =============================================================================

def get_reference_date(market_db=None, log=print) -> Optional[str]:
    """Return the history DB's latest date ('YYYY-MM-DD') for window anchoring.

    The everef archive lags 1-4 days behind the wall clock. Anchoring the
    30d/7d windows at date.today() leaves the missing days in ONLY the
    recent window, deflating it up to ~13% against a 5% STABLE threshold —
    systematic false FALLING verdicts. Anchor at the DB's latest date
    instead: days after it are "not gathered yet" (window slides back to
    stay a full 30 days), days before it with no row are genuine
    zero-trade days.

    Logs an error via log() if the DB has had no new data for more than
    STALE_DB_ERROR_DAYS — that means the daily archive update is broken.
    Returns None if the DB is unavailable/empty (callers fall back to the
    wall clock).
    """
    try:
        if market_db is None:
            from history.market_history import get_market_history_db
            market_db = get_market_history_db()
        latest = market_db.get_latest_date()
    except Exception as e:
        print(f"[LeadingIndicators] reference date lookup failed: {e}")
        return None

    if not latest:
        return None

    lag_days = (date.today() - date.fromisoformat(latest)).days
    if lag_days > STALE_DB_ERROR_DAYS:
        log(f"[LeadingIndicators] ERROR: market history DB has had no new "
            f"data in {lag_days} days (latest: {latest}). The daily archive "
            f"update appears broken — indicators are anchored at that date "
            f"and reflect the market as of then, not today.")
    return latest


def compute_leading_indicators(
    type_id: int,
    region_id: int,
    history: Optional[List[dict]] = None,
    reference_date: Optional[str] = None,
) -> Optional[LeadingIndicatorResult]:
    """Compute leading indicators for one (type_id, region_id) pair.

    Args:
        history: Pre-fetched history records (from get_full_history_bulk).
            When provided the DB fetch is skipped entirely.  Pass None to
            let this function fetch its own history (single-item path).
        reference_date: The history DB's latest date ('YYYY-MM-DD') —
            anchors all trend windows so the archive's 1-4 day lag doesn't
            deflate the recent side (see get_reference_date). Batch callers
            should resolve it once and pass it to every item. When None the
            single-item path resolves it itself; last resort is the wall
            clock.

    Returns None if no history data is available.
    """
    if history is None:
        from history.market_history import get_market_history_db
        try:
            market_db = get_market_history_db()
            history = market_db.get_full_history(region_id, type_id, years=3)
            if reference_date is None:
                reference_date = get_reference_date(market_db)
        except Exception as e:
            print(f"[LeadingIndicators] fetch error type={type_id} "
                  f"region={region_id}: {e}")
            return None

    if not history:
        return None

    today = (date.fromisoformat(reference_date) if reference_date
             else date.today())

    # Build date->record map
    by_date = {}
    for r in history:
        d = r.get("date")
        if d:
            by_date[d] = r

    # Run each subsection (silent versions)
    price_label = _calc_price_trend(by_date, today)
    volume_label = _calc_volume_trend(by_date, today)
    order_label = _calc_order_count_trend(by_date, today)
    spread_label = _calc_spread_trend(by_date, today)
    compression_label = _calc_range_compression(by_date, today)
    
    # Volume regime percentile - gates the two "liquidity is dying"
    # flags so they don't fire on commodities like Tritanium where
    # a 28% volume dip is a quiet weekend, not a crisis.
    regime_percentile = _calc_volume_regime_percentile(by_date, today)
    
    flags = _compute_divergence_flags(
        price_label, volume_label, order_label,
        spread_label, compression_label,
        regime_percentile,
    )

    # Pick primary verdict: first warning flag if any, else first flag,
    # else HEALTHY
    primary = "HEALTHY"
    if flags:
        warnings = [f for f in flags if f in WARNING_FLAGS]
        primary = warnings[0] if warnings else flags[0]

    is_warning = any(f in WARNING_FLAGS for f in flags)
    is_promotion = any(f in PROMOTION_FLAGS for f in flags)

    return LeadingIndicatorResult(
        type_id=type_id,
        region_id=region_id,
        flags=flags,
        primary_verdict=primary,
        is_warning=is_warning,
        is_promotion=is_promotion,
        price_label=price_label,
        volume_label=volume_label,
        order_count_label=order_label,
        spread_label=spread_label,
        compression_label=compression_label,
    )


def is_promotion_warning(flags: List[str]) -> bool:
    """Return True if the flag list contains any promotion-triggering flag."""
    return any(f in PROMOTION_FLAGS for f in flags)


def is_any_warning(flags: List[str]) -> bool:
    """Return True if the flag list contains any warning flag."""
    return any(f in WARNING_FLAGS for f in flags)


# =============================================================================
# Internal helpers (silent versions of audit subsections)
# =============================================================================

def _window_dates(today: date, days_back_start: int, days_back_end: int):
    """Return list of date strings for offsets [start, end)."""
    out = []
    for offset in range(days_back_start, days_back_end):
        out.append((today - timedelta(days=offset)).isoformat())
    return out


def _calc_volume_regime_percentile(by_date: dict, today: date):
    """Where does the last 7d total volume rank against historical
    7d rolling windows from the past year?
    
    Mirrors the audit-tool Section 6 'Volume Regime' calc.
    
    Returns int 0-100 (percentile rank), or None if insufficient
    history. A percentile of 10 means "the last 7 days saw lower
    volume than 90% of historical 7-day windows" - genuine outlier
    territory.
    """
    # Last 7d total
    recent_total = sum(
        (by_date.get(d) or {}).get("volume", 0)
        for d in _window_dates(today, 0, REGIME_WINDOW_DAYS)
    )
    if recent_total <= 0:
        return None
    
    # Build all historical 7d rolling windows. Start from 7d ago
    # (excluding the recent window itself) out to 1 year back.
    windows = []
    for start_offset in range(
        REGIME_WINDOW_DAYS,
        REGIME_LOOKBACK_DAYS,
    ):
        total = sum(
            (by_date.get(d) or {}).get("volume", 0)
            for d in _window_dates(
                today,
                start_offset,
                start_offset + REGIME_WINDOW_DAYS,
            )
        )
        if total > 0:
            windows.append(total)
    
    if len(windows) < REGIME_MIN_WINDOWS:
        return None
    
    below = sum(1 for w in windows if w < recent_total)
    return int(below / len(windows) * 100)


def _trend_label(pct_change: float) -> str:
    if abs(pct_change) <= TREND_FLAT_PCT:
        return "STABLE"
    return "RISING" if pct_change > 0 else "FALLING"


def _iqr(vals):
    s = sorted(vals)
    n = len(s)
    p25_idx = int(n * 0.25)
    p75_idx = int(n * 0.75)
    if p75_idx >= n:
        p75_idx = n - 1
    return s[p75_idx] - s[p25_idx]


def _calc_price_trend(by_date: dict, today: date) -> str:
    recent = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior = _window_dates(today, RECENT_WINDOW_DAYS,
                          RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS)

    def lows(date_list):
        out = []
        for d in date_list:
            r = by_date.get(d)
            if r:
                lo = r.get("lowest", 0)
                if lo > 0:
                    out.append(lo)
        return out

    recent_lows = lows(recent)
    prior_lows = lows(prior)
    if not recent_lows or not prior_lows:
        return "UNKNOWN"

    prior_mean = mean(prior_lows)
    if prior_mean <= 0:
        return "UNKNOWN"

    pct = (mean(recent_lows) - prior_mean) / prior_mean * 100
    return _trend_label(pct)


def _calc_volume_trend(by_date: dict, today: date) -> str:
    recent = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior = _window_dates(today, RECENT_WINDOW_DAYS,
                          RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS)

    recent_vol = sum((by_date.get(d) or {}).get("volume", 0) for d in recent)
    prior_vol = sum((by_date.get(d) or {}).get("volume", 0) for d in prior)

    # Calendar-day denominators (do NOT use record count - ESI omits
    # zero-volume days)
    recent_mean = recent_vol / RECENT_WINDOW_DAYS
    prior_mean = prior_vol / PRIOR_WINDOW_DAYS

    if prior_mean <= 0:
        return "UNKNOWN"

    pct = (recent_mean - prior_mean) / prior_mean * 100
    return _trend_label(pct)


def _calc_order_count_trend(by_date: dict, today: date) -> str:
    recent = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior = _window_dates(today, RECENT_WINDOW_DAYS,
                          RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS)

    recent_oc = sum(
        (by_date.get(d) or {}).get("order_count", 0) for d in recent
    )
    prior_oc = sum(
        (by_date.get(d) or {}).get("order_count", 0) for d in prior
    )

    recent_mean = recent_oc / RECENT_WINDOW_DAYS
    prior_mean = prior_oc / PRIOR_WINDOW_DAYS

    if prior_mean <= 0:
        return "UNKNOWN"

    pct = (recent_mean - prior_mean) / prior_mean * 100
    return _trend_label(pct)


def _calc_spread_trend(by_date: dict, today: date) -> str:
    recent = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    baseline = _window_dates(today, RECENT_WINDOW_DAYS,
                             RECENT_WINDOW_DAYS + SPREAD_BASELINE_DAYS)

    def daily_spread(d_str):
        r = by_date.get(d_str)
        if not r:
            return None
        avg = r.get("average", 0)
        hi = r.get("highest", 0)
        lo = r.get("lowest", 0)
        if avg <= 0 or hi <= 0 or lo <= 0:
            return None
        return (hi - lo) / avg

    recent_spreads = [
        s for s in (daily_spread(d) for d in recent) if s is not None
    ]
    baseline_spreads = [
        s for s in (daily_spread(d) for d in baseline) if s is not None
    ]

    if (len(recent_spreads) < MIN_SPREAD_RECENT or
            len(baseline_spreads) < MIN_SPREAD_BASELINE):
        return "UNKNOWN"

    baseline_mean = mean(baseline_spreads)
    if baseline_mean <= 0:
        return "UNKNOWN"

    ratio = mean(recent_spreads) / baseline_mean
    if ratio <= SPREAD_NARROWING_RATIO:
        return "NARROWING"
    if ratio >= SPREAD_WIDENING_RATIO:
        return "WIDENING"
    return "STABLE"


def _calc_range_compression(by_date: dict, today: date) -> str:
    recent = _window_dates(today, 0, COMPRESSION_RECENT_DAYS)
    yearly = _window_dates(today, 0, COMPRESSION_YEARLY_DAYS)

    def lowest_values(date_list):
        out = []
        for d in date_list:
            r = by_date.get(d)
            if r:
                lo = r.get("lowest", 0)
                if lo > 0:
                    out.append(lo)
        return out

    recent_lows = lowest_values(recent)
    yearly_lows = lowest_values(yearly)

    if (len(recent_lows) < MIN_COMPRESSION_RECENT or
            len(yearly_lows) < MIN_COMPRESSION_YEARLY):
        return "UNKNOWN"

    yearly_iqr = _iqr(yearly_lows)
    if yearly_iqr <= 0:
        return "UNKNOWN"

    recent_iqr = _iqr(recent_lows)
    ratio = recent_iqr / yearly_iqr

    if ratio <= COMPRESSION_TIGHT_RATIO:
        return "COMPRESSING"
    if ratio >= COMPRESSION_LOOSE_RATIO:
        return "EXPANDING"
    return "NORMAL"


def _compute_divergence_flags(
    price, volume, order_count, spread, compression,
    regime_percentile=None,
):
    """Return list of flag tags only (no descriptions, those live in help UI).
    
    Mirrors the audit_tool's compute_divergence_flags but returns just tags.
    
    regime_percentile gates the two "liquidity is dying" flags
    (STEALTH BLEED, LIQUIDITY DRAIN) so they only fire when the item's
    own recent volume is genuinely an outlier (<= 10th percentile of
    its yearly history). Behavioral flags (UNDERCUT SPIRAL etc) are
    not gated since their diagnostic value scales correctly with item
    volume.
    """
    flags = []
    
    # Drain flags only fire when volume is in critical-outlier territory.
    # If regime data is unavailable (new item, sparse history), default
    # to the old behavior - drain flags can fire.
    allow_drain_flags = (
        regime_percentile is None
        or regime_percentile <= REGIME_CRITICAL_PERCENTILE
    )

    if volume == "FALLING" and order_count == "RISING":
        flags.append("UNDERCUT SPIRAL")

    if (allow_drain_flags
            and price == "STABLE" and volume == "FALLING"):
        flags.append("STEALTH BLEED")

    if price == "FALLING" and volume == "RISING":
        flags.append("CAPITULATION")

    if (price == "STABLE" and volume == "RISING"
            and compression == "COMPRESSING"):
        flags.append("ACCUMULATION")

    if (compression == "COMPRESSING" and volume == "STABLE"
            and price == "STABLE"):
        flags.append("COILED")

    if price == "RISING" and volume == "FALLING":
        flags.append("DISTRIBUTION")

    if (allow_drain_flags
            and spread == "WIDENING" and volume == "FALLING"):
        flags.append("LIQUIDITY DRAIN")

    if compression == "EXPANDING" and volume == "RISING":
        flags.append("BREAKOUT SETUP")

    return flags
