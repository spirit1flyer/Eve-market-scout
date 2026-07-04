"""SDE (Static Data Export) manager for EVE Market Scout.

Provides local SQLite database of item types for instant lookups.
Eliminates ESI API calls for type names and enables future features
like cargo volume filtering and market group categorization.

Data source: Fuzzwork's SDE SQLite conversion
https://www.fuzzwork.co.uk/dump/latest/

Database location: %APPDATA%/EVEMarketScout/sde_types.db (Windows)
"""

import os
import sqlite3
import json
import asyncio
import aiohttp
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass

from core.sound_manager import get_data_dir
from core.ssl_context import make_connector
from core.config import ESI_USER_AGENT
from sde.sde_swap import build_then_swap


# File locations
SDE_DB_FILE = "sde_types.db"
SDE_VERSION_FILE = "sde_version.json"

# Fuzzwork SDE downloads
# invTypes for item-level data, invMarketGroups for the in-game market tree
# (the same hierarchy shown when a player opens the Regional Market window).
# Fuzzwork moved the CSV exports into a csv/ subdirectory and dropped the
# .bz2 variants (mid-2026); the old /dump/latest/*.csv.bz2 paths now 404.
FUZZWORK_BASE = "https://www.fuzzwork.co.uk/dump/latest/csv"
FUZZWORK_TYPES_URL = f"{FUZZWORK_BASE}/invTypes.csv"
FUZZWORK_MARKET_GROUPS_URL = f"{FUZZWORK_BASE}/invMarketGroups.csv"
# Reprocessing yields: typeID -> materialTypeID -> quantity (per portion_size
# units of the source type). Powers the Reprocess-or-Sell module. Covers
# non-mineral outputs too (Morphite, components), so materialTypeID is generic.
FUZZWORK_TYPE_MATERIALS_URL = f"{FUZZWORK_BASE}/invTypeMaterials.csv"
# Meta groups: typeID -> metaGroupID (1 Tech I, 2 Tech II, 3 Storyline,
# 4 Faction, 5 Officer, 6 Deadspace, 14 Tech III, 15 Abyssal, ...). The legacy
# invMetaTypes join table; typeIDs absent from it are treated as Tech I. Powers
# the Industry tab's tech-level filter (T1/T2/Faction/T3). Tech level is
# independent of BPO availability — Triglavian hulls are Tech I but BPC-only.
FUZZWORK_META_TYPES_URL = f"{FUZZWORK_BASE}/invMetaTypes.csv"

# How old before we suggest updating (days)
SDE_STALE_DAYS = 30


