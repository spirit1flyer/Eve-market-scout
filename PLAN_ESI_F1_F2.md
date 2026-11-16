# Targeted ESI plan: F1 and F2

Prepared 2026-11-16 against app `3.67 live`.
Status: F1 implemented locally on 2026-11-16; syntax/diff checks passed,
IDE runtime verification pending. F2 and its two checks remain planned.
The current IDE runner contains 54 checks; 56 remains the F1/F2 target.
Caleb approved the version bump and commit/push on 2026-11-16; current source
is `3.68 live`. This does not change the pending runtime-verification status.

## Scope and outcome

Address the two findings selected by Caleb:

- F1: queued contract requests must recheck the shared cooldown after obtaining
  a concurrency slot and before sending.
- F2: Tracking must reuse a complete regional snapshot through its advertised
  expiry, including when its acquisition age exceeds 300 seconds.

F3, executable packaging, retry-budget changes, new rate-limit machinery, and
the earlier pending-work list are outside this change. The current Git index
contains no `dist/` files or `.exe` files; this plan has no binary-release work.

## 1. F1: recheck cooldown at contract dispatch

**Production file:** `contracts/contracts_esi.py`, `_raw_request_network()`,
currently lines 88-94.

Retain the existing outer `_await_rate_limit_pause()` call. Insert a second
call inside `async with client.semaphore`, immediately before
`session.request(...)`:

```python
while True:
    await client._await_rate_limit_pause()
    async with client.semaphore:
        await client._await_rate_limit_pause()
        async with session.request(...) as response:
            ...
```

Add a short comment explaining that another response can establish a cooldown
while this task waits for its concurrency slot. This follows the existing
`core/esi_get.py` pattern and uses the existing process-wide gate for both 429
and legacy-error pauses.

The check runs on every loop iteration, including retries. Keep the current
response recording, headers, conditional requests, 429/520 retry behavior,
semaphore size and session lifecycle. A task may hold a concurrency slot while
waiting for the gate; cancellation must release that slot normally.

**Acceptance:** after a task passes the outer gate and queues for a slot, a
new cooldown prevents that task from calling `session.request` until the gate
opens. Already dispatched requests can finish.

## 2. F2: use the existing expiry-aware regional accessor

**Production file:** `gui/tracking/gui_tracking_market.py`,
`fetch_market_orders_sync()`, currently lines 9-16.

Replace `peek_cached_orders(region_id, max_age_seconds=300)` with
`get_cached_orders(region_id, max_age_seconds=300)`. The latter returns the
orders directly, rather than an `(orders, age)` tuple. Test the result with
`is not None`, so a valid empty complete book is a cache hit.

Return the full regional order list as this function already does. The
existing consumers filter by station, order side and relevant listings;
`scanning/underbid_monitor.py` already performs that filtering. Do not introduce
a new filtered representation or mutate the shared rows.

Retain the existing `[TradeSync] stage=underbid_snapshot decision=REUSE`
diagnostic and its source/region/order count. Obtain display-only age from
`get_cache_timing()`, formatting an unavailable age as `unknown`. This metadata
read must not make another admission decision or trigger acquisition.

On a cache miss, retain the exact existing per-type route-cache path, including
unique/sorted type IDs, `order_type=sell`, cached-response reuse and partial
failure handling. Retain the no-active-types skip and the separate private
structure path in `fetch_underbid_orders_sync()`.

The resulting decision table is:

| Regional book | Tracking behavior |
| --- | --- |
| Complete, future ESI expiry, any acquisition age | Reuse full book; no per-type acquisition |
| Complete and empty, future ESI expiry | Reuse empty result; no per-type acquisition |
| Explicitly expired, even if younger than 300 seconds | Use existing per-type fallback |
| Missing or incomplete | Use existing per-type fallback |
| No expiry metadata | Apply the accessor's existing 300-second age policy |
| No usable book and no active sell types | Return empty result without acquisition |
| Private structure hub | Use the existing authorized structure provider |

Removing the peek is intentional: an explicitly expired book younger than
300 seconds will now reach the fallback. That fallback still honors its own
cache. `core/order_cache.py` needs no change. Its existing local save retry for
a usable but nondurable regional snapshot remains part of the accessor.

## 3. Add four focused checks to the existing IDE suite

Add `tools/esi_audit_fix_checks.py`; register its four check functions in
`tools/private_cache_checks.py::run_checks()`. Baseline: 52 checks. Expected
total after this change: 56 checks.

### `contract_queued_cooldown`

Exercise the real `_raw_request_network()` with a fake client/session and a
local `asyncio.Semaphore(1)`. All gate state belongs to the fixture.

1. Have the test acquire the semaphore.
2. Start the request task and wait for an event proving its first gate call
   returned and it reached the occupied semaphore.
3. Close the fixture gate, then release the semaphore.
4. Race a probe for entry to the second gate check against task completion.
   The old code must fail by dispatching/completing, rather than merely timing out.
5. Assert the second gate check is waiting and the session has received zero
   request calls.
