"""Standalone Audit Tool for EVE Market Scout.

Simple GUI to search items by name and run full material analysis audits.
Results are displayed in a copyable text area.

Usage: python audit_tool.py
"""

# standalone script: make repo root importable when run directly
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

import tkinter as tk
from tkinter import ttk
import sqlite3
from typing import List, Tuple, Optional


class AuditTool:
    """Standalone audit tool window."""
    
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("EVE Market Scout - Audit Tool")
        self.root.geometry("900x700")
        
        # Store search results
        self.search_results: List[Tuple[int, str]] = []
        
        self._create_widgets()
    
    def _create_widgets(self):
        """Create the UI."""
        # Search frame
        search_frame = ttk.Frame(self.root)
        search_frame.pack(fill=tk.X, padx=10, pady=10)
        
        ttk.Label(search_frame, text="Item Name:").pack(side=tk.LEFT, padx=(0, 5))
        
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(search_frame, textvariable=self.search_var, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=(0, 5))
        self.search_entry.bind("<Return>", lambda e: self._do_search())
        
        ttk.Button(search_frame, text="Search", command=self._do_search).pack(side=tk.LEFT, padx=5)
        
        # Results listbox
        list_frame = ttk.Frame(self.root)
        list_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        ttk.Label(list_frame, text="Search Results (click to select):").pack(anchor=tk.W)
        
        list_container = ttk.Frame(list_frame)
        list_container.pack(fill=tk.X)
        
        self.results_list = tk.Listbox(list_container, height=6, exportselection=False)
        self.results_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        scrollbar = ttk.Scrollbar(list_container, orient=tk.VERTICAL, command=self.results_list.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.results_list.configure(yscrollcommand=scrollbar.set)
        
        # Region selection
        region_frame = ttk.Frame(self.root)
        region_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        ttk.Label(region_frame, text="Region:").pack(side=tk.LEFT, padx=(0, 5))
        
        self.region_var = tk.StringVar(value="Amarr")
        self.region_combo = ttk.Combobox(
            region_frame, 
            textvariable=self.region_var,
            values=["Amarr", "Jita", "Dodixie", "Hek", "Rens"],
            state="readonly",
            width=15
        )
        self.region_combo.pack(side=tk.LEFT, padx=(0, 20))
        
        ttk.Button(region_frame, text="Run Audit", command=self._run_audit).pack(side=tk.LEFT, padx=5)
        ttk.Button(region_frame, text="Clear", command=self._clear_output).pack(side=tk.LEFT, padx=5)
        ttk.Button(region_frame, text="Copy All", command=self._copy_all).pack(side=tk.LEFT, padx=5)

        # Filter Trace section - audit why a specific price would/wouldn't surface
        trace_frame = ttk.LabelFrame(self.root, text="Filter Trace (price audit)")
        trace_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        row1 = ttk.Frame(trace_frame)
        row1.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(row1, text="Observed sell price (ISK):").pack(side=tk.LEFT)
        self.observed_price_var = tk.StringVar()
        ttk.Entry(row1, textvariable=self.observed_price_var, width=18).pack(side=tk.LEFT, padx=(5, 15))
        ttk.Label(row1, text="Quantity:").pack(side=tk.LEFT)
        self.observed_qty_var = tk.StringVar(value="1")
        ttk.Entry(row1, textvariable=self.observed_qty_var, width=8).pack(side=tk.LEFT, padx=5)
        ttk.Button(row1, text="Trace Filters", command=self._run_filter_trace).pack(side=tk.RIGHT, padx=5)

        row2 = ttk.Frame(trace_frame)
        row2.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(row2, text="Overrides (optional):").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row2, text="2nd lowest sell:").pack(side=tk.LEFT)
        self.second_lowest_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.second_lowest_var, width=15).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row2, text="Highest buy:").pack(side=tk.LEFT)
        self.highest_buy_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.highest_buy_var, width=15).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row2, text="Live Jita sell:").pack(side=tk.LEFT)
        self.live_jita_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.live_jita_var, width=15).pack(side=tk.LEFT, padx=5)

        row3 = ttk.Frame(trace_frame)
        row3.pack(fill=tk.X, padx=5, pady=3)
        ttk.Label(row3, text="Thresholds:").pack(side=tk.LEFT, padx=(0, 10))
        ttk.Label(row3, text="Min profit/unit:").pack(side=tk.LEFT)
        self.min_profit_var = tk.StringVar(value="1000")
        ttk.Entry(row3, textvariable=self.min_profit_var, width=10).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row3, text="Min total profit:").pack(side=tk.LEFT)
        self.min_total_var = tk.StringVar(value="200000")
        ttk.Entry(row3, textvariable=self.min_total_var, width=12).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row3, text="Min margin %:").pack(side=tk.LEFT)
        self.min_margin_var = tk.StringVar(value="0")
        ttk.Entry(row3, textvariable=self.min_margin_var, width=6).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row3, text="Min vol/day:").pack(side=tk.LEFT)
        self.min_velocity_var = tk.StringVar(value="5")
        ttk.Entry(row3, textvariable=self.min_velocity_var, width=6).pack(side=tk.LEFT, padx=(5, 10))
        ttk.Label(row3, text="Max cost (ISK, 0=off):").pack(side=tk.LEFT)
        self.max_cost_var = tk.StringVar(value="0")
        ttk.Entry(row3, textvariable=self.max_cost_var, width=14).pack(side=tk.LEFT, padx=5)

        # Output text area
        output_frame = ttk.Frame(self.root)
        output_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        
        ttk.Label(output_frame, text="Audit Output (select and Ctrl+C to copy):").pack(anchor=tk.W)
        
        text_container = ttk.Frame(output_frame)
        text_container.pack(fill=tk.BOTH, expand=True)
        
        self.output_text = tk.Text(text_container, wrap=tk.NONE, font=("Consolas", 9))
        self.output_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        vsb = ttk.Scrollbar(text_container, orient=tk.VERTICAL, command=self.output_text.yview)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        
        hsb = ttk.Scrollbar(output_frame, orient=tk.HORIZONTAL, command=self.output_text.xview)
        hsb.pack(fill=tk.X)
        
        self.output_text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN).pack(
            fill=tk.X, padx=10, pady=(0, 10)
        )
    
    def _do_search(self):
        """Search for items by name."""
        query = self.search_var.get().strip()
        if not query:
            self._set_status("Enter a search term")
            return
        
        self.results_list.delete(0, tk.END)
        self.search_results.clear()
        
        try:
            from core.sound_manager import get_data_dir
            db_path = get_data_dir() / "sde_types.db"
            
            if not db_path.exists():
                self._set_status("ERROR: sde_types.db not found")
                return
            
            conn = sqlite3.connect(str(db_path))
            cursor = conn.execute(
                "SELECT type_id, name FROM types WHERE name LIKE ? ORDER BY name LIMIT 50",
                (f"%{query}%",)
            )
            
            results = cursor.fetchall()
            conn.close()
            
            if not results:
                self._set_status(f"No items found matching '{query}'")
                return
            
            for type_id, name in results:
                self.search_results.append((type_id, name))
                self.results_list.insert(tk.END, f"{name} (ID: {type_id})")
            
            self._set_status(f"Found {len(results)} items")
            
            # Auto-select first result
            if results:
                self.results_list.selection_set(0)
                
        except Exception as e:
            self._set_status(f"Search error: {e}")
    
    def _get_region_id(self) -> int:
        """Get region ID from selection."""
        region_map = {
            "Jita": 10000002,
            "Amarr": 10000043,
            "Dodixie": 10000032,
            "Hek": 10000042,
            "Rens": 10000030,
        }
        return region_map.get(self.region_var.get(), 10000043)
    
    def _run_audit(self):
        """Run the full audit on selected item."""
        selection = self.results_list.curselection()
        if not selection:
            self._set_status("Select an item first")
            return
        
        idx = selection[0]
        type_id, type_name = self.search_results[idx]
        region_id = self._get_region_id()
        region_name = self.region_var.get()
        
        self._set_status(f"Running audit for {type_name}...")
        self.root.update()
        
        # Capture output
        output_lines = []
        
        def log(msg: str):
            output_lines.append(msg)
        
        try:
            self._run_full_audit(type_id, type_name, region_id, region_name, log)
        except Exception as e:
            log(f"\n[ERROR] Audit failed: {e}")
            import traceback
            log(traceback.format_exc())
        
        # Display output
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", "\n".join(output_lines))
        
        self._set_status(f"Audit complete for {type_name}")
    
    def _run_stability_section(self, type_id: int, region_id: int, region_name: str, log):
        """Wrapper around stability_audit.run_stability_audit with error handling."""
        try:
            from tools.stability_audit import run_stability_audit
            run_stability_audit(type_id, region_id, region_name, log)
        except Exception as e:
            log(f"\n[ERROR] Stability analysis failed: {e}")
            import traceback
            log(traceback.format_exc())
    
    def _run_leading_indicators_section(self, type_id: int, region_id: int, region_name: str, log):
        """Wrapper around leading_indicators.run_leading_indicators with error handling."""
        try:
            from analytics.leading_indicators import run_leading_indicators
            run_leading_indicators(type_id, region_id, region_name, log)
        except Exception as e:
            log(f"\n[ERROR] Leading indicators analysis failed: {e}")
            import traceback
            log(traceback.format_exc())
    
    def _run_post_sections(self, type_id: int, region_id: int, region_name: str, log):
        """Run all post-material sections (stability, leading indicators) in order.
        
        Single entry point so future sections can be added in one place.
        """
        self._run_stability_section(type_id, region_id, region_name, log)
        self._run_leading_indicators_section(type_id, region_id, region_name, log)
    
    def _run_full_audit(self, type_id: int, type_name: str, region_id: int, region_name: str, log):
        """Run the complete audit."""
        log("=" * 80)
        log(f"FULL AUDIT: {type_name}")
        log(f"Type ID: {type_id} | Region: {region_name} (ID: {region_id})")
        log("=" * 80)
        
        # Section 1: Type lookup verification
        log("\n" + "-" * 40)
        log("SECTION 1: TYPE LOOKUP VERIFICATION")
        log("-" * 40)
        
        from sde.sde_manager import get_sde_manager
        sde = get_sde_manager()
        
        verified_name = sde.get_type_name(type_id)
        if verified_name:
            log(f"[OK] Type ID {type_id} -> '{verified_name}'")
            if verified_name != type_name:
                log(f"[WARN] Name mismatch: search='{type_name}', SDE='{verified_name}'")
        else:
            log(f"[WARN] Type ID {type_id} not found in SDE")

        # Section 1b: Market Snapshot (actual fulfilled trades, not listings)
        log("\n" + "-" * 40)
        log("MARKET SNAPSHOT (actual fulfilled trades)")
        log("-" * 40)
        self._run_market_snapshot(type_id, region_id, region_name, log)

        # Section 2: Profile Database Check
        log("\n" + "-" * 40)
        log("SECTION 2: PROFILE DATABASE CHECK")
        log("-" * 40)
        
        self._check_profile_database(type_id, type_name, region_id, region_name, sde, log)
        
        # Section 3: Blueprint lookup
        log("\n" + "-" * 40)
        log("SECTION 3: BLUEPRINT LOOKUP")
        log("-" * 40)
        
        from sde.sde_industry import get_sde_industry_db
        industry_db = get_sde_industry_db()
        
        if not industry_db.is_available():
            log("[ERROR] Industry database not available")
            log("Run 'Refresh SDE' from Stock Market settings")
            self._run_post_sections(type_id, region_id, region_name, log)
            log("\n" + "=" * 80)
            return
        
        blueprint_id = industry_db.get_blueprint_for_item(type_id)
        
        if blueprint_id is None:
            log(f"[INFO] No blueprint found for type {type_id}")
            log("This is a faction/officer/event item (not manufactured)")
            log("\n[RESULT] Material analysis: NOT APPLICABLE (no blueprint)")
            self._run_post_sections(type_id, region_id, region_name, log)
            log("\n" + "=" * 80)
            return
        
        log(f"[OK] Blueprint ID: {blueprint_id}")
        bp_name = sde.get_type_name(blueprint_id)
        if bp_name:
            log(f"     Blueprint Name: {bp_name}")
        
        # Section 4: Materials
        log("\n" + "-" * 40)
        log("SECTION 4: BLUEPRINT MATERIALS")
        log("-" * 40)
        
        materials = industry_db.get_materials(blueprint_id)
        
        if not materials:
            log("[WARN] Blueprint has no materials listed")
            log("\n[RESULT] Material analysis: NOT APPLICABLE (no materials)")
            self._run_post_sections(type_id, region_id, region_name, log)
            log("\n" + "=" * 80)
            return
        
        log(f"[OK] {len(materials)} input materials found:\n")
        log(f"{'Material':<35} {'Type ID':>10} {'Quantity':>12}")
        log("-" * 60)
        
        for mat in materials:
            mat_name = sde.get_type_name(mat.type_id) or f"Unknown"
            log(f"{mat_name:<35} {mat.type_id:>10} {mat.quantity:>12,}")
        
        # Section 5: Material Analysis
        log("\n" + "-" * 40)
        log("SECTION 5: MATERIAL ANALYSIS (TBC)")
        log("-" * 40)
        
        from history.market_history import get_market_history_db
        from analytics.material_analysis import (
            calculate_period_floor, calculate_tbc,
            SHORT_PERIOD_DAYS, MEDIUM_PERIOD_DAYS,
            ITEM_DIP_THRESHOLD, TBC_DIP_THRESHOLD
        )
        from core.config import JITA_REGION_ID
        
        market_db = get_market_history_db()
        
        log(f"Analysis periods:")
        log(f"  Recent:   0 to {SHORT_PERIOD_DAYS} days ago")
        log(f"  Baseline: {SHORT_PERIOD_DAYS} to {MEDIUM_PERIOD_DAYS} days ago")
        log(f"Thresholds:")
        log(f"  Item dip: {ITEM_DIP_THRESHOLD * 100:.0f}%")
        log(f"  TBC change: +/-{TBC_DIP_THRESHOLD * 100:.0f}%")
        
        # Item floors
        log("\n--- Item Price Floors ---")
        
        item_floor_recent = calculate_period_floor(
            type_id, region_id, SHORT_PERIOD_DAYS, 0, market_db
        )
        item_floor_baseline = calculate_period_floor(
            type_id, region_id, MEDIUM_PERIOD_DAYS, SHORT_PERIOD_DAYS, market_db
        )
        
        if item_floor_recent:
            log(f"Recent floor (0-{SHORT_PERIOD_DAYS}d):   {item_floor_recent:>15,.2f} ISK")
        else:
            log(f"Recent floor (0-{SHORT_PERIOD_DAYS}d):   NO DATA")
        
        if item_floor_baseline:
            log(f"Baseline floor ({SHORT_PERIOD_DAYS}-{MEDIUM_PERIOD_DAYS}d): {item_floor_baseline:>15,.2f} ISK")
        else:
            log(f"Baseline floor ({SHORT_PERIOD_DAYS}-{MEDIUM_PERIOD_DAYS}d): NO DATA")
        
        if not item_floor_recent or not item_floor_baseline:
            log("\n[RESULT] Classification: no_data (insufficient item price history)")
            self._run_post_sections(type_id, region_id, region_name, log)
            log("\n" + "=" * 80)
            return
        
        if item_floor_baseline > 0:
            item_dip_pct = (item_floor_recent - item_floor_baseline) / item_floor_baseline * 100
            log(f"Item change: {item_dip_pct:+.2f}%")
            
            if item_dip_pct > -ITEM_DIP_THRESHOLD * 100:
                log(f"\n[RESULT] Classification: no_dip")
                log(f"Item is NOT dipping enough ({item_dip_pct:+.2f}% > -{ITEM_DIP_THRESHOLD * 100:.0f}% threshold)")
                self._run_post_sections(type_id, region_id, region_name, log)
                log("\n" + "=" * 80)
                return
            else:
                log(f"[OK] Item IS dipping ({item_dip_pct:+.2f}% < -{ITEM_DIP_THRESHOLD * 100:.0f}% threshold)")
        
        # Material breakdown
        log("\n--- Material Price Breakdown (Jita) ---\n")
        log(f"{'Material':<30} {'Qty':>10} {'Recent':>12} {'Baseline':>12} {'ISK Recent':>14} {'ISK Base':>14}")
        log("-" * 100)
        
        tbc_recent_total = 0.0
        tbc_baseline_total = 0.0
        
        for mat in materials:
            mat_name = sde.get_type_name(mat.type_id) or f"Type {mat.type_id}"
            if len(mat_name) > 29:
                mat_name = mat_name[:26] + "..."
            
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
            
            log(f"{mat_name:<30} {mat.quantity:>10,} {recent_str:>12} {baseline_str:>12} {isk_recent_str:>14} {isk_baseline_str:>14}")
            
            if mat_floor_recent and mat_floor_recent > 0:
                tbc_recent_total += isk_recent
            if mat_floor_baseline and mat_floor_baseline > 0:
                tbc_baseline_total += isk_baseline
        
        log("-" * 100)
        log(f"{'TOTAL BUILD COST':<30} {'':<10} {'':<12} {'':<12} {tbc_recent_total:>14,.0f} {tbc_baseline_total:>14,.0f}")
        
        # TBC analysis
        log("\n--- TBC Comparison ---")
        log(f"TBC Recent:   {tbc_recent_total:>15,.2f} ISK")
        log(f"TBC Baseline: {tbc_baseline_total:>15,.2f} ISK")
        
        if tbc_baseline_total > 0:
            tbc_change_pct = (tbc_recent_total - tbc_baseline_total) / tbc_baseline_total * 100
            log(f"TBC Change:   {tbc_change_pct:>+15.2f}%")
            
            # Classification
            log("\n" + "=" * 40)
            log("FINAL CLASSIFICATION")
            log("=" * 40)
            
            if tbc_change_pct > TBC_DIP_THRESHOLD * 100:
                log(f"TBC is RISING ({tbc_change_pct:+.2f}% > +{TBC_DIP_THRESHOLD * 100:.0f}%)")
                log("\n[RESULT] Classification: CAUTION (margin squeeze)")
                log("Interpretation: Material costs rising while item price falling")
                risk = "HIGH RISK"
            elif tbc_change_pct < -TBC_DIP_THRESHOLD * 100:
                log(f"TBC is ALSO DIPPING ({tbc_change_pct:+.2f}% < -{TBC_DIP_THRESHOLD * 100:.0f}%)")
                log("\n[RESULT] Classification: WAIT (supply chain repricing)")
                log("Interpretation: Materials getting cheaper -> item price following")
                risk = "MEDIUM RISK (should NOT be in Low Risk)"
            else:
                log(f"TBC is STABLE ({tbc_change_pct:+.2f}% within +/-{TBC_DIP_THRESHOLD * 100:.0f}%)")
                log("\n[RESULT] Classification: BUY (demand dip)")
                log("Interpretation: Materials stable, item dipping = demand issue = buy signal")
                risk = "LOW RISK"
            
            log(f"\n>>> EXPECTED RISK BUCKET: {risk}")
            
            # Margin info
            if tbc_recent_total > 0 and item_floor_recent:
                margin = item_floor_recent - tbc_recent_total
                margin_pct = (margin / tbc_recent_total) * 100
                log(f"\nCurrent Margin: {margin:,.2f} ISK ({margin_pct:+.1f}% of TBC)")
        else:
            log("\n[RESULT] Classification: no_data (insufficient TBC data)")
        
        # Section 6: Stability Analysis (always runs)
        self._run_post_sections(type_id, region_id, region_name, log)
        
        log("\n" + "=" * 80)
    
    def _check_profile_database(self, type_id: int, type_name: str, region_id: int, region_name: str, sde, log):
        """Check the Stock Market profile database for this item."""
        import sqlite3
        from core.sound_manager import get_data_dir
        
        db_path = get_data_dir() / "stock_profiles.db"
        
        if not db_path.exists():
            log("[INFO] Profile database not found (stock_profiles.db)")
            log("This is normal if Stock Market hasn't been used yet")
            return
        
        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            
            # Check for this exact type_id
            cursor = conn.execute(
                "SELECT type_id, region_id, weighted_p_low, weighted_p_high FROM computed_profiles WHERE type_id = ?",
                (type_id,)
            )
            profiles = cursor.fetchall()
            
            if profiles:
                log(f"[OK] Found {len(profiles)} profile(s) for type_id {type_id}:\n")
                for p in profiles:
                    r_name = self._get_region_name(p['region_id'])
                    log(f"  Region: {r_name} (ID: {p['region_id']})")
                    log(f"    Floor:   {p['weighted_p_low']:,.2f} ISK")
                    log(f"    Ceiling: {p['weighted_p_high']:,.2f} ISK")
                    
                    # Check if this matches selected region
                    if p['region_id'] == region_id:
                        log(f"    [OK] This is the selected region")
                    log("")
            else:
                log(f"[INFO] No profile found for type_id {type_id}")
            
            # Search for items with similar names (potential duplicates/confusion)
            log("Checking for similar item names in profiles...")
            
            # Get all profiles and check names
            cursor = conn.execute(
                "SELECT DISTINCT type_id FROM computed_profiles"
            )
            all_type_ids = [row[0] for row in cursor.fetchall()]
            
            search_lower = type_name.lower()
            similar_items = []
            
            for tid in all_type_ids:
                name = sde.get_type_name(tid)
                if name and search_lower in name.lower() and tid != type_id:
                    similar_items.append((tid, name))
            
            if similar_items:
                log(f"\n[WARN] Found {len(similar_items)} other items with similar names in profiles:")
                for tid, name in similar_items[:10]:  # Limit to 10
                    # Get region info
                    cursor = conn.execute(
                        "SELECT region_id, weighted_p_low, weighted_p_high FROM computed_profiles WHERE type_id = ?",
                        (tid,)
                    )
                    for p in cursor.fetchall():
                        r_name = self._get_region_name(p['region_id'])
                        log(f"  Type ID {tid}: '{name}'")
                        log(f"    Region: {r_name}, Floor: {p['weighted_p_low']:,.2f}, Ceiling: {p['weighted_p_high']:,.2f}")
                log("")
                log("[!] If wrong item is showing, the profile DB may have wrong type_id stored")
            else:
                log("[OK] No similar named items found in profiles")
            
            # Check yearly stats for this type_id
            cursor = conn.execute(
                "SELECT year, p_low, p_high, avg_volume FROM yearly_stats WHERE type_id = ? AND region_id = ? ORDER BY year DESC",
                (type_id, region_id)
            )
            yearly = cursor.fetchall()
            
            if yearly:
                log(f"\nYearly stats for type_id {type_id} in {region_name}:")
                log(f"{'Year':<6} {'Floor':>12} {'Ceiling':>12} {'Avg Volume':>12}")
                log("-" * 45)
                for y in yearly:
                    log(f"{y['year']:<6} {y['p_low']:>12,.0f} {y['p_high']:>12,.0f} {y['avg_volume']:>12,.0f}")
            
            conn.close()
            
        except Exception as e:
            log(f"[ERROR] Failed to check profile database: {e}")
    
    def _get_region_name(self, region_id: int) -> str:
        """Get region name from ID."""
        region_map = {
            10000002: "Jita",
            10000043: "Amarr", 
            10000032: "Dodixie",
            10000042: "Hek",
            10000030: "Rens",
        }
        return region_map.get(region_id, f"Unknown ({region_id})")
    
    def _clear_output(self):
        """Clear the output text area."""
        self.output_text.delete("1.0", tk.END)
        self._set_status("Cleared")
    
    def _copy_all(self):
        """Copy all output to clipboard."""
        content = self.output_text.get("1.0", tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._set_status("Copied to clipboard")
    
    def _set_status(self, msg: str):
        """Update status bar."""
        self.status_var.set(msg)

    def _run_market_snapshot(self, type_id: int, region_id: int, region_name: str, log):
        """Show volume-weighted fulfilled-trade price + traded volume per day.

        Reads market *history* (everef/ESI daily aggregates), so the prices are
        the volume-weighted average of actual transactions each day, NOT current
        sell-order listings. The selected hub is shown beside Jita as a baseline
        so a "huge profit at <hub>" claim can be sanity-checked against whether
        the hub actually moves any volume.
        """
        from history.market_history import get_market_history_db
        from scanning.scanner_common import parse_history_stats
        from core.config import JITA_REGION_ID

        market_db = get_market_history_db()

        local_hist = market_db.get_history(region_id, type_id, days=30) or []
        local = parse_history_stats(local_hist)

        is_jita = (region_id == JITA_REGION_ID)
        if is_jita:
            # Selected hub IS Jita — one column is enough.
            log(f"{'':<18}{region_name:>16}")
            log("-" * 34)
            log(f"{'Avg price 7d':<18}{local.avg_price_7d:>12,.2f} ISK")
            log(f"{'Avg price 30d':<18}{local.avg_price_30d:>12,.2f} ISK")
            log(f"{'Avg volume 7d':<18}{local.avg_volume_7d:>11,.1f} /day")
            log(f"{'Avg volume 30d':<18}{local.avg_volume_30d:>11,.1f} /day")
        else:
            jita_hist = market_db.get_history(JITA_REGION_ID, type_id, days=30) or []
            jita = parse_history_stats(jita_hist)
            log(f"{'':<18}{region_name:>16}{'Jita':>16}")
            log("-" * 50)
            log(f"{'Avg price 7d':<18}{local.avg_price_7d:>12,.2f} ISK{jita.avg_price_7d:>12,.2f} ISK")
            log(f"{'Avg price 30d':<18}{local.avg_price_30d:>12,.2f} ISK{jita.avg_price_30d:>12,.2f} ISK")
            log(f"{'Avg volume 7d':<18}{local.avg_volume_7d:>11,.1f} /day{jita.avg_volume_7d:>11,.1f} /day")
            log(f"{'Avg volume 30d':<18}{local.avg_volume_30d:>11,.1f} /day{jita.avg_volume_30d:>11,.1f} /day")

        if local.avg_volume_7d <= 0 and local.avg_volume_30d <= 0:
            log(f"\n[!] No fulfilled trades recorded in {region_name} over 30 days.")
            log("    Any 'profit' shown elsewhere for this hub is not backed by real volume.")

        log("\nNote: prices are volume-weighted averages of actual daily fills")
        log("(market history), NOT current sell-order listings.")

    def _run_filter_trace(self):
        """Trace what the scanner pipeline would do with a hypothetical price."""
        selection = self.results_list.curselection()
        if not selection:
            self._set_status("Select an item first")
            return

        idx = selection[0]
        type_id, type_name = self.search_results[idx]
        region_id = self._get_region_id()
        region_name = self.region_var.get()

        def _parse_required_float(var, label):
            s = var.get().strip().replace(",", "").replace("_", "")
            if not s:
                raise ValueError(f"{label} is required")
            return float(s)

        def _parse_opt_float(var):
            s = var.get().strip().replace(",", "").replace("_", "")
            if not s:
                return None
            try:
                return float(s)
            except ValueError:
                return None

        try:
            observed_price = _parse_required_float(self.observed_price_var, "Observed sell price")
            observed_qty = int(self.observed_qty_var.get().strip() or "1")
            min_profit = float(self.min_profit_var.get().replace(",", "") or "0")
            min_total = float(self.min_total_var.get().replace(",", "") or "0")
            min_margin = float(self.min_margin_var.get() or "0")
            min_vol = float(self.min_velocity_var.get() or "0")
            max_cost = float(self.max_cost_var.get().replace(",", "") or "0")
        except ValueError as e:
            self._set_status(f"Input error: {e}")
            return

        second_lowest = _parse_opt_float(self.second_lowest_var)
        highest_buy = _parse_opt_float(self.highest_buy_var)
        live_jita = _parse_opt_float(self.live_jita_var)

        output_lines = []

        def log(msg: str):
            output_lines.append(msg)

        self._set_status(f"Tracing filters for {type_name}...")
        self.root.update()

        try:
            self._run_full_trace(
                type_id, type_name, region_id, region_name,
                observed_price, observed_qty,
                second_lowest, highest_buy, live_jita,
                min_profit, min_total, min_margin, min_vol, max_cost,
                log,
            )
        except Exception as e:
            log(f"\n[ERROR] Trace failed: {e}")
            import traceback
            log(traceback.format_exc())

        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", "\n".join(output_lines))
        self._set_status(f"Filter trace complete for {type_name}")

    def _run_full_trace(self, type_id, type_name, region_id, region_name,
                        observed_price, observed_qty,
                        second_lowest, highest_buy, live_jita,
                        min_profit, min_total, min_margin, min_vol, max_cost,
                        log):
        """Execute the full scanner pipeline against the user-provided price."""
        from history.market_history import get_market_history_db
        from scanning.scanner_common import (
            Candidate, parse_history_stats, calculate_ceiling,
            evaluate_risk_flags, build_deal, get_steal_color,
            STEAL_RATIO_THRESHOLD, JITA_CAP_PERCENT, VOLUME_CAP_FRACTION,
        )
        from core.config import JITA_REGION_ID, SCAM_THRESHOLD

        log("=" * 80)
        log(f"FILTER TRACE: {type_name}")
        log(f"Type ID: {type_id} | Hub region: {region_name} ({region_id})")
        log(f"Observed price: {observed_price:,.2f} ISK  |  Quantity: {observed_qty}")
        log("=" * 80)

        market_db = get_market_history_db()
        local_hist = market_db.get_history(region_id, type_id, days=30) or []
        jita_hist = market_db.get_history(JITA_REGION_ID, type_id, days=30) or []

        local_stats = parse_history_stats(local_hist)
        jita_stats = parse_history_stats(jita_hist)

        log("\n--- HISTORY (local hub region) ---")
        log(f"  avg_price_7d:    {local_stats.avg_price_7d:>15,.2f} ISK")
        log(f"  avg_price_30d:   {local_stats.avg_price_30d:>15,.2f} ISK")
        log(f"  avg_volume_7d:   {local_stats.avg_volume_7d:>15,.2f} /day")
        log(f"  avg_volume_30d:  {local_stats.avg_volume_30d:>15,.2f} /day")
        log(f"  safe_velocity:   {local_stats.safe_velocity:>15,.2f} /day  (min of 7d/30d)")
        log(f"  trading_days_30d: {local_stats.trading_days_30d}")
        log(f"  is_crashing:     {local_stats.is_crashing}")

        log("\n--- HISTORY (Jita) ---")
        log(f"  avg_price_7d:    {jita_stats.avg_price_7d:>15,.2f} ISK")
        log(f"  avg_price_30d:   {jita_stats.avg_price_30d:>15,.2f} ISK")
        log(f"  conservative:    {jita_stats.conservative_price:>15,.2f} ISK  (lower of 7d/30d)")

        notes = []
        if second_lowest is None:
            second_lowest = local_stats.optimistic_price or observed_price
            notes.append(
                f"second_lowest defaulted to local optimistic_price ({second_lowest:,.2f}). "
                "Provide the actual 2nd-lowest sell order for accurate ceiling."
            )
        if highest_buy is None:
            highest_buy = 0.0
            notes.append(
                "highest_buy not provided -> steal_ratio = 0 (item will NOT classify as STEAL). "
                "Provide the highest buy order to test the steal pathway."
            )
        if live_jita is None:
            live_jita = jita_stats.conservative_price
            notes.append(
                f"live_jita defaulted to jita conservative_price ({live_jita:,.2f}). "
                "Provide a live Jita sell price for the hard 105% cap to fire correctly."
            )

        candidate = Candidate(
            type_id=type_id,
            system_id=0,
            local_sell=observed_price,
            local_sell_2nd=second_lowest,
            local_buy=highest_buy,
            jita_sell=live_jita,
            volume=observed_qty,
        )

        log("\n--- SYNTHESIZED CANDIDATE ---")
        log(f"  local_sell (your price): {candidate.local_sell:>15,.2f}")
        log(f"  local_sell_2nd:          {candidate.local_sell_2nd:>15,.2f}")
        log(f"  local_buy:               {candidate.local_buy:>15,.2f}")
        log(f"  jita_sell (live):        {candidate.jita_sell:>15,.2f}")
        log(f"  volume:                  {candidate.volume:>15}")
        log(f"  steal_ratio:             {candidate.steal_ratio:>15.4f}  (buy/sell)")
        if notes:
            log("\n  [DEFAULTS APPLIED]")
            for n in notes:
                log(f"    - {n}")

        # STEP 0: pre-candidate filters (mirror scanner._build_candidates)
        log("\n--- STEP 0: Pre-Candidate Filters (candidate-build phase) ---")
        precandidate_dropped = False
        precandidate_reasons = []

        # Scam check: local_sell more than SCAM_THRESHOLD above jita_sell
        # Skipped when scanning Jita itself (region_id == JITA_REGION_ID)
        if region_id == JITA_REGION_ID:
            log(f"  scam check:    SKIPPED (hub is Jita)")
        elif candidate.jita_sell > 0:
            overpriced_ratio = (candidate.local_sell - candidate.jita_sell) / candidate.jita_sell
            log(f"  scam check:    local {candidate.local_sell:,.2f} vs jita {candidate.jita_sell:,.2f}"
                f"  -> overpriced {overpriced_ratio * 100:+.2f}%  (threshold +{SCAM_THRESHOLD * 100:.0f}%)")
            if overpriced_ratio > SCAM_THRESHOLD:
                precandidate_dropped = True
                precandidate_reasons.append(
                    f"SCAM CHECK: local price is {overpriced_ratio * 100:.1f}% above Jita "
                    f"(> {SCAM_THRESHOLD * 100:.0f}% threshold) -> candidate dropped before any processor"
                )
        else:
            log(f"  scam check:    SKIPPED (no live Jita price)")

        # Max cost: total_cost > max_cost
        total_cost = candidate.local_sell * candidate.volume
        if max_cost > 0:
            log(f"  max_cost:      total {total_cost:,.2f} vs cap {max_cost:,.2f}")
            if total_cost > max_cost:
                precandidate_dropped = True
                precandidate_reasons.append(
                    f"MAX COST: total cost {total_cost:,.2f} ISK > cap {max_cost:,.2f} ISK "
                    f"-> candidate dropped before any processor"
                )
        else:
            log(f"  max_cost:      SKIPPED (set to 0 = off)")

        if precandidate_dropped:
            log("\n  >>> CANDIDATE DROPPED at build phase")
            for r in precandidate_reasons:
                log(f"      - {r}")
            log("\n" + "=" * 40)
            log("VERDICTS")
            log("=" * 40)
            log("\n  All scanner pathways: WOULD NOT APPEAR")
            log("  Reason: candidate was eliminated before reaching steal/lowrisk/highrisk processors.")
            log("  Adjust the threshold above and re-run to test downstream behaviour.")
            log("\n" + "=" * 80)
            return
        else:
            log("  >>> Candidate survives pre-build filters")

        # STEP 1: steal classification
        log("\n--- STEP 1: Steal Classification ---")
        log(f"  steal_ratio = {candidate.steal_ratio:.4f}  vs  threshold {STEAL_RATIO_THRESHOLD}")
        if candidate.is_steal:
            log("  >>> Classified as STEAL (routes to scanner_steals)")
        else:
            log("  >>> NOT a steal (routes to scanner_lowrisk / scanner_highrisk)")

        # STEP 2: ceiling
        ceiling, ceiling_flags = calculate_ceiling(candidate, local_stats, jita_stats)
        undercut = max(0.01, candidate.local_sell_2nd * 0.001)
        starting = candidate.local_sell_2nd - undercut
        log("\n--- STEP 2: Ceiling Calculation ---")
        log(f"  start from 2nd lowest - undercut: {starting:,.2f}")
        if jita_stats.conservative_price > 0:
            log(f"  jita historical cap (105%):      {jita_stats.conservative_price * JITA_CAP_PERCENT:,.2f}")
        else:
            log("  jita historical cap: N/A (no Jita history)")
        if candidate.jita_sell > 0:
            log(f"  live jita cap (105%):            {candidate.jita_sell * JITA_CAP_PERCENT:,.2f}")
        else:
            log("  live jita cap: N/A (no live Jita)")
        log(f"  >>> final ceiling:               {ceiling:,.2f} ISK")
        if ceiling_flags:
            log(f"  flags from ceiling: {', '.join(f.value for f in ceiling_flags)}")

        # STEP 3: risk flags
        all_flags = evaluate_risk_flags(
            local_stats, jita_stats, min_vol, ceiling_flags,
            buy_price=candidate.local_sell,
        )
        log("\n--- STEP 3: Risk Flag Evaluation ---")
        log(f"  min_velocity threshold: {min_vol}")
        if all_flags:
            for f in all_flags:
                log(f"  [FLAG] {f.value}")
        else:
            log("  [OK] no risk flags")

        # STEP 4: deal
        deal = build_deal(
            candidate=candidate,
            name=type_name,
            system_name=region_name,
            ceiling=ceiling,
            local_stats=local_stats,
            jita_stats=jita_stats,
            risk_flags=all_flags,
            is_steal=candidate.is_steal,
        )
        log("\n--- STEP 4: Deal Built ---")
        log(f"  buy_price:        {deal.buy_price:>15,.2f}")
        log(f"  ceiling_price:    {deal.ceiling_price:>15,.2f}")
        log(f"  net_profit/unit:  {deal.net_profit:>15,.2f}")
        log(f"  margin_percent:   {deal.margin_percent:>15.2f} %")
        log(f"  effective_volume: {deal.volume:>15}  (raw={deal.raw_volume}, capped at {int(VOLUME_CAP_FRACTION*100)}% of safe_velocity)")
        log(f"  total_profit:     {deal.total_profit:>15,.2f}")

        # STEP 5: profit filters
        log("\n--- STEP 5: Profit Filters ---")
        p1 = deal.net_profit >= min_profit
        p2 = deal.total_profit >= min_total
        p3 = (min_margin <= 0) or (deal.margin_percent >= min_margin)
        log(f"  net_profit ({deal.net_profit:,.2f}) >= min_profit ({min_profit:,.0f}):    {'PASS' if p1 else 'FAIL'}")
        log(f"  total_profit ({deal.total_profit:,.2f}) >= min_total ({min_total:,.0f}): {'PASS' if p2 else 'FAIL'}")
        if min_margin > 0:
            log(f"  margin_percent ({deal.margin_percent:.2f}%) >= min_margin ({min_margin}%):  {'PASS' if p3 else 'FAIL'}")
        else:
            log("  margin_percent: skipped (min_margin = 0)")
        profit_pass = p1 and p2 and p3

        # VERDICTS
        log("\n" + "=" * 40)
        log("VERDICTS")
        log("=" * 40)

        log("\n[scanner_steals]")
        if not candidate.is_steal:
            log(f"  WOULD NOT APPEAR: steal_ratio {candidate.steal_ratio:.4f} < threshold {STEAL_RATIO_THRESHOLD}")
            if highest_buy == 0:
                log("    (provide Highest buy override to test the steal pathway)")
        elif not profit_pass:
            log("  WOULD NOT APPEAR: classified as steal but failed profit filters above")
        else:
            color = get_steal_color(all_flags)
            log(f"  WOULD APPEAR as STEAL ({color.value.upper()})")

        log("\n[scanner_lowrisk]")
        if candidate.is_steal:
            log("  SKIPPED: classified as a steal (routes to scanner_steals)")
        elif all_flags:
            log(f"  WOULD NOT APPEAR: requires zero risk flags, has {len(all_flags)}: "
                f"{', '.join(f.value for f in all_flags)}")
        elif not profit_pass:
            log("  WOULD NOT APPEAR: failed profit filters above")
        else:
            log("  WOULD APPEAR as LOW RISK")

        log("\n[scanner_highrisk]")
        if candidate.is_steal:
            log("  SKIPPED: classified as a steal (routes to scanner_steals)")
        elif not all_flags:
            log("  WOULD NOT APPEAR: requires >=1 risk flag, has 0 (would route to LOW RISK)")
        elif not profit_pass:
            log("  WOULD NOT APPEAR: failed profit filters above")
        else:
            log(f"  WOULD APPEAR as HIGH RISK ({', '.join(f.value for f in all_flags)})")

        log("\n[scanner_crosshub]")
        log("  NOT TRACED: cross-hub arbitrage needs a destination station + its order")
        log("  book (highest buy / lowest sell / 2nd lowest at the destination).")
        log("  Ask to extend this trace if you want cross-hub coverage.")

        log("\n" + "=" * 80)

    def run(self):
        """Start the application."""
        self.search_entry.focus_set()
        self.root.mainloop()


if __name__ == "__main__":
    app = AuditTool()
    app.run()
