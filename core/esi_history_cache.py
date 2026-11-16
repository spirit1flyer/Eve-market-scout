"""Durable recent ESI history, separate from the imported history archive."""

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import threading

from core.esi_cache_io import async_guard
from core.esi_diagnostics import decision_event
from core.esi_public_cache import _expires, _utc
from core.sound_manager import get_data_dir

_singleton = None
_singleton_lock = threading.Lock()


def merge_history(existing, recent):
    """The accepted ESI representation wins on overlapping archive dates."""
    rows = {row['date']: row for row in existing}
    rows.update({row['date']: row for row in recent})
    return sorted(rows.values(), key=lambda row: row['date'], reverse=True)


def validate_history(payload):
    if not isinstance(payload, list):
        raise ValueError('history list required')
    seen = set()
    for row in payload:
        day = row['date']
        date.fromisoformat(day)
        if day in seen:
            raise ValueError('duplicate history day')
        seen.add(day)
        for field in ('average', 'highest', 'lowest'):
            value = row[field]
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError('invalid history price')
        for field in ('volume', 'order_count'):
            if type(row[field]) is not int or row[field] < 0:
                raise ValueError('invalid history count')
        if not row['lowest'] <= row['average'] <= row['highest']:
            raise ValueError('inconsistent history prices')


class RecentHistoryCache:
    def __init__(self, directory, now=None):
        self.path = Path(directory) / 'esi_recent_history.db'
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.entries, self.locks = {}, {}
        self.meta_lock = threading.Lock()
        self._restore()

    def _lock(self, key):
        with self.meta_lock:
            return self.locks.setdefault(key, threading.Lock())

    def _event(self, key, action, entry=None, **fields):
        if entry:
            fields.update(age=f"{(self.now()-entry['acquired_at']).total_seconds():.0f}s",
                          expires=entry['expires'].isoformat(), items=len(entry['payload']),
                          durable=entry['durable'], complete=True)
            fields.setdefault('next_in', f"{max(0, (entry['expires']-self.now()).total_seconds()):.0f}s")
            fields.setdefault('status', 'durable' if entry['durable'] else 'memory_only')
            fields['source'] = entry['source']
        decision_event('market_history', action, 'recent_overlay',
                       context=f'{key[0]}:{key[1]}', **fields)

    def _restore(self):
        if not self.path.exists():
            return
        try:
            with closing(sqlite3.connect(self.path)) as db:
                rows = db.execute('SELECT region_id, type_id, snapshot FROM history').fetchall()
            for region, tid, raw in rows:
                key = (region, tid)
                try:
                    row = json.loads(raw)
                    if (row.get('schema') != 1 or row.get('complete') is not True
                            or row.get('region_id') != region or row.get('type_id') != tid
                            or row.get('source') != 'esi_recent_history'):
                        raise ValueError('unproven history snapshot')
                    validate_history(row['payload'])
                    row.update(acquired_at=_utc(row['acquired_at']),
                               expires=_utc(row['expires']), durable=True)
                    if row['acquired_at'] > self.now():
                        raise ValueError('future history acquisition')
                    self.entries[key] = row
                    self._event(key, 'RESTORE', row)
                except Exception as exc:
                    self._event(key, 'RESTORE_FAILED', reason=type(exc).__name__)
        except Exception as exc:
            decision_event('market_history', 'RESTORE_FAILED', 'recent_overlay',
                           reason=type(exc).__name__)

    def _write(self, key, row):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        saved = {k: v for k, v in row.items() if k != 'durable'}
        for field in ('acquired_at', 'expires'):
            saved[field] = row[field].isoformat()
        with closing(sqlite3.connect(self.path, timeout=30)) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS history (region_id INTEGER, '
                           'type_id INTEGER, snapshot TEXT, PRIMARY KEY(region_id, type_id))')
                db.execute('INSERT OR REPLACE INTO history VALUES (?, ?, ?)',
                           (*key, json.dumps(saved, allow_nan=False)))

    def _save(self, key, row):
        try:
            self._write(key, row)
            row['durable'] = True
            self._event(key, 'PERSIST', row)
        except Exception as exc:
            row['durable'] = False
            self._event(key, 'SAVE_FAILED', row, reason='previous_disk_retained',
                        status=type(exc).__name__)

    def overlay(self, target):
        for (region, tid), row in list(self.entries.items()):
            cache = target.setdefault(region, {})
            cache[tid] = merge_history(cache.get(tid, []), deepcopy(row['payload']))

    async def get(self, client, region, tid, min_age_seconds=0):
        key = (int(region), int(tid))
        if any(value <= 0 for value in key):
            raise ValueError('positive region/type IDs required')
        async with async_guard(self._lock(key)):
            previous = self.entries.get(key)
            now = self.now()
            if previous and (now < previous['expires'] or
                             (now-previous['acquired_at']).total_seconds() < min_age_seconds):
                if not previous['durable']:
                    await asyncio.to_thread(self._save, key, previous)
                self._event(key, 'REUSE', previous,
                            next_in=f"{max(0, (max(previous['expires'], previous['acquired_at'] + timedelta(seconds=min_age_seconds))-now).total_seconds()):.0f}s",
                            reason='esi_unexpired' if now < previous['expires'] else 'consumer_age')
                return deepcopy(previous), False
            self._event(key, 'FETCH', previous, reason='eligible' if previous else 'missing')
            try:
                payload, headers = await client._get_with_headers(
                    f'/markets/{region}/history/', {'type_id': tid})
                validate_history(payload)
                lowered = {k.lower(): v for k, v in headers.items()}
                if int(lowered.get('x-pages', '1')) != 1:
                    raise ValueError('unexpected history pagination')
                now = self.now()
                try:
                    expiry = _expires(headers, now)
                except ValueError:
                    expiry = now + timedelta(days=1)
                row = dict(schema=1, complete=True, source='esi_recent_history',
                           region_id=region, type_id=tid, payload=deepcopy(payload),
                           acquired_at=now, expires=expiry, headers=lowered, durable=False)
            except Exception as exc:
                self._event(key, 'FETCH_FAILED', previous, reason='previous_snapshot_retained',
                            status=type(exc).__name__)
                raise
            self.entries[key] = row
            self._event(key, 'ACCEPT', row)
            await asyncio.to_thread(self._save, key, row)
            return deepcopy(row), True


def get_history_cache():
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = RecentHistoryCache(get_data_dir())
        return _singleton