6. Open the gate; assert exactly one request and the expected successful
   status, headers and payload.

Use events for ordering and a short timeout only as a deadlock watchdog.
One blocked-gate scenario covers the dispatch placement shared by both server
pause reasons. Do not manufacture real throttling or test a fake implementation
of deadline arithmetic; the production gate itself is not being changed.

### `contract_cancelled_cooldown`

Repeat the blocked state above, then cancel the request task. Assert that
`CancelledError` propagates, no request was sent, and another task can acquire
the semaphore. Clean up all fixture tasks in `finally` blocks.

### `underbid_regional_expiry_reuse`

Use the actual `OrderCacheStore` behavior through the existing isolated
`tools/acquisition_batch_checks.py::_Orders` fixture. Use a route cache with an
injected requester that fails if acquisition occurs.

- Seed a complete book acquired 301 seconds ago with expiry 600 seconds after
  acquisition. Call the production Tracking helper; assert full rows returned
  and zero route requests.
- Restore the book into a fresh isolated store from the temporary directory;
  assert the same outcome and unchanged acquisition/expiry metadata.
- Seed a separate valid empty book with future expiry; assert `[]` and zero
  route requests. Use a fresh case directory to respect the cache's existing
  protection against replacing populated data with an unvalidated empty write.
- Include buy orders, other types and other locations in the populated fixture
  and assert the returned shared book is intact.

### `underbid_regional_fallback`

Use isolated regional and route stores with an injected successful requester.
Run each case with separate fixture state:

- A book acquired 60 seconds ago but already expired: per-type fallback.
- A missing book and an incomplete book: per-type fallback.
- A book without expiry, aged 60 seconds: reuse; aged 600 seconds: fallback.
- A cache miss with no active sell types: zero requests and an empty result.
- Duplicate active type IDs: one acquisition per unique type; a repeated call
  before route expiry adds zero requests.

Assert fallback responses are returned correctly and regional timestamps are
not refreshed by reading them. Use timestamps safely away from boundaries;
do not wait hundreds of seconds or patch a process-wide clock.

### Existing checks and isolation

Update the regional-cache doubles in
`tools/route_cache_checks.py::underbid_structure_routing()` to implement
`get_cached_orders` and display-only `get_cache_timing`. Keep the structure
guard rejecting any attempt to consult the public regional cache. Its existing
authorization, account-change and failed-private-fetch assertions must pass.

All new checks use temporary directories, fake sessions and injected transports.
Use the existing `_Orders` and route-store fixtures that suppress production
diagnostic counters. Do not instantiate the production ESI client, read live
credentials/caches, patch global transports or arm the process-wide cooldown.

## 4. Implementation order and evidence

1. Add the focused checks and runner registration. Demonstrate that the two
   regression scenarios detect the existing bugs in the IDE check environment
   when practical; otherwise record that pre-fix runtime evidence is unavailable.
2. Apply F1, then F2 as the small edits above. Update affected test doubles.
3. Check syntax of the changed Python files, inspect the diff, and run
   `git diff --check`. No standalone network harness is needed.
4. In the IDE-launched app from this checkout, run **Ctrl+F12**. Verify four
   named PASS lines and `[ESI-CacheCheck] passed=56 total=56 network=none
   data=synthetic` in `%APPDATA%/EVEMarketScout/eve_scout.log`.
5. Inspect ordinary Contracts and Tracking behavior in the next user-run IDE
   session. Read its log for successful operations and unexpected exceptions;
   use the synthetic regression as proof of the rare queue race. No live 429
   or unusually long server expiry is needed for acceptance.
6. Update `CLAUDE.md` with the new check module/count and `ESI_DIAGNOSTICS.md`
   with the new checks and their actual verification status. Append the F1/F2
   resolution and evidence to `ESI_FULL_AUDIT_2026-11-16.md`, preserving its
   original observations. Record any pending IDE verification explicitly.

Validation follows the repository's
[verify guide](.claude/skills/verify/SKILL.md): syntax checks plus the existing
IDE runner and saved log. The user controls app launch and Ctrl+F12; the agent
reads the results. Confirm the run identifies this checkout and includes the
new check names before treating an older passing log as evidence.

## Exact intended file set

| File | Planned change |
| --- | --- |
| `contracts/contracts_esi.py` | Inner cooldown check and explanatory comment |
| `gui/tracking/gui_tracking_market.py` | Expiry-aware accessor and compatible logging |
| `tools/esi_audit_fix_checks.py` | Four isolated checks and their small fixtures |
| `tools/private_cache_checks.py` | Register four checks |
| `tools/route_cache_checks.py` | Adapt regional-cache test doubles |
| `CLAUDE.md` | Map the check module and update check count |
| `ESI_DIAGNOSTICS.md` | Document checks and observed results |
| `ESI_FULL_AUDIT_2026-11-16.md` | Append F1/F2 disposition and evidence |

Completion means the two production paths meet the acceptance conditions,
all 56 checks pass in the correct IDE run, and documentation distinguishes
observed results from pending evidence. Versioning and publication remain
separate user decisions.
