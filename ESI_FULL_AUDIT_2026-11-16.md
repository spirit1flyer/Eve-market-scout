# EVE Market Scout: full ESI compliance audit

Audited 2026-11-16, app `3.67 live`, working tree at `b26f1d8`.
This is a fresh review of current source and saved runtime evidence. The
previous pending-work list did not determine the scope or findings.

## Verdict

**The current source substantially follows ESI guidance, but an unqualified
pass is not warranted.** There is one definite path that can send queued
contract requests during an active server cooldown. There is also a conditional
failure to reuse an unexpired regional book, an invalid-grant handling weakness,
and an old local binary that does not contain the current notice file.

The examined recent live run shows healthy normal operation: 2,470 HTTP
responses, zero errors, and zero observed pre-expiry repeats. That evidence does
not exercise throttling or establish correctness for every route.

No application code, credentials, caches, settings, existing work plans, or
version number were changed by this audit. Only this report was added. The app
was not launched or controlled, and no requests were sent to ESI or SSO to
manufacture coverage. Official documentation was read on the web.

## Findings

### F1 — High: queued contract requests can bypass an active cooldown

**Confirmed source defect; no occurrence established in the inspected live run.**

`contracts/contracts_esi.py:89` waits on the shared rate gate **before** acquiring
`client.semaphore`; `:91` then sends without checking again. Contract list
pagination queues all remaining pages at `:211`. The client's sessions use
`make_trace_config(pacing=False)` (`core/api.py:317`), so a trace callback does
not supply the missing dispatch check.

Concrete sequence, with the configured concurrency of 20:

1. Twenty-two page tasks pass the open rate gate; twenty send and two queue.
2. One response returns 429 with `Retry-After: 30`, arming the shared pause.
3. A different, already-in-flight response finishes and releases a slot.
4. A queued task takes that slot and sends immediately, during the cooldown.

The same placement can bypass a newly armed legacy-error pause. This concerns
new dispatches after the response, not requests already on the wire. The normal
GET implementation already rechecks inside its semaphore
(`core/esi_get.py:25`).

**Correction:** recheck the shared gate inside the semaphore immediately before
every contract send. A focused concurrency check should hold queued pages,
establish a cooldown, release a slot, and assert that no new dispatch occurs
until that cooldown ends. CCP requires clients to honor 429 backoff and react
to error limits. [Rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/)

### F2 — Medium: Tracking can discard a still-valid complete regional book

**Confirmed conditional acquisition defect; not observed in the recent log.**

`gui/tracking/gui_tracking_market.py:10` consults only
`peek_cached_orders(..., max_age_seconds=300)`. That accessor rejects a book at
300 seconds regardless of its original ESI expiry (`core/order_cache.py:201`).
On a miss, Tracking sends separately cached type-filtered sell-order requests
at `gui/tracking/gui_tracking_market.py:26`.

For a complete snapshot aged 301 seconds with a server expiry 600 seconds after
acquisition, Tracking fetches data it already has in an unexpired full book.
The expiry-aware accessor at `core/order_cache.py:98` would reuse that book.
This does not establish a violation of the separate query variant's own cache
window, or a failure with every normal five-minute response. It is unnecessary
overlapping acquisition and a failure of the app's server-expiry reuse policy.

