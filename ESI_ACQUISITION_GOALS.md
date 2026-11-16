# Agreed ESI acquisition and cache goals

Recorded 2026-11-15 from the user's instructions; progress reviewed the same day.
This file owns the agreed behavior and remaining work order.
[The compliance reassessment](ESI_COMPLIANCE_REVIEW.md) records baseline findings
and implementation evidence. An implementation entry is not a runtime pass.

## Original conversation and continuation rule

This update was checked against the original messages in both 2026-11-15
conversations, **Assess ESI route compliance** and **Start implementing the
plan**, including the user's corrections. The original local session records
are `01a1df98-2eba-7073-9a7e-7a9c92e6d517` and
`01a1e12b-b8c2-70a2-ad03-c9ec7b540f1a`, under the Codex sessions directory
for `2026/11/15`.

When continuing after compaction, consult the original relevant messages
before changing an agreed requirement or updating this plan. A compressed
handoff is a navigation aid, not a replacement for the transcript. Preserve
explicit corrections; do not revive an assistant proposal the user corrected.
If an original record is unavailable, identify that limitation instead of
claiming it was reviewed. Check current source and logs separately for status.

Decisions preserved from those messages:

- The scanner needs ESI's roughly five-minute turnover. Stock Market and
  Industry can use older orders. There is no global cooldown between hubs.
- Preserve Jita across shutdowns. Refresh its reference on a specific pull,
  a direct Jita scan, or at 24 hours old. A selected Jita scanner target uses
  normal scanner freshness.
- Stock Market checks each hub independently on load, using one hour since
  actual acquisition. A newer scanner snapshot counts; visiting a tab does not.
- Transactions, journal and order history link the entire buy/sell inventory
  flow. Balance alone cannot establish purchases, costs, fees or sale outcomes.
- The user's acceptance of roughly hourly linking data referred to ESI's
  cache window. The assistant's proposed extra one-hour acquisition delay was
  corrected and is **not an agreed requirement**.
- Each resource must save its data, last successful acquisition and expiry,
  then reuse it across starts for the remaining time. REUSE must show the wait.
- Finish and verify ESI/cache work, then work through Stock Market with the
  user, then Industry. The user runs the IDE app; use its saved log as evidence.
- Reputation ("rep") means saved faction/corporation standings, used for the
  seller and Industry. On 2026-11-15 the user chose manual updates plus a
  seven-day on-use refresh: keep using saved rep across starts and on refresh
  failure, request an eligible update explicitly or when used at seven days
  old. A freshness deadline must not turn known rep into zero.

## Execution status

| Work | Implemented / observed | Still needed |
| --- | --- | --- |
| Diagnostics and readable REUSE waits | User-run logs show requests, acceptance, persistence, original ages, readable waits, heartbeats and automatic reports. | Exercise remaining routes; reports alone cannot clear unexercised paths or cross-restart repeats. |
| Full public Industry prices and systems | Shared expiry-aware provider, locks and atomic persistence. Real acquisition, repeated reuse and restart restoration/reuse verified. | Retain regression coverage as other consumers are integrated. |
| Wallet balance, transactions, journal, active orders and order history | All five real resources persisted/restored. Four unexpired resources reused after a real restart, with original ages/expiry; expired balance acquired independently. Eleven offline cache/accounting checks passed at 18:32:08 Central. | Live character changes and discovery sharing; accounting cases beyond the synthetic fixtures, including late purchases. Retain regression checks while extending the provider. |
| Skills and standings, Trading and Industry | Seventeen checks passed at 19:00:02 Central. Real seller/Industry skills and standings acquired, saved, restored and reused across restart. | New seven-day/manual rep policy and two additional scenarios (nineteen total) await runtime verification. Matching-context sharing is synthetic-verified; buyer login fails with invalid_grant and needs reauthentication. |
| Structure discovery's active character orders | Routed through the private provider. | Live matching-context reuse; structure metadata still needs shared protection. |
| Remaining acquisition paths and consumer policies | Reviewed below; not finished. | Skills/standings, direct/manual paths, history overlay, order snapshot hardening, agreed consumer ages, SSO cooldown and notices. |

Latest wallet evidence: run `16552-1794789044` acquired eligible replacements
around 18:32 Central and passed all eleven offline checks at 18:32:08. After
restart, run `25944-1794789218` restored all five snapshots at 18:33:47 and
reused transactions, journal, orders and order history at 18:34:40 without
requests to those routes. Original ages/absolute expiry were preserved; balance
alone was expired and fetched again. The public underbid fallback separately
repeated eighteen requests before prior expiry in the earlier process. That
known direct-path gap remains open. See [verification instructions](ESI_DIAGNOSTICS.md).

