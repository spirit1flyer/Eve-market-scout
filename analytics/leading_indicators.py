"""Leading indicators analysis for EVE Market Scout audit tool.

Computes trend signals from 3-year market history to detect early shifts
in supply/demand dynamics:
  1. Volume trend (recent 30d mean vs prior 30d mean)
  2. Order count trend (recent 30d mean vs prior 30d mean)
  3. Spread trend (recent 30d mean spread vs prior 60d baseline)
  4. Range compression (recent 90d IQR vs full 365d IQR of lowest prices)
  5. Price trend (recent 30d mean lowest vs prior 30d mean lowest)
  6. Divergence flags - combinations that signal stealth bleeds, undercut
     spirals, capitulation, accumulation, coiled consolidation, etc.

Used by audit_tool.py to display leading indicators per item, after the
stability analysis section.
"""

from statistics import mean
from datetime import date, timedelta


# =============================================================================
# Tunable thresholds
# =============================================================================

# Trend % change classification (applies to volume, order_count, price)
TREND_FLAT_PCT = 5.0       # |% change| <= this = STABLE
TREND_STRONG_PCT = 20.0    # |% change| >= this = STRONG signal (annotated)

# Spread trend classification (recent / baseline ratio of mean spread)
SPREAD_NARROWING_RATIO = 0.85  # ratio <= this = NARROWING
SPREAD_WIDENING_RATIO = 1.15   # ratio >= this = WIDENING
                                # Between = STABLE

# Range compression classification (recent_90d_IQR / yearly_365d_IQR)
COMPRESSION_TIGHT_RATIO = 0.7   # ratio <= this = COMPRESSING (coiling)
COMPRESSION_LOOSE_RATIO = 1.3   # ratio >= this = EXPANDING
                                 # Between = NORMAL

# Window sizes
RECENT_WINDOW_DAYS = 30
PRIOR_WINDOW_DAYS = 30        # The 30 days before the recent window
SPREAD_BASELINE_DAYS = 60     # 30-90d back for spread baseline
COMPRESSION_RECENT_DAYS = 90  # Recent IQR window
COMPRESSION_YEARLY_DAYS = 365 # Yearly IQR baseline window

# Minimum sample counts
MIN_SPREAD_RECENT = 5
MIN_SPREAD_BASELINE = 10
MIN_COMPRESSION_RECENT = 20
MIN_COMPRESSION_YEARLY = 60


# =============================================================================
# Main entry point
# =============================================================================

def run_leading_indicators(type_id: int, region_id: int, region_name: str, log):
    """Run full leading indicators analysis and write results via log() callback.

    Args:
        type_id: Item type ID
        region_id: Region ID
        region_name: Region name (for display)
        log: Callable taking a string, appends to output
    """
    from history.market_history import get_market_history_db

    log("\n" + "-" * 40)
    log("SECTION 7: LEADING INDICATORS")
    log("-" * 40)

    # Fetch history
    try:
        market_db = get_market_history_db()
        history = market_db.get_full_history(region_id, type_id, years=3)
    except Exception as e:
        log(f"[ERROR] Could not fetch history: {e}")
        return

    if not history:
        log("[INFO] No history data available for this item in this region")
        log("Leading indicators: NOT APPLICABLE")
        return

    log(f"Days of data: {len(history)}")

    # Anchor windows at the DB's latest date, not the wall clock — the
    # everef archive lags 1-4 days and would deflate only the recent
    # window (review finding 6-2). get_reference_date logs an error via
    # log() if the DB has gone stale (>7 days without new data).
    from analytics.leading_indicators_batch import get_reference_date
    reference_date = get_reference_date(market_db, log=log)
    if reference_date:
        today = date.fromisoformat(reference_date)
        lag = (date.today() - today).days
        log(f"Window anchor: {reference_date} (DB latest, "
            f"{lag}d behind wall clock)")
    else:
        today = date.today()
        log("Window anchor: wall clock (DB latest date unavailable)")

    # Need at least 60 days for trend comparisons
    min_required = RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS
    if len(history) < min_required:
        log(f"[WARN] Only {len(history)} days, need at least {min_required}")
        log("Some indicators may be unreliable")

    # Build date->record map for fast window slicing
    by_date = {}
    for r in history:
        d = r.get('date')
        if d:
            by_date[d] = r

    # Run each subsection
    price_label, _ = _audit_price_trend(by_date, today, log)
    volume_label, _ = _audit_volume_trend(by_date, today, log)
    order_label, _ = _audit_order_count_trend(by_date, today, log)
    spread_label, _ = _audit_spread_trend(by_date, today, log)
    compression_label, _ = _audit_range_compression(by_date, today, log)
    _audit_divergence_verdict(
        price_label, volume_label, order_label, spread_label, compression_label,
        log
    )


