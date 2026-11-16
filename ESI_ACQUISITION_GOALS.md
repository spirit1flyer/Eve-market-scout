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

## Execution status

| Work | Implemented / observed | Still needed |
| --- | --- | --- |
| Diagnostics and readable REUSE waits | User-run logs show requests, acceptance, persistence, original ages, readable waits, heartbeats and automatic reports. | Exercise remaining routes; reports alone cannot clear unexercised paths or cross-restart repeats. |
| Full public Industry prices and systems | Shared expiry-aware provider, locks and atomic persistence. Real acquisition, repeated reuse and restart restoration/reuse verified. | Retain regression coverage as other consumers are integrated. |
| Wallet balance, transactions, journal, active orders and order history | Shared private provider, independent clocks, context isolation, atomic full snapshots and separate successful-page stages. All five real acquisitions persisted; eight synthetic checks passed. | Real private RESTORE then REUSE remains unobserved. Verify actual inventory reconciliation with fetched, reused and restored records. |
| Structure discovery's active character orders | Routed through the private provider. | Live matching-context reuse; structure metadata still needs shared protection. |
| Remaining acquisition paths and consumer policies | Reviewed below; not finished. | Skills/standings, direct/manual paths, history overlay, order snapshot hardening, agreed consumer ages, SSO cooldown and notices. |

Evidence at review: run `19360-1794786679` saved all five private resources at
17:52:39-46 Central and updated four inventory listings at 17:53:23. Ctrl+F12
passed eight synthetic checks at 17:54:03. The 18:02:54 heartbeat still belonged
to that original process: 73 responses, zero errors and zero observed early
repeats. This does not establish a real private-cache restart or all accounting
cases. That process shut down at 18:03:22; no subsequent startup was present at
review. See [verification instructions](ESI_DIAGNOSTICS.md).

## Remaining phase-1 work, in order

The five-stage implementation sequence agreed in the original conversation is
retained here. The detailed findings are code-review work items within that
sequence, not newly agreed product behavior or proof of runtime failures.

### 1. Finish shared acquisition, persistence and direct/manual coverage

**Close the current wallet verification first.** Restart and request Trade
Refresh while a saved linking resource is still unexpired; compare original
timestamps and private route counts. If its window has already elapsed,
acquire an eligible replacement and test during that new window. Verify
character changes, discovery sharing and independent resource clocks.

Run isolated accounting fixtures through the actual inventory sync with
fetched, reused and restored wallet records. Require equal lots, cost basis,
listings, partial fills, completed/cancelled orders, fees/taxes and realized
P&L; repeated sync/restart must not double-count. Include delayed linking
records and partial refresh failures. The existing inventory/P&L replay cases
are useful foundations; the eight cache checks alone do not cover this.
The new wallet constructor's incompatibility with `ESIWallet(None)` in existing
replay code was corrected during this review; its added assertion needs the
next in-app safety-check run.

Then extend the established provider approach in small verified steps:

- **Skills and standings:** persist full raw responses and ESI expiry; share
  matching authorization contexts between trading and Industry. Keep derived
  calculations local and preserve the existing user-triggered acquisition.
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

Next implementation chunk after the wallet verification gate: shared durable
skills/standings responses, followed by the private-structure/direct-path work.

## Consumer policies

| Consumer | Data it should use | When to acquire again |
| --- | --- | --- |
| Scanner's selected hub | Current complete orders for that resource | On demand when missing or when ESI makes an updated representation available, normally about five minutes; automatic scanning follows that turnover. |
| Cross-hub scanner | Current complete snapshots for both selected hubs | Apply the scanner policy independently to each resource, including Jita when selected. |
| Jita reference while scanning another hub | Preserved complete Jita snapshot, intentionally allowed to be semi-stale | Initially if absent; on an explicit Jita pull/direct Jita scan; or when the reference reaches 24 hours old. |
| Stock Market hub | Saved complete snapshot for that hub | On loading/opening the hub if missing or the last successful acquisition was at least one hour ago. A newer scanner acquisition satisfies this check. |
| Industry order consumers | Saved semi-stale snapshots | A slower cadence is acceptable. No new exact Industry refresh interval was agreed. Reuse available data rather than introducing scanner-frequency traffic. |

All actual acquisitions must respect the resource's ESI cache expiry and rate
limits. A consumer's preferred age does not authorize a request before ESI
expiry. Explicit refresh means request an eligible update, not bypass CCP's
cache. Expired ESI data may still be displayed by consumers that tolerate it;
label the age accurately.

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
