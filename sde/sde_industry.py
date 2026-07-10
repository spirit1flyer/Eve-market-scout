"""SDE Industry data manager for EVE Market Scout.

Downloads and caches blueprint manufacturing data from Fuzzwork SDE.
Provides lookups for item -> blueprint -> materials relationships.

Data source: Fuzzwork's SDE CSV exports
    https://www.fuzzwork.co.uk/dump/latest/csv/industryActivityMaterials.csv
    https://www.fuzzwork.co.uk/dump/latest/csv/industryActivityProducts.csv
    https://www.fuzzwork.co.uk/dump/latest/csv/industryActivityProbabilities.csv
    https://www.fuzzwork.co.uk/dump/latest/csv/industryActivitySkills.csv

Database location: %APPDATA%/EVEMarketScout/sde_industry.db

Stage 5.1 (2026-07-07) expanded the schema to import ALL activities (not just
manufacturing) plus invention probability/skill-requirement data, in prep for
Phase 5 (T2 invention + reaction chains). Every reader that existed before
Stage 5.1 keeps its EXACT prior semantics (manufacturing/activity-1 only) —
see the "Backward compatibility" note above `_schema_has_activity_id`.
"""

import csv
import io
import json
import sqlite3
import asyncio
import aiohttp
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any, Callable
from dataclasses import dataclass

from core.sound_manager import get_data_dir
from core.ssl_context import make_connector
from core.config import ESI_USER_AGENT
from sde.sde_swap import build_then_swap


# Database and version files
INDUSTRY_DB_FILE = "sde_industry.db"
INDUSTRY_VERSION_FILE = "sde_industry_version.json"

# Fuzzwork URLs
# Fuzzwork moved the CSV exports into a csv/ subdirectory (mid-2026); the old
# /dump/latest/*.csv paths now 404.
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"
MATERIALS_URL = f"{FUZZWORK_BASE}/industryActivityMaterials.csv"
PRODUCTS_URL = f"{FUZZWORK_BASE}/industryActivityProducts.csv"
# Per-activity base TIME (seconds/run) — drives Phase 4 build-time + research
# popup. Columns: typeID,activityID,time.
ACTIVITY_URL = f"{FUZZWORK_BASE}/industryActivity.csv"
# Stage 5.1: invention base probability per (source blueprint, activity 8,
# invented blueprint) and per-activity required skills. Both are treated as
# OPTIONAL downloads (see download_and_build) — if Fuzzwork ever renames/drops
# either file, the rest of the SDE (materials/products/activity, which every
# existing feature depends on) still builds successfully; only the new
# invention readers degrade to None/[] (has_invention_data() == False).
PROBABILITIES_URL = f"{FUZZWORK_BASE}/industryActivityProbabilities.csv"
SKILLS_URL = f"{FUZZWORK_BASE}/industryActivitySkills.csv"

# Activity IDs (EVE industryActivity table).
ACTIVITY_MANUFACTURING = 1
ACTIVITY_RESEARCH_TE = 3   # time-efficiency research (Research skill)
ACTIVITY_RESEARCH_ME = 4   # material-efficiency research (Metallurgy skill)
ACTIVITY_COPYING = 5
ACTIVITY_INVENTION = 8
ACTIVITY_REACTION = 11
# We import the time column for ALL activities (the table is one row per
# blueprint per activity — small) so the Phase 4 research popup (ME/TE times)
# needs no second re-import.


@dataclass
class BlueprintMaterial:
    """A single material requirement for a blueprint."""
    type_id: int
    quantity: int


