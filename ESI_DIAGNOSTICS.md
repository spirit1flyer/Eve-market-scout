# ESI runtime diagnostics

This instrumentation identifies the running checkout and records evidence of
actual activity. It does not implement the remaining fixes from the compliance
audit and does not label an untested route compliant.

Latest checkpoint: run `7296-1794837922` passed **52/52** at **08:31:08
Central, 2026-11-16**, revision `esi-evidence-2-policy-batch`, code
`df45afeff0d3`. Real restart `21248-1794840356` restored 27 recent-history
snapshots / 4,498 rows at 08:45:58, then reused ten Holdings histories without
acquisition. Older failed-run and pending instructions below are historical.
Caleb has set the remaining specific fix/check items aside; the list and
current work order are in
[ESI_ACQUISITION_GOALS.md](ESI_ACQUISITION_GOALS.md).

F1/F2 follow-up: the contract cooldown recheck, Tracking expiry-aware reuse
and four synthetic checks are implemented. The runner now contains **56**
checks; the latest observed pass remains **52/52**. F1/F2 IDE verification is
pending; see the handoffs below. Caleb approved `3.69 live` and commit/push
on 2026-11-16. The saved 52-check pass used `3.67 live`.

## Reading the output

Start `main.py` normally from the IDE. Look for `[ESI-Evidence] event=START`
(with a `run=...` field before `event`). It includes the source entry path,
process/run ID, app version, diagnostic revision, code fingerprint, log path,
and heartbeat interval. A stale executable or another checkout will have a
different entry path or will lack this marker.

- `REQUEST`: an outgoing HTTP attempt began. Only the first attempt per route
  family is printed immediately; all attempts are counted.
- `RESPONSE`: HTTP headers arrived, or a connection/timeout failure occurred.
  The first occurrence of each status is printed; subsequent matching traffic
  is summarized rather than printing hundreds of market pages.
- `DECISION`: a provider reused data, missed its cache, or finished parsing.
  Existing `[ESI-Timing]` decisions are included in the report too.
  `REUSE` includes readable `next_refresh_in` (for example `5h_42m_03s`)
  alongside numeric `next_in`. This is the wait until acquisition is eligible,
  not a promise of an automatic request. Unknown expiry is shown as `unknown`.
- `HEARTBEAT`: every 30 seconds, report HTTP/error totals, requests waiting for
  headers, the oldest wait, and early-repeat totals. A heartbeat with zero
  requests is evidence the observer is running, not evidence of an ESI fetch.
- `cache=BEFORE_PREVIOUS_EXPIRY`: this same GET representation was requested
  again before its previously observed `Expires`. This is an investigation
  signal; retries after an inconsistent snapshot can also cause it. It does
  not block, alter, or delay acquisition.

Click **ESI Report** in the top utility row, or press **F12**, to print a full report to the IDE
terminal and `eve_scout.log`. This action makes no network requests. Routes
without observed traffic remain `NOT_EXERCISED`. Observed routes show request
counts, HTTP status counts, transport errors, and early repeats. Consumer
decisions show cache reuse and completion separately from HTTP responses.
The full report also appears automatically after one minute, then every five
minutes, with compact heartbeats in between.

HTTP 200 alone does not prove that pagination was consistent or that data was
parsed, accepted, or persisted. Check the consumer's `ACCEPT`, `COMPLETE`,
`REJECT`, and failure output alongside the transport report. A route satisfied
from disk can remain `NOT_EXERCISED` for HTTP while its consumer reports reuse.

## Coverage and limits

All active ESI aiohttp sessions have observation traces, including the separate
nearest-station route session. Synchronous ESI calls pass through the existing
shared rate gate and the observer. Trading and Industry SSO token/verify calls
are observed too. Browser authorization retains its existing `[AuthState]`
diagnostics. Public image, Fuzzwork, and EVE Ref downloads are outside ESI.

Requests retain their original arguments, error handling, pacing, and cache
behavior. No extra requests are made to exercise routes. Metadata is confined
to this process and the existing local terminal/log destination. The observer
never records request/response bodies, tokens, authorization codes, headers
containing credentials, query text, or concrete character IDs. Private request
identity is hashed in memory to distinguish pages and authorized resources.

Early-repeat tracking is session-only and bounded to 20,000 expiry records;
unknown expiry, evicted records, concurrent initial requests, a restart, and
access-token rotation can leave an early repeat undetected. The report is
runtime evidence, not a complete compliance test.

## Verification handoff — 2026-11-15

Syntax and diff checks passed for the earlier instrumentation edits. The
existing log now verifies START identification, actual HTTP observation,
30-second heartbeats, and cache decisions from this checkout. Run
`7944-1794774835` reported 218 responses, three errors, and zero pending headers
at the latest inspected heartbeat. A preceding run detected two public-resource
requests before their observed expiry. See
[the reassessment](ESI_COMPLIANCE_REVIEW.md) for evidence and limitations.

