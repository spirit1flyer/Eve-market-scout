"""Dispatcher between regional ESI history and per-structure observed history.

The scanner's safety checks (velocity gates, ceiling caps, market-crashing
flags, Demand/Restock) all consume the same dict-of-lists shape from
`MarketHistoryDB.get_history_bulk`. This module decides — per hub — whether
to source that data from the regional history DB (NPC hubs) or the
per-structure observed-history DB (player structures with enough snapshots).

Gate:
  - NPC region hub → always regional.
  - Structure hub with < MIN_OBSERVED_DAYS days of snapshots → regional
    fallback (today's behaviour; the parent region is a noisy proxy, but
    it's better than zero data while observed history accumulates).
  - Structure hub with >= MIN_OBSERVED_DAYS days of snapshots → observed.

`parse_history_stats` in scanner_common divides volume by calendar days
(7 / 30), not by record count, so thin-volume structures naturally produce
low safe_velocity through the existing pipeline. We don't need to zero-fill
absent days — the math handles it.
"""

from typing import Any

from core.config import get_hub_config


MIN_OBSERVED_DAYS = 7


async def get_history_for_hub(client: Any, hub_key: str,
                              type_ids: list[int],
                              days: int = 30) -> dict[int, list[dict]]:
    """Return per-type history rows for `hub_key`.

    Result shape matches `MarketHistoryDB.get_history_bulk`:
        {type_id: [{date, average, lowest, highest, volume, order_count}, ...]}

    Downstream consumers (`parse_history_stats`, `HistoryStats`, etc.) cannot
    tell which source produced the data — that's the whole point: structure
    safety checks reuse every existing scanner path unchanged.
    """
    cfg = get_hub_config(hub_key)

    if cfg.get("type") == "structure":
        # Lazy import — keeps test/CLI tools that don't touch SQLite faster
        # to load and avoids a cycle if structure_history later grows deps
        # back into config.
        from history.structure_history import StructureHistoryDB
        structure_id = cfg["station_id"]
        db = StructureHistoryDB.singleton()
        if db.days_observed(structure_id) >= MIN_OBSERVED_DAYS:
            return db.get_history_bulk(structure_id, list(type_ids), days=days)
        # Not enough observed data yet — drop through to regional proxy.

    return await client.get_market_history_bulk(
        cfg["region_id"], list(type_ids), use_cache=True
    )
