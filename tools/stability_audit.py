"""Stability analysis for EVE Market Scout audit tool.

Computes three independent stability signals from 3-year market history:
  1. Burstiness fingerprint (CV of daily volume) - WAVE / MIXED / TRICKLE
  2. Floor decay (recent 7d median lowest vs 90d baseline)
  3. Volume regime (current 7d sum vs historical distribution)

Combines into a verdict tag indicating whether a sell recommendation is
likely to hold or built on a paper floor.

Used by audit_tool.py to display stability data per item.
"""

# standalone script: make repo root importable when run directly
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from statistics import mean, median, stdev
from datetime import date, timedelta


# =============================================================================
# Tunable thresholds
# =============================================================================

# Burstiness CV thresholds
BURSTINESS_TRICKLE_MAX = 0.6   # CV below this = TRICKLE
BURSTINESS_WAVE_MIN = 1.5      # CV above this = WAVE
                                # Between = MIXED

# Floor decay thresholds (recent_median / baseline_median ratios)
DECAY_HEALTHY_MIN = 0.97       # >= this = HEALTHY
DECAY_MILD_MIN = 0.92          # >= this = MILD DECAY
DECAY_SIGNIFICANT_MIN = 0.85   # >= this = SIGNIFICANT DECAY
                                # Below = SEVERE DECAY

# Volume regime percentile thresholds
REGIME_DRY_PCT = 25            # Below this percentile = DRY SPELL
REGIME_ELEVATED_PCT = 75       # Above this percentile = ELEVATED
                                # Between = NORMAL

# Window sizes
RECENT_WINDOW_DAYS = 7
BASELINE_WINDOW_DAYS = 90


# =============================================================================
# Main entry point
# =============================================================================

def run_stability_audit(type_id: int, region_id: int, region_name: str, log):
    """Run full stability analysis and write results via log() callback.
    
    Args:
        type_id: Item type ID
        region_id: Region ID
        region_name: Region name (for display)
        log: Callable taking a string, appends to output
    """
    from history.market_history import get_market_history_db
    
    log("\n" + "-" * 40)
    log("SECTION 6: STABILITY ANALYSIS")
    log("-" * 40)
    
    # Fetch history
    try:
        market_db = get_market_history_db()
        log("\nPulling 3-year history from market_history.db...")
        history = market_db.get_full_history(region_id, type_id, years=3)
    except Exception as e:
        log(f"[ERROR] Could not fetch history: {e}")
        return
    
    if not history:
        log("[INFO] No history data available for this item in this region")
        log("Stability analysis: NOT APPLICABLE")
        return
    
    log(f"Days of data: {len(history)}")
    
    today = date.today()
    
    # Data freshness check
    try:
        sorted_records = sorted(history, key=lambda r: r.get('date', ''))
        last_data_date = date.fromisoformat(sorted_records[-1]['date'])
        days_stale = (today - last_data_date).days
        if days_stale > 2:
            log(f"[WARN] Most recent data is {days_stale} days old")
    except Exception:
        pass
    
    if len(history) < BASELINE_WINDOW_DAYS:
        log(f"[WARN] Only {len(history)} days of data, need at least {BASELINE_WINDOW_DAYS}")
        log("Results below may be unreliable")
    
    # Run each subsection
    burstiness_label = _audit_burstiness(history, log)
    decay_label = _audit_floor_decay(history, today, log)
    regime_label = _audit_volume_regime(history, today, log)
    _audit_recent_table(history, today, log)
    _audit_verdict(burstiness_label, decay_label, regime_label, log)


# =============================================================================
# Subsection 1: Burstiness Fingerprint
# =============================================================================

