"""Consumer age limits on top of shared, ESI-expiry-aware order snapshots."""

from datetime import datetime, timezone

from core.config import JITA_REGION_ID
from core.esi_diagnostics import decision_event

JITA_REFERENCE_SECONDS = 24 * 3600
STOCK_ORDER_SECONDS = 3600


class JitaReferenceMixin:
    def clear_jita_cache(self):
        """Clear the legacy view; eligible refresh still honors shared ESI expiry."""
        self.jita_orders_cache = []
        self.jita_orders_timestamp = None

    def has_jita_cache(self):
        return self.order_cache.peek_cached_orders(
            JITA_REGION_ID, JITA_REFERENCE_SECONDS) is not None

    def get_jita_cache_age(self):
        entry = self.order_cache.get_snapshot_entry(JITA_REGION_ID)
        if not entry:
            return 'No cache'
        minutes = max(0, int((datetime.now(timezone.utc)-entry['timestamp']).total_seconds()/60))
        if minutes < 1:
            return '< 1 min'
        if minutes < 60:
            return f'{minutes} min'
        return f'{minutes//60}h {minutes%60}m'

    async def get_jita_reference(self, refresh=False):
        """Only reference use gets 24 hours; active Jita uses get_market_orders."""
        cached = self.order_cache.peek_cached_orders(JITA_REGION_ID, JITA_REFERENCE_SECONDS)
        if cached is not None and not refresh:
            orders, age = cached
            decision_event('regional_orders', 'REUSE', 'jita_reference',
                           reason='reference_under_24h', age=f'{age:.0f}s', items=len(orders))
            return orders
        decision_event('regional_orders', 'FETCH', 'jita_reference',
                       reason='explicit_refresh' if refresh else 'missing_or_24h')
        return await self.get_market_orders(JITA_REGION_ID)