## Remaining phase-1 work, in order

The five-stage implementation sequence agreed in the original conversation is
retained here. The detailed findings are code-review work items within that
sequence, not newly agreed product behavior or proof of runtime failures.

### 1. Finish shared acquisition, persistence and direct/manual coverage

**Wallet restart/reuse and the eleven-check accounting checkpoint passed.**
Continue protecting that behavior as the provider is extended. Live character
changes and discovery sharing remain unobserved; do not mark those complete.

Run isolated accounting fixtures through the actual inventory sync with
fetched, reused and restored wallet records. Require equal lots, cost basis,
listings, partial fills, completed/cancelled orders, fees/taxes and realized
P&L; repeated sync/restart must not double-count. Include delayed linking
records and partial refresh failures. The existing inventory/P&L replay cases
are useful foundations; the eight cache checks alone do not cover this.
The wallet constructor's compatibility assertion for `ESIWallet(None)` passed
in the 18:32 eleven-check run.

The checkpoint through the prior review was committed and pushed as `46c76f4`
on 2026-11-15, retaining `APP_VERSION` 3.67 live. Subsequent work adds three
isolated in-app scenarios: complete order lifecycle across FIFO lots; delayed
journal fee/tax backfill with independent expiry; and partial journal-refresh
failure/recovery. Each compares fetched, reused and restored wallet data through
the actual inventory sync, then repeats sync and reloads inventory from disk.
These three accounting scenarios passed in the user's 18:32 in-app run.
Late-arriving purchase records and other delayed-linking cases remain outside
these three scenarios; their pass is not exhaustive coverage.

Then extend the established provider approach in small verified steps:

