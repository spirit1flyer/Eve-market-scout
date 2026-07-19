"""Shared helper for running an ESIClient coroutine on a throwaway event loop.

Worker threads that want to call the shared async `ESIClient` spin up their own
event loop (sessions/semaphores are per-loop). The correct lifecycle — the one
`gui_add_station._run_async` established — is: new loop, `reset_for_new_loop`,
open one aiohttp session on it, run the coroutine, then close BOTH the session
(via `client.close_loop_state`) and the loop. Skipping the session close leaks a
socket per call AND leaves a stale `_per_loop` entry that a future loop reusing
the same `id()` can inherit ("Event loop is closed" errors). Centralizing that
here keeps every call site honest.

Two entry points:
  * `run_coro_blocking(client, coro_fn)` — call from INSIDE an existing worker
    thread; runs synchronously on a fresh loop and returns the result (or raises).
  * `run_async(get_client, coro_fn, callback)` — spawns a daemon worker thread
    and posts (result, err) back to the Tk thread via `tk_queue.submit`.
"""

import asyncio
import threading

import aiohttp

from core.config import REQUEST_TIMEOUT, ESI_USER_AGENT
from core.ssl_context import make_connector


def run_coro_blocking(client, coro_fn):
    """Run `coro_fn(client)` on a fresh event loop and return its result.

    Intended to be called from within a worker thread (it blocks that thread on
    `run_until_complete`). Creates and owns the loop + aiohttp session, and — on
    both the success and error paths — closes the session via
    `client.close_loop_state(loop)` BEFORE closing the loop, so nothing leaks.
    Exceptions from `coro_fn` propagate to the caller after cleanup runs.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        client.reset_for_new_loop()

        async def _run():
            # aiohttp's TCPConnector binds to (and requires) a running loop at
            # construction, so the session must be built here inside
            # run_until_complete, not before it.
            session = aiohttp.ClientSession(
                connector=make_connector(),
                headers={"User-Agent": ESI_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            )
            client.session = session
            return await coro_fn(client)

        return loop.run_until_complete(_run())
    finally:
        # close_loop_state awaits session.close() on this still-open loop, then
        # forgets the per-loop entry — must run before loop.close().
        client.close_loop_state(loop)
        loop.close()


def run_async(get_client, coro_fn, callback):
    """Run an async coroutine in a daemon thread; post result to the Tk thread.

    `get_client()` supplies the shared client (may be None → error callback).
    `callback(result, err)` is marshaled onto the Tk main thread via
    `tk_queue.submit`. Thin threading wrapper over `run_coro_blocking`.
    """
    def worker():
        # Import here so importing this module stays headless-safe (no Tk root
        # is touched at import time).
        from core.tk_queue import submit

        client = get_client() if get_client else None
        if not client:
            submit(lambda: callback(None, RuntimeError("No ESI client")))
            return

        result = err = None
        try:
            result = run_coro_blocking(client, coro_fn)
        except Exception as e:
            err = e
            print(f"[AddStation] async error: {e}")

        submit(lambda r=result, e=err: callback(r, e))

    threading.Thread(target=worker, daemon=True).start()