# =============================================================================
# Helpers
# =============================================================================

def _window_dates(today: date, days_back_start: int, days_back_end: int):
    """Return list of date strings for offsets [days_back_start, days_back_end).

    Example: _window_dates(today, 0, 30) -> last 30 days (today inclusive).
    Example: _window_dates(today, 30, 60) -> the 30 days before that.
    """
    dates = []
    for offset in range(days_back_start, days_back_end):
        d = today - timedelta(days=offset)
        dates.append(d.isoformat())
    return dates


def _trend_label(pct_change: float) -> str:
    """Classify a % change into RISING / STABLE / FALLING base label."""
    if abs(pct_change) <= TREND_FLAT_PCT:
        return "STABLE"
    return "RISING" if pct_change > 0 else "FALLING"


def _trend_strength_suffix(pct_change: float) -> str:
    """Return ' (strong)' if change is strong, else ''."""
    if abs(pct_change) >= TREND_STRONG_PCT:
        return " (strong)"
    return ""


def _iqr(vals):
    """Return (iqr, p25, p75) for a list of values."""
    s = sorted(vals)
    n = len(s)
    p25_idx = int(n * 0.25)
    p75_idx = int(n * 0.75)
    if p75_idx >= n:
        p75_idx = n - 1
    p25 = s[p25_idx]
    p75 = s[p75_idx]
    return (p75 - p25, p25, p75)


# =============================================================================
# Subsection 1: Price Trend (floor)
# =============================================================================