- **Skills and standings:** persist full raw responses and ESI expiry; share
  matching authorization contexts between trading and Industry. Keep derived
  calculations local and preserve the existing user-triggered acquisition.
  Original shared-provider chunk passed seventeen checks and normal live
  seller/Industry acquisition/restart/reuse. The newer reputation policy is
  implemented locally and awaits nineteen-check/live verification.
  Skills retain their existing one-hour non-forced preference; rep uses the
  user-selected seven-day/manual policy. Explicit force may bypass a consumer
  preference only after server freshness permits it. Skills follow
  Cache-Control where supplied (ESI's event-driven route); original Expires,
  Cache-Control, Date and Age metadata remain saved. This is a route-specific
  interpretation of server caching, not a new wallet delay or scheduler change.
- **Private structures:** isolate books and metadata by authorization context
  before sharing Browse Orders, discovery and Industry lookups. The current
  shared structure-book cache is keyed only by structure ID and can return
  before checking authentication. Routing Browse into it alone is insufficient.
- **Other direct paths:** protect Tracking's per-type order fallback and the
  station/system metadata calls with reusable payloads, expiry and request
  deduplication. A later-page 404 must not accept accumulated partial orders.
- **Orders:** make regional/structure disk replacement atomic and report save
  failures. Retain successful pages across retry/restart, check all page counts,
  shapes and consistency metadata, and respect every page's expiry. Missing
  validators must not silently establish a complete consistent multi-page
  snapshot. Existing whole-snapshot retries can refetch successful page 1 too
  soon; preserve the working low-bandwidth retry behavior while closing this.
- **Blueprints:** deduplicate simultaneous pulls, review authorization scoping,
  validate all pages/rows before replacing the database, and distinguish a
  database-write failure from a valid empty result. Currently a caught write
  failure returns zero and the caller can still report successful storage.

Finish the smaller route inventory too: contract cross-caller deduplication,
name/ID failure isolation and negative caching, remaining universe metadata,
and concurrent route/jump-count misses. These do not all require the same TTL;
choose reuse appropriate to each resource. Do not declare every route covered
because the public Industry and wallet families passed.

### 2. Preserve recent history data and report durable completion honestly

Persist the recent ESI market-history overlay with acquisition/expiry metadata,
separately from archive data. Restore it with the saved freshness state.
Replace the current timestamp-only success behavior and propagate per-item
failure/partial results; a swallowed fetch error must not print FETCHED and
advance the successful-refresh clock. Preserve the previous good data on
fetch, parse or write failure. Keep memory acceptance distinct from disk save.

### 3. Apply the agreed scanner, Jita and Stock Market policies

Enforce the consumer table below through the shared snapshots: selected scanner
resources follow ESI turnover; Jita reference has its own 24-hour bound; Stock
Market checks one hour from actual acquisition on hub load. Remove the
unbounded active-Jita fallback and inconsistent five-/20-minute Stock Market
thresholds. A force/manual flag or fixed 300-second age must not bypass an
unexpired ESI response. No all-hub preload is required.

Review the Trade Refresh scheduler separately: its existing 20-minute floor
and active-order clock can miss linking-data expiry by seconds and defer the
next acquisition until a later wake. Agree any scheduling change before
implementation. Do not introduce an extra hour after acquisition or force
all wallet resources onto one clock.

### 4. Finish SSO cooldown and distribution notice work

Share and honor SSO Retry-After/error cooldowns across token/verify callers.
Make CCP's notice and the data-use disclosure accessible in the packaged app;
correct claims about universal manual reuse to match the final implementation.
The developer account and contact email were already user-confirmed compliant.
Do not reopen those or the previously deferred auth/versioning redesigns.

### 5. Verify each stage and close phase 1

Use the user's normal IDE runs and saved log, plus isolated in-app checks where
failure cases cannot be safely exercised with real data. Cover startup and
restart, manual repeats, matching/different authorization contexts, failed
pages/saves, hub switching, consumer age boundaries and accounting outcomes.
Compare original timestamps across runs; process-local repeat counters alone
are insufficient. Keep implemented, statically checked and live-verified
statuses separate. Only then proceed to the follow-on workflow phases below.

Current verification gate: reputation retention and manual/seven-day refresh
(nineteen-check runner and normal IDE behavior). Next implementation
chunk after that: private-structure/direct-path work.

## Consumer policies

| Consumer | Data it should use | When to acquire again |
| --- | --- | --- |
| Scanner's selected hub | Current complete orders for that resource | On demand when missing or when ESI makes an updated representation available, normally about five minutes; automatic scanning follows that turnover. |
| Cross-hub scanner | Current complete snapshots for both selected hubs | Apply the scanner policy independently to each resource, including Jita when selected. |
| Jita reference while scanning another hub | Preserved complete Jita snapshot, intentionally allowed to be semi-stale | Initially if absent; on an explicit Jita pull/direct Jita scan; or when the reference reaches 24 hours old. |
| Stock Market hub | Saved complete snapshot for that hub | On loading/opening the hub if missing or the last successful acquisition was at least one hour ago. A newer scanner acquisition satisfies this check. |
| Industry order consumers | Saved semi-stale snapshots | A slower cadence is acceptable. No new exact Industry refresh interval was agreed. Reuse available data rather than introducing scanner-frequency traffic. |
| Reputation for seller/Industry | Last good saved raw standings in the matching authorization context | On an explicit rep refresh or an existing worker acquisition path when seven days old, respecting ESI freshness. Cache-only reads keep using saved rep even when an update is due; failed refresh retains it and its original timestamps. |

All actual acquisitions must respect the resource's ESI cache expiry and rate
limits. A consumer's preferred age does not authorize a request before ESI
expiry. Explicit refresh means request an eligible update, not bypass CCP's
cache. Expired ESI data may still be displayed by consumers that tolerate it;
label the age accurately.

Reputation's seven-day age is measured from successful acquisition, not a tab
visit or restart. Trading's **Refresh Skills** button explicitly requests an
eligible rep update; ordinary character/hub use follows the seven-day policy.
Industry's Standings dialog reuses on open and offers **Refresh rep** for an
explicit update. Cached social skill levels remain available for effective
rep calculations so those modifiers do not vanish after the skills window.
Raw/base rep remains the source for seller broker-fee calculations.

The Stock Market one-hour rule concerns order snapshots. Holdings-history
freshening is a separate operation and must not be mistaken for proof that
orders were refreshed.

Wallet transactions, journal and order history follow each response's absolute
ESI expiry, with a resource-specific estimate only if metadata is unusable.
An hourly server cache may have only minutes remaining when fetched. Save and
restore that original expiry; do not add one hour to acquisition or restart.
Balance and active orders retain their own clocks. The existing Trade Refresh
scheduler is unchanged pending the separate review above.

## Hub switching and the preserved Jita reference

- Caches are independent by region/resource. Switching Amarr to Jita does not
  impose an app-wide cooldown or require pulling every hub first.
- When a scanner starts on the destination hub, reuse its still-current
  snapshot or acquire it if due. Returning to another still-current hub reuses
  that hub's snapshot.
- Merely changing a selector need not start a scan. The scanner acquisition
  policy applies when the selected data is actually requested.
- For a scan of another hub, Jita is a reference, not a second active target.
  Do not refresh that reference every five minutes merely because its ESI
  representation expired.
- A successful direct Jita acquisition updates the shared snapshot and its
  original acquisition time for subsequent reference use. This includes a
  cross-hub scan that actually selects Jita.
- Enforce the 24-hour Jita-reference limit in its own provider; it must not
  depend on opening Stock Market or running an unrelated daily burst.
- No new all-hub preloading requirement is part of these goals.

## Persistence and acquisition ownership

- Save the complete accepted payload, original successful acquisition time,
  absolute ESI expiry, available validators, source, and completeness marker.
- Restore saved data before deciding whether startup needs an acquisition.
  Shutdown/restart must not reset its age or discard an otherwise usable
  snapshot.
- A saved scheduling timestamp without its corresponding data is not a
  restored cache. In particular, persist recent ESI history with its freshness
  state, not just a "last freshened" flag.
- Share complete snapshots and same-resource in-flight acquisitions across
  consumers. Filter locally for individual views; do not reacquire the same
  unexpired public response because a different consumer needs different IDs.
- Keep authenticated resource caches separated by character and authorization
  context. Do not share private data across unrelated accounts.
- Validate every required page before accepting a snapshot. Failure or partial
  pagination must not overwrite a good complete snapshot or advance its
  successful-acquisition timestamp.
- Preserve the last good disk snapshot on write/fetch failure; report failures
  explicitly. Durable replacement should be atomic.

## Evidence the user must be able to see

Use the IDE terminal and the existing persistent `eve_scout.log` to explain:

- Which checkout, app version, code fingerprint, and process/run are active.
- Disk restore success/failure, original data age, and completeness.
- Consumer/resource-specific REUSE, FETCH, WAIT, SKIP, ACCEPT, and failure
  decisions, with the reason, age, ESI expiry, and next eligible refresh.
- Accepted page/order counts and successful disk persistence, separately from
  an HTTP response or a worker starting.
- Network/parse/pagination/save failures and whether the previous snapshot was
  retained. A generic "finished" line is insufficient.
- Periodic summaries and an on-demand report that itself makes no requests.
  Unused routes remain untested, not implicitly compliant.

Logs must allow comparison across restarts. Session-only repeat counters cannot
prove that a restarted app respected a response obtained by its previous run.
Do not log credentials, tokens, authorization codes, or private payloads.

## Acceptance scenarios for later implementation

1. Restart with a usable disk snapshot: restore the original data/time and show
   the applicable reuse decision without an unnecessary acquisition.
2. Scanner uses Amarr, then Jita, then Amarr: evaluate each selected resource
   independently; reuse Amarr if still current. Respect ESI's turnover when due.
3. Scan another hub using a 23-hour Jita reference: reuse it. At 24 hours,
   acquire an eligible update even if Stock Market is disabled. A direct Jita
   scan uses scanner freshness, not the 24-hour allowance.
4. Open a Stock Market hub whose snapshot is 59 minutes old: reuse. Open at
   one hour: acquire if eligible. A scanner snapshot from ten minutes ago
   resets this need because it is a newer actual acquisition, not a tab visit.
5. Open two consumers of public industry data: share the full unexpired
   response. Restart before its expiry: restore and reuse that response.
6. Fail a page or disk write: preserve the last good snapshot, log the failure,
   and do not claim durable successful completion.
7. Reconcile identical inventory scenarios using freshly fetched, reused and
   disk-restored linking records: require the same accounting results and no
   duplicate purchases, fills, fees or realized profit after repeated sync.

## Follow-on work order

Agreed 2026-11-15. These are later phases, not additional behavior changes
authorized by recording this roadmap:

1. Finish the ESI/acquisition, persistence, and refresh-policy work in the
   compliance plan. Verify it works through the acceptance scenarios and the
   user's normal IDE-run evidence before moving on.
2. Work through Stock Market with the user: establish how they expect it to
   operate, trace the actual data and calculations through the workflow, and
   identify any mismatch. Agree and implement essential corrections, then
   verify the intended behavior. Do not assume the current implementation is
   the specification or treat this as permission for an unrelated redesign.
3. After Stock Market is working as intended, review and refine Industry.
   Its detailed changes remain to be discussed; no additional features or
   exact refresh cadence are agreed by this roadmap.

Industry-related acquisition fixes still belong to phase 1; broader Industry
workflow/feature work waits until phase 3.

The user runs the app from the IDE. Review its saved log; do not launch or
control the GUI as a substitute. Saving these goals authorizes documentation,
not silently implementing the remaining behavior changes.
