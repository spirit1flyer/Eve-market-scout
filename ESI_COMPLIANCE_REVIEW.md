# ESI compliance reassessment

Reviewed 2026-11-15 against the current working tree, app version `3.67 live`,
the existing local runtime log, and the official sources linked below.
Uncommitted diagnostics are part of the reviewed tree. This is an engineering
assessment, not legal certification or proof that every route was exercised.

## Current plan and reading this review

The [execution plan and status](ESI_ACQUISITION_GOALS.md) were updated after
checking the original assessment and implementation conversations, including
the user's corrections. Use that plan for the current work order.

The conclusion, evidence and seven findings immediately below describe the
**pre-implementation baseline**. In particular, public Industry response sharing
and wallet persistence have since been implemented. The coverage table reflects
the later source review; dated implementation/evidence entries retain the
history. Neither the old baseline nor an HTTP-success counter is a current
all-route pass/fail list.

## Baseline conclusion

The [agreed acquisition policy](ESI_ACQUISITION_GOALS.md) is compatible with
ESI. Fresh scanner orders and deliberately older Stock Market, Industry, and
Jita-reference data can coexist. Different hubs do not require one global
refresh cooldown. Fetching only the resources being used is appropriate.

The app is **not ready for an unqualified compliance sign-off**. There are
observed pre-expiry duplicates, other acquisition paths without expiry-aware
reuse, restart gaps, and a distribution-notice gap. These are distinct from
product freshness bugs. Intentionally reusing an older Jita reference is not
itself cache circumvention.

The developer account and monitored contact email are user-confirmed compliant;
they were not reopened or independently inspected. No credentials were read,
no app was launched, and this reassessment changes documentation only.

## Official baseline