def _audit_burstiness(history: list, log) -> str:
    """Compute coefficient of variation on daily volume.
    
    High CV = wave demand (good for sellers).
    Low CV = trickle demand (chronic undercutting).
    
    Returns label string: WAVE, MIXED, TRICKLE, or UNKNOWN.
    """
    log("\n--- Burstiness Fingerprint ---")
    
    volumes_nonzero = [r['volume'] for r in history if r.get('volume', 0) > 0]
    excluded_zero = len(history) - len(volumes_nonzero)
    
    log(f"Daily volume samples: {len(volumes_nonzero)} (excluded {excluded_zero} zero-vol days)")
    
    if len(volumes_nonzero) < 30:
        log("[WARN] Insufficient non-zero volume data for burstiness analysis")
        return "UNKNOWN"
    
    v_mean = mean(volumes_nonzero)
    v_stdev = stdev(volumes_nonzero)
    cv = v_stdev / v_mean if v_mean > 0 else 0
    
    log(f"Mean daily volume:        {v_mean:>12,.1f}")
    log(f"Stdev daily volume:       {v_stdev:>12,.1f}")
    log(f"Coefficient of Variation: {cv:>12.2f}")
    
    if cv < BURSTINESS_TRICKLE_MAX:
        label = "TRICKLE"
        desc = "Steady daily flow - undercutter heaven"
    elif cv > BURSTINESS_WAVE_MIN:
        label = "WAVE"
        desc = "Demand comes in spikes - clearing events do happen"
    else:
        label = "MIXED"
        desc = "Moderate variability - between trickle and wave"
    
    log(f"\n>>> Character: {label}")
    log(f"    {desc}")
    return label


# =============================================================================
# Subsection 2: Floor Decay
# =============================================================================

def _audit_floor_decay(history: list, today: date, log) -> str:
    """Compare recent 7d median lowest to 90d baseline median lowest.
    
    Catches the case where a sell recommendation is built on a floor
    that has already started dropping.
    
    Returns label: HEALTHY, MILD DECAY, SIGNIFICANT DECAY, SEVERE DECAY, UNKNOWN.
    """
    log("\n--- Floor Decay ---")
    
    recent_cutoff = (today - timedelta(days=RECENT_WINDOW_DAYS)).strftime('%Y-%m-%d')
    baseline_cutoff = (today - timedelta(days=BASELINE_WINDOW_DAYS)).strftime('%Y-%m-%d')
    
    recent_lows = [r['lowest'] for r in history 
                   if r.get('date', '') >= recent_cutoff and r.get('lowest', 0) > 0]
    baseline_lows = [r['lowest'] for r in history
                     if r.get('date', '') >= baseline_cutoff and r.get('lowest', 0) > 0]
    
    if not recent_lows or not baseline_lows:
        log("[WARN] Insufficient data for floor decay analysis")
        if recent_lows:
            log(f"  Recent samples found: {len(recent_lows)}")
        if baseline_lows:
            log(f"  Baseline samples found: {len(baseline_lows)}")
        return "UNKNOWN"
    
    recent_median = median(recent_lows)
    baseline_median = median(baseline_lows)
    decay_ratio = recent_median / baseline_median if baseline_median > 0 else 0
    decay_pct = (decay_ratio - 1) * 100
    
    log(f"Recent {RECENT_WINDOW_DAYS}d median lowest:    {recent_median:>15,.0f} ISK ({len(recent_lows)} samples)")
    log(f"Baseline {BASELINE_WINDOW_DAYS}d median lowest: {baseline_median:>15,.0f} ISK ({len(baseline_lows)} samples)")
    log(f"Decay ratio:                  {decay_ratio:>15.3f}")
    log(f"Floor change:                 {decay_pct:>+15.1f}%")
    
    if decay_ratio >= DECAY_HEALTHY_MIN:
        label = "HEALTHY"
        desc = "Floor is current, recommendation valid"
    elif decay_ratio >= DECAY_MILD_MIN:
        label = "MILD DECAY"
        desc = "Recommendation slightly stale"
    elif decay_ratio >= DECAY_SIGNIFICANT_MIN:
        label = "SIGNIFICANT DECAY"
        desc = "Recommendation likely stale - posted floor will not hold"
    else:
        label = "SEVERE DECAY"
        desc = "Recommendation built on dying floor - do not post at recommended price"
    
    log(f"\n>>> Floor status: {label}")
    log(f"    {desc}")
    return label


# =============================================================================
# Subsection 3: Volume Regime
# =============================================================================

