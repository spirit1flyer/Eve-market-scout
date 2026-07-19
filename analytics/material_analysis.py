"""Material cost analysis for EVE Market Scout Stock Market.

Analyzes the relationship between item price dips and input material costs
to distinguish between demand dips (buy opportunities) and supply chain
repricing events (wait for new floor).

Core concept:
    Total Build Cost (TBC) = sum of (material_quantity * material_floor_price)
    
    If TBC is stable but item price is dipping -> demand dip -> buy signal
    If TBC is also dipping -> supply chain repricing -> wait
    If TBC is rising while item dips -> margin squeeze -> caution

Uses Jita (The Forge) as reference region for all material prices.
"""

from datetime import date, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from sde.sde_industry import get_sde_industry_db, BlueprintMaterial
from history.market_history import MarketHistoryDB, get_market_history_db
from core.config import JITA_REGION_ID


# Analysis thresholds
ITEM_DIP_THRESHOLD = 0.10    # 10% - item must drop this much to trigger analysis
TBC_DIP_THRESHOLD = 0.05     # 5% - TBC change must exceed this to count as "moving"

# Time periods (months approximated as 30 days)
SHORT_PERIOD_DAYS = 90       # 3 months - "recent"
MEDIUM_PERIOD_DAYS = 180     # 6 months - baseline for comparison
LONG_PERIOD_DAYS = 365       # 12 months - extended baseline


