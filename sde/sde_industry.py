"""SDE Industry data manager for EVE Market Scout.

Downloads and caches blueprint manufacturing data from Fuzzwork SDE.
Provides lookups for item -> blueprint -> materials relationships.

Data source: Fuzzwork's SDE CSV exports
    https://www.fuzzwork.co.uk/dump/latest/industryActivityMaterials.csv
    https://www.fuzzwork.co.uk/dump/latest/industryActivityProducts.csv

Database location: %APPDATA%/EVEMarketScout/sde_industry.db
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

# Activity IDs (EVE industryActivity table).
ACTIVITY_MANUFACTURING = 1
ACTIVITY_RESEARCH_TE = 3   # time-efficiency research (Research skill)
ACTIVITY_RESEARCH_ME = 4   # material-efficiency research (Metallurgy skill)
ACTIVITY_COPYING = 5
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
    # Lookup Methods
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
        
        try:
            conn = self._get_conn()
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
        try:
            conn = self._get_conn()
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
        """Get materials required for a blueprint.
        
        Args:
            blueprint_id: Blueprint type ID
            
        Returns:
            List of BlueprintMaterial (type_id, quantity)
        """
        # Check cache
        if blueprint_id in self._blueprint_materials:
            return self._blueprint_materials[blueprint_id]
        
        try:
            conn = self._get_conn()
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
        try:
            conn = self._get_conn()
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
        """Get list of all type_ids that have blueprints (can be manufactured)."""
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT DISTINCT product_type_id FROM industry_products")
            result = [row[0] for row in cursor.fetchall()]
            conn.close()
            return result
        except Exception:
            return []
    
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
                materials_data = await self._download_csv(session, MATERIALS_URL, update, 5, 30)
                if materials_data is None:
                    return False
                
                # Download products CSV
                update("Downloading products data...", 35)
                products_data = await self._download_csv(session, PRODUCTS_URL, update, 35, 48)
                if products_data is None:
                    return False

                # Download per-activity base-time CSV (Phase 4)
                update("Downloading activity time data...", 48)
                activity_data = await self._download_csv(session, ACTIVITY_URL, update, 48, 55)
                if activity_data is None:
                    return False

            # Build the replacement at <db>.new; build_then_swap swaps it
            # in only on success, so a failed download/build leaves the
            # previous working DB in place (finding 5-5).
            update("Building database...", 55)
            with build_then_swap(self.db_path) as tmp_path:
                materials_count, products_count, activity_count = self._build_database(
                    materials_data, products_data, activity_data, update, tmp_path
                )

            # Clear caches so readers reload from the new DB
            self._product_to_blueprint.clear()
            self._blueprint_materials.clear()
            self._activity_time.clear()
            self._has_activity_time = None

            # Save version info
            version_info = {
                "download_date": datetime.now().isoformat(),
                "source": "fuzzwork",
                "materials_count": materials_count,
                "products_count": products_count,
                "activity_count": activity_count,
            }
            with open(self.version_path, "w") as f:
                json.dump(version_info, f, indent=2)
            
            update(f"Complete: {materials_count:,} materials, "
                   f"{products_count:,} products, {activity_count:,} activity times", 100)
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
        update: Callable,
        db_path: Path
    ) -> Tuple[int, int, int]:
        """Build SQLite database from CSV data at db_path (a swap temp file)."""
        conn = sqlite3.connect(str(db_path))

        # Create tables
        conn.execute("""
            CREATE TABLE industry_materials (
                blueprint_id INTEGER NOT NULL,
                material_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, material_type_id)
            )
        """)

        conn.execute("""
            CREATE TABLE industry_products (
                blueprint_id INTEGER NOT NULL,
                product_type_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                PRIMARY KEY (blueprint_id, product_type_id)
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
        
        # Index for reverse lookup (item -> blueprint)
        conn.execute(
            "CREATE INDEX idx_product_type ON industry_products(product_type_id)"
        )
        
        # Parse and insert materials
        # Columns: typeID,activityID,materialTypeID,quantity
        update("Importing materials...", 60)
        materials_count = 0
        reader = csv.DictReader(io.StringIO(materials_csv))
        batch = []
        
        for row in reader:
            try:
                activity_id = int(row["activityID"])
                if activity_id != ACTIVITY_MANUFACTURING:
                    continue
                
                batch.append((
                    int(row["typeID"]),          # blueprint_id
                    int(row["materialTypeID"]),
                    int(row["quantity"])
                ))
                materials_count += 1
                
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Materials: {materials_count:,}...", 65)
                    
            except (ValueError, KeyError):
                continue
        
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_materials VALUES (?, ?, ?)",
                batch
            )
        
        # Parse and insert products
        # Columns: typeID,activityID,productTypeID,quantity
        update("Importing products...", 80)
        products_count = 0
        reader = csv.DictReader(io.StringIO(products_csv))
        batch = []
        
        for row in reader:
            try:
                activity_id = int(row["activityID"])
                if activity_id != ACTIVITY_MANUFACTURING:
                    continue
                
                batch.append((
                    int(row["typeID"]),           # blueprint_id
                    int(row["productTypeID"]),
                    int(row["quantity"])
                ))
                products_count += 1
                
                if len(batch) >= 5000:
                    conn.executemany(
                        "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?)",
                        batch
                    )
                    batch = []
                    update(f"Products: {products_count:,}...", 85)
                    
            except (ValueError, KeyError):
                continue
        
        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_products VALUES (?, ?, ?)",
                batch
            )

        # Parse and insert per-activity base times.
        # Columns: typeID,activityID,time
        update("Importing activity times...", 90)
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
                    update(f"Activity times: {activity_count:,}...", 92)

            except (ValueError, KeyError):
                continue

        if batch:
            conn.executemany(
                "INSERT OR REPLACE INTO industry_activity VALUES (?, ?, ?)",
                batch
            )

        conn.commit()
        conn.close()

        return materials_count, products_count, activity_count
    
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