# Fuzzwork's CSV exports use the literal string "None" for NULL cells (not an
# empty cell). Both helpers below treat "" and "None" as the absence of a
# value rather than letting int()/float() raise.
def _parse_optional_int(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "None" or s == "NULL":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_optional_float(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s or s == "None" or s == "NULL":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


@dataclass
class TypeInfo:
    """Full type information from SDE."""
    type_id: int
    name: str
    volume: float  # Packaged volume in m3
    market_group_id: Optional[int]
    published: bool
    portion_size: int
    group_id: Optional[int] = None  # Item group (e.g., "Frigate", "Shield Hardener")
    meta_group_id: Optional[int] = None  # 1 Tech I, 2 Tech II, 4 Faction, 14 Tech III, … (None = Tech I)


@dataclass
class MarketGroupInfo:
    """invMarketGroups row: one node in the market tree shown in-game.

    `parent_group_id` is None for top-level entries (Ammunition & Charges,
    Ship Equipment, etc.). The hierarchy can be many levels deep — items
    live at leaves and we walk up via parent_group_id to find ancestors.
    """
    market_group_id: int
    name: str
    parent_group_id: Optional[int]


class SDEManager:
    """Manages local SDE database for type lookups."""
    
    def __init__(self):
        self.data_dir = get_data_dir()
        self.db_path = self.data_dir / SDE_DB_FILE
        self.version_path = self.data_dir / SDE_VERSION_FILE
        
        # In-memory cache for hot lookups
        self._name_cache: Dict[int, str] = {}
        self._info_cache: Dict[int, TypeInfo] = {}
        # Market-group ancestry lives entirely in memory after the first load.
        # The full set is small (~3-4k rows) so we keep parents + names hot.
        self._mg_parents: Optional[Dict[int, Optional[int]]] = None
        self._mg_names: Dict[int, str] = {}
        # Whether the types table carries the meta_group_id column (added with
        # the Industry tech-level filter). Old installs lack it → re-download.
        self._has_meta_col: Optional[bool] = None
    
    def _get_conn(self) -> sqlite3.Connection:
        """Get database connection. Creates new connection each call for thread safety."""
        if not self.db_path.exists():
            raise FileNotFoundError(f"SDE database not found: {self.db_path}")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def close(self):
        """Close database connection (legacy - now no-op since connections are per-call)."""
        pass
    
    def is_available(self) -> bool:
        """Check if SDE database exists and is usable."""
        if not self.db_path.exists():
            return False
        try:
            conn = self._get_conn()
            cursor = conn.execute("SELECT COUNT(*) FROM types LIMIT 1")
            count = cursor.fetchone()[0]
            conn.close()
            return count > 0
        except Exception:
            return False
    
    def get_version_info(self) -> Dict[str, Any]:
        """Get SDE version info (download date, record count, etc)."""
        if not self.version_path.exists():
            return {}
        try:
            with open(self.version_path, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    
    def is_stale(self) -> bool:
        """Check if SDE is older than threshold."""
        info = self.get_version_info()
        if not info:
            return True
        
        download_date = info.get("download_date")
        if not download_date:
            return True
        
        try:
            downloaded = datetime.fromisoformat(download_date)
            age_days = (datetime.now() - downloaded).days
            return age_days > SDE_STALE_DAYS
        except Exception:
            return True
    
    def get_age_days(self) -> Optional[int]:
        """Get age of SDE in days, or None if unknown."""
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
    # LOOKUP METHODS
    # =========================================================================
    
    def get_type_name(self, type_id: int) -> Optional[str]:
        """
        Get item name by type ID.
        
        Returns None if not found (caller should fall back to ESI).
        """
        # Check cache first
        if type_id in self._name_cache:
            return self._name_cache[type_id]
        
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT name FROM types WHERE type_id = ?",
                (type_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                name = row["name"]
                self._name_cache[type_id] = name
                return name
            return None
        except Exception:
            return None
    
    def get_type_names_bulk(self, type_ids: list[int]) -> Dict[int, str]:
        """
        Get names for multiple type IDs.
        
        Returns dict of {type_id: name}.
        Missing IDs are not included in result.
        """
        result = {}
        uncached = []
        
        # Check cache first
        for tid in type_ids:
            if tid in self._name_cache:
                result[tid] = self._name_cache[tid]
            else:
                uncached.append(tid)
        
        if not uncached:
            return result
        
        try:
            conn = self._get_conn()
            # SQLite has a limit on placeholders, batch if needed
            BATCH_SIZE = 500
            for i in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[i:i + BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                cursor = conn.execute(
                    f"SELECT type_id, name FROM types WHERE type_id IN ({placeholders})",
                    batch
                )
                for row in cursor:
                    tid = row["type_id"]
                    name = row["name"]
                    result[tid] = name
                    self._name_cache[tid] = name
            conn.close()
        except Exception:
            pass
        
        return result
    
    def get_type_info(self, type_id: int) -> Optional[TypeInfo]:
        """
        Get full type information.
        
        Returns None if not found.
        """
        # Check cache
        if type_id in self._info_cache:
            return self._info_cache[type_id]
        
        meta_col = ", meta_group_id" if self.has_meta_group_data() else ""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                f"""SELECT type_id, name, volume, market_group_id,
                          published, portion_size, group_id{meta_col}
                   FROM types WHERE type_id = ?""",
                (type_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                info = TypeInfo(
                    type_id=row["type_id"],
                    name=row["name"],
                    volume=row["volume"] or 0.0,
                    market_group_id=row["market_group_id"],
                    published=bool(row["published"]),
                    portion_size=row["portion_size"] or 1,
                    group_id=row["group_id"],
                    meta_group_id=(row["meta_group_id"] if meta_col else None),
                )
                self._info_cache[type_id] = info
                return info
            return None
        except Exception:
            return None
    
    def get_type_volume(self, type_id: int) -> Optional[float]:
        """Get packaged volume in m3 for a type."""
        info = self.get_type_info(type_id)
        return info.volume if info else None

    def search_types_by_name(self, query: str, limit: int = 50,
                             published_only: bool = True) -> list[dict]:
        """Resolve a typed item name to candidate type_ids via the local SDE.

        Powers the Contracts tab's search box: the user must resolve to a real
        type_id before a contract search runs (no blank search — that's also
        what blocks an accidental "search everything"). Returns
        [{type_id, name}, ...] ordered by an exact-match-first, then
        prefix-match, then shortest-name heuristic so the obvious item floats
        to the top of the dropdown.

        Matching is case-insensitive substring (LIKE %query%). Returns [] for
        an empty/whitespace query or if the SDE isn't present.
        """
        q = (query or "").strip()
        if not q:
            return []
        try:
            conn = self._get_conn()
            where = "name LIKE ? COLLATE NOCASE"
            params: list = [f"%{q}%"]
            if published_only:
                where += " AND published = 1"
            cursor = conn.execute(
                f"SELECT type_id, name FROM types WHERE {where} "
                f"ORDER BY length(name) ASC LIMIT ?",
                [*params, int(limit) * 4],
            )
            rows = [{"type_id": r["type_id"], "name": r["name"]} for r in cursor]
            conn.close()
        except Exception:
            return []

        ql = q.lower()

        def _rank(row: dict) -> tuple:
            n = row["name"].lower()
            if n == ql:
                bucket = 0
            elif n.startswith(ql):
                bucket = 1
            else:
                bucket = 2
            return (bucket, len(row["name"]), row["name"].lower())

        rows.sort(key=_rank)
        return rows[:limit]
    
    def has_market_group_data(self) -> bool:
        """Whether the `market_groups` table exists in this SDE install.

        Older installs only have `types`. Callers should treat market-group
        lookups as unavailable and hide any UI that depends on them until
        the user re-downloads the SDE.
        """
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name = 'market_groups'"
            )
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def has_type_materials_data(self) -> bool:
        """Whether the `type_materials` table exists in this SDE install.

        Older installs predate the Reprocess-or-Sell module and only have
        `types`/`market_groups`. The module should prompt a re-download when
        this returns False rather than silently showing empty yields.
        """
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name = 'type_materials'"
            )
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except Exception:
            return False

    def has_meta_group_data(self) -> bool:
        """Whether the `types` table has the `meta_group_id` column.

        Added with the Industry tab's tech-level filter; installs that predate
        it must re-download the SDE. Cached after first check.
        """
        if self._has_meta_col is not None:
            return self._has_meta_col
        try:
            conn = self._get_conn()
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(types)")]
            conn.close()
            self._has_meta_col = "meta_group_id" in cols
        except Exception:
            self._has_meta_col = False
        return self._has_meta_col

    def get_type_materials(self, type_id: int) -> list[tuple[int, int]]:
        """Reprocessing output for one type: [(material_type_id, quantity), ...].

        Quantity is the SDE base yield per `portion_size` units of the source
        type (before station rate / skill modifiers). Returns [] if the type
        has no materials, or if this SDE install lacks the table.
        """
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT material_type_id, quantity FROM type_materials "
                "WHERE type_id = ?",
                (type_id,),
            )
            rows = [(r["material_type_id"], r["quantity"]) for r in cursor]
            conn.close()
            return rows
        except Exception:
            return []

    def get_type_info_bulk(self, type_ids: list[int]) -> Dict[int, TypeInfo]:
        """Bulk fetch TypeInfo for many type_ids in one connection."""
        result: Dict[int, TypeInfo] = {}
        uncached = []
        for tid in type_ids:
            if tid in self._info_cache:
                result[tid] = self._info_cache[tid]
            else:
                uncached.append(tid)
        if not uncached:
            return result
        meta_col = ", meta_group_id" if self.has_meta_group_data() else ""
        try:
            conn = self._get_conn()
            BATCH_SIZE = 500
            for i in range(0, len(uncached), BATCH_SIZE):
                batch = uncached[i:i + BATCH_SIZE]
                placeholders = ",".join("?" * len(batch))
                cursor = conn.execute(
                    f"""SELECT type_id, name, volume, market_group_id,
                               published, portion_size, group_id{meta_col}
                        FROM types WHERE type_id IN ({placeholders})""",
                    batch,
                )
                for row in cursor:
                    info = TypeInfo(
                        type_id=row["type_id"],
                        name=row["name"],
                        volume=row["volume"] or 0.0,
                        market_group_id=row["market_group_id"],
                        published=bool(row["published"]),
                        portion_size=row["portion_size"] or 1,
                        group_id=row["group_id"],
                        meta_group_id=(row["meta_group_id"] if meta_col else None),
                    )
                    result[row["type_id"]] = info
                    self._info_cache[row["type_id"]] = info
            conn.close()
        except Exception:
            pass
        return result

    # ---------------------------------------------------------- market groups

    def _ensure_market_groups_loaded(self):
        """Load the full parent/name map on first access.

        Market-group lookups (ancestry, children, name) all consult the same
        in-memory dicts, so one load amortises every later query.
        """
        if self._mg_parents is not None:
            return
        self._mg_parents = {}
        self._mg_names = {}
        if not self.has_market_group_data():
            return
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                "SELECT market_group_id, parent_group_id, name FROM market_groups"
            )
            for row in cursor:
                mg_id = row["market_group_id"]
                self._mg_parents[mg_id] = row["parent_group_id"]
                self._mg_names[mg_id] = row["name"]
            conn.close()
        except Exception:
            pass

    def get_market_group_name(self, mg_id: int) -> Optional[str]:
        self._ensure_market_groups_loaded()
        return self._mg_names.get(mg_id)

    def get_market_group_ancestry(self, mg_id: Optional[int]) -> list[int]:
        """Return ancestry chain root-first: [top_level, ..., leaf].

        Returns [] if mg_id is None or not present. The first element is the
        depth-1 ancestor (matches what shows in the in-game market browser's
        top-level list), the second is depth-2, etc.
        """
        if mg_id is None:
            return []
        self._ensure_market_groups_loaded()
        if mg_id not in self._mg_parents:
            return []
        chain: list[int] = []
        current: Optional[int] = mg_id
        # Guard against pathological cycles in malformed data.
        for _ in range(32):
            if current is None:
                break
            chain.append(current)
            current = self._mg_parents.get(current)
        chain.reverse()
        return chain

    def list_top_level_market_groups(self) -> list[tuple[int, str]]:
        """Top-level market groups (parent_group_id IS NULL), sorted by name."""
        self._ensure_market_groups_loaded()
        result = [
            (mg_id, self._mg_names[mg_id])
            for mg_id, parent in self._mg_parents.items()
            if parent is None
        ]
        return sorted(result, key=lambda x: x[1].lower())

    def get_market_group_children(self, parent_id: int) -> list[tuple[int, str]]:
        """Direct children of `parent_id`, sorted by name."""
        self._ensure_market_groups_loaded()
        result = [
            (mg_id, self._mg_names[mg_id])
            for mg_id, parent in self._mg_parents.items()
            if parent == parent_id
        ]
        return sorted(result, key=lambda x: x[1].lower())

    def get_types_by_market_group(self, market_group_id: int) -> list[TypeInfo]:
        """Get all types in a market group (for future filtering)."""
        try:
            conn = self._get_conn()
            cursor = conn.execute(
                """SELECT type_id, name, volume, market_group_id,
                          published, portion_size, group_id
                   FROM types WHERE market_group_id = ? AND published = 1""",
                (market_group_id,)
            )
            results = []
            for row in cursor:
                results.append(TypeInfo(
                    type_id=row["type_id"],
                    name=row["name"],
                    volume=row["volume"] or 0.0,
                    market_group_id=row["market_group_id"],
                    published=bool(row["published"]),
                    portion_size=row["portion_size"] or 1,
                    group_id=row["group_id"]
                ))
            conn.close()
            return results
        except Exception:
            return []
    
    # =========================================================================
    # DOWNLOAD AND INITIALIZATION
    # =========================================================================
    
    async def download_and_build(
        self,
        progress_callback: Optional[callable] = None
    ) -> bool:
        """
        Download SDE data and build local database.
        
        Args:
            progress_callback: Optional callback(status_message, percent_complete)
            
        Returns:
            True if successful, False on error.
        """
        import bz2
        import csv
        import io
        
        def update(msg: str, pct: int):
            print(f"[SDE] {msg}")
            if progress_callback:
                progress_callback(msg, pct)
        
        update("Starting SDE download...", 0)

        try:
            # Download invTypes.csv.bz2 + invMarketGroups.csv.bz2
            update("Downloading type data from Fuzzwork...", 5)

            async def _download(session, url: str, label: str) -> bytes:
                async with session.get(url) as response:
                    if response.status != 200:
                        raise RuntimeError(f"{label} download failed: HTTP {response.status}")
                    chunks = []
                    async for chunk in response.content.iter_chunked(65536):
                        chunks.append(chunk)
                    return b"".join(chunks)

            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(connector=make_connector(), headers={"User-Agent": ESI_USER_AGENT}, timeout=timeout) as session:
                types_bytes = await _download(session, FUZZWORK_TYPES_URL, "invTypes")
                update("Downloading market-group tree...", 35)
                market_groups_bytes = await _download(
                    session, FUZZWORK_MARKET_GROUPS_URL, "invMarketGroups"
                )
                update("Downloading reprocessing yields...", 45)
                type_materials_bytes = await _download(
                    session, FUZZWORK_TYPE_MATERIALS_URL, "invTypeMaterials"
                )
                update("Downloading meta groups...", 50)
                meta_types_bytes = await _download(
                    session, FUZZWORK_META_TYPES_URL, "invMetaTypes"
                )

            update("Decompressing...", 50)

            # Fuzzwork now serves plain CSVs (with a UTF-8 BOM); keep bz2
            # handling in case the compressed variants ever come back.
            def _to_text(raw: bytes) -> str:
                if raw[:3] == b"BZh":
                    raw = bz2.decompress(raw)
                return raw.decode("utf-8-sig")

            csv_data = _to_text(types_bytes)
            market_groups_csv = _to_text(market_groups_bytes)
            type_materials_csv = _to_text(type_materials_bytes)
            meta_types_csv = _to_text(meta_types_bytes)

            # typeID -> metaGroupID (Tech I/II/Faction/Tech III/…). Built before
            # the types insert so each row carries its meta group inline.
            meta_by_type: Dict[int, int] = {}
            for row in csv.DictReader(io.StringIO(meta_types_csv)):
                try:
                    meta_by_type[int(row["typeID"])] = int(row["metaGroupID"])
                except (ValueError, KeyError, TypeError):
                    continue
            
            update("Building database...", 55)
            
            # Build the replacement at <db>.new; build_then_swap swaps it
            # in only on success, so a failed download/build leaves the
            # previous working DB in place (finding 5-5).
            with build_then_swap(self.db_path) as tmp_path:
                conn = sqlite3.connect(str(tmp_path))
                conn.execute("""
                    CREATE TABLE types (
                        type_id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        volume REAL,
                        market_group_id INTEGER,
                        published INTEGER,
                        portion_size INTEGER,
                        group_id INTEGER,
                        meta_group_id INTEGER
                    )
                """)
                conn.execute("CREATE INDEX idx_market_group ON types(market_group_id)")
                conn.execute("CREATE INDEX idx_published ON types(published)")
                conn.execute("CREATE INDEX idx_group ON types(group_id)")

                conn.execute("""
                    CREATE TABLE market_groups (
                        market_group_id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        parent_group_id INTEGER
                    )
                """)
                conn.execute("CREATE INDEX idx_mg_parent ON market_groups(parent_group_id)")

                # Reprocessing yields. One row per (source type, output material).
                # quantity is the base yield per portion_size units of type_id.
                conn.execute("""
                    CREATE TABLE type_materials (
                        type_id INTEGER NOT NULL,
                        material_type_id INTEGER NOT NULL,
                        quantity INTEGER NOT NULL,
                        PRIMARY KEY (type_id, material_type_id)
                    )
                """)
                conn.execute("CREATE INDEX idx_tm_type ON type_materials(type_id)")

                # Parse CSV
                # Fuzzwork invTypes columns:
                # typeID,groupID,typeName,description,mass,volume,capacity,portionSize,
                # raceID,basePrice,published,marketGroupID,iconID,soundID,graphicID
                reader = csv.DictReader(io.StringIO(csv_data))
            
                update("Importing types...", 60)
            
                records = []
                count = 0
                for row in reader:
                    try:
                        type_id = int(row["typeID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    name = row.get("typeName", "") or f"#{type_id}"
                    volume = _parse_optional_float(row.get("volume")) or 0.0
                    market_group = _parse_optional_int(row.get("marketGroupID"))
                    published = _parse_optional_int(row.get("published")) or 0
                    portion_size = _parse_optional_int(row.get("portionSize")) or 1
                    group_id = _parse_optional_int(row.get("groupID"))
                    meta_group = meta_by_type.get(type_id)  # None = Tech I

                    records.append((
                        type_id, name, volume, market_group,
                        published, portion_size, group_id, meta_group
                    ))
                    count += 1

                    # Batch insert
                    if len(records) >= 5000:
                        conn.executemany(
                            """INSERT INTO types
                               (type_id, name, volume, market_group_id,
                                published, portion_size, group_id, meta_group_id)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            records
                        )
                        records = []
                        pct = int(60 + (count / 50000) * 30)  # Estimate ~47k types
                        update(f"Imported {count:,} types...", min(pct, 90))

                # Insert remaining
                if records:
                    conn.executemany(
                        """INSERT INTO types
                           (type_id, name, volume, market_group_id,
                            published, portion_size, group_id, meta_group_id)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        records
                    )

                # Import invMarketGroups
                # Fuzzwork columns: marketGroupID,parentGroupID,marketGroupName,
                # description,iconID,hasTypes
                # NOTE: Fuzzwork represents NULL as the literal string "None", not
                # an empty cell. Naive `int(row["parentGroupID"])` would explode on
                # every top-level market group (parent is None) and we'd drop those
                # rows entirely — which is exactly what bit us. Parse defensively.
                update("Importing market groups...", 94)
                mg_records = []
                for row in csv.DictReader(io.StringIO(market_groups_csv)):
                    try:
                        mg_id = int(row["marketGroupID"])
                    except (ValueError, KeyError, TypeError):
                        continue
                    name = row.get("marketGroupName", "") or f"#{mg_id}"
                    parent = _parse_optional_int(row.get("parentGroupID"))
                    mg_records.append((mg_id, name, parent))
                if mg_records:
                    conn.executemany(
                        "INSERT INTO market_groups (market_group_id, name, parent_group_id) "
                        "VALUES (?, ?, ?)",
                        mg_records,
                    )

                # Import invTypeMaterials (reprocessing yields).
                # Fuzzwork columns: typeID,materialTypeID,quantity
                update("Importing reprocessing yields...", 96)
                tm_records = []
                for row in csv.DictReader(io.StringIO(type_materials_csv)):
                    tid = _parse_optional_int(row.get("typeID"))
                    mtid = _parse_optional_int(row.get("materialTypeID"))
                    qty = _parse_optional_int(row.get("quantity"))
                    if tid is None or mtid is None or qty is None:
                        continue
                    tm_records.append((tid, mtid, qty))
                if tm_records:
                    conn.executemany(
                        "INSERT OR IGNORE INTO type_materials "
                        "(type_id, material_type_id, quantity) VALUES (?, ?, ?)",
                        tm_records,
                    )

                conn.commit()
                conn.close()
            
            # Save version info
            version_info = {
                "download_date": datetime.now().isoformat(),
                "source": "fuzzwork",
                "record_count": count,
                "sde_version": date.today().isoformat()
            }
            with open(self.version_path, "w") as f:
                json.dump(version_info, f, indent=2)
            
            # Clear caches — force the market-group map to reload from the new DB.
            self._name_cache.clear()
            self._info_cache.clear()
            self._mg_parents = None
            self._mg_names = {}
            self._has_meta_col = None  # re-detect the meta_group_id column
            self._conn = None

            update(
                f"SDE loaded: {count:,} types, {len(mg_records):,} market groups, "
                f"{len(tm_records):,} material rows",
                100,
            )
            return True
            
        except Exception as e:
            # The working DB (if any) was never touched — build_then_swap
            # already removed the partial .new file.
            update(f"Error: {e}", 0)
            return False


# Global instance for easy access
_sde_instance: Optional[SDEManager] = None


def get_sde_manager() -> SDEManager:
    """Get the global SDE manager instance."""
    global _sde_instance
    if _sde_instance is None:
        _sde_instance = SDEManager()
    return _sde_instance
