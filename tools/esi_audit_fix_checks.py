"""Synthetic ESI audit regressions for the IDE's Ctrl+F12 runner.

Only fixture-local gate/session/semaphore state is used. No live client,
transport, credentials, cache or process-wide cooldown is touched.
"""

import asyncio
from contextlib import asynccontextmanager

from contracts.contracts_esi import _raw_request_network


def _require(condition, message):
    if not condition:
        raise AssertionError(message)


class _Response:
    status = 200

    def __init__(self):
        self.headers = {"ETag": "synthetic-contract"}
        self.payload = [{"contract_id": 1}]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self.payload


class _Session:
    def __init__(self):
        self.calls = []
        self.response = _Response()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class _Client:
    def __init__(self):
        self.semaphore = asyncio.Semaphore(1)
        self.gate = asyncio.Event()
        self.gate.set()
        self.outer_passed = asyncio.Event()
        self.inner_entered = asyncio.Event()
        self.gate_calls = 0
        self.session = _Session()
        self.recorded = []

    def ensure_session(self):
        return self.session

    async def _await_rate_limit_pause(self):
        self.gate_calls += 1
        if self.gate_calls > 1:
            self.inner_entered.set()
        await self.gate.wait()
        if self.gate_calls == 1:
            self.outer_passed.set()

    def _record_response_headers(self, response, endpoint):
        self.recorded.append((response, endpoint))


@asynccontextmanager
async def _queued_during_cooldown():
    client = _Client()
    await client.semaphore.acquire()
    slot_held = True
    request = asyncio.create_task(_raw_request_network(
        client, "GET", "/contracts/public/1/", params={"page": 1},
        extra_headers={"If-None-Match": "synthetic-old"}))
    probe = None
    try:
        # The request signals its outer pass and then queues at the held slot
        # before yielding control back to this fixture.
        await asyncio.wait_for(client.outer_passed.wait(), timeout=3)
        _require(not request.done() and not client.session.calls,
                 "request did not queue behind the occupied semaphore")
        client.gate.clear()
        client.semaphore.release()
        slot_held = False
        probe = asyncio.create_task(client.inner_entered.wait())
        done, _ = await asyncio.wait(
            (request, probe), timeout=3, return_when=asyncio.FIRST_COMPLETED)
        # Without the inner check, the real request completes and fails here
        # immediately; the timeout is only a deadlock watchdog.
        _require(request not in done and not request.done(),
                 "queued contract dispatched during the new cooldown")
        _require(probe in done and client.inner_entered.is_set(),
                 "queued contract never reached its dispatch gate")
        _require(client.gate_calls == 2 and not client.gate.is_set()
                 and not client.session.calls and not client.recorded,
                 "blocked dispatch sent a request or recorded a response")
        yield client, request
    finally:
        if slot_held:
            client.semaphore.release()
        tasks = [task for task in (request, probe) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def contract_queued_cooldown(directory):
    async def scenario():
        async with _queued_during_cooldown() as (client, request):
            client.gate.set()
            result = await asyncio.wait_for(request, timeout=3)
            response = client.session.response
            _require(result == (200, response.headers, response.payload),
                     "gate release changed successful status, headers or payload")
            _require(len(client.session.calls) == 1,
                     "gate release did not send exactly one request")
            method, url, kwargs = client.session.calls[0]
            _require(method == "GET" and url.endswith("/contracts/public/1/")
                     and kwargs == {"params": {"page": 1}, "json": None,
                                    "headers": {"If-None-Match": "synthetic-old"}},
                     "dispatch changed request arguments or conditional headers")
            _require(client.recorded == [(response, "/contracts/public/1/")],
                     "successful response was not recorded exactly once")

    asyncio.run(scenario())


def contract_cancelled_cooldown(directory):
    async def scenario():
        async with _queued_during_cooldown() as (client, request):
            request.cancel()
            try:
                await request
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError("cooldown cancellation did not propagate")
            _require(not client.session.calls and not client.recorded,
                     "cancelled contract dispatched or recorded a response")

            async def acquire_slot():
                async with client.semaphore:
                    return True

            follower = asyncio.create_task(acquire_slot())
            try:
                _require(await asyncio.wait_for(follower, timeout=3),
                         "cancelled contract retained its semaphore slot")
            finally:
                if not follower.done():
                    follower.cancel()
                await asyncio.gather(follower, return_exceptions=True)

    asyncio.run(scenario())