**Correction:** consult the expiry-aware complete book first, filter locally,
then use the existing exact type cache when a usable full book is unavailable.
Verify a later-than-300-second server expiry. CCP advises reusing acquired data
until its advertised expiry. [Caching guidance](https://developers.eveonline.com/docs/services/esi/best-practices/)

### F3 — Medium: terminally invalid SSO grants remain eligible for retries

**Confirmed handling weakness; not proof of SSO abuse or a current bad grant.**

Both refresh implementations treat every non-200 response as a generic failure
and retain the same refresh token (`esi/esi_auth.py:834`,
`industry/industry_characters.py:329`). The shared gate suppresses a failing
grant for 60 seconds, then permits it again (`core/sso_cooldown.py:96`). Neither
path distinguishes terminal `invalid_grant` from a transient failure.

Consequently, a revoked or otherwise invalid grant can be retried by later
automatic or manual acquisitions indefinitely. This is substantially safer
than a tight retry loop, and 429 backoff is implemented; it still leaves
unnecessary failed traffic and does not transition the character to a clear
reauthorization state. CCP explicitly encourages clearing invalid refresh
tokens as part of SSO failure-limit handling.
[SSO failure limits and refresh rotation](https://developers.eveonline.com/blog/sso-endpoint-deprecations-2)

**Correction:** recognize allowlisted terminal OAuth errors, mark that grant as
requiring login, and suppress automatic reuse until authorization is replaced.
Retain retry behavior for transport failures, throttling, and server failures.

### F4 — Medium for distribution: the existing local bundle is outdated

**Confirmed artifact difference; no claim that this bundle is publicly shipped.**

The executable under `dist/EVE-Market-Scout/` is dated 2026-08-13. A recursive
file inventory found no `ESI_DATA_USAGE.md` in that bundle. The current source
provides the notice, exposes it through `gui/gui_main_controls.py:179`, and
includes it in `EVE-Market-Scout.spec:31`. The frozen notice loader expects that
file under the bundle root (`gui/gui_data_notice.py:14`).

The source-level packaging check (`tools/policy_batch_checks.py:268`) checks the
specification when run from the IDE; it does not rebuild or validate this old
executable. Its pass cannot establish that the existing binary contains the
new disclosure or current ESI behavior.

**Correction before distributing a binary:** produce a fresh public build,
verify the notice from that executable, and audit the actual release package.
The published agreement requires preservation of CCP proprietary notices.
[Developer License Agreement, section 7](https://developers.eveonline.com/license-agreement)

## Additional recommendations, not established compliance failures

| Item | Evidence and implication | Recommended treatment |
| --- | --- | --- |
| Bound contract 429 retries | `contracts/contracts_esi.py:88–108` retries 429 indefinitely and sleeps while holding a semaphore slot. Persistent throttling can leave a sync pending indefinitely. Each retry does wait; unbounded retries alone do not prove a limit violation. | Add a retry or elapsed-time budget and return a recoverable failure. |
| Improve preventive bucket pacing | `core/esi_rate_limit.py:124–126` uses an average-cost delay near depletion, with no dispatch reservations or bucket history. Concurrent callers can consume the remaining budget together. | Reserve estimated cost per group/identity or serialize dispatch near depletion. The current global pause is conservative across unrelated groups. |
| Use saved validators | Regional pages, history, public Industry and private snapshots retain ETags, but their transports do not send conditional headers. Contract lists do. | Add per-representation conditional revalidation after eligibility, retaining bodies and updating metadata correctly on 304. This is an efficiency improvement, not a requirement to refresh sooner. |
| Make token persistence atomic | `esi/esi_auth.py:555` and `industry/industry_characters.py:117` truncate the destination before JSON is fully written. An interrupted write can destroy usable rotating credentials. | Use atomic replacement and report persistence failure accurately. |
| Coordinate Industry login with refresh | `industry/industry_characters.py:158` updates token fields without the per-character refresh lock used at `:312`. A login and an expired-token refresh can overwrite each other's credentials. | Use the same character lock and check that the record is still current before accepting results. |
| Sanitize OAuth error bodies | Authentication logs/UI include arbitrary non-200 body snippets (`esi/esi_auth.py:748`, `:788`, `:835`; Industry equivalents). No actual token leak was established. | Log HTTP status and allowlisted OAuth error fields; avoid arbitrary response bodies. |
| Avoid idle contract list acquisition | The scheduler starts during tab construction (`gui/gui_contracts.py:141`) and refreshes all enabled NPC hub lists after 180 seconds and hourly, including regions with no watchlist (`contracts/contracts_scheduler.py:152–168`). | Consider demand-based list acquisition. Item crawls are already restricted to selected scopes. Cache-compliant prefetch alone is not established abuse. |
| Complete the expected diagnostic inventory | `core/esi_diagnostics.py:22` lists 28 expected route families but omits character structure search, implemented at `core/esi_route_cache.py:90`. Actual search traffic is still discovered dynamically. | Include the 29th family so an unused search route explicitly appears as `NOT_EXERCISED`. |

Conditional requests reduce bandwidth and token consumption. CCP also advises
slowing near bucket depletion and requesting data the app uses.
[Best practices](https://developers.eveonline.com/docs/services/esi/best-practices/),
[rate-limit model](https://developers.eveonline.com/docs/services/esi/rate-limiting/),
[market-order rollout](https://developers.eveonline.com/blog/market-orders-rate-limit-rolls-out-on-february-24-2026)

## Coverage of current acquisition paths

Inventory covered 29 ESI/SSO route families, including exact query variants and
their production callers. Static inspection included startup, scanner, Stock
Market, Tracking, Watchlist, Industry, structures, Contracts, and lookup flows.

| Resources | Current acquisition protection | Audit result |
| --- | --- | --- |
| Regional market orders; direct scans; Watchlist Max Buy | Complete books, shared durable pages, original expiry, cross-loop acquisition guards, page-count/Last-Modified validation, duplicate order rejection | No ordinary force bypass found; F2 concerns Tracking's alternate consumer path |
| Type-filtered regional sell orders | Exact region/type/order-side keys, shared route cache, durable successful pages, snapshot validation | Its own expiry is protected; see F2 for full-book reuse |
| Structure market orders | Character/owner/client/scopes isolation; shared provider for scanner, Browse and underbid; durable pages; context rechecks | No unscoped reuse or regional fallback found |
| Regional/type market history | Shared direct/bulk/recent-overlay provider; original expiry; persistent rows; per-key acquisition guard | No pre-expiry force bypass found; failures do not claim durable success |
| Adjusted prices; Industry system indices | Full shared public responses, six-hour consumer preference or later ESI expiry, atomic persistence | No duplicate provider bypass found |
| Wallet balance; transactions; journal; active orders; historical orders | Separate resource/parameter clocks; authorization partition; durable raw snapshots and partial pages | No bundle-wide expiry reset found; cursor results stay separate |
| Character skills; standings | Shared raw private responses; one-hour skills preference; seven-day ordinary reputation preference; manual requests still respect server eligibility | Cache-aware source paths present; skills honors Cache-Control |
| Character blueprints | Authorization-scoped full snapshots/pages; page validation; one-hour preference plus later expiry; transactional DB application | Repeated manual pulls do not bypass provider expiry |
| Structure metadata; character structure search | Private context and exact query keys; shared durable route cache | No search discovery crawler found; search is user-driven |
| Stations; systems; constellations; regions/list; types; stargates | Shared exact public route snapshots and negative metadata cache; SDE-first type lookup | No raw metadata cache bypass found |
| System routes | Shared exact origin/destination snapshot; durable jump-count consumer cache | No repeated uncached direct route loop found |
| Public names and IDs | Shared batched lookup, persisted positives/negatives, failed-ID isolation, short transient suppression | No per-caller raw lookup bypass found |
| Public contract lists and items | Exact-page persistence/single-flight; expiry reuse; page validation; conditional lists; immutable accepted items | F1 applies to network dispatch; bounded retry recommendation also applies |
| SSO token and verify | Shared persisted cooldown, numeric/date Retry-After, failed-grant isolation, refresh locks | F3 and token-handling recommendations remain |

The production network-call search found no ESI write operations, cache-busting
query parameters, disabled TLS verification, or credential transmission to a
developer server. Public portraits, EVE Ref archives and Fuzzwork SDE downloads
are separate services and were distinguished from ESI calls. Optional local
consumers were checked for transport bypasses without recording private
production details. Offline diagnostic fixtures are not production traffic.

## Authentication, consent and agreement checks

- PKCE uses a cryptographically random verifier and S256 challenge. Each login
  owns random state, verifier and target; callbacks reject missing, duplicate,
  mismatched, expired, consumed and superseded state before code exchange.
  The listener binds to localhost, checks `/callback`, suppresses request logs,
  escapes its response text and returns `Cache-Control: no-store`.
- Both flows use the official v2 SSO endpoints and form-encoded token requests.
  No client secret is used. Server-side `/v2/oauth/verify` remains supported;
  replacing it with offline JWT validation is not required to close this audit.
  [SSO guidance](https://developers.eveonline.com/docs/services/sso/),
  [2026 verification-endpoint guidance](https://developers.eveonline.com/blog/spring-cleaning-legacy-routes-removed-24-march-2026)
- The seven Trading scopes and four Industry scopes form eight unique read-only
  scopes. They cover the private routes used. Industry standings is currently
  display/future-use data; narrower profiles are optional minimization.
- Tokens reside in the local user data directory. Unencrypted storage is
  disclosed. No standalone encryption requirement was found in the examined
  ESI/SSO guidance. Actual file ACLs, account backups and external sync were not
  certified or inspected. No credential/token file contents were read.
- The notice describes third-party status, calculation/data limitations,
  official SSO, read permissions, local data/tokens, lack of separate token
  encryption, removal and external revocation, and contains the prescribed
  proprietary notice. Current source packaging includes it; see F4.
- The app's observed purpose is EVE market/industry analysis. No paid access,
  advertising, gambling, game-client automation or undisclosed telemetry path
  was found in the inspected source. The public agreement covers permitted
  purpose, consent, intellectual-property notices and developer contact duties.
  [Developer License Agreement](https://developers.eveonline.com/license-agreement)
- Earlier project evidence records a registered application, matching callback
  and client ID, and user-confirmed developer-account/contact status. Those
  external facts were not independently reopened in this audit. Current portal
  settings, monitored mail and any distribution outside this checkout remain
  outside what source inspection can prove.
- ESI headers identify the application/version and contact, and pin
  `X-Compatibility-Date: 2026-11-13`. The date is not in the future. Existing
  `/latest` URLs remain supported under the published migration guidance;
  migration remains optional for these existing routes.
  [Versioning guidance](https://developers.eveonline.com/blog/changing-versions-v42-was-getting-out-of-hand)
- Skills explicitly uses Cache-Control rather than relying on the legacy
  Expires value, consistent with the event-driven cache announcement.
  [Skills caching](https://developers.eveonline.com/blog/smarter-caching-when-events-drive-invalidation)

## Runtime evidence and limits

The existing `%APPDATA%/EVEMarketScout/eve_scout.log` was inspected through
selected diagnostic lines, not private payloads or credentials.

| Evidence | What it establishes |
| --- | --- |
| IDE run `7296-1794837922`, started 2026-11-16 08:05:24 Central, revision `esi-evidence-2-policy-batch`, diagnostic fingerprint `df45afeff0d3` | Correct checkout and app version were identified |
| Its 08:45:27 report: 2,470 responses, 0 errors, 0 pending, 0 early repeats, 12 observed route families | Healthy observed transport during that run, not all-route coverage |
| Its 08:31:08 check result: `passed=52 total=52 network=none data=synthetic` | Existing offline cache/policy checks passed inside the IDE process; no fresh test run was performed for this audit |
| Restart run `21248-1794840356`, started 08:45:58; 08:46:16 Holdings result: 10 accepted, 10 durable, 0 fetched, 10 reused | Real history reuse across restart |
| Restart's 08:52:59 heartbeat: 432 responses, 0 errors, 0 pending, 0 early repeats | Further healthy normal traffic with limited route coverage |

The 52 checks do not cover the queued-contract dispatch race described in F1
or terminal-grant retirement in F3. Their source distribution check inspects
the build declaration in an IDE run, not the existing frozen artifact.

Unexercised HTTP routes in the recent run include structure books/metadata,
skills/standings, contract items, routes, several public metadata resources,
ID lookup and SSO verify. Some resources were supplied from saved caches;
absence of a network call is not a failure. It also cannot establish that a
new failure or authorization case was exercised.

The early-repeat observer hashes the access token and exact URL, is bounded,
and resets with the process. It can miss overlapping query variants, token
rotation, concurrent first requests and cross-process/restart duplicates.
The rate gate and acquisition locks coordinate within one process; multiple
simultaneously running app copies were not validated.

The route inventory comes from application call sites. The web reader could
not parse the live OpenAPI endpoint's content type, so this audit does not
claim an independent field-by-field comparison with every live response schema
or an enumeration of every current route-group budget. The limiter reads
response headers rather than hard-coding those budgets.

## Recommended disposition

1. Correct F1 before relying on the app under throttling or heavy contract work.
2. Close F2's later-expiry case and F3's terminal-grant lifecycle.
3. Treat F4 as a release condition for binary distribution.
4. Address the recommendations according to operational need; they are not a
   claim that the entire app presently violates ESI rules.

An engineering sign-off should combine those corrections with focused
failure-case evidence and a verified actual release artifact. Normal live
success and a cache-test counter alone cannot provide that sign-off.

## F1 follow-up — 2026-11-16

Caleb authorized F1 implementation separately from F2. The contract helper now
rechecks the existing shared cooldown after taking its semaphore slot and
immediately before `session.request`, including retry iterations. No new
rate-limit machinery or retry-budget changes were introduced. The original
audit observations above are preserved.

Two synthetic checks in `tools/esi_audit_fix_checks.py` exercise the real
helper: `contract_queued_cooldown` holds a slot, closes a fixture-local gate
after the outer check, then verifies dispatch waits until release;
`contract_cancelled_cooldown` verifies cancellation propagates without a send
and another task can acquire the released slot. Gate/session state is isolated
from production and fixture tasks are cleaned up. Registered suite size is
now 54; F2 and the final two checks remain planned.

**Evidence:** changed Python files pass syntax compilation; diff whitespace
checks pass. Pre-fix runtime evidence was unavailable because no user-run IDE
check was performed during implementation. The new checks have not yet been
runtime-verified. F1 is source-corrected, with acceptance pending the correct
checkout's Ctrl+F12 run (both named checks PASS and `passed=54 total=54
network=none data=synthetic`) and normal Contracts operation/log review.
The earlier 52/52 pass cannot establish this fix. No app was launched and no
live cooldown was induced. See `ESI_DIAGNOSTICS.md` for the exact handoff.

Caleb subsequently approved the version bump to `3.68 live` and commit/push
on 2026-11-16. The original audit remains against `3.67 live`; publication
does not close the pending F1 runtime checks or implement F2.
