"""Burst-mode order pull mixin for StockMarketTab."""

import asyncio
import threading
from datetime import datetime, timezone

from core.tk_queue import submit
from core.config import TRADE_HUBS
from analytics.stockmarket_filters import get_hub_burst_tracker


class StockMarketBurstMixin:

    _ACTIVE_REGION_MAX_AGE = 300  # 5 min = one scanner cycle

    def _run_daily_hub_burst(self):
        """Pull any hub whose cache is older than 24h. Silent background pull."""
        if not self.get_client:
            return
        client = self.get_client()
        if not client:
            return

        tracker = get_hub_burst_tracker()
        stale = [
            (hub_key, region_id, name)
            for hub_key, region_id, name in self._get_stale_hubs(client)
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
                    client.ensure_session()
                    client.reset_for_new_loop()
                    active = self._active_hub_key or self._get_current_hub_key()
                    for hub_key, region_id, _ in stale:
                        try:
                            print(f"[StockMarket] Daily burst: {hub_key}")
                            await client.get_market_orders(region_id)
                            tracker.mark_complete(hub_key)
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
                loop.close()

        threading.Thread(target=run_burst, daemon=True).start()

    def _pull_active_region_if_stale(self, hub_key, panel, client):
        """Pull fresh orders for the active tab if cache is older than 5 min."""
        hub_cfg = TRADE_HUBS.get(hub_key, {})
        region_id = hub_cfg.get("region_id")
        if not region_id:
            return

        entry = client.order_cache._order_cache.get(region_id, {})
        ts = entry.get("timestamp")

        if ts is not None:
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < self._ACTIVE_REGION_MAX_AGE:
                panel.render_from_cache(client.order_cache)
                return
            print(f"[StockMarket] Active tab pull: {hub_key} ({age:.0f}s old)")
        else:
            print(f"[StockMarket] Active tab pull: {hub_key} (no cache)")

        def do_pull():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                async def pull():
                    client.ensure_session()
                    client.reset_for_new_loop()
                    await client.get_market_orders(region_id)
                loop.run_until_complete(pull())
            except Exception as e:
                print(f"[StockMarket] Active tab pull failed for {hub_key}: {e}")
            finally:
                loop.close()
            submit(lambda: panel.render_from_cache(client.order_cache))

        threading.Thread(target=do_pull, daemon=True).start()