def _audit_price_trend(by_date: dict, today: date, log):
    """Price trend: mean of 'lowest' recent 30d vs prior 30d.

    Returns (label, pct_change). Label is RISING / STABLE / FALLING / UNKNOWN.
    """
    log("\n--- Price Trend (floor) ---")

    recent_dates = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior_dates = _window_dates(
        today, RECENT_WINDOW_DAYS, RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS
    )

    def lows(date_list):
        out = []
        for d in date_list:
            r = by_date.get(d)
            if r:
                lo = r.get('lowest', 0)
                if lo > 0:
                    out.append(lo)
        return out

    recent_lows = lows(recent_dates)
    prior_lows = lows(prior_dates)

    log(f"Recent 30d samples:            {len(recent_lows)}")
    log(f"Prior 30d samples:             {len(prior_lows)}")

    if not recent_lows or not prior_lows:
        log("[WARN] Insufficient samples for price trend")
        log("\n>>> Price trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    recent_mean = mean(recent_lows)
    prior_mean = mean(prior_lows)

    log(f"Recent 30d mean lowest:        {recent_mean:>15,.2f} ISK")
    log(f"Prior 30d mean lowest:         {prior_mean:>15,.2f} ISK")

    if prior_mean <= 0:
        log("[WARN] Prior mean is zero - cannot compute trend")
        log("\n>>> Price trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    pct = (recent_mean - prior_mean) / prior_mean * 100
    log(f"Change:                        {pct:>+15.2f}%")

    label = _trend_label(pct)
    suffix = _trend_strength_suffix(pct)
    log(f"\n>>> Price trend: {label}{suffix}")
    return (label, pct)


# =============================================================================
# Subsection 2: Volume Trend
# =============================================================================

def _audit_volume_trend(by_date: dict, today: date, log):
    """Volume trend: mean daily volume recent 30d vs prior 30d.

    Sums volume across calendar days and divides by calendar-day count
    (NOT by record count - ESI omits zero-volume days, which would
    inflate velocity if record count were used).

    Returns (label, pct_change).
    """
    log("\n--- Volume Trend ---")

    recent_dates = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior_dates = _window_dates(
        today, RECENT_WINDOW_DAYS, RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS
    )

    recent_vol = sum(
        (by_date.get(d) or {}).get('volume', 0) for d in recent_dates
    )
    prior_vol = sum(
        (by_date.get(d) or {}).get('volume', 0) for d in prior_dates
    )

    recent_days_with_data = sum(1 for d in recent_dates if d in by_date)
    prior_days_with_data = sum(1 for d in prior_dates if d in by_date)

    recent_mean = recent_vol / RECENT_WINDOW_DAYS
    prior_mean = prior_vol / PRIOR_WINDOW_DAYS

    log(f"Recent 30d total volume:       {recent_vol:>15,} ({recent_days_with_data} days w/ data)")
    log(f"Prior 30d total volume:        {prior_vol:>15,} ({prior_days_with_data} days w/ data)")
    log(f"Recent 30d mean daily volume:  {recent_mean:>15,.2f}")
    log(f"Prior 30d mean daily volume:   {prior_mean:>15,.2f}")

    if prior_mean <= 0:
        log("[WARN] Prior window has no volume - cannot compute trend")
        log("\n>>> Volume trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    pct = (recent_mean - prior_mean) / prior_mean * 100
    log(f"Change:                        {pct:>+15.2f}%")

    label = _trend_label(pct)
    suffix = _trend_strength_suffix(pct)
    log(f"\n>>> Volume trend: {label}{suffix}")
    return (label, pct)


# =============================================================================
# Subsection 3: Order Count Trend
# =============================================================================

def _audit_order_count_trend(by_date: dict, today: date, log):
    """Order count trend: mean daily order_count recent 30d vs prior 30d.

    Same calendar-day denominator as volume trend.

    Returns (label, pct_change).
    """
    log("\n--- Order Count Trend ---")

    recent_dates = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    prior_dates = _window_dates(
        today, RECENT_WINDOW_DAYS, RECENT_WINDOW_DAYS + PRIOR_WINDOW_DAYS
    )

    recent_oc = sum(
        (by_date.get(d) or {}).get('order_count', 0) for d in recent_dates
    )
    prior_oc = sum(
        (by_date.get(d) or {}).get('order_count', 0) for d in prior_dates
    )

    recent_mean = recent_oc / RECENT_WINDOW_DAYS
    prior_mean = prior_oc / PRIOR_WINDOW_DAYS

    log(f"Recent 30d total orders:       {recent_oc:>15,}")
    log(f"Prior 30d total orders:        {prior_oc:>15,}")
    log(f"Recent 30d mean daily orders:  {recent_mean:>15,.2f}")
    log(f"Prior 30d mean daily orders:   {prior_mean:>15,.2f}")

    if prior_mean <= 0:
        log("[WARN] Prior window has no order data - cannot compute trend")
        log("\n>>> Order count trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    pct = (recent_mean - prior_mean) / prior_mean * 100
    log(f"Change:                        {pct:>+15.2f}%")

    label = _trend_label(pct)
    suffix = _trend_strength_suffix(pct)
    log(f"\n>>> Order count trend: {label}{suffix}")
    return (label, pct)


# =============================================================================
# Subsection 4: Spread Trend
# =============================================================================

def _audit_spread_trend(by_date: dict, today: date, log):
    """Spread trend: daily (highest-lowest)/average, recent 30d mean vs prior 60d mean.

    Returns (label, ratio). Label is NARROWING / STABLE / WIDENING / UNKNOWN.
    """
    log("\n--- Spread Trend ---")

    recent_dates = _window_dates(today, 0, RECENT_WINDOW_DAYS)
    baseline_dates = _window_dates(
        today, RECENT_WINDOW_DAYS, RECENT_WINDOW_DAYS + SPREAD_BASELINE_DAYS
    )

    def daily_spread(d_str):
        r = by_date.get(d_str)
        if not r:
            return None
        avg = r.get('average', 0)
        hi = r.get('highest', 0)
        lo = r.get('lowest', 0)
        if avg <= 0 or hi <= 0 or lo <= 0:
            return None
        return (hi - lo) / avg

    recent_spreads = [s for s in (daily_spread(d) for d in recent_dates) if s is not None]
    baseline_spreads = [s for s in (daily_spread(d) for d in baseline_dates) if s is not None]

    log(f"Recent 30d spread samples:     {len(recent_spreads)}")
    log(f"Baseline 60d spread samples:   {len(baseline_spreads)}")

    if len(recent_spreads) < MIN_SPREAD_RECENT or len(baseline_spreads) < MIN_SPREAD_BASELINE:
        log(f"[WARN] Insufficient samples (need {MIN_SPREAD_RECENT} recent, {MIN_SPREAD_BASELINE} baseline)")
        log("\n>>> Spread trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    recent_mean = mean(recent_spreads)
    baseline_mean = mean(baseline_spreads)

    log(f"Recent 30d mean spread:        {recent_mean:>15.4f}")
    log(f"Baseline 60d mean spread:      {baseline_mean:>15.4f}")

    if baseline_mean <= 0:
        log("[WARN] Baseline mean spread is zero - cannot compute trend")
        log("\n>>> Spread trend: UNKNOWN")
        return ("UNKNOWN", 0.0)

    ratio = recent_mean / baseline_mean
    log(f"Ratio (recent/baseline):       {ratio:>15.3f}")

    if ratio <= SPREAD_NARROWING_RATIO:
        label = "NARROWING"
        desc = "Bid-ask tightening - liquidity firming up"
    elif ratio >= SPREAD_WIDENING_RATIO:
        label = "WIDENING"
        desc = "Bid-ask widening - liquidity loosening or volatility rising"
    else:
        label = "STABLE"
        desc = "Spread within typical range"

    log(f"\n>>> Spread trend: {label}")
    log(f"    {desc}")
    return (label, ratio)


# =============================================================================
# Subsection 5: Range Compression
# =============================================================================

def _audit_range_compression(by_date: dict, today: date, log):
    """Range compression: recent 90d IQR of lowest vs full 365d IQR.

    Compares recent (last 90 days) interquartile range of daily lowest
    prices to the yearly (last 365 days) IQR. Tight ratio = consolidation.

    Returns (label, ratio). Label is COMPRESSING / NORMAL / EXPANDING / UNKNOWN.
    """
    log("\n--- Range Compression ---")

    recent_dates = _window_dates(today, 0, COMPRESSION_RECENT_DAYS)
    yearly_dates = _window_dates(today, 0, COMPRESSION_YEARLY_DAYS)

    def lowest_values(date_list):
        vals = []
        for d in date_list:
            r = by_date.get(d)
            if r:
                lo = r.get('lowest', 0)
                if lo > 0:
                    vals.append(lo)
        return vals

    recent_lows = lowest_values(recent_dates)
    yearly_lows = lowest_values(yearly_dates)

    log(f"Recent 90d samples:            {len(recent_lows)}")
    log(f"Yearly 365d samples:           {len(yearly_lows)}")

    if len(recent_lows) < MIN_COMPRESSION_RECENT or len(yearly_lows) < MIN_COMPRESSION_YEARLY:
        log(f"[WARN] Insufficient samples (need {MIN_COMPRESSION_RECENT} recent, {MIN_COMPRESSION_YEARLY} yearly)")
        log("\n>>> Range compression: UNKNOWN")
        return ("UNKNOWN", 0.0)

    recent_iqr, r_p25, r_p75 = _iqr(recent_lows)
    yearly_iqr, y_p25, y_p75 = _iqr(yearly_lows)

    log(f"Recent 90d:  p25={r_p25:>15,.0f}  p75={r_p75:>15,.0f}  IQR={recent_iqr:>15,.0f}")
    log(f"Yearly 365d: p25={y_p25:>15,.0f}  p75={y_p75:>15,.0f}  IQR={yearly_iqr:>15,.0f}")

    if yearly_iqr <= 0:
        log("[WARN] Yearly IQR is zero - cannot compute compression")
        log("\n>>> Range compression: UNKNOWN")
        return ("UNKNOWN", 0.0)

    ratio = recent_iqr / yearly_iqr
    log(f"Ratio (recent/yearly):         {ratio:>15.3f}")

    if ratio <= COMPRESSION_TIGHT_RATIO:
        label = "COMPRESSING"
        desc = "Range tightening - consolidation or coiling"
    elif ratio >= COMPRESSION_LOOSE_RATIO:
        label = "EXPANDING"
        desc = "Range widening - increased volatility"
    else:
        label = "NORMAL"
        desc = "Range within typical yearly bounds"

    log(f"\n>>> Range compression: {label}")
    log(f"    {desc}")
    return (label, ratio)


# =============================================================================
# Subsection 6: Divergence Verdict
# =============================================================================

def _audit_divergence_verdict(price, volume, order_count, spread, compression, log):
    """Combine all five indicators into divergence flags."""
    log("\n" + "=" * 40)
    log("DIVERGENCE VERDICT")
    log("=" * 40)
    log(f"Price:        {price}")
    log(f"Volume:       {volume}")
    log(f"Order Count:  {order_count}")
    log(f"Spread:       {spread}")
    log(f"Range:        {compression}")

    flags = compute_divergence_flags(price, volume, order_count, spread, compression)

    if not flags:
        log("\n>>> Flags: [HEALTHY]")
        log("    No divergence signals detected")
        return

    log(f"\n>>> Flags raised ({len(flags)}):")
    for tag, desc in flags:
        log(f"    [{tag}] {desc}")


def compute_divergence_flags(price, volume, order_count, spread, compression):
    """Compute divergence flags from indicator labels.

    Args:
        price: RISING / STABLE / FALLING / UNKNOWN
        volume: RISING / STABLE / FALLING / UNKNOWN
        order_count: RISING / STABLE / FALLING / UNKNOWN
        spread: NARROWING / STABLE / WIDENING / UNKNOWN
        compression: COMPRESSING / NORMAL / EXPANDING / UNKNOWN

    Returns:
        List of (tag, description) tuples. Empty list if no flags raised.
    """
    flags = []

    # Undercut spiral: volume falling + order count rising
    # More sellers competing for shrinking demand - classic margin death.
    if volume == "FALLING" and order_count == "RISING":
        flags.append((
            "UNDERCUT SPIRAL",
            "Volume falling + order count rising - more sellers fighting for less demand"
        ))

    # Stealth bleed: price flat + volume falling
    # Demand drying up before price reacts - floor will crack soon.
    if price == "STABLE" and volume == "FALLING":
        flags.append((
            "STEALTH BLEED",
            "Price flat + volume falling - demand drying up before price reacts"
        ))

    # Capitulation: price falling + volume rising
    # Panic selling, possible bottom forming.
    if price == "FALLING" and volume == "RISING":
        flags.append((
            "CAPITULATION",
            "Price falling + volume rising - panic selling, possible bottom forming"
        ))

    # Accumulation: price flat + volume rising + range compressing
    # Buying pressure absorbed without moving price.
    if price == "STABLE" and volume == "RISING" and compression == "COMPRESSING":
        flags.append((
            "ACCUMULATION",
            "Price flat + volume rising + range tight - quiet accumulation"
        ))

    # Coiled: range compressing + flat volume + flat price
    # Waiting for breakout in either direction.
    if compression == "COMPRESSING" and volume == "STABLE" and price == "STABLE":
        flags.append((
            "COILED",
            "Range compressing on flat volume - waiting for direction"
        ))

    # Distribution: price rising + volume falling
    # Rally losing steam, smart money exiting.
    if price == "RISING" and volume == "FALLING":
        flags.append((
            "DISTRIBUTION",
            "Price rising on falling volume - rally losing steam"
        ))

    # Liquidity drain: spread widening + volume falling
    # Market makers backing away.
    if spread == "WIDENING" and volume == "FALLING":
        flags.append((
            "LIQUIDITY DRAIN",
            "Spread widening + volume falling - market makers backing away"
        ))

    # Breakout setup: range expanding + volume rising
    # Volatility waking up with participation.
    if compression == "EXPANDING" and volume == "RISING":
        flags.append((
            "BREAKOUT SETUP",
            "Range expanding + volume rising - volatility waking up with participation"
        ))

    return flags