The recorded process loaded code fingerprint `7658427254de` before the newer
automatic full-report schedule and F12 binding were added. Those newer features
remain live-unverified; the old process's heartbeats do not establish that the
new code ran. On the user's next normal IDE run, check the new fingerprint and
the full report after one minute, or use ESI Report/F12. Repeating a report
while idle must not initiate a fetch. Do not launch the app on the user's
behalf to obtain this evidence.

The instrumentation/cache checkpoint was committed and pushed as `46c76f4`
on 2026-11-15; APP_VERSION remains 3.67 live. The compliance
audit's remaining acquisition, persistence, and distribution work is separate.
The [saved goals](ESI_ACQUISITION_GOALS.md) define the intended consumer policies;
this instrumentation does not implement them.

### Subsequent IDE-run verification — 2026-11-15

User-started run `2920-1794783205`, fingerprint `b0289dba4ca8`, verifies the
automatic full-report schedule and readable REUSE waits in the current code.
At 17:03:42 Central, adjusted prices and industry systems each reported
`next_refresh_in=5h_50m_45s`. Reports retained one HTTP request per public route
after repeated refreshes; both complete payloads were persisted with original
times and validators. See the compliance review's live-evidence section.
The subsequent run `30952-1794783973` restored both complete public snapshots
at 17:07:09 Central, preserving ages of 761/762 seconds, then logged REUSE with
waits of `5h_47m_19s` / `5h_47m_18s`. Its automatic report recorded those
decisions with zero HTTP requests at that point. Restart restoration is now
verified for these two resources. F12 still requires direct runtime evidence.

## Wallet/order cache verification — partial live verification

After restarting from the IDE, **Ctrl+F12** runs nineteen offline safety checks in
a background worker and prints `[ESI-CacheCheck]` results. Expect
`passed=8 total=8 network=none data=synthetic`. These cover independent expiry,
restart payload/time preservation, character/owner/client/scope separation,
ordinary token rotation, concurrent acquisitions, failed/mixed pagination,
save failure/retry, cursor isolation, empty responses and authorization changes.
They use temporary files and an injected synthetic transport; no live tokens
or caches are read, no ESI requests are made, and synthetic decisions do not
enter the normal ESI evidence report. The user's Ctrl+F12 invocation passed
all eight checks at 17:54:03 Central on 2026-11-15 in run `19360-1794786679`.
The later plan review restored compatibility with unauthenticated
`ESIWallet(None)` inventory replay and added an assertion to the wallet_adapter
scenario. That assertion needs the next Ctrl+F12 run; the earlier pass does
not verify code added afterward.

For real traffic, use **Tracking → Trade Refresh**. The first acquisition after
this implementation should record FETCH → ACCEPT → PERSIST for each of
`balance`, `transactions`, `journal`, `orders` and `order_history`. Existing
analytics files cannot substitute for these exact-resource snapshots. Refresh
again before each resource's expiry: expect REUSE with readable
`next_refresh_in` and READY with `status=durable`. Balance can become due before
the other resources. Restart and refresh promptly: expect RESTORE with the
original ages, then REUSE for resources still unexpired. An expired resource
may fetch independently. Use ESI Report to compare route counts.

Private decision lines include a short hashed `context`, never character IDs,
tokens or private payloads. Counts are separated by that context in reports.
`PAGE_RESTORE` / `PAGE_REUSE` describe successful individual response pages,
including retries following a failed later page; they do not establish a
complete accepted snapshot. Only full validation emits ACCEPT. Failed durable
save emits SAVE_FAILED, retains the old complete disk file, and marks the
wallet bundle as a partial failure; reuse can retry saving without another
request. If ESI expiry metadata is absent/unusable, the previous resource's
fallback duration is preserved and acceptance reports `status=fallback_expiry`.

**Find Structures → Discover** now shares the selected character's active-order
snapshot with Tracking when their authorization context matches. Structure
metadata still uses its existing separate acquisition path; it remains open
work. Discovery sharing is still live-unverified. The Ctrl+F12 binding and all
eight offline checks are now observed in the user's IDE-run process.

### First live private acquisition — 2026-11-15

User-started run `19360-1794786679` (fingerprint `9edf9dc87f43`) verifies the
new code loaded. At 17:52:39–46 Central all five initially missing private
resources completed ACCEPT → PERSIST → READY with durable status; the wallet
bundle completed successfully. Counts were 97 transactions, 280 journal
entries, 19 active orders and 336 historical orders. At 17:53:23 the normal
inventory sync updated four listings and saved; other accounting cases had no
new events and remain unverified. The three linking resources had saved
expiries around 18:07 Central. Ctrl+F12 subsequently passed all eight checks at
17:54:03. The original process shut down at 18:03:22; no subsequent startup
was present at this review. Real private restart/reuse still requires evidence.
Test **Tracking → Trade Refresh** after a restart while the relevant saved
resource remains unexpired. If that window has passed, acquire an eligible new
snapshot, then restart and test within its actual saved window. The automatic
scheduler may otherwise wait until its existing 20-minute floor.

