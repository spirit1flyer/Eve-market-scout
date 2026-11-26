"""Offline history/policy/SSO checks for the normal IDE Ctrl+F12 runner."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import gzip
from types import SimpleNamespace

from core.config import JITA_REGION_ID
from core.esi_history_cache import RecentHistoryCache
from core.history_mixin import HistoryMixin
from core.order_policy import JitaReferenceMixin, STOCK_ORDER_SECONDS
from core.sso_cooldown import SSOCooldown, SSOCooldownError
from tools.acquisition_batch_checks import _Clock, _Orders, _RegionalClient, _order
from tools.private_cache_checks import _require


def _cooling(callback):
    try:
        callback()
    except SSOCooldownError:
        return
    raise AssertionError('SSO cooldown did not block the request')


class _HistoryClient(HistoryMixin):
    def __init__(self, directory, clock):
        self.clock, self.calls = clock, []
        self.history_cache = {}
        self.recent_history = RecentHistoryCache(directory, now=clock.now)
        self.recent_history.overlay(self.history_cache)
        self.fail, self.invalid, self.empty = set(), set(), set()
        self.seconds = 7200

    async def _get_with_headers(self, endpoint, params):
        tid = params['type_id']
        self.calls.append(tid)
        await asyncio.sleep(0)
        if tid in self.fail:
            raise OSError('synthetic history failure')
        rows = [] if tid in self.empty else [dict(date='2026-01-02', average=2.,
                    highest=3., lowest=1., volume=100, order_count=2)]
        if tid in self.invalid:
            rows[0]['volume'] = -1
        return rows, self.clock.headers(self.seconds)


def history_restart_and_merge(directory):
    clock = _Clock()
    first = _HistoryClient(directory, clock)
    original = asyncio.run(first.recent_history.get(first, 10000043, 34))[0]
    clock.time += timedelta(seconds=90)
    restored = _HistoryClient(directory, clock)
    row, fetched = asyncio.run(restored.recent_history.get(restored, 10000043, 34))
    _require(not fetched and not restored.calls and row['durable']
             and row['acquired_at'] == original['acquired_at']
             and row['expires'] == original['expires'], 'history restart lost original clock or reused nothing')
    old = dict(date='2026-01-01', average=1., highest=1., lowest=1., volume=1, order_count=1)
    overlap = {**old, 'date': '2026-01-02'}
    restored.market_history = SimpleNamespace(get_history_bulk=lambda *args, **kwargs: {34: [old, overlap]})
    merged = asyncio.run(restored.get_market_history_bulk(10000043, [34]))[34]
    _require(len(merged) == 2 and merged[0]['average'] == 2., 'archive overwrote recent history')


def history_failures_and_save_recovery(directory):
    clock, tid, region = _Clock(), 34, 10000043
    client = _HistoryClient(directory, clock)
    _require(asyncio.run(client.freshen_history_for_items(region, [tid]))['complete'], 'initial history failed')
    old = client.recent_history.entries[(region, tid)]
    disk_before = client.recent_history.path.read_bytes()
    clock.time += timedelta(seconds=7201)
    client.fail.add(tid)
    result = asyncio.run(client.freshen_history_for_items(region, [tid]))
    _require(result['failed'] == [tid] and not result['complete']
             and client.recent_history.entries[(region, tid)] is old
             and client.recent_history.path.read_bytes() == disk_before, 'fetch failure advanced history success')
    client.fail.clear()
    client.invalid.add(tid)
    _require(not asyncio.run(client.freshen_history_for_items(region, [tid]))['complete']
             and client.recent_history.entries[(region, tid)] is old, 'invalid history replaced previous rows')
    client.invalid.clear()
    write = client.recent_history._write
    def broken(*args):
        raise OSError('synthetic disk failure')
    client.recent_history._write = broken
    result = asyncio.run(client.freshen_history_for_items(region, [tid]))
    _require(result['accepted'] == [tid] and not result['complete'] and not result['durable']
             and client.recent_history.path.read_bytes() == disk_before, 'save failure claimed durable history')
    before = len(client.calls)
    client.recent_history._write = write
    result = asyncio.run(client.freshen_history_for_items(region, [tid]))
    _require(result['complete'] and len(client.calls) == before, 'history save retry reacquired unexpired data')


def history_partial_and_hourly_policy(directory):
    clock = _Clock()
    client = _HistoryClient(directory, clock)
    client.seconds = 90
    client.fail.add(35)
    partial = asyncio.run(client.freshen_history_for_items(10000043, [34, 35]))
    _require(partial['durable'] == [34] and partial['failed'] == [35]
             and not partial['complete'], 'partial history reported complete')
    client.fail.clear()
    complete = asyncio.run(client.freshen_history_for_items(10000043, [34, 35]))
    _require(complete['complete'] and client.calls.count(34) == 1, 'partial retry refetched good item')
    clock.time += timedelta(seconds=3599)
    _require(asyncio.run(client.freshen_history_for_items(10000043, [34]))['reused'] == [34],
             'history ignored one-hour consumer preference')
    clock.time += timedelta(seconds=1)
    _require(asyncio.run(client.freshen_history_for_items(10000043, [34]))['fetched'] == [34],
             'history did not acquire at eligible hourly boundary')


def history_concurrent_and_empty(directory):
    clock = _Clock()
    client = _HistoryClient(directory, clock)
    client.empty.add(34)
    def pull():
        return asyncio.run(client.freshen_history_for_items(10000043, [34]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: pull(), range(2)))
    _require(all(row['complete'] for row in results) and client.calls == [34],
             'concurrent history calls duplicated acquisition or rejected valid empty')
    restored = _HistoryClient(directory, clock)
    _require(restored.history_cache[10000043][34] == []
             and asyncio.run(restored.freshen_history_for_items(10000043, [34]))['complete']
             and not restored.calls, 'empty history failed restart/reuse')


def history_cache_control_age(directory):
    clock = _Clock()
    client = _HistoryClient(directory, clock)
    async def response(*args):
        return [], {'Cache-Control': 'max-age=7200', 'Age': '7100',
                    'Date': format_datetime(clock.time-timedelta(seconds=7100), usegmt=True)}
    client._get_with_headers = response
    row, _ = asyncio.run(client.recent_history.get(client, 10000043, 34))
    _require(row['expires'] == clock.time + timedelta(seconds=100), 'history restarted max-age instead of retaining Age')


def jita_reference_and_active_orders(directory):
    now = datetime.now(timezone.utc)
    store = _Orders(directory)
    store.cache_orders_for_region(JITA_REGION_ID, [_order()], now-timedelta(seconds=1),
                                  acquired_at=now-timedelta(hours=23))
    class Reference(JitaReferenceMixin):
        def __init__(self):
            self.order_cache, self.calls = store, []
        async def get_market_orders(self, region):
            self.calls.append(region)
            return [_order(2)]
    reference = Reference()
    _require(asyncio.run(reference.get_jita_reference())[0]['order_id'] == 1
             and not reference.calls, '23-hour reference reacquired unnecessarily')
    store._order_cache[JITA_REGION_ID]['timestamp'] = now-timedelta(hours=24)
    _require(asyncio.run(reference.get_jita_reference())[0]['order_id'] == 2
             and reference.calls == [JITA_REGION_ID], '24-hour Jita bound was not enforced')
    store._order_cache[JITA_REGION_ID]['timestamp'] = now-timedelta(hours=23)
    client = _RegionalClient(directory/'pages', _Clock())
    client.order_cache = store
    client.jita_orders_cache = [_order(99)]
    orders = asyncio.run(client.get_market_orders(JITA_REGION_ID, use_cache=True))
    _require(len(client.calls) == 3 and len(orders) == 3,
             'active Jita scan used unbounded legacy/reference fallback')


def stock_age_and_hub_independence(directory):
    now, store = datetime.now(timezone.utc), _Orders(directory)
    for region, seconds in ((10000043, 3599), (JITA_REGION_ID, 3600)):
        store.cache_orders_for_region(region, [_order()], now-timedelta(seconds=1),
                                      acquired_at=now-timedelta(seconds=seconds))
    _require(store.peek_cached_orders(10000043, STOCK_ORDER_SECONDS) is not None
             and store.peek_cached_orders(JITA_REGION_ID, STOCK_ORDER_SECONDS) is None,
             'Stock Market one-hour acquisition boundary is wrong')
    store.cache_orders_for_region(JITA_REGION_ID, [_order(2)], now+timedelta(seconds=900),
                                  acquired_at=now-timedelta(minutes=10))
    restored = _Orders(directory)
    restored._load_region_disk_cache(JITA_REGION_ID)
    _require(restored.peek_cached_orders(JITA_REGION_ID, STOCK_ORDER_SECONDS) is not None,
             'newer scanner snapshot was not usable by Stock Market after restart')
    client = _RegionalClient(directory/'pages', _Clock())
    client.order_cache = store
    asyncio.run(client.get_market_orders(10000043))
    count = len(client.calls)
    asyncio.run(client.get_market_orders(JITA_REGION_ID))
    asyncio.run(client.get_market_orders(10000043, force_refresh=True))
    _require(count == 3 and len(client.calls) == count, 'hub switch/force reacquired unexpired book')


def order_force_singleflight(directory):
    client = _RegionalClient(directory/'pages', _Clock())
    client.order_cache = _Orders(directory)
    async def both():
        return await asyncio.gather(client.get_market_orders(JITA_REGION_ID, force_refresh=True),
                                    client.get_market_orders(JITA_REGION_ID, force_refresh=True))
    replies = asyncio.run(both())
    _require(replies[0] == replies[1] and len(client.calls) == 3,
             'forced concurrent pulls bypassed post-lock reuse')


class _SSOServer(_Clock):
    def __init__(self):
        super().__init__()
        self.calls, self.status, self.retry = [], 429, '120'
    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        return SimpleNamespace(status_code=self.status,
                               headers={} if self.retry is None else {'Retry-After': self.retry})


_TOKEN = 'https://login.eveonline.com/v2/oauth/token'
_VERIFY = 'https://login.eveonline.com/v2/oauth/verify'


def sso_shared_cooldown_restart(directory):
    server = _SSOServer()
    gate = SSOCooldown(directory, request=server.request, now=server.now)
    gate.request('POST', _TOKEN, data={'refresh_token': 'synthetic-secret'})
    _cooling(lambda: gate.request('GET', _VERIFY))
    restored = SSOCooldown(directory, request=server.request, now=server.now)
    _cooling(lambda: restored.request('POST', _TOKEN))
    _require(len(server.calls) == 1, 'SSO cooldown did not cover other endpoint/restart')
    with gzip.open(gate.path, 'rt', encoding='utf-8') as stream:
        saved = stream.read()
    _require('synthetic-secret' not in saved, 'SSO cooldown saved a credential')
    server.time += timedelta(seconds=121)
    server.status, server.retry = 200, None
    _require(restored.request('GET', _VERIFY).status_code == 200, 'expired SSO cooldown did not recover')


def sso_failed_grant_isolation(directory):
    server = _SSOServer()
    server.status, server.retry = 400, None
    gate = SSOCooldown(directory, request=server.request, now=server.now)
    gate.request('POST', _TOKEN, data={'refresh_token': 'synthetic-bad'})
    _cooling(lambda: gate.request('POST', _TOKEN, data={'refresh_token': 'synthetic-bad'}))
    server.status = 200
    _require(gate.request('POST', _TOKEN, data={'refresh_token': 'synthetic-good'}).status_code == 200
             and len(server.calls) == 2, 'bad grant blocked unrelated valid sign-in')


def sso_date_and_concurrent_throttle(directory):
    server = _SSOServer()
    server.retry = format_datetime(server.time+timedelta(seconds=180), usegmt=True)
    gate = SSOCooldown(directory, request=server.request, now=server.now)
    def pull(_):
        try:
            gate.request('POST', _TOKEN)
            return True
        except SSOCooldownError:
            return False
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(pull, range(3)))
    _require(sum(results) == 1 and len(server.calls) == 1
             and gate.deadline == server.time+timedelta(seconds=180),
             'HTTP-date Retry-After or concurrent SSO gate failed')
    server.time += timedelta(seconds=181)
    for retry in (None, 'malformed'):
        server.retry = retry
        gate.request('POST', _TOKEN)
        before = len(server.calls)
        _cooling(lambda: gate.request('GET', _VERIFY))
        _require(len(server.calls) == before, 'missing/malformed Retry-After allowed an early SSO request')
        server.time += timedelta(seconds=61)


def distribution_notice(directory):
    from gui.gui_data_notice import notice_path
    import ast
    text = notice_path().read_text(encoding='utf-8')
    _require('Fenris Creations' in text and 'Local storage' in text and 'Removing access' in text,
             'notice missing required disclosure sections')
    # The source check can inspect the build declaration; a frozen app has
    # already proven packaging by reading the notice from its bundle above.
    import sys
    if not getattr(sys, 'frozen', False):
        tree = ast.parse((notice_path().parent/'EVE-Market-Scout.spec').read_text(encoding='utf-8'))
        bundled = [ast.literal_eval(keyword.value) for node in ast.walk(tree)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == 'Analysis' for keyword in node.keywords if keyword.arg == 'datas']
        _require(any(('ESI_DATA_USAGE.md', '.') in items for items in bundled), 'binary build omits the notice')