@dataclass
class MaterialAnalysisResult:
    """Result of material correlation analysis."""
    
    # Classification
    classification: str  # 'buy', 'wait', 'caution', 'no_blueprint', 'no_dip', 'no_data'
    
    # Item metrics
    item_floor_recent: float      # 3-month floor
    item_floor_baseline: float    # 6-month floor
    item_dip_pct: float           # % change (negative = dip)
    
    # TBC metrics
    tbc_recent: float             # TBC using recent material floors
    tbc_baseline: float           # TBC using baseline material floors
    tbc_change_pct: float         # % change (negative = materials dropping)
    
    # Margin metrics
    current_margin: float         # item_floor_recent - tbc_recent (ISK)
    margin_pct: float             # margin as % of TBC
    
    # Details
    material_count: int           # Number of input materials
    materials_analyzed: int       # Materials with sufficient data
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for display/storage."""
        return {
            'classification': self.classification,
            'item_dip_pct': self.item_dip_pct,
            'tbc_change_pct': self.tbc_change_pct,
            'current_margin': self.current_margin,
            'margin_pct': self.margin_pct,
            'material_count': self.material_count,
        }


def calculate_period_floor(
    type_id: int,
    region_id: int,
    start_days_ago: int,
    end_days_ago: int,
    market_db: MarketHistoryDB
) -> Optional[float]:
    """Calculate floor (lowest average) for a time period.
    
    Args:
        type_id: Item type ID
        region_id: Region ID
        start_days_ago: Start of period (e.g., 180 = 6 months ago)
        end_days_ago: End of period (e.g., 90 = 3 months ago)
        market_db: Market history database
        
    Returns:
        Floor price (minimum of daily averages), or None if no data
    """
    today = date.today()
    start_date = (today - timedelta(days=start_days_ago)).strftime('%Y-%m-%d')
    end_date = (today - timedelta(days=end_days_ago)).strftime('%Y-%m-%d')
    
    conn = market_db._get_conn()
    cursor = conn.execute("""
        SELECT MIN(average) as floor
        FROM daily_history
        WHERE type_id = ? AND region_id = ? AND date BETWEEN ? AND ?
    """, (type_id, region_id, start_date, end_date))
    
    row = cursor.fetchone()
    if row and row['floor'] is not None:
        return row['floor']
    return None


def prebuild_material_floor_cache(
    material_type_ids: List[int],
    market_db: MarketHistoryDB,
    region_id: int = JITA_REGION_ID,
    context_label: str = "",
) -> tuple:
    """Pre-compute Jita floors for all unique materials in two batched queries.
    
    Avoids the per-blueprint slowdown where the same Tritanium/Pyerite/etc.
    floor would be recomputed hundreds of times.  Two SQL aggregates over
    daily_history with `type_id IN (...)` populate dicts keyed by type_id.
    
    Args:
        material_type_ids: All unique material type IDs to look up.
        market_db: Market history database.
        region_id: Region for material prices (default Jita).
        context_label: Optional caller identifier (typically hub_key) for
            log tagging so concurrent runs are distinguishable in
            interleaved console output.
        
    Returns:
        Tuple of (recent_floors, baseline_floors) dicts.
        recent_floors:   {type_id: floor over [SHORT_PERIOD_DAYS, 0]}
        baseline_floors: {type_id: floor over [MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS]}
        
        Missing type_ids will be absent from the dict (callers should
        treat absent or zero as "no data").
    """
    if not material_type_ids:
        return {}, {}
    
    today = date.today()
    recent_start = (today - timedelta(days=SHORT_PERIOD_DAYS)).strftime('%Y-%m-%d')
    recent_end = today.strftime('%Y-%m-%d')
    baseline_start = (today - timedelta(days=MEDIUM_PERIOD_DAYS)).strftime('%Y-%m-%d')
    baseline_end = (today - timedelta(days=SHORT_PERIOD_DAYS)).strftime('%Y-%m-%d')
    
    # Deduplicate; convert to list for parameterized IN clause
    unique_ids = list(set(material_type_ids))
    placeholders = ",".join("?" * len(unique_ids))
    
    conn = market_db._get_conn()
    
    # Recent period floor per type_id (one round trip)
    cursor = conn.execute(f"""
        SELECT type_id, MIN(average) AS floor
        FROM daily_history
        WHERE region_id = ?
          AND type_id IN ({placeholders})
          AND date BETWEEN ? AND ?
        GROUP BY type_id
    """, (region_id, *unique_ids, recent_start, recent_end))
    recent_floors = {row['type_id']: row['floor'] for row in cursor.fetchall()
                     if row['floor'] is not None}
    
    # Baseline period floor per type_id (one round trip)
    cursor = conn.execute(f"""
        SELECT type_id, MIN(average) AS floor
        FROM daily_history
        WHERE region_id = ?
          AND type_id IN ({placeholders})
          AND date BETWEEN ? AND ?
        GROUP BY type_id
    """, (region_id, *unique_ids, baseline_start, baseline_end))
    baseline_floors = {row['type_id']: row['floor'] for row in cursor.fetchall()
                       if row['floor'] is not None}
    
    print(f"[MaterialAnalysis{':' + context_label if context_label else ''}] "
          f"Pre-built material floor cache: "
          f"{len(unique_ids)} unique materials, "
          f"{len(recent_floors)} recent / {len(baseline_floors)} baseline")
    
    return recent_floors, baseline_floors


def calculate_tbc(
    materials: List[BlueprintMaterial],
    start_days_ago: int,
    end_days_ago: int,
    market_db: MarketHistoryDB,
    floor_cache: Optional[Dict[int, float]] = None,
) -> Optional[float]:
    """Calculate Total Build Cost for a time period.
    
    Uses Jita floors for all material prices.
    
    Args:
        materials: List of BlueprintMaterial (type_id, quantity)
        start_days_ago: Start of period
        end_days_ago: End of period
        market_db: Market history database
        floor_cache: Optional pre-computed {type_id: floor} dict.  When
            provided, avoids per-material SQL queries entirely.  Should
            match the period defined by start_days_ago/end_days_ago —
            callers using prebuild_material_floor_cache() should pass the
            recent dict for the recent period and baseline for baseline.
        
    Returns:
        Total build cost, or None if insufficient data
    """
    total_cost = 0.0
    materials_with_data = 0
    
    for mat in materials:
        if floor_cache is not None:
            # Fast path: dict lookup
            floor = floor_cache.get(mat.type_id)
        else:
            # Slow path: per-material SQL aggregate
            floor = calculate_period_floor(
                mat.type_id,
                JITA_REGION_ID,
                start_days_ago,
                end_days_ago,
                market_db
            )
        
        if floor is not None and floor > 0:
            total_cost += mat.quantity * floor
            materials_with_data += 1
    
    # Require at least some materials to have data
    if materials_with_data == 0:
        return None
    
    return total_cost


def analyze_material_dip(
    type_id: int,
    region_id: int,
    market_db: Optional[MarketHistoryDB] = None,
    recent_floor_cache: Optional[Dict[int, float]] = None,
    baseline_floor_cache: Optional[Dict[int, float]] = None,
    item_floor_recent_cache: Optional[Dict[int, float]] = None,
    item_floor_baseline_cache: Optional[Dict[int, float]] = None,
) -> MaterialAnalysisResult:
    """Analyze whether an item's price dip correlates with material costs.

    Compares recent (0-3 month) floors against baseline (3-6 month) floors
    for both the item and its Total Build Cost.

    Args:
        type_id: Item type ID to analyze
        region_id: Region ID for item price (materials always use Jita)
        market_db: Market history database (uses singleton if not provided)
        recent_floor_cache: Pre-computed {material_type_id: floor} for the
            recent period (material floors, Jita).
        baseline_floor_cache: Pre-computed dict for baseline period (materials).
        item_floor_recent_cache: Pre-computed {type_id: floor} for the item's
            own recent floor in the hub region.  Skips calculate_period_floor
            when provided.
        item_floor_baseline_cache: Pre-computed {type_id: floor} for item
            baseline floor.

    Returns:
        MaterialAnalysisResult with classification and metrics
    """
    if market_db is None:
        market_db = get_market_history_db()
    
    industry_db = get_sde_industry_db()
    
    # Check if industry data is available
    if not industry_db.is_available():
        return MaterialAnalysisResult(
            classification='no_data',
            item_floor_recent=0, item_floor_baseline=0, item_dip_pct=0,
            tbc_recent=0, tbc_baseline=0, tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=0, materials_analyzed=0
        )
    
    # Get materials for this item
    materials = industry_db.get_materials_for_item(type_id)
    
    if materials is None:
        # No blueprint - faction/officer/event item
        return MaterialAnalysisResult(
            classification='no_blueprint',
            item_floor_recent=0, item_floor_baseline=0, item_dip_pct=0,
            tbc_recent=0, tbc_baseline=0, tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=0, materials_analyzed=0
        )
    
    if len(materials) == 0:
        return MaterialAnalysisResult(
            classification='no_blueprint',
            item_floor_recent=0, item_floor_baseline=0, item_dip_pct=0,
            tbc_recent=0, tbc_baseline=0, tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=0, materials_analyzed=0
        )
    
    # Calculate item floors (use pre-built cache when available)
    if item_floor_recent_cache is not None:
        item_floor_recent = item_floor_recent_cache.get(type_id)
    else:
        item_floor_recent = calculate_period_floor(
            type_id, region_id, SHORT_PERIOD_DAYS, 0, market_db
        )
    if item_floor_baseline_cache is not None:
        item_floor_baseline = item_floor_baseline_cache.get(type_id)
    else:
        item_floor_baseline = calculate_period_floor(
            type_id, region_id, MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS, market_db
        )
    
    if item_floor_recent is None or item_floor_baseline is None:
        return MaterialAnalysisResult(
            classification='no_data',
            item_floor_recent=item_floor_recent or 0,
            item_floor_baseline=item_floor_baseline or 0,
            item_dip_pct=0,
            tbc_recent=0, tbc_baseline=0, tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=len(materials), materials_analyzed=0
        )
    
    # Calculate item dip percentage
    if item_floor_baseline > 0:
        item_dip_pct = (item_floor_recent - item_floor_baseline) / item_floor_baseline
    else:
        item_dip_pct = 0
    
    # Check if item has meaningful dip
    if item_dip_pct > -ITEM_DIP_THRESHOLD:
        # Item is not dipping significantly (or rising)
        return MaterialAnalysisResult(
            classification='no_dip',
            item_floor_recent=item_floor_recent,
            item_floor_baseline=item_floor_baseline,
            item_dip_pct=item_dip_pct * 100,
            tbc_recent=0, tbc_baseline=0, tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=len(materials), materials_analyzed=0
        )
    
    # Calculate TBC for both periods (using pre-computed floor cache if provided)
    tbc_recent = calculate_tbc(
        materials, SHORT_PERIOD_DAYS, 0, market_db,
        floor_cache=recent_floor_cache,
    )
    tbc_baseline = calculate_tbc(
        materials, MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS, market_db,
        floor_cache=baseline_floor_cache,
    )
    
    if tbc_recent is None or tbc_baseline is None:
        return MaterialAnalysisResult(
            classification='no_data',
            item_floor_recent=item_floor_recent,
            item_floor_baseline=item_floor_baseline,
            item_dip_pct=item_dip_pct * 100,
            tbc_recent=tbc_recent or 0,
            tbc_baseline=tbc_baseline or 0,
            tbc_change_pct=0,
            current_margin=0, margin_pct=0,
            material_count=len(materials), materials_analyzed=0
        )
    
    # Calculate TBC change percentage
    if tbc_baseline > 0:
        tbc_change_pct = (tbc_recent - tbc_baseline) / tbc_baseline
    else:
        tbc_change_pct = 0
    
    # Calculate margin
    current_margin = item_floor_recent - tbc_recent
    if tbc_recent > 0:
        margin_pct = (current_margin / tbc_recent) * 100
    else:
        margin_pct = 0
    
    # Classify based on TBC movement
    if tbc_change_pct > TBC_DIP_THRESHOLD:
        # TBC rising while item dipping = margin squeeze
        classification = 'caution'
    elif tbc_change_pct < -TBC_DIP_THRESHOLD:
        # TBC also dipping = supply chain repricing
        classification = 'wait'
    else:
        # TBC stable, item dipping = demand dip = buy opportunity
        classification = 'buy'
    
    return MaterialAnalysisResult(
        classification=classification,
        item_floor_recent=item_floor_recent,
        item_floor_baseline=item_floor_baseline,
        item_dip_pct=item_dip_pct * 100,  # Convert to percentage
        tbc_recent=tbc_recent,
        tbc_baseline=tbc_baseline,
        tbc_change_pct=tbc_change_pct * 100,  # Convert to percentage
        current_margin=current_margin,
        margin_pct=margin_pct,
        material_count=len(materials),
        materials_analyzed=len(materials)  # Could track this more precisely
    )


def analyze_batch(
    type_ids: List[int],
    region_id: int,
    market_db: Optional[MarketHistoryDB] = None
) -> Dict[int, MaterialAnalysisResult]:
    """Analyze multiple items.
    
    Args:
        type_ids: List of item type IDs
        region_id: Region ID for item prices
        market_db: Market history database
        
    Returns:
        Dict mapping type_id -> MaterialAnalysisResult
    """
    if market_db is None:
        market_db = get_market_history_db()
    
    results = {}
    for type_id in type_ids:
        results[type_id] = analyze_material_dip(type_id, region_id, market_db)
    
    return results


def get_classification_display(classification: str) -> str:
    """Get display text for classification.
    
    Uses ASCII only per project requirements.
    """
    display_map = {
        'buy': '[BUY] Demand dip - inputs stable',
        'wait': '[WAIT] Repricing - inputs dropping',
        'caution': '[CAUTION] Margin squeeze - inputs rising',
        'no_blueprint': '(no blueprint)',
        'no_dip': '(no recent dip)',
        'no_data': '(insufficient data)',
    }
    return display_map.get(classification, classification)


def get_classification_short(classification: str) -> str:
    """Get short display text for classification (column display)."""
    short_map = {
        'buy': 'BUY',
        'wait': 'WAIT',
        'caution': 'CAUTION',
        'no_blueprint': '--',
        'no_dip': '--',
        'no_data': '--',
    }
    return short_map.get(classification, '--')


def audit_material_analysis(type_id: int, region_id: int, item_name: str = "") -> None:
    """Print detailed material analysis breakdown to terminal.
    
    For debugging/verification of TBC correlation logic.
    
    Args:
        type_id: Item type ID to analyze
        region_id: Region ID for item price lookup
        item_name: Optional item name for display
    """
    from sde.sde_manager import get_sde_manager
    sde = get_sde_manager()
    
    market_db = get_market_history_db()
    industry_db = get_sde_industry_db()
    
    print("\n" + "=" * 70)
    print(f"MATERIAL ANALYSIS AUDIT: {item_name or f'Type {type_id}'}")
    print(f"Type ID: {type_id} | Region ID: {region_id}")
    print("=" * 70)
    
    # Check industry DB
    if not industry_db.is_available():
        print("[ERROR] Industry database not available")
        return
    
    # Blueprint lookup
    blueprint_id = industry_db.get_blueprint_for_item(type_id)
    if blueprint_id is None:
        print("[RESULT] No blueprint found - faction/officer/event item")
        print("Classification: no_blueprint")
        return
    
    print(f"\n[BLUEPRINT] ID: {blueprint_id}")
    
    # Get materials
    materials = industry_db.get_materials(blueprint_id)
    if not materials:
        print("[RESULT] Blueprint has no materials")
        print("Classification: no_blueprint")
        return
    
    print(f"[MATERIALS] {len(materials)} inputs found")
    
    # Item floor comparison
    print("\n--- ITEM PRICE ANALYSIS ---")
    item_floor_recent = calculate_period_floor(
        type_id, region_id, SHORT_PERIOD_DAYS, 0, market_db
    )
    item_floor_baseline = calculate_period_floor(
        type_id, region_id, MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS, market_db
    )
    
    print(f"Recent floor (0-{SHORT_PERIOD_DAYS}d):   {item_floor_recent:,.2f} ISK" if item_floor_recent else "Recent floor: NO DATA")
    print(f"Baseline floor ({SHORT_PERIOD_DAYS}-{MEDIUM_PERIOD_DAYS}d): {item_floor_baseline:,.2f} ISK" if item_floor_baseline else "Baseline floor: NO DATA")
    
    if item_floor_recent and item_floor_baseline and item_floor_baseline > 0:
        item_dip_pct = (item_floor_recent - item_floor_baseline) / item_floor_baseline * 100
        print(f"Item change: {item_dip_pct:+.2f}%")
        print(f"Dip threshold: {ITEM_DIP_THRESHOLD * 100:.0f}%")
        
        if item_dip_pct > -ITEM_DIP_THRESHOLD * 100:
            print(f"[RESULT] Item NOT dipping enough ({item_dip_pct:+.2f}% > -{ITEM_DIP_THRESHOLD * 100:.0f}%)")
            print("Classification: no_dip")
            return
        else:
            print(f"[OK] Item IS dipping ({item_dip_pct:+.2f}% < -{ITEM_DIP_THRESHOLD * 100:.0f}%)")
    else:
        print("[RESULT] Insufficient item price data")
        print("Classification: no_data")
        return
    
    # Per-material breakdown
    print("\n--- MATERIAL BREAKDOWN (Jita prices) ---")
    print(f"{'Material':<35} {'Qty':>10} {'Recent':>12} {'Baseline':>12} {'ISK Recent':>14} {'ISK Base':>14}")
    print("-" * 105)
    
    tbc_recent_total = 0.0
    tbc_baseline_total = 0.0
    materials_with_data = 0
    
    for mat in materials:
        mat_name = sde.get_type_name(mat.type_id) or f"Type {mat.type_id}"
        
        mat_floor_recent = calculate_period_floor(
            mat.type_id, JITA_REGION_ID, SHORT_PERIOD_DAYS, 0, market_db
        )
        mat_floor_baseline = calculate_period_floor(
            mat.type_id, JITA_REGION_ID, MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS, market_db
        )
        
        recent_str = f"{mat_floor_recent:,.2f}" if mat_floor_recent else "NO DATA"
        baseline_str = f"{mat_floor_baseline:,.2f}" if mat_floor_baseline else "NO DATA"
        
        isk_recent = mat.quantity * mat_floor_recent if mat_floor_recent else 0
        isk_baseline = mat.quantity * mat_floor_baseline if mat_floor_baseline else 0
        
        isk_recent_str = f"{isk_recent:,.0f}" if mat_floor_recent else "--"
        isk_baseline_str = f"{isk_baseline:,.0f}" if mat_floor_baseline else "--"
        
        print(f"{mat_name:<35} {mat.quantity:>10,} {recent_str:>12} {baseline_str:>12} {isk_recent_str:>14} {isk_baseline_str:>14}")
        
        if mat_floor_recent and mat_floor_recent > 0:
            tbc_recent_total += isk_recent
            materials_with_data += 1
        if mat_floor_baseline and mat_floor_baseline > 0:
            tbc_baseline_total += isk_baseline
    
    print("-" * 105)
    print(f"{'TOTAL BUILD COST':<35} {'':<10} {'':<12} {'':<12} {tbc_recent_total:>14,.0f} {tbc_baseline_total:>14,.0f}")
    
    # TBC comparison
    print("\n--- TBC ANALYSIS ---")
    print(f"TBC Recent:   {tbc_recent_total:,.2f} ISK")
    print(f"TBC Baseline: {tbc_baseline_total:,.2f} ISK")
    
    if tbc_baseline_total > 0:
        tbc_change_pct = (tbc_recent_total - tbc_baseline_total) / tbc_baseline_total * 100
        print(f"TBC change: {tbc_change_pct:+.2f}%")
        print(f"TBC threshold: +/-{TBC_DIP_THRESHOLD * 100:.0f}%")
        
        # Classification
        print("\n--- CLASSIFICATION ---")
        if tbc_change_pct > TBC_DIP_THRESHOLD * 100:
            print(f"[RESULT] TBC RISING ({tbc_change_pct:+.2f}% > +{TBC_DIP_THRESHOLD * 100:.0f}%)")
            print("Classification: caution (margin squeeze)")
        elif tbc_change_pct < -TBC_DIP_THRESHOLD * 100:
            print(f"[RESULT] TBC ALSO DIPPING ({tbc_change_pct:+.2f}% < -{TBC_DIP_THRESHOLD * 100:.0f}%)")
            print("Classification: wait (supply chain repricing)")
        else:
            print(f"[RESULT] TBC STABLE ({tbc_change_pct:+.2f}% within +/-{TBC_DIP_THRESHOLD * 100:.0f}%)")
            print("Classification: buy (demand dip)")
    else:
        print("[RESULT] Insufficient TBC data")
        print("Classification: no_data")
    
    # Margin info
    if tbc_recent_total > 0 and item_floor_recent:
        margin = item_floor_recent - tbc_recent_total
        margin_pct = (margin / tbc_recent_total) * 100
        print(f"\nCurrent margin: {margin:,.2f} ISK ({margin_pct:+.1f}% of TBC)")
    
    print("=" * 70 + "\n")
if __name__ == "__main__":
    audit_material_analysis(33475, 10000043, "Mobile Tractor Unit")