Cache acceptance is only one verification layer. The
[updated plan](ESI_ACQUISITION_GOALS.md) also requires actual inventory sync
fixtures to reconcile the same buys, listings, fills, fees and profit from
fetched, reused and disk-restored transactions/journal/order history. Repeat
syncs and restart must not double-count; partial or late linking data needs
coverage. The current synthetic cache checks and four observed listing updates
do not establish those accounting results.

### Expanded accounting checks — prepared 2026-11-15

The runner now contains the original eight cache checks plus
`inventory_cache_lifecycle`, `inventory_delayed_journal` and
`inventory_partial_refresh`. No result from this expanded runner has been
observed yet. Restart `main.py` from the IDE and press **Ctrl+F12**; require
each scenario to report PASS and the summary to report `passed=11 total=11
network=none data=synthetic`. The eight-check result above predates this work.

The new scenarios use actual wallet parsing, inventory sync, FIFO, fee/tax
backfill and inventory serialization in temporary directories. They compare
freshly fetched, reused and cache-restored views; repeat sync and reload the
inventory; and check partial fills, fulfilled/cancelled listings, cost basis,
profit and failure recovery. Inventory storage is injected per instance and
the replay defers writes to explicit flush, avoiding global path changes or
Tk callbacks from the test worker. Normal inventory storage remains the default.
No synthetic acquisition events enter the production ESI evidence counters.

These fixtures cover delayed journal records, not every possible delayed buy,
sale or history record. They also do not replace the real authorization-context
restart/reuse check through **Tracking → Trade Refresh**. If saved linking data
has expired, acquire an eligible snapshot first, then restart and refresh
within its saved window. Confirm RESTORE followed by REUSE with original times.

### Wallet/accounting checkpoint passed — 2026-11-15, 18:32–18:35 Central

Run `16552-1794789044` passed all eleven existing checks at 18:32:08, including
the unauthenticated-wallet compatibility assertion and all three accounting
scenarios. Eligible real wallet replacements were fully acquired and saved
around 18:32. A same-process manual refresh reused them.

After the 18:33:34 shutdown, run `25944-1794789218` restored all five snapshots
at 18:33:47, preserving original acquisition times and absolute expiry. Trade
Refresh at 18:34:40 reused transactions (age 155s), journal (153s), active orders
(151s) and order history (150s), with no HTTP requests to those four routes.
Balance was independently expired and fetched again. The real scanner
inventory sync updated four listings and recorded no new buys, sales or fees
on repeated syncs; other live accounting cases remain unexercised.

The earlier process also observed eighteen pre-expiry public market-order
requests from `underbid_fallback` after its full regional-cache miss. This is
the known uncached per-type fallback, not a private-wallet repeat. Restart
resets the observer's repeat counter, so the newer process's zero does not
clear this gap. No exceptions or HTTP failures were found in these runs.

### Shared skills/standings — prepared, not runtime-verified

The runner now adds six scenarios: `character_sharing_restart`,
`character_context_isolation`, `character_expiry_force`,
`character_validation_failures`, `character_concurrency`, and
`character_local_views`. Restart the IDE app and press **Ctrl+F12**; require
`passed=17 total=17 network=none data=synthetic`. The earlier eleven-check pass
does not verify these additions or the new provider hooks.

For real routes, use **Tracking → Refresh Skills**, then Industry's
**Characters → Skills / Standings** dialogs. Look for skills/standings
FETCH → ACCEPT → PERSIST → COMPLETE (`status=durable`) or REUSE with a readable
wait. Both consumers now use the same raw-resource names with different
triggers. Matching character/owner/application/scopes share snapshots; different
scope sets intentionally do not. Being the same character alone is insufficient.

Repeat and restart within the usable window. Getters may RESTORE from disk
without acquiring; explicit refresh should REUSE the original response/time
when still eligible for reuse. Cache-only getters and derived recalculation
must not issue ESI requests. Compare original times and route counts across
runs, not just the current process's repeat counter.