def _audit_volume_regime(history: list, today: date, log) -> str:
    """Compare current 7-day volume to historical 7-day window distribution.
    
    Tells you if you're in a dry spell (no buyers) or elevated demand
    (clearing wave active) relative to this item's typical pattern.
    
    Returns label: DRY SPELL, NORMAL, ELEVATED, UNKNOWN.
    """
    log("\n--- Volume Regime ---")
    
    # Build date->volume map for rolling window calculation
    vol_by_date = {}
    for r in history:
        d = r.get('date')
        v = r.get('volume', 0)
        if d:
            vol_by_date[d] = v
    
    if len(vol_by_date) < BASELINE_WINDOW_DAYS:
        log("[WARN] Insufficient data for volume regime analysis")
        return "UNKNOWN"
    
    try:
        sorted_dates = sorted(vol_by_date.keys())
        first_date = date.fromisoformat(sorted_dates[0])
        last_date = date.fromisoformat(sorted_dates[-1])
        
        # Build all 7-day rolling window sums (calendar-day based)
        # Treats missing days as 0 volume - this is correct because
        # ESI omits zero-volume days from history
        rolling_7d = []
        cur = first_date + timedelta(days=RECENT_WINDOW_DAYS - 1)
        while cur <= last_date:
            window_sum = 0
            for offset in range(RECENT_WINDOW_DAYS):
                d_str = (cur - timedelta(days=offset)).isoformat()
                window_sum += vol_by_date.get(d_str, 0)
            rolling_7d.append(window_sum)
            cur += timedelta(days=1)
        
        # Current 7-day sum (anchored to today)
        current_7d = 0
        for offset in range(RECENT_WINDOW_DAYS):
            d_str = (today - timedelta(days=offset)).isoformat()
            current_7d += vol_by_date.get(d_str, 0)
        
        log(f"Last 7d total volume:      {current_7d:>10,}")
        log(f"Historical 7d distribution ({len(rolling_7d)} windows):")
        
        sorted_windows = sorted(rolling_7d)
        n = len(sorted_windows)
        
        def pct_value(p):
            idx = int(n * p / 100)
            if idx >= n:
                idx = n - 1
            return sorted_windows[idx]
        
        log(f"  10th percentile: {pct_value(10):>10,}")
        log(f"  25th percentile: {pct_value(25):>10,}")
        log(f"  50th percentile: {pct_value(50):>10,}")
        log(f"  75th percentile: {pct_value(75):>10,}")
        log(f"  90th percentile: {pct_value(90):>10,}")
        
        # Find current's percentile rank
        below = sum(1 for w in sorted_windows if w < current_7d)
        regime_pct = (below / n) * 100 if n > 0 else 0
        
        log(f"Current 7d sits at:        {regime_pct:.0f}th percentile")
        
        if regime_pct < REGIME_DRY_PCT:
            label = "DRY SPELL"
            desc = "Demand below normal - clearing wave has not arrived"
        elif regime_pct > REGIME_ELEVATED_PCT:
            label = "ELEVATED"
            desc = "Demand above normal - clearing wave likely active"
        else:
            label = "NORMAL"
            desc = "Demand within typical range"
        
        log(f"\n>>> Regime: {label}")
        log(f"    {desc}")
        return label
    except Exception as e:
        log(f"[ERROR] Volume regime calculation failed: {e}")
        return "UNKNOWN"


# =============================================================================
# Subsection 4: Recent History Table
# =============================================================================

def _audit_recent_table(history: list, today: date, log):
    """Print last 14 days of raw daily data for visual inspection."""
    log("\n--- Daily History (last 14 days) ---")
    
    cutoff_14 = (today - timedelta(days=14)).strftime('%Y-%m-%d')
    recent_14 = sorted(
        [r for r in history if r.get('date', '') >= cutoff_14],
        key=lambda r: r.get('date', ''),
        reverse=True
    )
    
    if not recent_14:
        log("No data in last 14 days")
        return
    
    log(f"{'Date':<12} {'Avg':>14} {'Low':>14} {'High':>14} {'Volume':>10} {'Orders':>8}")
    log("-" * 78)
    for r in recent_14:
        log(
            f"{r.get('date', ''):<12} "
            f"{r.get('average', 0):>14,.0f} "
            f"{r.get('lowest', 0):>14,.0f} "
            f"{r.get('highest', 0):>14,.0f} "
            f"{r.get('volume', 0):>10,} "
            f"{r.get('order_count', 0):>8,}"
        )


