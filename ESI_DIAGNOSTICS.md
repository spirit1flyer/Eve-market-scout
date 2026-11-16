# ESI runtime diagnostics

This instrumentation identifies the running checkout and records evidence of
actual activity. It does not implement the remaining fixes from the compliance
audit and does not label an untested route compliant.

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

Changes are uncommitted and APP_VERSION remains 3.67 live. The compliance
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

After restarting from the IDE, **Ctrl+F12** runs eight offline safety checks in
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