- Identify the application; reuse responses until ESI expiry; check paginated
  representations for consistency. Cache circumvention is a real enforcement
  risk, but not every recommendation is a hard requirement.
  [ESI best practices](https://developers.eveonline.com/docs/services/esi/best-practices/)
- Rate budgets are per group and user identity, not one cooldown per hub.
  Observe both legacy error headers and bucket headers, slow down near limits,
  and honor `Retry-After` on 429. Multiple hubs can consume the same bucket.
  [Rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/)
- Regional market orders have a five-minute server cache. CCP's February 2026
  rollout documents a 12,000-token market-order budget per 15-minute window,
  with successful pages costing two tokens. This is capacity, not permission
  to fetch unused regions or poll faster.
  [Market-order rollout](https://developers.eveonline.com/blog/market-orders-rate-limit-rolls-out-on-february-24-2026)
- Compatibility-date headers are the current versioning mechanism. Legacy
  version aliases remain supported under the published migration guidance;
  an earlier matched response date is not by itself a fault.
  [Versioning guidance](https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand)
- SSO requires appropriate consent and secure handling of grants. Repeated
  failed requests must not ignore SSO rate limiting.
  [SSO documentation](https://developers.eveonline.com/docs/services/sso/),
  [SSO rate-limit warning](https://developers.eveonline.com/blog/sso-endpoint-deprecations-2)
- Distribution must retain CCP's proprietary notices; necessary consent and
  third-party rights also matter. This review does not certify all applicable
  privacy law or external-data licensing.
  [Developer License Agreement, sections 2, 7, and 9](https://developers.eveonline.com/license-agreement)

## Baseline evidence

The saved log contains two starts from this checkout, including run
`7944-1794774835`, with the source path, version, and fingerprint. That run
restored 412,992 Jita orders and 190,837 Amarr orders. An unexpired Amarr
snapshot was reused. Thus regional disk restoration is functioning; restarting
does not automatically force a fresh full regional pull.

Jita's persisted snapshot metadata had acquisition time
`2026-11-15T12:46:52.539425+00:00`, `complete=true`, and the same restored count.
This supports preservation of that snapshot, not a claim that all historical
Jita references were fresh or days old. Its server expiry can pass while it
remains an acceptable reference under the separate 24-hour product policy.

In the preceding recorded run, `/markets/prices/` and `/industry/systems/`
were requested at log time 14:32:51 and again at 14:33:32. The observer recorded
approximately 2,954 and 3,143 seconds remaining before their previous expiry.
These are actual pre-expiry repeat attempts, not merely speculative findings.

The later run's latest inspected heartbeat showed 218 HTTP responses, three
errors, zero pending headers, and 13 observed route families. Three contract
name-resolution batches returned 404. No 420/429 was observed in the inspected
run; this is not a guarantee against future limiting. Its zero early-repeat
count cannot clear requests across process restarts because expiry observation
is session-only.

The running process loaded diagnostics fingerprint `7658427254de`. Automatic
full reports and F12 were added to disk afterward; their absence from that
process's log does not prove the new disk code ran and failed. Heartbeats are
verified; the newer report behavior remains live-unverified. HTTP headers
alone never prove successful parsing, complete pagination, or persistence.

## Baseline findings, prioritized

### 1. Observed public-data repeats: acquisition compliance gap

`industry/industry_market_data.py::_fetch_industry` obtains adjusted prices
and cost indices through `_get`, discards response cache metadata, filters to
the current consumer's IDs, and applies its own six-hour clock. Independent
consumers can acquire the same unexpired public representations. The log
demonstrates the repeats described above.

Required direction: one shared full-response, expiry-aware, durable provider
per public resource with in-flight deduplication; consumers filter afterward.
A six-hour consumer preference alone neither proves nor disproves correct
server-expiry handling. Do not solve this by making all Industry data fresh
every five minutes.

### 2. Direct/manual paths: statically demonstrated repeat-request risks

- `gui/gui_browse_orders.py::_kick_fetch` calls `fetch_structure_orders`
  directly and discards expiry/metadata instead of using the shared snapshot.
  Reopening/refreshing can refetch an unexpired structure book.
- `gui/gui_discover_structures.py::_on_discover` and
  `esi/esi_structures.py::discover_accessible_structures` reacquire active
  character orders and structure metadata without expiry-aware reuse. The
  wallet path cannot protect a separate direct caller.
- `gui/tracking/gui_tracking_sync.py::fetch_market_orders_sync` reuses a recent
  full snapshot, but its per-type fallback saves neither payload nor expiry.
  Subsequent refreshes after a full-cache miss can repeat the same requests.
- `core/station_data.py::fetch_stations_in_system` intentionally refetches
  system/station resources on each invocation. Region/system list persistence
  does not cover this path. Related metadata providers have separate caches.

These are code-backed risks, not claims that each action was reproduced live.
Manual intent does not exempt a request from ESI's cache policy.

### 3. Restart protection is incomplete outside regional orders

`esi/esi_wallet.py` starts balance/transaction/journal/order data and each
resource's success/expiry state empty. Its independent in-process expiry gates
work, but restart bootstraps acquisition again even if the previous response
is still valid. Persisted trade analytics are not a substitute for the exact
reusable resource payload and metadata.

Trading and Industry skills/standings use separate in-memory hourly caches;
their response-expiry state is not durably shared. Type/system fallbacks also
have in-memory components. These need resource-appropriate persistence and
reuse rather than a global startup delay.

### 4. Holdings history can say "fresh" without retaining its data

`gui/stockmarket/gui_stockmarket_actions.py::_freshen_holdings_for_hub` persists
an hourly success timestamp. `core/api.py::freshen_history_for_items` merges
the newly obtained ESI history only into memory. After restart, the saved
timestamp can suppress fetching while those recent ESI rows are absent. The
archive history database does not automatically contain that recent overlay.
The log shows an hourly skip at age 76 seconds after restart.

Additionally, history fetch failures are collected without necessarily raising
to this caller; it can print `FETCHED` and save a success timestamp despite
missing results. Persist accepted history with per-resource expiry and report
partial/failure results honestly. The hourly schedule is a consumer preference,
not a substitute for the history route's actual expiry.

### 5. Agreed freshness goals are not fully implemented

- Jita preservation works, but `has_jita_cache()` and the non-Jita scanner's
  reference branch test presence, not the 24-hour limit. A reference can be
  reused indefinitely without another acquisition trigger.
- `core/api.py::get_market_orders(use_cache=True)` has an unbounded legacy
  Jita fallback. Cross-hub scans pass this flag when `refresh_data` is false,
  allowing an active Jita target to use reference-age data.
- Stock Market active-hub logic uses a five-minute threshold in
  `gui_stockmarket_burst.py`, while another refresh path uses 20 minutes in
  `gui_stockmarket_actions.py`. Neither consistently implements the requested
  one-hour on-load policy. The 24-hour daily burst is another separate policy.

These are product/data-freshness gaps. Old displayed data does not itself
violate ESI limits; unnecessarily repeating an unexpired acquisition does.
Regional caches also use a fixed 300-second freshness ceiling, so a future
server expiry longer than that must still prevent premature acquisition.

### 6. SSO cooldown handling is separate from ESI pacing

Trading and Industry token/verify calls are observed through `traced_request`,
not the ESI rate gate. They return failure on non-200 but do not retain an SSO
`Retry-After` cooldown. Repeated login/refresh attempts could ignore a previous
429. Add a shared SSO-specific cooldown/error-state policy; no SSO rate-limit
failure was demonstrated by the inspected log.

### 7. Public binary notice/disclosure gap

`ESI_DATA_USAGE.md` contains the CCP notice and local-storage disclosure, but
`EVE-Market-Scout.spec` bundles no documentation and no in-app notice entry
was found. Source documentation alone is not reliable notice delivery to a
binary-only recipient. Include an accessible notice/disclosure in the public
distribution before claiming distribution compliance.

Its current blanket claim that manual actions always reuse unexpired data
also exceeds the implementation described above. Correct that wording rather
than treating this assessment as proof those paths were fixed.

## Route-family coverage

Static coverage is grouped below; it is not a runtime pass list. Header and
shared-rate-gate wiring were reviewed across async and synchronous ESI callers.

| Route family / consumer | Current assessment |
| --- | --- |
| Regional market orders: scanner and shared hub provider | Normal disk restoration/reuse observed. Remaining gaps: non-atomic disk writes, successful-page reuse across full retries/restart, missing-validator acceptance and later-page metadata checks; Jita fallback and consumer ages also need correction. |
| Regional orders: tracking per-type fallback | Shared full-cache reuse first; fallback lacks durable payload/expiry. A vanished later page can also leave partial data looking complete. |
| Structure market orders: shared hub provider | Cache currently keyed by structure ID alone and returned before authentication checks. Scope by authorization context before routing Browse through it. Pagination/retry and direct Browse gaps remain. |
| Wallet balance, transactions, journal, active orders, order history | Shared durable provider, real restart/reuse with original clocks verified, eleven offline cache/accounting checks passed. Live character changes/discovery sharing and accounting beyond the fixtures remain open. |
| Character skills and standings, trading and Industry | Seventeen checks and real seller/Industry acquisition, persistence, restoration/reuse passed. Skills follow Cache-Control. New manual/seven-day reputation retention policy is implemented, awaiting nineteen-check/live verification. Buyer login fails before acquisition and needs reauthentication. |
| Character blueprints | SQLite payload and expiry exist. Remaining: concurrent-pull deduplication, context review, stricter page/row validation, and honest DB-failure result; a caught write failure can be reported as successful zero-row storage. |
| Public adjusted prices and industry systems | Shared full-response expiry-aware provider and atomic saves implemented. Real acquisition, repeated reuse and restart restoration/reuse verified. Original observed repeats were the baseline defect. |
| Regional market history | Archive/supplement reuse exists; direct/holdings freshening lacks equivalent durable expiry-aware overlay. |
| Public contract lists and items | Durable scope expiry/ETag, conditional revalidation, page checks, immutable item caching. Ordinary GUI callers do not set the available force bypass; cross-caller in-flight deduplication remains a review item. |
| Universe names and IDs | Local/SDE lookup and batching reduce requests. Three observed name-batch 404s need bad-ID isolation/negative caching to avoid recurring error cost. POST responses may lack cache headers. |
| Universe types | SDE first, memory cache and ESI fallback; not universally durable or shared with other metadata providers. |
| Universe regions, constellations, systems, stargates, stations | Mixed disk/memory/static lookup caches; station-picker and independent metadata calls lack consistent expiry-aware reuse. |
| Universe structures and structure discovery | Discovery active orders share the private provider; live sharing unverified. Structure metadata still lacks common context-isolated expiry-aware persistence across callers. |
| Route/jump-count lookups | Successful values persisted; no excess periodic polling found. Concurrent misses need deduplication; topology-age invalidation is a correctness improvement. |
| Character structure search helper | Defined but no active caller found. Do not infer live compliance from an unused helper or require scope removal to settle this review. |
| SSO authorize, token, verify | PKCE/state, official endpoints, rotated refresh-token saving, and refresh locks exist. SSO cooldown gap above. |
| Images, static-data downloads, market-history archives | Outside ESI. Their traffic is not an ESI bucket/cache violation; third-party rights and service-specific policies remain separate. |

The shared rate gate reads both generations of ESI limit headers and observes
429 cooldowns. It is reactive and process-local, not an atomic token reservation
across queued requests or multiple app processes. Therefore its presence and a
20-request concurrency limit do not guarantee the app can never reach a limit.

## Items not established as compliance violations

- Reusing intentionally older references or display data; acquiring a different
  missing/expired hub; not preloading every region.
- Remaining `/latest` aliases or older matched compatibility dates alone. The
  code sends an explicit `2026-11-13` compatibility date. Migrate aliases as
  maintenance, not as a claimed cure for the acquisition gaps.
- Calling the official SSO `/v2/oauth/verify` endpoint. CCP explicitly retained
  it as the replacement for removed ESI `/verify`; local JWT validation is an
  optimization, not proof that this supported endpoint is forbidden.
  [Legacy-route removal guidance](https://developers.eveonline.com/blog/spring-cleaning-legacy-routes-removed-24-march-2026)
- Not sending conditional validators on every regional pull. Conditional
  requests can reduce transfer/token cost but do not authorize pre-expiry polls.
- Unencrypted local tokens by themselves. The Windows profile-access-control
  decision and disclosure were accepted previously; POSIX file-mode concerns
  do not establish a Windows failure. Actual ACLs were not audited here.
- An existing helper's unused scope, an untested route, or a small number of
  observed 404s by themselves. Scope minimization and avoiding repeat errors
  are useful hardening, not evidence of an existing ban.

## Next implementation order and sign-off boundary

Follow the [remaining phase-1 sequence](ESI_ACQUISITION_GOALS.md): close wallet
restart/accounting verification, then skills/standings and remaining direct
paths; durable history and honest completion; agreed consumer policies; SSO
cooldowns/notices; stage-by-stage acceptance and final route coverage. Public
Industry acquisition is already verified and should not be restarted as an
unfinished implementation task.

The original reassessment was documentation-only. Subsequent authorized
implementation and dated evidence are recorded below. The updated plan keeps
new code-review findings distinct from user-agreed product behavior. Broader
Stock Market workflow work follows completed phase 1; Industry follows that.

## Follow-up source review - 2026-11-15

The original messages were recovered from both local session records before
updating the plan. They confirm that transactions, journal and historical orders
underpin all buy/sell inventory tracking. The assistant's proposed extra hourly
consumer delay was corrected; it must not become a requirement. Actual ESI
expiry controls eligibility, independently per resource and across restarts.

Additional code-backed findings beyond the baseline descriptions:

- `core/api.py::get_orders_for_hub` looks up a structure-only order-cache key
  before resolving the current authentication context. Scope that private
  cache and its restoration before integrating Browse. This is a static
  isolation risk; the log does not demonstrate an unauthorized disclosure.
- `core/order_cache.py::_save_region_cache_to_disk` writes directly to the
  final gzip file and only prints exceptions. The new public/private providers'
  atomic writes do not fix this older order-cache path.
- `core/market_snapshot.py` and `esi/esi_structures.py::fetch_structure_orders`
  use first-page expiry and can return complete multi-page results without
  consistency validators. Later-page headers need fuller checks, and a retry
  of the whole snapshot can refetch a successful unexpired page. Retain staged
  pages across failures/restarts without publishing partial snapshots.
- `gui/tracking/gui_tracking_sync.py::fetch_market_orders_sync` stops on a
  later-page 404 and can return the accumulated partial orders. Its fallback
  needs durable expiry-aware storage and full-snapshot acceptance checks.
- `industry/industry_blueprints.py::replace_for_character` catches a database
  failure and returns zero; `BlueprintPuller.pull` then returns success with
  "stored 0 blueprints". Valid empty data must be distinguishable from failure.
  Missing consistency metadata, later-page expiry/count checks, skipped invalid
  rows, character-only cache scoping and concurrent pulls also need review.
- The Trade Refresh scheduler's existing 20-minute floor and active-order
  clock can delay linking-data acquisition after ESI expiry. Review wakeups
  without silently adding another hour or merging independent resource clocks.
- Existing inventory replay creates `ESIWallet(None)`. The new constructor had
  broken this compatibility; null-auth construction/refresh was corrected and
  a synthetic assertion added. That new assertion is not covered by the
  earlier eight-pass runtime result and needs the next in-app check run.

These findings refine the existing acquisition work, not a request for a
broader inventory redesign. Acceptance must compare the real inventory sync's
results from fetched, reused and restored linking data, including delayed
records and partial failures. Four listing updates alone do not prove FIFO,
fills, fees, cancellations or realized P&L.

## Implementation progress — 2026-11-15

The user authorized starting the saved plan. The first implementation chunk
addresses finding 1, the observed public Industry response repeats:

- `core/esi_public_cache.py` owns the complete `/markets/prices/` and
  `/industry/systems/` responses process-wide, independently keyed and locked
  across worker event loops. Existing parameter-free `_get` callers use it
  through `core/esi_get.py`; GET transport retains its existing rate gate,
  response observation, retry behavior, and exception handling.
- Payloads, original UTC acquisition times, absolute expiry, validators,
  route/source/schema and single-page completeness are saved together.
  Restoration precedes reuse decisions. Replacements use a unique temporary
  gzip file, flush/fsync, close and atomic `os.replace`.
- The existing six-hour public Industry preference is retained. An unexpired
  ESI response is reused even after six hours. An ESI-expired response younger
  than six hours is still acceptable for this consumer. This is separate from
  Industry order-snapshot policy; no new order-refresh interval was introduced.
- Industry filters full snapshots locally on each refresh, including newly
  needed IDs, and reports their original age. Its legacy filtered cache is
  display fallback, not evidence of a complete restored public response.
- Invalid/empty/unexpectedly paginated responses retain the prior snapshot.
  Disk write failure retains the previous file and keeps accepted memory data
  explicitly marked as not durable; subsequent reuse can retry saving without
  another network request. The UI reports a public-snapshot save failure.
- Events distinguish RESTORE, WAIT, REUSE, FETCH, ACCEPT, PERSIST and failures,
  including age, completeness, expiry and next eligible acquisition. The
  existing on-demand evidence report includes those metadata fields.
  REUSE also shows readable `next_refresh_in`; cached-order messages show time
  until the next eligible scan, and Industry status shows the wait independently
  for prices and indices. These are eligibility waits, not new automatic timers.

Syntax and diff checks passed. No live app was launched and no ESI acquisition
was initiated by the agent during implementation. The user's subsequent normal
IDE run verifies initial acquisition, durable payloads, and repeated reuse with
readable timing, followed by restoration and reuse across restart; details
follow below. Concurrent acquisition deduplication and failure handling remain
live-unverified.
The exact authenticated resources, direct/manual paths, history overlay,
scanner/Jita/Stock Market policies, SSO cooldown and distribution work remain
open. APP_VERSION remains `3.67 live`, pending the user's bump decision.

Next IDE-run check: Industry → Top Profit — T1 → **Refresh from caches**.
For `adjusted_prices` and `industry_systems`, initial missing snapshots should
show FETCH → ACCEPT → PERSIST. Refresh again while eligible for reuse: expect
REUSE and no new requests to those routes. Restart normally and repeat: expect
RESTORE then REUSE with original ages. A report can be printed using ESI Report
or F12 without starting a request. A first pull is expected when only the old
filtered Industry cache exists. Failed writes/pagination and concurrent callers
still need evidence; ordinary successful runs do not validate those cases.

### Live evidence — 2026-11-15, inspected after 17:04 Central

The user's IDE run `2920-1794783205` started at 16:53:27 from this checkout,
version `3.67 live`, diagnostics fingerprint `b0289dba4ca8`.

- At 16:54:27–28, both missing public snapshots completed FETCH → ACCEPT →
  PERSIST: 16,181 price rows and 5,485 industry-system rows, one page each.
- Refreshes at 17:03:09 and 17:03:42 logged REUSE for both resources. The latter
  showed `next_refresh_in=5h_50m_45s` for each. Industry completion lines also
  included readable per-resource waits. These are the existing six-hour public
  Industry policy, not the scanner order refresh cadence.
- The 17:03:28 report still counted exactly one HTTP request per public route.
  The latest inspected heartbeat at 17:03:58 had 78 responses, zero errors,
  zero pending headers and zero observed pre-expiry repeats for this run.
- Read-only inspection of both saved gzip files confirmed their complete
  payload counts, original UTC acquisition times, expiry, validators, source
  and single-page completeness. Acquisitions were `22:54:27.916811+00:00`
  (prices) and `22:54:27.512470+00:00` (systems), matching the log.

The automatic full report and public Industry wait formatting are now observed
live. The subsequent restart check below verifies these public snapshots across
restart too; it does not establish restoration for other resource families.

### Restart evidence — 2026-11-15, 17:07 Central

The user's next IDE run `30952-1794783973` started at 17:06:15 with the same
checkout and fingerprint. At 17:07:09, both public snapshots logged RESTORE
with complete payloads (16,181 prices and 5,485 systems), then REUSE. Ages were
761 and 762 seconds, preserving the original acquisitions from the prior run.
Readable waits were `5h_47m_19s` and `5h_47m_18s`.

The automatic report at 17:07:15–16 counted zero HTTP requests while recording
both RESTORE/REUSE decisions. Industry completed at 17:07:25 with snapshot age
13 minutes and the same per-resource waits. This verifies restart restoration
and reuse without reacquiring these public responses. The 17:07:46 heartbeat
reported zero errors and zero observed pre-expiry repeats; unrelated startup
requests had begun by then. Failure injection and simultaneous acquisitions
remain untested.

## Wallet and character-order implementation — 2026-11-15

Authorized after the public-cache restart check. This chunk implements the
wallet/character-order part of finding 3 and the active-order duplicate in
structure discovery from finding 2:

- `core/esi_private_cache.py` shares exact balance, transaction, journal,
  active-order and historical-order representations across consumers/threads.
  Payloads, original acquisition/absolute expiry, per-page validators,
  request parameters, schema/source and completeness persist atomically.
- Cache identity includes character, token owner (when available), application,
  authorized party, granted scopes, datasource and compatibility date. Only
  hashed context identifiers enter diagnostics and filenames. Ordinary token
  rotation leaves known-owner partitions stable. An opaque/legacy token falls
  back to its own token-specific hash partition, so rotation may cause a cache
  miss instead of unsafe sharing. JWT decoding partitions the already existing
  authenticated session; it does not replace SSO verification or implement the
  deliberately deferred local signature-validation work. Claim references:
  [current SSO documentation](https://developers.eveonline.com/docs/services/sso/)
  and [official legacy JWT example](https://docs.esi.evetech.net/docs/sso/validating_eve_jwt.html).
- Wallet restores complete payloads and their original refresh clocks before
  scheduling. Every direct fetch also uses the provider, including structure
  discovery's active orders for either selected slot. Changing authorization
  clears the prior character's wallet view and aborts a mixed-context bundle.
- Journal, active orders and order history require consistent page counts and
  Last-Modified values across every required page. Multi-page responses without
  validators are rejected. All pages must expire before the complete resource
  is acquired again. Successful individual pages have a separate atomic stage
  with `complete=false`; retry/restart can reuse them until expiry after a later
  page fails. A partial stage never replaces an accepted full snapshot or its
  successful-acquisition timestamp. Cursor transactions and suffix-page pulls
  have separate exact-resource keys and do not advance the base resource clock.
- Parsing stages the whole payload before acceptance. Valid empty lists and
  zero balances are reusable. Save failure retains the old disk file, exposes
  accepted memory data as not durable, and prevents an unqualified successful
  bundle report. A later reuse retries saving without another acquisition.
  Response-expiry fallback durations from the prior wallet implementation are
  retained if ESI metadata is unusable and are explicitly labeled in acceptance.
- Existing wallet model imports remain compatible through re-exports. The
  model/parser and fetch adapter were extracted into cohesive files, keeping
  wallet orchestration and analysis below the repository's size limit.
- Ctrl+F12 runs synthetic offline safety checks in the normal app process,
  including concurrency, failure/restart and authorization isolation. They use
  injected transport/clock/storage, never live credentials or payloads, and do
  not contribute synthetic decisions to production evidence counters.

Syntax and diff checks passed. Initial real acquisition and downstream sync
are now observed in the user's run below. All eight offline safety checks and
the Ctrl+F12 binding are verified. Real private-data reuse/restoration and
discovery sharing still require evidence.
The verification path and expected markers are in
[ESI_DIAGNOSTICS.md](ESI_DIAGNOSTICS.md).

Remaining phase-1 work includes skills/standings persistence; structure metadata
and Browse Orders/direct-public paths; history overlay persistence and honest
completion; scanner/Jita/Stock Market policies; SSO cooldowns and distribution
notices. APP_VERSION remains `3.67 live`; no version bump, commit or push was
performed in this chunk.

### First wallet-cache IDE run — 2026-11-15, 17:51–17:53 Central

Run `19360-1794786679` started from this checkout at 17:51:22, fingerprint
`9edf9dc87f43`. All five private resources initially reported no disk snapshot,
as expected for the first run of this implementation. The automatic wallet
refresh then fetched, fully accepted and atomically persisted all five at
17:52:39–46: balance, 97 transactions, 280 journal entries, 19 active orders
and 336 historical orders. Every resource reported READY with `status=durable`;
the bundle reported COMPLETE with `status=ok`.

ESI's saved expiry was 18:07:29 for transactions, 18:07:30 for journal and
18:07:34 for historical orders. Those responses had about 15 minutes remaining,
preserving ESI's existing absolute cache window rather than adding an hour.
Balance expired at 17:54:38; active orders at 18:12:43.

At 17:53:23, the normal downstream callbacks ran for trade matching, holdings,
P&L and inventory. Inventory reported four listings updated and completed its
coalesced disk save. No new buys, sales, retirements or fee backfills occurred,
so this run does not establish correctness for those unexercised accounting
cases. The latest inspected heartbeat showed zero errors/early repeats.

This establishes initial acquisition, persistence acknowledgements and a normal
inventory update. It does not yet establish reuse/restoration of the new private
snapshots; no subsequent restart or offline-check results were present at the
time of that initial inspection. Subsequent offline-check evidence follows.

### Offline safety results — 2026-11-15, 17:54 Central

Ctrl+F12 in run `19360-1794786679` completed at 17:54:03 with
`passed=8 total=8 network=none data=synthetic`. Every check passed:
reuse_and_restart, isolation, concurrent, page_failure, mixed_pages,
save_failure, wallet_adapter and authorization_race. This verifies the injected
transport/clock and temporary-file scenarios, including failed-save retention,
page-stage reuse across a simulated restart and normal token-rotation isolation.
It does not substitute for restoration using the user's real authorization
context. The latest inspected real-process heartbeat at 17:55:24 still belonged
to the original run and reported zero errors, no pending requests and zero
observed early repeats. No subsequent real restart had appeared yet.

The later log shows that original process shutting down at 18:03:22, with no
subsequent startup yet. Next live check: restart from the IDE and use
**Tracking → Trade Refresh** within the saved resource's actual unexpired
window. If the original 18:07 linking-data expiries have passed, acquire an
eligible replacement and test reuse during its new window. Expect original
payloads/times restored and REUSE without requests to still-unexpired private
routes. Balance is eligible independently. Restart alone may restore resources
without immediately running the wallet refresh because the existing automatic
schedule retains its 20-minute floor. The added null-auth compatibility
assertion also needs the next Ctrl+F12 run; the 17:54 result predates that edit.

### Checkpoint and expanded accounting runner — 2026-11-15

The preceding ESI/cache implementation and evidence were committed and pushed
as `46c76f4`, keeping `APP_VERSION` 3.67 live. The next local chunk adds three
cache-to-inventory acceptance scenarios to Ctrl+F12 (eleven checks total).
They exercise the actual FIFO/sync/serialization path for fetched, reused and
restored records, including journal delay and partial failure. Syntax and diff
checks pass; the expanded runner and real private-cache restart/reuse remain
live-unverified. See [the diagnostic handoff](ESI_DIAGNOSTICS.md) for the exact
actions and limits. No additional acquisition family is marked complete.

### Latest wallet verification and shared character implementation — 2026-11-15

The user's run `16552-1794789044` passed all eleven checks at 18:32:08 Central.
Run `25944-1794789218` restored all five real private snapshots at 18:33:47 and
reused the four unexpired resources at 18:34:40 without requests to those
routes. Balance acquired independently after expiry. This closes the ordinary
wallet restart/reuse gate and verifies the listed synthetic accounting cases;
it does not clear every authorization or delayed-accounting scenario.

The first run also observed eighteen early repeats in the public Tracking
underbid fallback. Source confirms no durable per-type fallback cache; that
previously identified direct-path work remains necessary.

The next local implementation adds `PrivateCharacterCache` over the same
transport/lock/validation/staging/atomic-save machinery. Trading and Industry
skills/standings use full raw responses and exact private context partitions.
Cache-only reads restore/rederive without acquisition. Existing one-hour
non-forced preference remains; force cannot bypass server freshness. Six new
offline checks extend Ctrl+F12 to seventeen; their runtime results and actual
character-route acquisitions/reuse/restoration remain pending. Wallet hooks
retain their default behavior and need the regression run too.

The [2026-01-27 skills caching announcement](https://developers.eveonline.com/blog/smarter-caching-when-events-drive-invalidation)
states that skills/skillqueue use event-driven invalidation and should follow
Cache-Control rather than legacy Expires. The skills-specific expiry hook
honors that distinction, accounting for Date/Age and preserving original
headers in snapshot page metadata. Other resources keep their existing expiry
interpretation. No version bump, new commit or push is part of this chunk.

### Character evidence and reputation clarification — 2026-11-15

Run `30072-1794790665` passed seventeen checks at 19:00:02 Central. Real seller
and Industry skill/standing responses were saved and restored/reused in run
`5748-1794790867` with original ages. Buyer SSO refresh instead failed with
invalid_grant; no successful buyer acquisition was implied by these results.

The user clarified that rep is needed for the seller and Industry and selected
manual refresh plus seven-day on-use refresh. Rep now stays readable from the
last good matching-context disk snapshot after its update deadline and during
failed refresh. Manual controls request eligible updates while honoring server
freshness. Normal skill freshness stays separate; old social levels may be
read solely to preserve effective rep modifiers. Raw rep continues to feed
broker-fee calculations. Two new offline scenarios bring the runner to
nineteen checks. Syntax/diff checks pass; this newer policy is not yet marked
runtime-verified. No version bump, commit or push was performed.