# =============================================================================
# Subsection 5: Combined Verdict
# =============================================================================

def _audit_verdict(burstiness: str, decay: str, regime: str, log):
    """Combine three factors into a single verdict tag."""
    log("\n" + "=" * 40)
    log("COMBINED VERDICT")
    log("=" * 40)
    log(f"Character: {burstiness}")
    log(f"Floor:     {decay}")
    log(f"Regime:    {regime}")
    
    verdict, verdict_desc = compute_verdict(burstiness, decay, regime)
    log(f"\n>>> Verdict: [{verdict}]")
    for line in verdict_desc:
        log(f"    {line}")


def compute_verdict(burstiness: str, decay: str, regime: str):
    """Compute combined verdict from three stability factors.
    
    Verdict precedence:
      1. SEVERE DECAY -> [STALE] regardless of other factors
      2. TRICKLE + any decay -> [AVOID]
      3. TRICKLE healthy -> [TRICKLE] (relisting expected)
      4. SIGNIFICANT DECAY (non-trickle) -> [CAUTION]
      5. WAVE + DRY SPELL -> [WAIT] or [CAUTION] if mild decay
      6. WAVE + ELEVATED -> [CLEAR] (post now)
      7. Otherwise -> [OK]
    
    Args:
        burstiness: WAVE / MIXED / TRICKLE / UNKNOWN
        decay: HEALTHY / MILD DECAY / SIGNIFICANT DECAY / SEVERE DECAY / UNKNOWN
        regime: DRY SPELL / NORMAL / ELEVATED / UNKNOWN
    
    Returns:
        Tuple of (verdict_tag: str, description_lines: list[str])
    """
    # Severe decay overrides everything else
    if decay == "SEVERE DECAY":
        return ("STALE", [
            "Recommendation built on a dying floor",
            "DO NOT post at recommended price"
        ])
    
    # Trickle items - chronic undercut risk
    if burstiness == "TRICKLE":
        if decay in ("MILD DECAY", "SIGNIFICANT DECAY"):
            return ("AVOID", [
                "Trickle item with declining floor",
                "Chronic undercutting will continue - demote out of Low Risk"
            ])
        return ("TRICKLE", [
            "Steady-flow item, expect frequent relisting",
            "Use small position sizes"
        ])
    
    # Significant decay on non-trickle items
    if decay == "SIGNIFICANT DECAY":
        return ("CAUTION", [
            "Floor slipping faster than typical",
            "If posting, use recent floor not recommended floor"
        ])
    
    # Wave items - regime drives decision
    if burstiness == "WAVE":
        if regime == "DRY SPELL":
            if decay == "MILD DECAY":
                return ("CAUTION", [
                    "Wave item with decaying floor in dry spell",
                    "Wave has not arrived, floor is bleeding while you wait"
                ])
            return ("WAIT", [
                "Wave has not arrived",
                "May sit a while before clearing event hits"
            ])
        elif regime == "ELEVATED":
            return ("CLEAR", [
                "Wave item in elevated demand",
                "Clearing event likely active - post now"
            ])
        else:
            return ("OK", [
                "Wave item, normal regime, floor healthy"
            ])
    
    # Mixed items - middle ground
    if burstiness == "MIXED":
        if decay == "MILD DECAY" and regime == "DRY SPELL":
            return ("CAUTION", [
                "Soft demand and slipping floor",
                "Consider waiting or posting below recommended"
            ])
        return ("OK", [
            "Mixed item, no major warning signals"
        ])
    
    # Unknown / fallback
    return ("UNKNOWN", [
        "Insufficient data for clear verdict"
    ])
