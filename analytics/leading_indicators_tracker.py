"""Leading indicators tracker - once-per-24h-per-hub gating.

Mirrors MaterialFilterTracker pattern. Tracks which hubs have had
leading indicators computed within the last 24h, both in-memory (for
the current session) and via a JSON timestamp file (for cross-launch
detection).

Public API:
    get_leading_indicators_tracker() -> LeadingIndicatorsTracker
    LeadingIndicatorsTracker.should_run(hub_key) -> bool
    LeadingIndicatorsTracker.mark_complete(hub_key)
    LeadingIndicatorsTracker.has_run(hub_key) -> bool
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.sound_manager import get_data_dir


_LI_RUN_TIMES_PATH = get_data_dir() / "li_run_times.json"
_RUN_MAX_AGE_SECONDS = 86400


def _load_run_times() -> dict:
    try:
        with open(_LI_RUN_TIMES_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_run_times(data: dict):
    try:
        Path(_LI_RUN_TIMES_PATH).parent.mkdir(parents=True, exist_ok=True)
        with open(_LI_RUN_TIMES_PATH, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[LeadingIndicators] Error saving run times: {e}")


def _is_within_24h(iso_str: str) -> bool:
    try:
        ts = datetime.fromisoformat(iso_str)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() < _RUN_MAX_AGE_SECONDS
    except Exception:
        return False


class LeadingIndicatorsTracker:
    """Tracks which hubs have had leading indicators computed within 24h.

    Singleton pattern matches MaterialFilterTracker.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._ran_today: Set[str] = set()
        return cls._instance

    def should_run(self, hub_key: str) -> bool:
        """Return True if leading indicators should be computed for this hub."""
        if hub_key in self._ran_today:
            return False

        data = _load_run_times()
        if hub_key in data and _is_within_24h(data[hub_key]):
            self._ran_today.add(hub_key)
            ts = datetime.fromisoformat(data[hub_key])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            print(f"[LeadingIndicators] {hub_key}: skipping (ran {age_h:.1f}h ago)")
            return False

        print(f"[LeadingIndicators] {hub_key}: will run")
        return True

    def mark_complete(self, hub_key: str):
        """Mark hub as having completed leading indicators."""
        self._ran_today.add(hub_key)
        data = _load_run_times()
        data[hub_key] = datetime.now(timezone.utc).isoformat()
        _save_run_times(data)
        print(f"[LeadingIndicators] {hub_key}: marked complete")

    def has_run(self, hub_key: str) -> bool:
        """Check if hub has run within the last 24h (no side effects)."""
        if hub_key in self._ran_today:
            return True
        data = _load_run_times()
        return hub_key in data and _is_within_24h(data[hub_key])

    def get_status(self) -> Dict[str, Any]:
        """Status for debugging."""
        return {
            "last_run_times": _load_run_times(),
            "hubs_completed_session": list(self._ran_today),
        }


_tracker_instance: Optional[LeadingIndicatorsTracker] = None


def get_leading_indicators_tracker() -> LeadingIndicatorsTracker:
    """Get the global tracker instance."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = LeadingIndicatorsTracker()
    return _tracker_instance
