"""Burst-mode order pull mixin for StockMarketTab."""

import asyncio
import threading

from core.tk_queue import submit
from core.order_policy import STOCK_ORDER_SECONDS
from core.config import TRADE_HUBS, get_hub_order_cache_key, is_stock_market_hub
from analytics.stockmarket_filters import get_hub_burst_tracker


class StockMarketBurstMixin:

    _ACTIVE_REGION_MAX_AGE = STOCK_ORDER_SECONDS

    def _on_stock_tab_visible(self, event=None):
        if event is None or event.widget is not self.frame.master:
            return
        if self.frame.master.select() != str(self.frame) or self._stock_market_off():
            return
        hub = self._get_current_hub_key()
        panel = self.hub_panels.get(hub)
        client = self.get_client() if self.get_client else None
        if panel and client:
            self._pull_active_region_if_stale(hub, panel, client)
            self._freshen_holdings_for_hub(hub, panel, client)

    def _get_stale_hubs(self, client) -> list:
        """Return enabled hubs without a complete snapshot under 24 hours old."""
        stale = []
        for hub_key, config in TRADE_HUBS.items():
            if not is_stock_market_hub(hub_key):
                continue
            cache_key = get_hub_order_cache_key(hub_key)
            snapshot = client.order_cache.peek_cached_orders(
                cache_key, max_age_seconds=86400
            )
            if snapshot is not None:
                _orders, age = snapshot
                print(f"[StockMarket] {hub_key}: cache fresh ({age/3600:.1f}h old)")
                continue
            timing = client.order_cache.get_cache_timing(cache_key, 86400)
            age = timing['age_seconds']
            if age is None:
                print(f"[StockMarket] {hub_key}: no cache entry - will pull")
            else:
                print(
                    f"[StockMarket] {hub_key}: cache unusable "
                    f"({age/3600:.1f}h old, {timing['reason']}) - will pull"
                )
            stale.append((hub_key, cache_key, config["name"]))
        return stale

    def _run_daily_hub_burst(self):
        """Pull any hub whose cache is older than 24h. Silent background pull."""
        if not self.get_client:
            return
        client = self.get_client()
        if not client:
            return

        tracker = get_hub_burst_tracker()
        stale = [
            (hub_key, cache_key, name)
            for hub_key, cache_key, name in self._get_stale_hubs(client)
            if tracker.should_run(hub_key, client.order_cache)
        ]
        if not stale:
            return

        hub_names = ", ".join(h for h, _, _ in stale)
        print(f"[StockMarket] Daily burst: pulling {hub_names}")

        def run_burst():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def do_burst():
                    client.reset_for_new_loop()
                    client.ensure_session()
                    active = self._active_hub_key or self._get_current_hub_key()
                    for hub_key, cache_key, _ in stale:
                        try:
                            current = client.order_cache.peek_cached_orders(
                                cache_key, max_age_seconds=86400
                            )
                            if current is not None:
                                tracker.mark_complete(hub_key)
                                print(
                                    f"[StockMarket] Daily burst: {hub_key} "
                                    "already refreshed; skipping"
                                )
                                continue
                            print(f"[StockMarket] Daily burst: {hub_key}")
                            await client.get_orders_for_hub(hub_key)
                            timing = client.order_cache.get_cache_timing(
                                cache_key, max_age_seconds=86400
                            )
                            if timing['complete']:
                                tracker.mark_complete(hub_key)
                            else:
                                print(
                                    f"[StockMarket] Daily burst: {hub_key} "
                                    "returned no complete snapshot; leaving due"
                                )
                            if hub_key == active:
                                panel = self.hub_panels.get(hub_key)
                                if panel:
                                    submit(lambda p=panel: p.render_from_cache(
                                        client.order_cache))
                        except Exception as e:
                            print(f"[StockMarket] Daily burst failed "
                                  f"for {hub_key}: {e}")
                loop.run_until_complete(do_burst())
            finally:
                client.close_loop_state(loop)
                loop.close()

        threading.Thread(target=run_burst, daemon=True).start()

    def _pull_active_region_if_stale(self, hub_key, panel, client):
        """Pull fresh orders for the active tab when its saved acquisition is at least one hour old."""
        hub_cfg = TRADE_HUBS.get(hub_key, {})
        if not hub_cfg:
            return
        cache_key = get_hub_order_cache_key(hub_key)

        snapshot = client.order_cache.peek_cached_orders(
            cache_key, max_age_seconds=self._ACTIVE_REGION_MAX_AGE
        )
        if snapshot is not None:
            _orders, _age = snapshot
            print("[ESI-Timing] resource=hub_orders trigger=stock_hub_load "
                  f"hub={hub_key} decision=REUSE reason=under_one_hour age={_age:.0f}s")
            panel.render_from_cache(client.order_cache)
            return

        # Keep the saved view readable while an eligible update is acquired.
        panel.render_from_cache(client.order_cache)
        timing = client.order_cache.get_cache_timing(
            cache_key, max_age_seconds=self._ACTIVE_REGION_MAX_AGE
        )
        age = timing['age_seconds']
        if age is not None:
            print(
                f"[StockMarket] Active tab pull: {hub_key} "
                f"({age:.0f}s old, {timing['reason']})"
            )
        else:
            print(f"[StockMarket] Active tab pull: {hub_key} (no cache)")

        def do_pull():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def pull():
                    client.reset_for_new_loop()
                    client.ensure_session()
                    if client.order_cache.peek_cached_orders(
                            cache_key, max_age_seconds=self._ACTIVE_REGION_MAX_AGE) is None:
                        await client.get_orders_for_hub(hub_key)
                loop.run_until_complete(pull())
            except Exception as e:
                print(f"[StockMarket] Active tab pull failed for {hub_key}: {e}")
            finally:
                client.close_loop_state(loop)
                loop.close()
            submit(lambda: panel.render_from_cache(client.order_cache))

        threading.Thread(target=do_pull, daemon=True).start()
