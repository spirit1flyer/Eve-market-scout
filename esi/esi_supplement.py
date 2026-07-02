"""ESI supplement cache - JSONL-based persistence for items missing from bulk history."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.sound_manager import get_data_dir


def _get_default_filepath() -> Path:
    """Get default filepath using centralized data directory."""
    return get_data_dir() / "esi_supplement.jsonl"


class ESISupplementCache:
    """
    Persistent cache for ESI history data that supplements bulk downloads.
    
    Used for items that are too new to appear in everef bulk archives.
    Stores data in append-only JSONL format for fast writes.
    """
    
    DEFAULT_MAX_AGE_HOURS = 72  # Re-fetch items older than this
    
    def __init__(self, filepath: Path = None, max_age_hours: int = None):
        self.filepath = filepath or _get_default_filepath()
        self.max_age_hours = max_age_hours or self.DEFAULT_MAX_AGE_HOURS
        
        # In-memory cache: {region_id: {type_id: {"data": [...], "timestamp": "ISO", ...}}}
        self.cache: dict[int, dict[int, dict]] = {}
        
        self._load()
    
    def _load(self):
        """Load cache from JSONL file."""
        print(f"[ESI] Looking for supplement cache at: {self.filepath}")
        
        # Check for old .json file and migrate if needed
        old_json_file = self.filepath.with_suffix('.json')
        if old_json_file.exists() and not self.filepath.exists():
            print(f"[ESI] Found old .json cache, migrating to .jsonl...")
            self._migrate_json_to_jsonl(old_json_file)
            return
        
        # Also check old location (cache/history/) and migrate
        old_location = Path("cache/history/esi_supplement.jsonl")
        if old_location.exists() and not self.filepath.exists():
            print(f"[ESI] Found cache at old location, migrating to {self.filepath}...")
            self._migrate_from_old_location(old_location)
            return
        
        try:
            if self.filepath.exists():
                loaded = 0
                expired = 0
                now = datetime.now(timezone.utc)
                
                with open(self.filepath, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            region_id = entry['r']
                            type_id = entry['t']
                            
                            # Check if expired
                            ts = datetime.fromisoformat(entry['ts'])
                            age_hours = (now - ts).total_seconds() / 3600
                            if age_hours >= self.max_age_hours:
                                expired += 1
                                continue
                            
                            # Build full entry format
                            full_entry = {
                                "data": entry['d'],
                                "timestamp": entry['ts']
                            }
                            if entry.get('err'):
                                full_entry["error"] = True
                                full_entry["attempts"] = entry.get('att', 1)
                            
                            # Store (later entries overwrite earlier for same key)
                            if region_id not in self.cache:
                                self.cache[region_id] = {}
                            self.cache[region_id][type_id] = full_entry
                            loaded += 1
                            
                        except (json.JSONDecodeError, KeyError):
                            continue  # Skip malformed lines
                
                total_items = sum(len(types) for types in self.cache.values())
                print(f"[ESI] Loaded supplement cache: {total_items} items ({expired} expired entries skipped)")
                
                # Compact if we skipped a lot of expired entries
                if expired > 1000:
                    print(f"[ESI] Compacting supplement cache (many expired entries)...")
                    self.compact()
            else:
                print(f"[ESI] No supplement cache file found (will create after first ESI fetch)")
        except Exception as e:
            print(f"[ESI] Could not load supplement cache: {e}")
            import traceback
            traceback.print_exc()
            self.cache = {}
    
    def _migrate_from_old_location(self, old_path: Path):
        """Migrate cache from old relative path to new data dir location."""
        try:
            # Read from old location
            with open(old_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            # Ensure new directory exists
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            
            # Write to new location
            with open(self.filepath, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            
            print(f"[ESI] Migrated {len(lines)} entries to new location")
            
            # Rename old file
            backup_path = old_path.with_suffix('.jsonl.migrated')
            old_path.rename(backup_path)
            print(f"[ESI] Old file renamed to {backup_path}")
            
            # Now load normally
            self._load()
            
        except Exception as e:
            print(f"[ESI] Migration from old location failed: {e}")
            self.cache = {}
    
    def _migrate_json_to_jsonl(self, old_json_file: Path):
        """Migrate old .json supplement cache to new .jsonl format."""
        try:
            with open(old_json_file, 'r') as f:
                raw = json.load(f)
            
            # Convert and load into memory
            self.cache = {
                int(region_id): {
                    int(type_id): entry 
                    for type_id, entry in types.items()
                }
                for region_id, types in raw.items()
            }
            
            total_items = sum(len(types) for types in self.cache.values())
            print(f"[ESI] Migrated {total_items} items from old .json format")
            
            # Write out as JSONL
            self.compact()
            
            # Rename old file
            backup_path = old_json_file.with_suffix('.json.bak')
            old_json_file.rename(backup_path)
            print(f"[ESI] Old .json file renamed to {backup_path}")
            
        except Exception as e:
            print(f"[ESI] Migration failed: {e}")
            self.cache = {}
    
    def compact(self):
        """Rewrite cache file with only current valid entries (removes duplicates/expired)."""
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            total_items = sum(len(types) for types in self.cache.values())
            print(f"[ESI] Compacting supplement cache: {total_items} items to {self.filepath}")
            
            with open(self.filepath, 'w', encoding='utf-8') as f:
                for region_id, types in self.cache.items():
                    for type_id, entry in types.items():
                        compact = {
                            'r': region_id,
                            't': type_id,
                            'd': entry.get('data', []),
                            'ts': entry.get('timestamp', datetime.now(timezone.utc).isoformat())
                        }
                        if entry.get('error'):
                            compact['err'] = True
                            compact['att'] = entry.get('attempts', 1)
                        f.write(json.dumps(compact, separators=(',', ':')) + '\n')
            
            print(f"[ESI] Compact complete")
        except Exception as e:
            print(f"[ESI] Could not compact supplement cache: {e}")
            import traceback
            traceback.print_exc()
    
    def get_if_fresh(self, region_id: int, type_id: int) -> Optional[list[dict]]:
        """
        Get cached history if it exists and is fresh (< max_age_hours old).
        
        Returns:
            list[dict]: History data if fresh and valid
            []: Empty list if item is marked as having no data (don't retry ESI)
            None: Not in cache or expired (should fetch from ESI)
        """
        region_data = self.cache.get(region_id, {})
        entry = region_data.get(type_id)
        
        if entry is None:
            return None
        
        # Check age
        try:
            timestamp = datetime.fromisoformat(entry["timestamp"])
            age_hours = (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600
            
            if age_hours < self.max_age_hours:
                # Fresh entry - return data (may be empty list for known-empty items)
                return entry["data"]
        except (KeyError, ValueError):
            pass
        
        return None
    
    def is_known_bad(self, region_id: int, type_id: int) -> bool:
        """Check if an item has failed ESI fetch multiple times (don't retry)."""
        region_data = self.cache.get(region_id, {})
        entry = region_data.get(type_id)
        
        if entry is None:
            return False
        
        # Check if marked as error with 2+ attempts
        if entry.get("error") and entry.get("attempts", 0) >= 2:
            # Check if still within max_age window
            try:
                timestamp = datetime.fromisoformat(entry["timestamp"])
                age_hours = (datetime.now(timezone.utc) - timestamp).total_seconds() / 3600
                if age_hours < self.max_age_hours:
                    return True
            except (KeyError, ValueError):
                pass
        
        return False
    
    def store(self, region_id: int, type_id: int, data: list[dict], is_error: bool = False):
        """
        Store history data in cache (memory + append to JSONL file).
        
        Args:
            region_id: Region ID
            type_id: Type ID
            data: History data (empty list for items with no history)
            is_error: True if this was an ESI error (will track attempts)
        """
        if region_id not in self.cache:
            self.cache[region_id] = {}
        
        timestamp = datetime.now(timezone.utc).isoformat()
        
        entry = {
            "data": data,
            "timestamp": timestamp
        }
        
        # Build compact JSONL entry
        compact = {
            'r': region_id,
            't': type_id,
            'd': data,
            'ts': timestamp
        }
        
        if is_error:
            # Track error attempts
            existing = self.cache[region_id].get(type_id, {})
            entry["error"] = True
            entry["attempts"] = existing.get("attempts", 0) + 1
            compact['err'] = True
            compact['att'] = entry["attempts"]
        
        self.cache[region_id][type_id] = entry
        
        # Append to JSONL file (fast - just one line)
        try:
            self.filepath.parent.mkdir(parents=True, exist_ok=True)
            with open(self.filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(compact, separators=(',', ':')) + '\n')
        except Exception as e:
            print(f"[ESI] Could not append to supplement cache: {e}")