The existing one-hour consumer preference is retained, bounded by any later
server freshness. Force bypasses only that preference, never server freshness.
Skills use Cache-Control when supplied; Date/Age prevent restarting max-age.
The legacy Expires value is retained in page metadata. This follows the
[event-based skills caching announcement](https://developers.eveonline.com/blog/smarter-caching-when-events-drive-invalidation).
Standings retain normal expiry handling. Invalid/full-page failures preserve
prior accepted data; failed disk saves report MEMORY_ONLY and retry persistence
on reuse without acquiring again. The new scenarios cover these paths using
injected data only. The user's normal app run remains the runtime gate.

### Shared character checkpoint passed — 2026-11-15, 18:59–19:02 Central

Run `30072-1794790665` passed all seventeen checks at 19:00:02. Seller standings
saved at 18:59:46 and Industry standings at 19:00:32, both with durable status.
After restart, run `5748-1794790867` restored seller standings at 19:01:19,
reused them at 19:01:42, and restored/reused Industry standings at 19:01:56.
Both skill responses also persisted/restored/reused with original ages.
This verifies those real contexts separately; matching-context sharing is
covered by the synthetic scenarios, not by two different real characters.

Buyer acquisition failed twice at SSO refresh with HTTP 400 / invalid_grant.
Buyer skills failed before its standings acquisition; no buyer standings
snapshot was created. This is distinct from seller/Industry disk persistence.
The buyer needs reauthentication; no credentials were inspected or changed.

### Reputation retention — implemented, runtime check pending

The user chose saved rep until explicitly refreshed plus refresh on use at
seven days. Standings/rep now have a seven-day normal acquisition preference;
cache-only reads keep using the last good matching-context snapshot after
that window. Failure emits FETCH_FAILED then STALE_REUSE, preserving the old
snapshot and original age, without a successful-acquisition COMPLETE. Saved
social skill levels remain available for effective rep calculations. Skills'
normal read/acquisition freshness is otherwise separate.

Restart the IDE app and press **Ctrl+F12**. The runner now adds
`reputation_retention` and `reputation_refresh_policy`; require
`passed=19 total=19 network=none data=synthetic`. These cover old-rep restoration,
social modifiers, context isolation, the seven-day boundary, explicit eligible
updates and failure retention. The seventeen-check pass predates these changes.

For manual real behavior use **Tracking → Refresh Skills**, or Industry's
**Characters → Standings → Refresh rep**. Opening the Industry dialog normally
reuses rep younger than seven days. Explicit buttons bypass the seven-day
preference but must still REUSE before ESI expiry. Reprocessing's explicit ESI
button follows the same rule for seller rep. Inspect original ages and readable
waits; never erase live snapshots or manipulate dates to force the seven-day
case, which the isolated fixtures cover.


### Private structures and direct routes — local implementation, runtime pending

2026-11-15: the user skipped the first pending check and authorized this chunk.
Syntax compilation and diff checks pass; no live-app or synthetic runtime pass
is claimed. The prior reputation checks remain unverified as well.

`core/esi_route_cache.py` reuses the established private transport with narrow
URL/query/pagination/validation hooks. Structure books and metadata are keyed
by character/owner/client/scopes and exact structure ID. Browse and scanner
share the book; discovery and Industry's roster adapter share matching-context
metadata. Legacy unscoped structure files are ignored. Public per-type sell
orders and station/system/constellation metadata have separate public keys.

Full snapshots and successful individual pages persist atomically. A later-page
404, changed count, missing multi-page validator, malformed row or mixed
Last-Modified rejects the book and retains the last good accepted snapshot.
Retry/restart reuses successful unexpired pages. Full reacquisition waits for
all page expiries; manual refresh cannot bypass them. Save failure is reported
as MEMORY_ONLY and reuse retries the save without fetching again.

Structure history hooks use original acquisition timestamps. Reusing the same
snapshot cannot add another observation or count its inferred sales twice.
Browse displays the book's acquisition age and discards results for a structure
or authorization context changed while the worker was running.

When verification resumes, restart the normal IDE app and press **Ctrl+F12**.
Require `passed=29 total=29 network=none data=synthetic`. New scenarios:
`structure_sharing_restart`, `structure_authorization`, `structure_page_restart`,
`structure_validation`, `structure_save_recovery`, `structure_concurrency`,
`underbid_route_cache`, `underbid_structure_routing`, `public_metadata_cache`,
`structure_observation_reuse`.
They inject transport, clock and identities and use temporary files only.

For real evidence, open a structure in **Browse Orders**, repeat Refresh and
scan the same structure in the matching seller context. Repeat/restart before
expiry must show RESTORE/REUSE with original age/expiry and no repeated HTTP
requests. Discovery and Industry registration share metadata only if their
full authorization contexts match. A different character/owner/scope set or
logout must not receive the old context's private snapshot. Check the normal
Trading refresh twice with active sell types and no full regional snapshot:
`type_orders` should show REUSE, preserving its saved expiry across restart.
Do not delete live snapshots or edit timestamps to force these conditions.
Full regional order hardening, blueprints and other small-route work remain open.

### Real structure evidence and Tracking correction — 2026-11-16

IDE run `28296-1794823224` (04:00:26–04:13:58 Central) accepted/persisted 20
structure orders at 04:05:41. It reused the original snapshot twice (ages 4s
and 16s) and fetched again at 04:10:47 after its 04:10:39 expiry, accepting and
persisting at 04:10:52. The run observed zero early repeats. It did not run
Ctrl+F12 or restore a newly acquired structure book after another restart.
Structure metadata remained NOT_EXERCISED.

Tracking's refresh on the same structure incorrectly used public regional
orders: 17 type-filtered requests at 04:11:36–04:12:02 returned zero orders.
Later refreshes at 04:13:50 and 04:13:56 reused those empty responses, proving
same-process public-cache reuse but not correct structure underbid behavior.

The user authorized correcting this gap. `gui_tracking_market.py` now selects
the private structure snapshot before any public-cache/route lookup, using
the wallet's seller authorization and `trigger=underbid_structure`. Private
acquisition failures propagate to the sync manager, which clears the market
view and logs FAILED; there is no public fallback. Changing hubs clears the
old pending book, and results from a changed hub/wallet are not applied.
NPC hubs retain regional reuse and cached per-type fallback.

The added `underbid_structure_routing` scenario exercises Browse-to-Tracking
sharing/restart, auth isolation, missing auth, later-page failure without a
public fallback, and both NPC paths. The runner now has 29 checks. Syntax
checks passed; this correction and the expanded suite remain runtime-pending.

Real recheck: restart normally, select the same private structure, open Browse
Orders, then refresh Tracking twice before expiry. Require
`resource=structure_orders decision=REUSE trigger=underbid_structure`
and `[TradeSync] ... source=shared_structure_snapshot ... orders=...` matching
the full book (not a guaranteed fixed count of 20). There must be no
`underbid_fallback` public type-order requests for that structure. Press
Ctrl+F12 for `passed=29 total=29 network=none data=synthetic`, then F12.

### Routing correction and all 29 checks passed — 2026-11-16

IDE run `10604-1794824623` (04:23:45–04:24:34 Central) passed all 29 isolated
checks at 04:24:27, including both reputation-policy checks and
`underbid_structure_routing`.

At 04:24:00 it restored the real 20-order structure book with its original
788-second age and 04:15:50 expiry. It was already expired, so the provider
correctly fetched, accepted and atomically persisted a replacement at 04:24:03,
preserving the server's 04:27:20 expiry (196 seconds remaining).

Tracking at 04:24:18 and 04:24:21 reused that same book at ages 15s and 18s,
both reporting `source=shared_structure_snapshot ... orders=20`. The manual
04:24:30 report counted two `underbid_structure` REUSE decisions, one actual
structure-order request, zero errors and zero early repeats. No public
underbid fallback requests occurred. Repeated inventory sync recorded no new
purchases, sales or fees in this run.

This clears the observed Tracking routing bug and the expanded offline suite.
It does not clear every live case: structure metadata remained NOT_EXERCISED;
different live authorization contexts, manual rep controls and unexpired
private-book cross-restart reuse are still unobserved. The latter cases are
covered by isolated scenarios, which remain distinct from real-route evidence.

### Combined regional / blueprint / smaller-route batch — prepared 2026-11-16

Implemented together at the user's direction. Syntax compilation and diff
checks pass. The earlier 29-check runtime pass does not verify this batch.
Ctrl+F12 now requires `passed=40 total=40 network=none data=synthetic`.

The eleven additions cover regional page recovery across retry/restart, page
counts/validators/duplicate rows, atomic regional writes and force-before-expiry,
blueprint restore/empty data, rollback and save retry, blueprint authorization
races/concurrent pulls, mixed name/ID failures and negative restoration,
overlapping lookup consumers, contract sharing across event loops and restart,
304/failure handling, and public metadata/route/private-search reuse.
All transports, identities, clocks and storage are injected/temporary. The
blueprint rollback scenario deliberately triggers a synthetic SQLite error;
its individual PASS/FAIL result determines whether recovery worked.

Regional pages keep original acquisition/expiry and persist before a later
page can fail. The 20-to-1 staged timeout retry path remains in use. Page 1 is
reused before expiry; a missing multi-page validator cannot establish a complete
book. Full snapshots preserve original times and per-page metadata, atomically
replace disk files, and report SAVE_FAILED separately from memory acceptance.
Manual force cannot bypass an unexpired snapshot.

Blueprints use the same private transport with strict full-row validation and
the existing one-hour consumer preference/0.3-second page pace. Database metadata
now includes authorization context and original acquisition. Pulls serialize by
database/character; failed DB transactions retain the previous rows and allow
application retry from the saved raw snapshot. Empty is successful; write failure
is not zero-success. Private pages never share across unrelated authorizations.

Contract pages share exact public request keys and conditional responses across
callers/loops. A transactional reply database avoids thousands of tiny cache
files; obsolete raw replies are pruned after seven days past expiry. A failed
refresh keeps the prior successful payload/time and returns failure during its
cooldown. A bodyless 304 is never reused for a different conditional validator.
Name/ID lookups share persisted per-ID outcomes, isolate bad batch members and
retain known values/times on failure. Region lists/details, type/stargate data,
routes and private structure-name search now reuse exact snapshots; public 404s
have isolated negative entries. Existing derived permanent lookup caches remain
in place; opening them need not trigger new acquisitions.

One combined live walkthrough after the offline pass:

1. From the IDE, restart `main.py`, press **Ctrl+F12**, and require **40/40**.
2. Scan an NPC hub, then request it again before expiry. Expect shared REUSE or
   `[RegionalPages] decision=REUSE`, original ages, and no repeated order-page
   requests. Failed-page/write behavior is covered by the isolated scenarios;
   do not damage live data to induce it.
3. **Industry → Characters → Pull blueprints** for an authenticated character,
   then repeat. Require durable completion or a readable cached wait; confirm
   the owned-blueprint list remains intact. Empty can legitimately mean zero.
4. Repeat one normal **Contracts → Search** for the same item/scope. Use the
   existing station-scoped workflow; no full-region item crawl is required.
   Names/IDs and extra metadata can reuse existing derived caches, so absent
   traffic is recorded as unexercised rather than a pass.
5. Restart once and repeat the same scan/pull/search while snapshots are usable,
   then press **F12**. Compare saved ages/expiry across runs in `eve_scout.log`.

This is one batch test gate. History-overlay persistence, scanner/Jita/Stock
Market consumer ages and SSO/notices are the next implementation batch; they
were not silently changed by these acquisition fixes. The regional server-expiry
protection closes a force bypass, not a new consumer cadence.

### Combined batch results and correction — 2026-11-16

Reviewed both completed IDE runs from the current checkout:
`23264-1794828329` (05:25–05:53 Central) and `6424-1794830000`
(05:53–05:56 Central). Ctrl+F12 logged **39/40** at 05:26:29 and 05:54:46.
The same scenario, `metadata_route_sharing`, failed in both. The public-404
branch referenced `_expires` without importing it, preventing negative-entry
persistence. The fixture's broad rejection helper hid the resulting NameError.
The import is corrected, and the scenario now requires an HTTP 404 first,
then `cached_public_not_found` in memory and after restart. Unexpected errors
surface directly. Syntax compilation and diff checks pass; runtime is pending.

Retain this live evidence from the completed walkthrough:

- Regional Amarr books: 192 pages accepted, 191,636 orders atomically saved
  at 05:26:58 and reused at 05:28:12. A later eligible refresh saved 191,659
  orders. Restart loaded that book at 05:53:26; it was expired by the next
  scan, so it correctly acquired 191,665 orders, saved them at 05:55:14 and
  reused them at 05:55:23. Unexpired regional cross-restart reuse remains
  covered by synthetic scenarios rather than this live restart.
- Blueprints: 57 accepted and persisted at 05:28:32, with durable DB completion
  and an immediate cached repeat. After restart, 05:56:08 restored the same
  57 with original age 1,656 seconds and original 06:28:30 Central expiry.
  Pulls at 05:56:08 and 05:56:11 both reported CACHE without reacquisition.
- Contracts: the first run fetched 31,759 of 31,826 pending item responses,
  with 2 gone and 65 bad. HTTP evidence includes 204/400/403/404 responses;
  sampled 400s reported a contract that was not an item exchange or auction.
  This is not a clean all-items pass. After restart, repeated Jita searches
  reused the saved list (06:07:35 Central expiry), retained 57 cached hits,
  and made no list request. One pending item remained bad on both attempts.
  A later eligible Amarr list refresh assembled 3,313 rows across four pages.
- Both manual reports recorded zero early repeats. The first report recorded
  35 HTTP errors during the large contract-item crawl; the second recorded
  zero HTTP errors. Session counters do not prove cross-restart compliance.

Next action is only an IDE restart and **Ctrl+F12**, requiring
`passed=40 total=40 network=none data=synthetic`. Do not repeat the whole
scan/blueprint/contracts walkthrough for the missing-import correction.

### Follow-up result and filtered-page correction — 2026-11-16

Run `2988-1794830742` from the current checkout logged **39/40** at 06:06:38
Central. `metadata_route_sharing` now passed, confirming the missing-import fix.
The different failure was `underbid_route_cache`, reporting
`PrivateSnapshotError: cached_public_not_found`. A page-2 HTTP 404 had been
persisted under the whole filtered-order query's identity. The immediate restart
retry was then blocked even though page 1 was valid and the failed page could
recover. The original missing import had masked this interaction.

`RouteSnapshotCache.get` now applies public negative entries only to
single-response metadata/routes. Paginated order books use the shared staged
page transport without whole-book negative poisoning; old erroneous order
negative files are ignored. Private authorization behavior is unchanged.
The underbid scenario now tests both in-memory and restart recovery, requires
the initial HTTP 404 explicitly, verifies the last complete book and original
acquisition remain intact, requires the request sequence `[1, 2, 2]` (successful
page 1 reused), and verifies reuse of the recovered complete book. The suite
still contains 40 scenarios. Syntax compilation and diff checks pass.

Runtime confirmation remains pending: restart from the IDE and press
**Ctrl+F12 only**, requiring **40/40**. Existing live walkthrough evidence stays
valid; no scan/blueprint/contracts repeat is needed for this correction.

### Batch 1 cleared; original transcripts rechecked — 2026-11-16

Run `11364-1794830978`, launched at 06:09:40 Central from the current
repository's `main.py`, logged:

```text
06:11:52 check=underbid_route_cache result=PASS
06:11:55 check=metadata_route_sharing result=PASS
06:11:55 passed=40 total=40 network=none data=synthetic
```

This clears the corrected combined-batch regression gate. Retain the completed
live walkthrough evidence above; no standalone repeat is pending. It does not
turn unexercised metadata/auth routes or failed contract items into live passes.

The original assessment, implementation, rep and current-batch transcripts were
rechecked directly after the user's compaction concern. The plan now advances
to the second batch: history payload persistence/honest completion, agreed
scanner/Jita/Stock Market consumer ages, and SSO cooldowns/packaged notices.
Prepare those together and consolidate their new scenarios and outstanding
relevant live cases into the next IDE test round. Broader workflow order stays
ESI closeout → Stock Market → Industry. No behavior changes were made by this
documentation update.


### History / consumer-policy / SSO / notice batch — 2026-11-16

Implemented locally; syntax and diff checks passed. No new IDE run or general
ESI compliance audit was performed. The saved 40/40 result remains the previous
batch's evidence. Ctrl+F12 now prepares **52** checks (the prior 40 plus twelve).

New runtime revision: **esi-evidence-2-policy-batch** in the START line.
Restart from the normal IDE entry before testing; a currently running process
does not gain these changes merely because files changed.

Combined verification handoff:

1. Press **Ctrl+F12**. Require the final ESI-CacheCheck line to say
   passed=52 total=52 network=none data=synthetic. New cases cover recent-history
   restore/actual archive merging, fetch/parse/save failure retention/recovery,
   per-item partial results and hourly age, concurrent/valid-empty history,
   Cache-Control Age, Jita reference vs active scans, Stock Market's one-hour
   boundary/hub independence, forced singleflight, SSO shared/date/restart and
   failed-grant isolation, and notice packaging.
2. Open Stock Market and switch hubs. Look for stock_hub_load REUSE below one
   hour, or a due acquisition gated by the saved ESI expiry. A newer scanner
   acquisition must satisfy this check. Reopening or switching does not reset
   the acquisition timestamp.
3. With tracked holdings, exercise history freshening and restart. Look for
   market_history RESTORE/REUSE with original age/expiry and HistoryOverlay
   accepted/durable/failed counts. Only complete durable results advance the
   optional hub success metadata. PARTIAL and SAVE_FAILED retain prior disk
   data and do not claim durable success. No timestamp-only hourly skip remains.
4. Scan another hub using the preserved Jita reference, and scan Jita directly.
   The former logs jita_reference reuse below 24 hours; direct Jita follows
   server freshness. Age-boundary cases are injected in the offline checks;
   ordinary testing does not require waiting a day.
5. Open **Data use / CCP** in the main control bar. Confirm the complete notice
   is readable. Source/spec checks do not replace testing a future packaged
   executable; none was built here.
6. Retain the earlier open live cases: reputation controls, structure
   metadata/discovery, matching/different authorization contexts, and
   unexpired regional/private-book reuse across restart. Do not deliberately
   trigger real SSO throttling; injected cases exercise Retry-After safely.

The next general ESI compliance audit is deferred until after Caleb tests the
batch. Trade Refresh's 20-minute scheduler and broader Stock Market/Industry
workflow reviews remain outside this implementation.

### Policy-batch pass, real history restart and deferred follow-ups — 2026-11-16

The saved log confirms these separate observations:

- Run `7296-1794837922` started at 08:05:24 Central with revision
  `esi-evidence-2-policy-batch`, code `df45afeff0d3`. At 08:31:08 it recorded
  `passed=52 total=52 network=none data=synthetic`. The injected history
  RESTORE at 08:31:07 belongs to that suite, not an application restart.
- The actual recent-history database contained 27 complete snapshots / 4,498
  rows from Amarr and Jita, with saved acquisition/expiry and actual payloads.
  This was checked read-only; it did not itself establish live restart reuse.
- New process `21248-1794840356` started at 08:45:58 with the same revision/code.
  It restored all 27 snapshots / 4,498 rows, with original ages of 1,425–2,414
  seconds and original expiry `2026-11-17T11:05:00+00:00`. Every restored
  snapshot's row count and expiry matched the prior process's PERSIST record.
  At 08:46:16 Holdings recorded
  `requested=10 accepted=10 durable=10 failed=0 fetched=0 reused=10 complete=True`.
  No history FETCH or RESTORE_FAILED was recorded in that reviewed run through
  08:49. This closes the actual-row history restart/reuse gap.
- Stock Market reused Amarr after restart at age 278 seconds (08:46:15), then
  reused the newer scanner snapshot at age 24 seconds (08:46:59). Earlier live
  use acquired Amarr at age 7,837 seconds and repeatedly reused newer snapshots.
  Scanner schedules followed ESI expiry; Jita reference reuse was observed.
  Boundary/hub-independence checks passed in the synthetic suite. Stock Market's
  active-hub policy is at least one hour from acquisition, not 20 minutes.
- SSO numeric/date/missing-header cooldown, concurrency/restart and failed-grant
  isolation checks passed synthetically. Notice content and build declarations
  passed; successful notice opens are not logged. Direct computer-use capture
  failed twice with timeouts, so readability was not visually verified.

The retained November 14–16 logs show two Trading manual skills/standings
operations and two Industry standings operations (each acquisition then reuse),
three private-book acquisitions across two runs, four manual contract searches,
21 contract scheduler cycles and 95 completed regional sync operations. These
are recorded operations, not lifetime usage or proof of every edge case.
The rep dialog and explicit Refresh rep button are not distinguished in logging.

Caleb asked to set the remaining items aside as a later fix/check list. See
[the deferred list](ESI_ACQUISITION_GOALS.md#deferred-fix-and-verification-list)
for notice readability, the explicit rep controls, structure metadata/discovery
and authorization/unexpired-book cases, buyer authentication and contract item
errors. Confirmed errors and verification gaps remain distinct. No application
code changed and no general ESI audit was performed by this documentation update.

### F1 contract dispatch cooldown — implemented, IDE verification pending, 2026-11-16

`contracts/contracts_esi.py::_raw_request_network()` now rechecks the existing
shared cooldown inside the semaphore immediately before dispatch, on every
loop iteration. The outer check and existing retry/session/response behavior
remain in place. A cooldown established while a task queues therefore applies
before that task sends; cancellation exits the semaphore context normally.

`tools/esi_audit_fix_checks.py` adds `contract_queued_cooldown` and
`contract_cancelled_cooldown` to the existing Ctrl+F12 runner (54 total).
They exercise the actual network helper with an occupied local semaphore,
event-ordered gate closure and a fake session. They assert zero sends during
the new pause, one successful send after release with preserved arguments and
response, and cancellation propagation/slot release. The old implementation
would complete the request and fail the blocked-state assertion rather than
depending on a timeout. No pre-fix runtime result was obtained: the IDE test
run is user-controlled, and no standalone harness was used.

Syntax compilation of all three changed Python files and `git diff --check`
passed. These are static results, not a synthetic runtime pass. No app was
launched or controlled, and no live throttle was manufactured.

Restart `main.py` from this checkout in the IDE, then press **Ctrl+F12**.
Confirm the `[ESI-Evidence] event=START` entry path identifies this checkout,
both new named checks report PASS, and the summary reads
`[ESI-CacheCheck] passed=54 total=54 network=none data=synthetic` in
`%APPDATA%/EVEMarketScout/eve_scout.log`. Earlier 52-check results do not verify
F1. Ordinary Contracts behavior and unexpected exceptions remain to be
reviewed in the next user-run session. F2 and its two additional checks are
not part of this implementation; the combined target remains 56.

### F2 Tracking regional expiry reuse — implemented, IDE verification pending, 2026-11-16

Tracking now uses `get_cached_orders`: complete regional books, including
empty books, remain reusable through ESI expiry beyond 300 seconds. Expired,
missing or incomplete books retain the per-type sell fallback; absent expiry
uses the existing 300-second policy. Full shared rows remain intact; REUSE
age is display-only, with unavailable age logged as `unknown`.

`underbid_regional_expiry_reuse` checks an unexpired 301-second-old full book,
restore with original clocks and valid empty reuse with zero route requests.
`underbid_regional_fallback` covers expired/missing/incomplete/no-expiry books,
no active types, unique sorted types and cached repeats without regional
mutation. Real isolated stores/injected requesters are used. Structure-routing
doubles retain private guards and cover unknown age independent of admission.

All four changed Python files pass syntax compilation; `git diff --check`
passes. No pre-fix or post-fix IDE runtime result was obtained. Restart
`main.py` from this checkout, press **Ctrl+F12**, and confirm the START entry
identifies this checkout, all four F1/F2 named checks PASS, and
`[ESI-CacheCheck] passed=56 total=56 network=none data=synthetic` appears in
`%APPDATA%/EVEMarketScout/eve_scout.log`. Earlier 52/54 results do not verify F2.
Exercise ordinary Tracking/Contracts use for saved-log review of completed
operations/exceptions. Caleb approved `3.69 live` and commit/push on
2026-11-16; this version checkpoint does not establish runtime acceptance.