class SDEIndustryDB:
    """Manages SDE industry data for blueprint/material lookups."""

    def __init__(self):
        self.data_dir = get_data_dir()
        self.db_path = self.data_dir / INDUSTRY_DB_FILE
        self.version_path = self.data_dir / INDUSTRY_VERSION_FILE

        # Caches
        self._product_to_blueprint: Dict[int, int] = {}
        self._blueprint_to_product: Dict[int, int] = {}
        self._blueprint_materials: Dict[int, List[BlueprintMaterial]] = {}
        self._product_output: Dict[int, int] = {}
        # Phase 4 base-time cache. _has_activity_time is a tri-state column-
        # presence flag (None = unchecked) so old SDEs are detected once.
        self._activity_time: Dict[Tuple[int, int], Optional[int]] = {}
        self._has_activity_time: Optional[bool] = None

        # Stage 5.1 caches. _has_activity_id_col / _has_invention are tri-
        # state (None = unchecked) so old (pre-5.1) SDEs are detected once,
        # mirroring _has_activity_time above.
        self._has_activity_id_col: Optional[bool] = None
        self._has_invention: Optional[bool] = None
        self._producer_cache: Dict[Tuple[int, int], Optional[int]] = {}
        self._recipe_activity_cache: Dict[Tuple[int, int], Optional[Dict[str, Any]]] = {}
        self._skills_req_cache: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
        self._invention_cache: Dict[int, Optional[Dict[str, Any]]] = {}
        self._invention_sources_cache: Dict[int, List[int]] = {}
        # warm_cache() bulk-loads the COMPLETE tables into the memo dicts
        # above. Once True, a memo miss is authoritative (the row genuinely
        # doesn't exist) — readers return their None/[]/0 without opening a
        # connection. The per-call readers open+close a connection per miss
        # (~4ms each), which cost the Industry tab ~60s across its ~15k
        # first-compute lookups before this existed (2026-07-10).
        self._fully_warmed: bool = False

    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"Industry database not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def is_available(self) -> bool:
        """Check if database exists and is usable."""
        if not self.db_path.exists():
            return False
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM industry_materials LIMIT 1")
            count = cursor.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False

    def get_version_info(self) -> Dict[str, Any]:
        """Get version info (download date, record counts)."""
        if not self.version_path.exists():
            return {}
        try:
            with open(self.version_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}

    def get_age_days(self) -> Optional[int]:
        """Get age of data in days."""
        info = self.get_version_info()
        download_date = info.get("download_date")
        if not download_date:
            return None
        try:
            downloaded = datetime.fromisoformat(download_date)
            return (datetime.now() - downloaded).days
        except Exception:
            return None

    # =========================================================================
    # Stage 5.1 — schema detection
    # =========================================================================
    #
    # Backward compatibility: DBs built before Stage 5.1 have NO activity_id
    # column on industry_materials/industry_products (they only ever held
    # manufacturing rows, filtered at import time). DBs built by this version
    # carry activity_id on both tables and hold ALL activities. Every reader
    # below that existed before Stage 5.1 detects which schema is present
    # (once, cached) and branches its WHERE clause so its return value is
    # IDENTICAL either way — old installs keep working un-re-downloaded, and
    # new installs get exactly the same manufacturing-only rows out of the old
    # readers. Mirrors sde_manager's has_meta_group_data column-presence check.

    def _schema_has_activity_id(self) -> bool:
        """True if industry_materials/industry_products carry activity_id
        (Stage 5.1 schema). False for pre-5.1 DBs (manufacturing-only,
        3-column PK). Cached tri-state; reset on download_and_build."""
        if self._has_activity_id_col is not None:
            return self._has_activity_id_col
        try:
            conn = self._get_conn()
            cols = [row[1] for row in conn.execute("PRAGMA table_info(industry_materials)")]
            conn.close()
            self._has_activity_id_col = "activity_id" in cols
        except Exception:
            self._has_activity_id_col = False
        return self._has_activity_id_col

    def has_invention_data(self) -> bool:
        """True if industry_probabilities exists and is populated.

        False for: pre-5.1 DBs (table doesn't exist), or a 5.1+ DB whose
        optional probabilities download failed/was skipped (table exists but
        empty — see download_and_build). Either way invention readers
        (get_invention / get_invention_sources) degrade to None/[] instead of
        raising. Mirrors has_activity_time_data()'s table-presence check.
        """
        if self._has_invention is not None:
            return self._has_invention
        try:
            conn = self._get_conn()
            row = conn.execute("SELECT COUNT(*) FROM industry_probabilities").fetchone()
            conn.close()
            self._has_invention = bool(row and row[0] > 0)
        except Exception:
            self._has_invention = False
        return self._has_invention

    # =========================================================================
    # Lookup Methods (pre-5.1 — manufacturing/activity-1 only, UNCHANGED semantics)
    # =========================================================================

    def get_blueprint_for_item(self, type_id: int) -> Optional[int]:
        """Get the blueprint ID that produces this item.

        Args:
            type_id: The product type ID

        Returns:
            Blueprint type ID, or None if no blueprint (faction/officer/etc)
        """
        # Check cache
        if type_id in self._product_to_blueprint:
            return self._product_to_blueprint[type_id]
        if self._fully_warmed:  # complete map loaded — miss = no blueprint
            self._product_to_blueprint[type_id] = None
            return None

        try:
            conn = self._get_conn()
            if self._schema_has_activity_id():
                cursor = conn.execute(
                    "SELECT blueprint_id FROM industry_products "
                    "WHERE product_type_id = ? AND activity_id = ?",
                    (type_id, ACTIVITY_MANUFACTURING)
                )
            else:
                cursor = conn.execute(
                    "SELECT blueprint_id FROM industry_products WHERE product_type_id = ?",
                    (type_id,)
                )
            row = cursor.fetchone()
            conn.close()

            if row:
                bp_id = row["blueprint_id"]
                self._product_to_blueprint[type_id] = bp_id
                return bp_id

            # Cache miss (no blueprint)
            self._product_to_blueprint[type_id] = None
            return None

        except Exception as e:
            print(f"[SDEIndustry] Error looking up blueprint for {type_id}: {e}")
            return None

    def get_product_for_blueprint(self, blueprint_id: int) -> Optional[int]:
        """Get the product type_id a blueprint manufactures (reverse of
        get_blueprint_for_item).

        Phase 3 needs this because the owned-blueprints ESI pull gives blueprint
        type_ids, but the engine/recipe path is keyed on the product type_id.
        Manufacturing blueprints are 1:1 with their product. Returns None if the
        blueprint has no manufacturing product (e.g. a copy/research-only BP).
        """
        if blueprint_id in self._blueprint_to_product:
            return self._blueprint_to_product[blueprint_id]
        if self._fully_warmed:  # complete map loaded — miss = no product
            self._blueprint_to_product[blueprint_id] = None
            return None
        try:
            conn = self._get_conn()
            if self._schema_has_activity_id():
                cursor = conn.execute(
                    "SELECT product_type_id FROM industry_products "
                    "WHERE blueprint_id = ? AND activity_id = ?",
                    (blueprint_id, ACTIVITY_MANUFACTURING)
                )
            else:
                cursor = conn.execute(
                    "SELECT product_type_id FROM industry_products WHERE blueprint_id = ?",
                    (blueprint_id,)
                )
            row = cursor.fetchone()
            conn.close()
            product_id = row["product_type_id"] if row else None
            self._blueprint_to_product[blueprint_id] = product_id
            return product_id
        except Exception as e:
            print(f"[SDEIndustry] Error looking up product for blueprint {blueprint_id}: {e}")
            return None

    def get_materials(self, blueprint_id: int) -> List[BlueprintMaterial]:
        """Get MANUFACTURING materials required for a blueprint (activity 1).

        Args:
            blueprint_id: Blueprint type ID

        Returns:
            List of BlueprintMaterial (type_id, quantity)
        """
        # Check cache
        if blueprint_id in self._blueprint_materials:
            return self._blueprint_materials[blueprint_id]
        if self._fully_warmed:  # complete map loaded — miss = no materials
            self._blueprint_materials[blueprint_id] = []
            return []

        try:
            conn = self._get_conn()
            if self._schema_has_activity_id():
                cursor = conn.execute(
                    "SELECT material_type_id, quantity FROM industry_materials "
                    "WHERE blueprint_id = ? AND activity_id = ?",
                    (blueprint_id, ACTIVITY_MANUFACTURING)
                )
            else:
                cursor = conn.execute(
                    "SELECT material_type_id, quantity FROM industry_materials WHERE blueprint_id = ?",
                    (blueprint_id,)
                )

            materials = []
            for row in cursor:
                materials.append(BlueprintMaterial(
                    type_id=row["material_type_id"],
                    quantity=row["quantity"]
                ))
            conn.close()

            self._blueprint_materials[blueprint_id] = materials
            return materials

        except Exception as e:
            print(f"[SDEIndustry] Error looking up materials for blueprint {blueprint_id}: {e}")
            return []

    def get_materials_for_item(self, type_id: int) -> Optional[List[BlueprintMaterial]]:
        """Convenience method: get materials for an item by its type_id.

        Args:
            type_id: Product type ID

        Returns:
            List of BlueprintMaterial, or None if no blueprint exists
        """
        blueprint_id = self.get_blueprint_for_item(type_id)
        if blueprint_id is None:
            return None
        return self.get_materials(blueprint_id)

    def get_output_quantity(self, product_type_id: int) -> int:
        """Units produced per manufacturing run for a product.

        Most items produce 1/run, but charges/drones/etc. produce more (e.g.
        ammo 100/run), which is essential for correct per-unit build cost.
        Returns 0 if the product has no blueprint.
        """
        if product_type_id in self._product_output:
            return self._product_output[product_type_id]
        if self._fully_warmed:  # complete map loaded — miss = no blueprint
            self._product_output[product_type_id] = 0
            return 0
        try:
            conn = self._get_conn()
            if self._schema_has_activity_id():
                cursor = conn.execute(
                    "SELECT quantity FROM industry_products "
                    "WHERE product_type_id = ? AND activity_id = ?",
                    (product_type_id, ACTIVITY_MANUFACTURING)
                )
            else:
                cursor = conn.execute(
                    "SELECT quantity FROM industry_products WHERE product_type_id = ?",
                    (product_type_id,)
                )
            row = cursor.fetchone()
            conn.close()
            qty = int(row["quantity"]) if row else 0
            self._product_output[product_type_id] = qty
            return qty
        except Exception as e:
            print(f"[SDEIndustry] Error looking up output qty for {product_type_id}: {e}")
            return 0

    def get_recipe(self, product_type_id: int) -> Optional[Dict[str, Any]]:
        """Full manufacturing recipe for a product, or None if not buildable.

        Returns {'blueprint_id', 'output_per_run', 'materials': [(type_id,
        base_qty), ...]} using base (ME 0) quantities. Convenience for the
        industry engine's recipe provider.
        """
        blueprint_id = self.get_blueprint_for_item(product_type_id)
        if blueprint_id is None:
            return None
        out = self.get_output_quantity(product_type_id)
        if out <= 0:
            return None
        materials = [(m.type_id, m.quantity)
                     for m in self.get_materials(blueprint_id)]
        return {"blueprint_id": blueprint_id, "output_per_run": out,
                "materials": materials}

    def has_activity_time_data(self) -> bool:
        """True if the per-activity base-time table exists and is populated.

        Added with Phase 4 (build time / research popup). Old installs built
        before this column was imported return False — the caller then hides the
        time/batch-cap features and prompts a re-download (mirrors the
        meta_group / type_materials guards in sde_manager).
        """
        if self._has_activity_time is not None:
            return self._has_activity_time
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM industry_activity"
            ).fetchone()
            conn.close()
            self._has_activity_time = bool(row and row[0] > 0)
        except Exception:
            self._has_activity_time = False
        return self._has_activity_time

    def get_activity_time(self, blueprint_id: int,
                          activity_id: int = ACTIVITY_MANUFACTURING) -> Optional[int]:
        """Base time (seconds per run/level) for one blueprint activity.

        Returns None if the activity-time table is absent (old SDE) or the
        blueprint has no row for that activity.
        """
        key = (int(blueprint_id), int(activity_id))
        if key in self._activity_time:
            return self._activity_time[key]
        if not self.has_activity_time_data():
            return None
        if self._fully_warmed:  # complete table loaded — miss = no row
            self._activity_time[key] = None
            return None
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT time FROM industry_activity "
                "WHERE blueprint_id = ? AND activity_id = ?",
                key,
            ).fetchone()
            conn.close()
            t = int(row["time"]) if row else None
            self._activity_time[key] = t
            return t
        except Exception as e:
            print(f"[SDEIndustry] Error looking up time for {key}: {e}")
            return None

    def get_base_build_time(self, product_type_id: int) -> Optional[int]:
        """Base manufacturing time (seconds/run) for a product, via its blueprint.

        Returns None if the product has no blueprint or the SDE predates the
        activity-time import.
        """
        bp = self.get_blueprint_for_item(product_type_id)
        if bp is None:
            return None
        return self.get_activity_time(bp, ACTIVITY_MANUFACTURING)

    def get_all_manufacturable_items(self) -> List[int]:
        """Get list of all type_ids that have MANUFACTURING blueprints (activity 1)."""
        try:
            conn = self._get_conn()
            if self._schema_has_activity_id():
                cursor = conn.execute(
                    "SELECT DISTINCT product_type_id FROM industry_products "
                    "WHERE activity_id = ?",
                    (ACTIVITY_MANUFACTURING,)
                )
            else:
                cursor = conn.execute("SELECT DISTINCT product_type_id FROM industry_products")
            result = [row[0] for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception:
            return []

    # =========================================================================
    # Lookup methods (Stage 5.1 — all activities: invention/reaction/etc.)
    # =========================================================================

    def get_all_products_for_activity(self, activity_id: int) -> List[int]:
        """All product type_ids producible via the given activity — e.g.
        ACTIVITY_REACTION enumerates every reaction-formula output (moon goo
        intermediates, advanced materials, gas/booster intermediates) for the
        Phase 7 Extra list.

        Pre-5.1 DBs (no activity_id column) only ever held manufacturing rows:
        ACTIVITY_MANUFACTURING delegates to get_all_manufacturable_items, any
        other activity returns [] (degrade, never raise).
        """
        if not self._schema_has_activity_id():
            return (self.get_all_manufacturable_items()
                    if activity_id == ACTIVITY_MANUFACTURING else [])
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT DISTINCT product_type_id FROM industry_products "
                "WHERE activity_id = ?",
                (int(activity_id),)
            )
            result = [row[0] for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception as e:
            print(f"[SDEIndustry] Error listing products for activity "
                  f"{activity_id}: {e}")
            return []

    def get_producer(self, product_type_id: int, activity_id: int) -> Optional[int]:
        """Blueprint/formula type_id that produces this product via the given
        activity. Generalized get_blueprint_for_item — e.g. pass
        ACTIVITY_REACTION to find the formula behind a reaction product, or
        ACTIVITY_INVENTION to find what invents a given T2/T3 blueprint (see
        also get_invention_sources, which returns ALL such sources at once).

        Returns None if the SDE predates Stage 5.1 and activity_id isn't
        ACTIVITY_MANUFACTURING (old DBs only ever held manufacturing rows).
        """
        key = (int(product_type_id), int(activity_id))
        if key in self._producer_cache:
            return self._producer_cache[key]
        if not self._schema_has_activity_id():
            result = (self.get_blueprint_for_item(product_type_id)
                      if activity_id == ACTIVITY_MANUFACTURING else None)
            self._producer_cache[key] = result
            return result
        if self._fully_warmed:  # complete map loaded — miss = no producer
            self._producer_cache[key] = None
            return None
        try:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT blueprint_id FROM industry_products "
                "WHERE product_type_id = ? AND activity_id = ?",
                key,
            ).fetchone()
            conn.close()
            result = row["blueprint_id"] if row else None
            self._producer_cache[key] = result
            return result
        except Exception as e:
            print(f"[SDEIndustry] Error looking up producer for {key}: {e}")
            return None

    def get_recipe_for_activity(self, product_type_id: int,
                                 activity_id: int) -> Optional[Dict[str, Any]]:
        """Like get_recipe but for an arbitrary activity (e.g. ACTIVITY_REACTION
        for a reaction formula's inputs/outputs).

        Returns {'blueprint_id', 'output_per_run', 'materials': [(type_id,
        base_qty), ...]} using base quantities for that activity, or None if
        not producible via that activity (incl. pre-5.1 DBs for any activity
        other than manufacturing).
        """
        key = (int(product_type_id), int(activity_id))
        if key in self._recipe_activity_cache:
            return self._recipe_activity_cache[key]
        if self._fully_warmed:  # complete map loaded — miss = not producible
            self._recipe_activity_cache[key] = None
            return None
        blueprint_id = self.get_producer(product_type_id, activity_id)
        if blueprint_id is None:
            self._recipe_activity_cache[key] = None
            return None
        try:
            conn = self._get_conn()
            out_row = conn.execute(
                "SELECT quantity FROM industry_products "
                "WHERE blueprint_id = ? AND activity_id = ? AND product_type_id = ?",
                (blueprint_id, activity_id, product_type_id),
            ).fetchone()
            mat_rows = conn.execute(
                "SELECT material_type_id, quantity FROM industry_materials "
                "WHERE blueprint_id = ? AND activity_id = ?",
                (blueprint_id, activity_id),
            ).fetchall()
            conn.close()
            out = int(out_row["quantity"]) if out_row else 0
            if out <= 0:
                self._recipe_activity_cache[key] = None
                return None
            materials = [(r["material_type_id"], r["quantity"]) for r in mat_rows]
            result = {"blueprint_id": blueprint_id, "output_per_run": out,
                      "materials": materials}
            self._recipe_activity_cache[key] = result
            return result
        except Exception as e:
            print(f"[SDEIndustry] Error building recipe for activity {key}: {e}")
            return None

    def get_required_skills(self, blueprint_id: int, activity_id: int) -> List[Tuple[int, int]]:
        """(skill_type_id, level) pairs required to run this blueprint activity
        (e.g. the two science skills + encryption skill gating an invention).

        Returns [] for pre-5.1 DBs (table doesn't exist) or if the optional
        skills download failed/was skipped (table exists but no rows for this
        blueprint/activity) — never raises.
        """
        key = (int(blueprint_id), int(activity_id))
        if key in self._skills_req_cache:
            return self._skills_req_cache[key]
        if self._fully_warmed:  # complete table loaded — miss = no skills
            self._skills_req_cache[key] = []
            return []
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT skill_type_id, level FROM industry_skills_req "
                "WHERE blueprint_id = ? AND activity_id = ?",
                key,
            ).fetchall()
            conn.close()
            result = [(r["skill_type_id"], r["level"]) for r in rows]
        except Exception:
            # Table missing entirely (pre-5.1 DB) — not an error, just no data.
            result = []
        self._skills_req_cache[key] = result
        return result

    def get_invention(self, source_blueprint_id: int) -> Optional[Dict[str, Any]]:
        """Activity-8 (invention) data for a T1 blueprint or T3 relic type_id.

        Returns {"products": [{"blueprint_id": int, "probability": float,
        "base_runs": int}, ...], "datacores": [(type_id, qty), ...], "time":
        Optional[int]}. base_runs is the industry_products.quantity for the
        activity-8 row (BPC runs per successful invention, before decryptor
        run modifiers — Phase 5.2's job). datacores are the activity-8
        industry_materials rows (consumed per attempt, incl. on failure).
        time is the base invention job time (industry_activity, activity 8).

        Returns None if this source has no invention data at all (no
        activity-8 product rows) or has_invention_data() is False (pre-5.1
        DB, or the optional probabilities download failed/was skipped).
        """
        if source_blueprint_id in self._invention_cache:
            return self._invention_cache[source_blueprint_id]
        if not self.has_invention_data():
            self._invention_cache[source_blueprint_id] = None
            return None
        if self._fully_warmed:  # complete map loaded — miss = not inventable
            self._invention_cache[source_blueprint_id] = None
            return None
        try:
            conn = self._get_conn()
            prod_rows = conn.execute(
                "SELECT product_type_id, quantity FROM industry_products "
                "WHERE blueprint_id = ? AND activity_id = ?",
                (source_blueprint_id, ACTIVITY_INVENTION),
            ).fetchall()
            if not prod_rows:
                conn.close()
                self._invention_cache[source_blueprint_id] = None
                return None

            products = []
            for r in prod_rows:
                prob_row = conn.execute(
                    "SELECT probability FROM industry_probabilities "
                    "WHERE blueprint_id = ? AND activity_id = ? AND product_type_id = ?",
                    (source_blueprint_id, ACTIVITY_INVENTION, r["product_type_id"]),
                ).fetchone()
                products.append({
                    "blueprint_id": r["product_type_id"],
                    "probability": float(prob_row["probability"]) if prob_row else 0.0,
                    "base_runs": int(r["quantity"]),
                })

            datacore_rows = conn.execute(
                "SELECT material_type_id, quantity FROM industry_materials "
                "WHERE blueprint_id = ? AND activity_id = ?",
                (source_blueprint_id, ACTIVITY_INVENTION),
            ).fetchall()
            conn.close()
            datacores = [(r["material_type_id"], r["quantity"]) for r in datacore_rows]

            time = self.get_activity_time(source_blueprint_id, ACTIVITY_INVENTION)
            result = {"products": products, "datacores": datacores, "time": time}
            self._invention_cache[source_blueprint_id] = result
            return result
        except Exception as e:
            print(f"[SDEIndustry] Error looking up invention data for {source_blueprint_id}: {e}")
            return None

    def get_invention_sources(self, invented_blueprint_id: int) -> List[int]:
        """All source blueprint/relic type_ids whose activity-8 product row
        invents this blueprint (reverse of get_invention). A T2 blueprint
        normally has exactly 1 source T1 blueprint; a T3 blueprint has 3
        (Intact/Malfunctioning/Wrecked relic quality).

        Returns [] if none exist or has_invention_data() is False.
        """
        if invented_blueprint_id in self._invention_sources_cache:
            return self._invention_sources_cache[invented_blueprint_id]
        if not self.has_invention_data():
            self._invention_sources_cache[invented_blueprint_id] = []
            return []
        if self._fully_warmed:  # complete map loaded — miss = no sources
            self._invention_sources_cache[invented_blueprint_id] = []
            return []
        try:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT blueprint_id FROM industry_products "
                "WHERE activity_id = ? AND product_type_id = ?",
                (ACTIVITY_INVENTION, invented_blueprint_id),
            ).fetchall()
            conn.close()
            result = [r["blueprint_id"] for r in rows]
            self._invention_sources_cache[invented_blueprint_id] = result
            return result
        except Exception as e:
            print(f"[SDEIndustry] Error looking up invention sources for {invented_blueprint_id}: {e}")
            return []

    # =========================================================================
    # Bulk cache warm (2026-07-10)
    # =========================================================================

    def warm_cache(self) -> bool:
        """Bulk-prime every per-item memo dict with a handful of table scans
        on ONE connection, then mark the caches authoritative
        (`_fully_warmed`) so misses answer without touching the DB.

        Every reader above opens+closes its own sqlite connection per memo
        miss (~4ms each). The Industry tab's first compute makes ~15k such
        lookups (recipe universe scan, chain expansion, invention-source
        scan), which measured at ~60s of its 80s total — this replaces that
        with <1s of bulk loading. Values are assembled into local dicts
        first and swapped in atomically at the end, so a failure part-way
        leaves the instance exactly as it was (per-call readers still work).

        Safe to call repeatedly (no-op once warmed) and from a worker thread.
        Returns False if the DB is unavailable or the load failed — callers
        just fall back to the per-call readers.
        """
        if self._fully_warmed:
            return True
        try:
            has_act_col = self._schema_has_activity_id()
            conn = self._get_conn()

            product_to_bp: Dict[int, Optional[int]] = {}
            bp_to_product: Dict[int, Optional[int]] = {}
            product_output: Dict[int, int] = {}
            bp_materials: Dict[int, List[BlueprintMaterial]] = {}
            producer: Dict[Tuple[int, int], Optional[int]] = {}
            recipes: Dict[Tuple[int, int], Optional[Dict[str, Any]]] = {}
            mats_by_bp_act: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
            activity_time: Dict[Tuple[int, int], Optional[int]] = {}
            skills_req: Dict[Tuple[int, int], List[Tuple[int, int]]] = {}
            inv_products: Dict[int, List[Dict[str, Any]]] = {}
            inv_sources: Dict[int, List[int]] = {}
            probabilities: Dict[Tuple[int, int], float] = {}

            # -- materials (one scan) --------------------------------------
            if has_act_col:
                mat_rows = conn.execute(
                    "SELECT blueprint_id, activity_id, material_type_id, "
                    "quantity FROM industry_materials").fetchall()
            else:
                mat_rows = conn.execute(
                    "SELECT blueprint_id, ? AS activity_id, material_type_id, "
                    "quantity FROM industry_materials",
                    (ACTIVITY_MANUFACTURING,)).fetchall()
            for r in mat_rows:
                bp, act = r["blueprint_id"], r["activity_id"]
                mats_by_bp_act.setdefault((bp, act), []).append(
                    (r["material_type_id"], r["quantity"]))
                if act == ACTIVITY_MANUFACTURING:
                    bp_materials.setdefault(bp, []).append(BlueprintMaterial(
                        type_id=r["material_type_id"],
                        quantity=r["quantity"]))

            # -- probabilities (optional table; one scan) -------------------
            has_inv = False
            try:
                for r in conn.execute(
                        "SELECT blueprint_id, product_type_id, probability "
                        "FROM industry_probabilities "
                        "WHERE activity_id = ?", (ACTIVITY_INVENTION,)):
                    probabilities[(r["blueprint_id"], r["product_type_id"])] = \
                        float(r["probability"])
                has_inv = bool(probabilities)
            except sqlite3.Error:
                pass  # pre-5.1 DB — table absent
            self._has_invention = has_inv

            # -- activity times (optional table; one scan) ------------------
            has_time = False
            try:
                for r in conn.execute(
                        "SELECT blueprint_id, activity_id, time "
                        "FROM industry_activity"):
                    activity_time[(int(r["blueprint_id"]),
                                   int(r["activity_id"]))] = int(r["time"])
                    has_time = True
            except sqlite3.Error:
                pass  # pre-Phase-4 DB — table absent
            self._has_activity_time = has_time

            # -- skill requirements (optional table; one scan) --------------
            try:
                for r in conn.execute(
                        "SELECT blueprint_id, activity_id, skill_type_id, "
                        "level FROM industry_skills_req"):
                    skills_req.setdefault(
                        (int(r["blueprint_id"]), int(r["activity_id"])),
                        []).append((r["skill_type_id"], r["level"]))
            except sqlite3.Error:
                pass  # pre-5.1 DB — table absent

            # -- products (one scan; drives most maps) ----------------------
            # First row wins for the single-row lookups, matching the
            # per-call readers' un-ORDERed fetchone() over the same scan.
            if has_act_col:
                prod_rows = conn.execute(
                    "SELECT blueprint_id, activity_id, product_type_id, "
                    "quantity FROM industry_products").fetchall()
            else:
                prod_rows = conn.execute(
                    "SELECT blueprint_id, ? AS activity_id, product_type_id, "
                    "quantity FROM industry_products",
                    (ACTIVITY_MANUFACTURING,)).fetchall()
            conn.close()
            for r in prod_rows:
                bp, act, ptid = (r["blueprint_id"], r["activity_id"],
                                 r["product_type_id"])
                qty = int(r["quantity"])
                if act == ACTIVITY_MANUFACTURING:
                    if ptid not in product_to_bp:
                        product_to_bp[ptid] = bp
                        product_output[ptid] = qty
                    if bp not in bp_to_product:
                        bp_to_product[bp] = ptid
                if (ptid, act) not in producer:
                    producer[(ptid, act)] = bp
                    recipes[(ptid, act)] = (
                        {"blueprint_id": bp, "output_per_run": qty,
                         "materials": mats_by_bp_act.get((bp, act), [])}
                        if qty > 0 else None)
                if act == ACTIVITY_INVENTION:
                    inv_sources.setdefault(ptid, []).append(bp)
                    inv_products.setdefault(bp, []).append({
                        "blueprint_id": ptid,
                        "probability": probabilities.get((bp, ptid), 0.0),
                        "base_runs": qty,
                    })

            invention: Dict[int, Optional[Dict[str, Any]]] = {}
            if has_inv:
                for src, prods in inv_products.items():
                    invention[src] = {
                        "products": prods,
                        "datacores": mats_by_bp_act.get(
                            (src, ACTIVITY_INVENTION), []),
                        "time": activity_time.get((src, ACTIVITY_INVENTION)),
                    }

            # -- atomic swap-in ---------------------------------------------
            self._product_to_blueprint = product_to_bp
            self._blueprint_to_product = bp_to_product
            self._product_output = product_output
            self._blueprint_materials = bp_materials
            self._producer_cache = producer
            self._recipe_activity_cache = recipes
            self._activity_time = activity_time
            self._skills_req_cache = skills_req
            self._invention_cache = invention
            self._invention_sources_cache = inv_sources
            self._fully_warmed = True
            return True
        except Exception as e:
            print(f"[SDEIndustry] warm_cache failed (per-call reads still "
                  f"work): {e}")
            return False

    # =========================================================================
    # Download and Build
    # =========================================================================

    async def download_and_build(
        self,
        progress_callback: Optional[Callable[[str, int], None]] = None
    ) -> bool:
        """Download SDE industry data and build database.

        Args:
            progress_callback: Optional callback(status_message, percent)

        Returns:
            True if successful
        """
        def update(msg: str, pct: int):
            print(f"[SDEIndustry] {msg}")
            if progress_callback:
                progress_callback(msg, pct)

        update("Starting industry data download...", 0)

        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(connector=make_connector(), headers={"User-Agent": ESI_USER_AGENT}, timeout=timeout) as session:

                # Download materials CSV
                update("Downloading materials data...", 5)
                materials_data = await self._download_csv(session, MATERIALS_URL, update, 5, 25)
                if materials_data is None:
                    return False

                # Download products CSV
                update("Downloading products data...", 25)
                products_data = await self._download_csv(session, PRODUCTS_URL, update, 25, 40)
                if products_data is None:
                    return False

                # Download per-activity base-time CSV (Phase 4)
                update("Downloading activity time data...", 40)
                activity_data = await self._download_csv(session, ACTIVITY_URL, update, 40, 48)
                if activity_data is None:
                    return False

                # Stage 5.1: invention probability + skill-requirement data.
                # OPTIONAL — a failure here (network hiccup, Fuzzwork renaming
                # the file, ...) must not sink the materials/products/activity
                # data every existing feature depends on. Degrades to an empty
                # CSV (0 rows imported); has_invention_data() then reports
                # False and the new invention readers return None/[].
                update("Downloading invention probability data...", 48)
                try:
                    probabilities_data = await self._download_csv(
                        session, PROBABILITIES_URL, update, 48, 56)
                except Exception as e:
                    update(f"Probabilities download error (optional, continuing): {e}", 48)
                    probabilities_data = None
                if probabilities_data is None:
                    update("Probabilities data unavailable — invention lookups will be disabled.", 56)
                    probabilities_data = ""

                update("Downloading activity skill requirements...", 56)
                try:
                    skills_data = await self._download_csv(
                        session, SKILLS_URL, update, 56, 62)
                except Exception as e:
                    update(f"Skills download error (optional, continuing): {e}", 56)
                    skills_data = None
                if skills_data is None:
                    update("Skill-requirement data unavailable — required-skills lookups will be empty.", 62)
                    skills_data = ""

            # Build the replacement at <db>.new; build_then_swap swaps it
            # in only on success, so a failed download/build leaves the
            # previous working DB in place (finding 5-5).
            update("Building database...", 62)
            with build_then_swap(self.db_path) as tmp_path:
                (materials_count, products_count, activity_count,
                 probabilities_count, skills_req_count) = self._build_database(
                    materials_data, products_data, activity_data,
                    probabilities_data, skills_data, update, tmp_path
                )

            # Clear caches so readers reload from the new DB
            self._fully_warmed = False   # next warm_cache() re-loads
            self._product_to_blueprint.clear()
            self._blueprint_to_product.clear()
            self._blueprint_materials.clear()
            self._product_output.clear()
            self._activity_time.clear()
            self._has_activity_time = None
            self._has_activity_id_col = None
            self._has_invention = None
            self._producer_cache.clear()
            self._recipe_activity_cache.clear()
            self._skills_req_cache.clear()
            self._invention_cache.clear()
            self._invention_sources_cache.clear()

            # Save version info
            version_info = {
                "download_date": datetime.now().isoformat(),
                "source": "fuzzwork",
                "materials_count": materials_count,
                "products_count": products_count,
                "activity_count": activity_count,
                "probabilities_count": probabilities_count,
                "skills_req_count": skills_req_count,
            }
            with open(self.version_path, "w") as f:
                json.dump(version_info, f, indent=2)

            update(f"Complete: {materials_count:,} materials, "
                   f"{products_count:,} products, {activity_count:,} activity times, "
                   f"{probabilities_count:,} invention probabilities, "
                   f"{skills_req_count:,} skill requirements", 100)
            return True

        except Exception as e:
            # The working DB (if any) was never touched — build_then_swap
            # already removed the partial .new file.
            update(f"Error: {e}", 0)
            return False

    async def _download_csv(
        self,
        session: aiohttp.ClientSession,
        url: str,
        update: Callable,
        start_pct: int,
        end_pct: int
    ) -> Optional[str]:
        """Download a CSV file."""
        try:
            async with session.get(url) as response:
                if response.status != 200:
                    update(f"Download failed: HTTP {response.status}", start_pct)
                    return None

                total_size = int(response.headers.get("content-length", 0))
                downloaded = 0
                chunks = []

                async for chunk in response.content.iter_chunked(65536):
                    chunks.append(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = int(start_pct + (downloaded / total_size) * (end_pct - start_pct))
                        update(f"Downloading... {downloaded // 1024}KB", pct)

                # utf-8-sig: the current Fuzzwork CSVs start with a BOM,
                # which would otherwise corrupt the first column name.
                return b"".join(chunks).decode("utf-8-sig")

        except Exception as e:
            update(f"Download error: {e}", start_pct)
            return None

    def _build_database(
        self,
        materials_csv: str,
        products_csv: str,
        activity_csv: str,
        probabilities_csv: str,
        skills_csv: str,
        update: Callable,
        db_path: Path
    ) -> Tuple[int, int, int, int, int]:
        """Build SQLite database from CSV data at db_path (a swap temp file).

        Returns (materials_count, products_count, activity_count,
        probabilities_count, skills_req_count).
        """
        conn = sqlite3.connect(str(db_path))

        # Stage 5.1: activity_id joins the PK on both tables so ALL activities
        # (manufacturing, research, copying, invention, reaction, ...) can
        # coexist per blueprint. Pre-5.1 readers filter to activity_id = 1
        # (ACTIVITY_MANUFACTURING) to keep their exact prior behavior — see
        # _schema_has_activity_id.
        conn.execute("""
            CREATE TABLE industry_materials (
                blueprint_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                material_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, activity_id, material_type_id)
            )
        """)

        conn.execute("""
            CREATE TABLE industry_products (
                blueprint_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                product_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, activity_id, product_type_id)
            )
        """)

        # Per-activity base time (seconds/run). All activities kept so the
        # Phase 4 research popup (ME act 4 / TE act 3) needs no re-import.
        conn.execute("""
            CREATE TABLE industry_activity (
                blueprint_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                time INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, activity_id)
            )
        """)

        # Stage 5.1: invention base probability per (source blueprint,
        # activity 8, invented blueprint). Populated from the OPTIONAL
        # industryActivityProbabilities.csv download; may be empty.
        conn.execute("""
            CREATE TABLE industry_probabilities (
                blueprint_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                product_type_id INTEGER NOT NULL,
                probability REAL NOT NULL,
                PRIMARY KEY (blueprint_id, activity_id, product_type_id)
            )
        """)

        # Stage 5.1: (skill, level) requirements per blueprint activity —
        # e.g. the two datacore sciences + the encryption skill gating an
        # invention job. Populated from the OPTIONAL
        # industryActivitySkills.csv download; may be empty.
        conn.execute("""
            CREATE TABLE industry_skills_req (
                blueprint_id INTEGER NOT NULL,
                activity_id INTEGER NOT NULL,
                skill_type_id INTEGER NOT NULL,
                level INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, activity_id, skill_type_id)
            )
        """)

        # Index for reverse lookup (item -> blueprint), now activity-aware so
        # e.g. "what invents this blueprint" (activity 8) and "what
        # manufactures this item" (activity 1) can both use it.
        conn.execute(
            "CREATE INDEX idx_product_type ON industry_products(product_type_id, activity_id)"
        )

        # Parse and insert materials for ALL activities.
        # Columns: typeID,activityID,materialTypeID,quantity
        update("Importing materials...", 65)
        materials_count = 0
        reader = csv.DictReader(io.StringIO(materials_csv))
        batch = []

        for row in reader:
            try:
                batch.append((
                    int(row["typeID"]),          # blueprint_id
                    int(row["activityID"]),
                    int(row["materialTypeID"]),
                    int(row["quantity"])
                ))
                materials_count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Materials: {materials_count:,}...", 70)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?, ?)",
                batch
            )

        # Parse and insert products for ALL activities.
        # Columns: typeID,activityID,productTypeID,quantity
        update("Importing products...", 76)
        products_count = 0
        reader = csv.DictReader(io.StringIO(products_csv))
        batch = []

        for row in reader:
            try:
                batch.append((
                    int(row["typeID"]),           # blueprint_id
                    int(row["activityID"]),
                    int(row["productTypeID"]),
                    int(row["quantity"])
                ))
                products_count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Products: {products_count:,}...", 80)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?, ?)",
                batch
            )

        # Parse and insert per-activity base times.
        # Columns: typeID,activityID,time
        update("Importing activity times...", 85)
        activity_count = 0
        reader = csv.DictReader(io.StringIO(activity_csv))
        batch = []

        for row in reader:
            try:
                t = int(row["time"])
                if t <= 0:
                    continue  # 0-time rows carry no useful info
                batch.append((
                    int(row["typeID"]),        # blueprint_id
                    int(row["activityID"]),
                    t,
                ))
                activity_count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_activity VALUES (?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Activity times: {activity_count:,}...", 88)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_activity VALUES (?, ?, ?)",
                batch
            )

        # Parse and insert invention probabilities (optional; CSV may be "").
        # Columns: typeID,activityID,productTypeID,probability
        update("Importing invention probabilities...", 91)
        probabilities_count = 0
        reader = csv.DictReader(io.StringIO(probabilities_csv))
        batch = []

        for row in reader:
            try:
                batch.append((
                    int(row["typeID"]),         # blueprint_id
                    int(row["activityID"]),
                    int(row["productTypeID"]),
                    float(row["probability"]),
                ))
                probabilities_count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_probabilities VALUES (?, ?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Probabilities: {probabilities_count:,}...", 93)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_probabilities VALUES (?, ?, ?, ?)",
                batch
            )

        # Parse and insert per-activity skill requirements (optional; CSV may
        # be ""). Columns: typeID,activityID,skillID,level
        update("Importing activity skill requirements...", 95)
        skills_req_count = 0
        reader = csv.DictReader(io.StringIO(skills_csv))
        batch = []

        for row in reader:
            try:
                batch.append((
                    int(row["typeID"]),         # blueprint_id
                    int(row["activityID"]),
                    int(row["skillID"]),
                    int(row["level"]),
                ))
                skills_req_count += 1

                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_skills_req VALUES (?, ?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Skill requirements: {skills_req_count:,}...", 97)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_skills_req VALUES (?, ?, ?, ?)",
                batch
            )

        conn.commit()
        conn.close()

        return (materials_count, products_count, activity_count,
                probabilities_count, skills_req_count)

    def refresh(self, progress_callback: Optional[Callable[[str, int], None]] = None) -> bool:
        """Synchronous wrapper for download_and_build.

        For use with GUI thread callbacks.
        """
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.download_and_build(progress_callback))
        finally:
            loop.close()


# =============================================================================
# Module-level singleton
# =============================================================================

_instance: Optional[SDEIndustryDB] = None


def get_sde_industry_db() -> SDEIndustryDB:
    """Get or create the singleton SDEIndustryDB instance."""
    global _instance
    if _instance is None:
        _instance = SDEIndustryDB()
    return _instance


def refresh_sde_industry(progress_callback: Optional[Callable[[str, int], None]] = None) -> bool:
    """Force refresh of industry data.

    Convenience function for GUI button callbacks.
    """
    db = get_sde_industry_db()
    return db.refresh(progress_callback)
