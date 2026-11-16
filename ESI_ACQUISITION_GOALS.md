# Agreed ESI acquisition and cache goals

Recorded 2026-11-15; transcript and runtime progress rechecked 2026-11-16.
This file owns the agreed behavior and remaining work order.
[The compliance reassessment](ESI_COMPLIANCE_REVIEW.md) records baseline findings
and implementation evidence. An implementation entry is not a runtime pass.

## F1 checkpoint update — 2026-11-16

Current source is **3.68 live**, with Caleb's approval to bump, commit and
push. The general ESI audit has now been performed; see
[the audit and F1 follow-up](ESI_FULL_AUDIT_2026-11-16.md). The selected
[F1/F2 plan](PLAN_ESI_F1_F2.md) governs the next targeted work.

F1 is implemented: queued contract requests recheck the shared cooldown
inside the semaphore before sending. Two isolated checks bring the IDE
runner to 54. Syntax/diff checks pass; the latest observed runtime pass
remains 52/52, and F1's 54-check IDE run and ordinary Contracts review remain
pending. F2 and its two checks are still planned. The deferred list below
remains parked; this checkpoint does not declare ESI sign-off.

## Prior checkpoint — 2026-11-16, before the general audit and F1

- **Current pushed code checkpoint:** `b26f1d8` on
  `checkpoint/esi-compliance-2026-11-16`. It contains both the earlier
  acquisition batch and the newest history/policy/SSO/notice batch. This is
  a recovery checkpoint, not a verified release. Subsequent testing is
  recorded below. The
  remaining-list/handoff update below is documentation written after that
  commit; it does not change application code.
- **Previous pushed baseline:** `b329c8c` on `main`, following `46c76f4`.
  `APP_VERSION` remains **3.67 live**. That baseline commit includes shared
  skills/standings persistence, reputation retention/manual/seven-day policy,
  and the expanded offline accounting/character/rep runner.
- **Current position:** batch 2 passed **52/52** offline checks at **08:31:08
  Central on 2026-11-16**, run `7296-1794837922`, revision
  `esi-evidence-2-policy-batch`, code `df45afeff0d3`. The real restart in run
  `21248-1794840356` restored 27 history snapshots / 4,498 rows at 08:45:58,
  preserving original ages and expiry; Holdings reused ten at 08:46:16 with
  `durable=10 fetched=0 reused=10 complete=True`. The remaining items are now
  **set aside at Caleb's request**, as listed below. The general ESI audit
  remains the next planned assessment; it has not been performed.
- **Earlier verified checkpoint:** IDE run `11364-1794830978` passed **40/40** offline checks at
  **06:11:55 Central on 2026-11-16**, including both corrected 404 regressions,
  regional page/save recovery, blueprints, small routes, accounting, reputation
  and private structures. Earlier real seller, Industry, wallet and structure
  persistence/restart evidence remains valid. Synthetic and live evidence
  remain distinct.
- **Local implementation added:** authorization-scoped structure books/metadata,
  shared Browse/scanner/discovery/Industry lookups, persisted per-type underbid
  responses and public station/system/constellation metadata. Original page
  expiry and acquisition times survive restart. Successful pages are staged;
  partial/mixed books are rejected. Details are in ESI_DIAGNOSTICS.md.
- **Pending live verification:** rep controls, structure metadata/discovery and
  matching/different authorization cases remain open. A real private book
  restored with original age/expiry after restart, but was already expired;
  unexpired cross-restart reuse remains synthetic-verified only.
- **Private-structure live evidence:** run `28296-1794823224` on 2026-11-16 acquired and
  atomically saved 20 structure orders at 04:05:41 Central, reused that snapshot
  twice, and acquired again only after expiry at 04:10:52. No early repeats
  were observed. That run did not exercise restart, metadata or auth changes.
  Tracking exposed a remaining routing gap: 17 public regional requests for
  the selected private structure returned zero orders. The authorized local
  correction now routes Tracking to the shared private book. The later run
  restored the old book at 04:24:00, correctly refreshed it because it had
  expired, then Tracking reused the new 20-order book at 04:24:18 and 04:24:21.
  There was one structure-order request, no public underbid fallback, no
  observed early repeats and no production HTTP/network errors in that run.
- **Completed local batch:** regional page recovery/validation and atomic book
  saves; authorization-scoped blueprints with serialized, failure-aware DB
  application; shared contract pages, name/ID isolation, remaining universe
  metadata, routes/jump counts and private structure search. Syntax/diff checks
  passed, followed by the **40/40 runtime pass**. The missing public-404 expiry
  import and filtered-page negative-cache interaction are fixed and verified.
  Negative caching applies to public single-response metadata/routes; failed
  order pages retain the last complete book and can recover using staged pages.
  The failed-run history is retained in ESI_DIAGNOSTICS.md.
- **Batch live evidence:** Amarr's 192-page books were accepted, atomically
  saved and reused. Restart restored 191,659 orders; that book was expired
  by the next scan, so it correctly acquired and reused a replacement.
  All 57 blueprints persisted, then restored with original age/expiry and
  reused twice after restart. Contracts reused its saved list after restart;
  some item fetches failed and remain separate from successful list reuse.
  Neither run reported early repeats. Details are in ESI_DIAGNOSTICS.md.
- **Consumer-policy evidence:** live scans followed ESI expiry and reused the
  Jita reference. Stock Market reused scanner snapshots and, after restart,
  reused Amarr at age 278 seconds. The 52-check pass includes Jita's 23h/24h
  boundary, direct Jita freshness, Stock Market's one-hour boundary and hub
  independence, shared SSO cooldowns, and notice content/build declarations.
  Stock Market uses **at least one hour from actual acquisition** on hub
  load/open; the old 20-minute Stock Market policy in imported memory is stale.
- **Next work:** keep the deferred list parked. Do not request another blanket
  test round or start repairs from it automatically. The previously requested
  general ESI compliance audit is the next planned assessment, with unresolved
  cases explicitly recorded; this documentation update does not perform it or
  declare ESI closeout.

## Remaining problem list and new-chat handoff — 2026-11-16

**Latest direction: set the remaining items aside as a later fix/check list.**
The 52/52 IDE suite and real recent-history restart/reuse verification have
passed. Do not reopen them merely because a fresh chat begins. Setting items
aside does not mark them verified or authorize implementation.

### Deferred fix and verification list

Counts below come from the retained November 14–16 logs, not lifetime usage.
Successful dialog openings and explicit rep-button clicks are not separately
logged, so absence of those events cannot establish non-use.

| Item | Evidence and classification | Later action |
| --- | --- | --- |
| Data use / CCP notice | Content and build-declaration checks passed. Successful opens are not logged; attempted computer-use inspection timed out. Visual readability remains unverified, not a demonstrated UI defect. | Verify the dialog when resumed. Verify the bundled notice before any future executable distribution; no build is requested. |
| Explicit reputation refresh controls | Two Trading manual skills/standings operations and two Industry standings operations on November 15 each demonstrated acquisition followed by reuse. Persistence/restart and synthetic seven-day/failure policies pass. Logging does not distinguish a dialog open from the newer explicit Refresh rep button. | Verify that specific control behavior; retain established seller/Industry acquisition/reuse evidence. Do not label reputation as wholly untested or broken. |
| Structure metadata/discovery, authorization changes and unexpired regional/private-book reuse after restart | Private books had three successful acquisitions across two runs, reuse and Browse-to-Tracking sharing. No shared-provider structure metadata/discovery/search activity was recorded. Specific authorization-change and unexpired-book restart cases remain live verification gaps; synthetic coverage passes. | Exercise those specific cases when resumed. Do not substitute the now-passed recent-history restart test or treat ordinary private-book use as untested. |
| Buyer invalid_grant | Observed authentication failure, including both explicit buyer skill-fetch attempts on November 15. Successful reauthentication/data acquisition is not recorded. | Buyer reauthentication, then targeted data-path verification. Do not infer seller/Industry cache failure or redesign authentication from this observation alone. |
| Contract item fetch errors | Four manual searches, 21 scheduler cycles and 95 completed regional sync operations are recorded. The large item batch returned 31,759 successes, two gone and 65 bad; subsequent searches repeatedly encountered one bad item. Successful list persistence/restart reuse remains established. | Investigate causes and decide whether a code correction is needed. Error responses alone do not prove a cache defect; do not call the whole Contracts feature broken or untested. |

**Separate next assessment:** the requested general ESI compliance audit,
after the testing now recorded. Carry the deferred cases explicitly and
classify findings as original omissions, regressions we introduced, unverified
cases, or genuinely new requirements before proposing work. Parking this list
is not ESI sign-off, and no audit or repair was performed by this update.

**Retain completed evidence:** public Industry persistence/restart reuse;
wallet restoration and independent expiry; seller/Industry skills/standings
persistence; private-book Browse-to-Tracking sharing; regional acceptance/save
and reuse; blueprint save/restart reuse; the corrected 40-check suite; the
52/52 policy-batch pass; and actual recent-history row restoration/reuse after
the 08:45 restart. See the latest results in [ESI_DIAGNOSTICS.md](ESI_DIAGNOSTICS.md).
The missing 404 expiry import and failed-page negative-cache interaction were
regressions introduced by our cache work and subsequently passed their checks.
Do not reopen these merely because a fresh chat begins. New evidence can
justify a targeted regression investigation.

**Important correction from the three-day review:** commit af8cf40 persisted
Holdings' last-refreshed timestamp without persisting its recent history rows.
That introduced a restart case where the app could suppress acquisition while
the rows were absent. The earlier SKIP observation was insufficient proof of
working history restoration. The new provider in b26f1d8 addresses this in
source; its correction subsequently passed both the 52-check suite and real
row restoration/reuse after restart. This was rework against the original
history fix, not an additional product feature.

**Scope and communication for the next chat:**

- Do only what Caleb explicitly requests. Report additional findings separately
  without implementing them automatically. No feature or refactor backlog is
  implied by this handoff.
- Keep code-written, static-check-passed, synthetic-test-passed, and live-tested
  statuses distinct. A growing check count is not a growing defect count.
- Explain a proposed correction in plain language: original problem, intended
  behavior, what changes internally, and what evidence would show it works.
- Preserve bounded checkpoints; ask for commit approval rather than silently
  accumulating further implementation batches. A commit approval includes push.
- Caleb prefers ending a long chat and starting a new one over compaction.
  Use saved state and original transcripts for handoffs. No setting was changed
  to disable compaction, and the agent cannot guarantee preventing it.

**Deliberately outside the remaining implementation list:** changes to the
Trade Refresh scheduler's 20-minute floor; an extra hourly wallet delay;
offline JWT verification, token-encryption or scope redesign; reopening the
confirmed developer account/email; and unrelated performance/feature work.
The previously noted late-purchase accounting cases are coverage limitations,
not newly proven accounting defects or authorization to rewrite accounting.
After ESI closeout, review Stock Market against Caleb's intended workflow;
Industry follows that. Do not substitute the older Claude backlog.

## Plain-language checkpoint record — 2026-11-16

This section records evidence **at the original commit**, before the later
52/52 pass and live history restart. Use Current checkpoint and the deferred
list above for current status; pending statements in this historical record
are not instructions to repeat completed testing.

Checkpoint branch: `checkpoint/esi-compliance-2026-11-16`, based on `b329c8c`.
The checkpoint was committed and pushed as `b26f1d8`.
This preserves the current work for recovery and review. It is not a tested
release or a declaration that the application is ESI compliant.

| Original problem | What the code now does | Evidence at this checkpoint |
| --- | --- | --- |
| Different screens could request the same data again before it was due. | Screens share saved responses and in-progress requests. The saved ESI expiry decides when another request is allowed. Private responses also require the matching character and authorization. | Earlier acquisition changes passed the 40-check IDE run. Some real routes and authorization cases still need testing. |
| A failed page or save could lose useful progress or misrepresent completion. | Successful pages survive retry/restart. A complete book is accepted only after validation; failed replacements retain the previous disk data. Blueprints report database failures rather than successful empty results. | Page, save, blueprint, and smaller-route scenarios passed within the earlier 40 checks. |
| Holdings could remember that history was refreshed without retaining the actual recent rows. | Recent history rows are saved separately from the archive, with their original acquisition/expiry. Fetch and save outcomes are reported per item. | New code is syntax-checked; the expanded IDE test run is pending. |
| Scanner, Jita reference, and Stock Market had conflicting freshness rules. | Active scanner hubs follow ESI expiry. Jita reference has a 24-hour limit. Stock Market checks one hour since acquisition on hub load/open. | New age-boundary scenarios are prepared; the expanded IDE test run is pending. |
| SSO retries and binary notices needed separate handling. | Trading and Industry share SSO retry cooldowns. The main window opens the data-use/CCP notice, and the build includes that notice. | New synthetic checks are prepared. Live dialog testing and any future packaged-build verification remain pending. |

Some code moved into shared helper files to make these paths use the same
rules. The larger file count does not represent that many separate features.
The Trade Refresh scheduler, accounting rules, and broader Stock Market and
Industry workflows were not intentionally redesigned by this checkpoint.

**Next gate:** Caleb tests the IDE run, including Ctrl+F12 (expected 52/52).
The prior 40/40 result does not verify the twelve new scenarios. Report any
additional findings separately; do not silently add them to the implementation
scope. After user testing, perform the requested general ESI compliance audit.
APP_VERSION remains 3.67 live.

## Original conversation and continuation rule

This update was checked against the original messages in both 2026-11-15
conversations, **Assess ESI route compliance** and **Start implementing the
plan**, including the user's corrections. The original local session records
are `01a1df98-2eba-7073-9a7e-7a9c92e6d517` and
`01a1e12b-b8c2-70a2-ad03-c9ec7b540f1a`, under the Codex sessions directory
for `2026/11/15`.

On 2026-11-16, re-read those source transcripts plus the reputation continuation
`01a1e1b1-3ff2-7993-a3f2-1c63c39c1968` and the current batch thread
`01a1e20a-814c-7550-8e83-dfead2439039`, rather than relying on a compacted
handoff. In the original assessment, user messages at JSONL lines 558, 593
and 705 establish Jita's 24-hour rule, Stock Market's one-hour rule and the
ESI → Stock Market → Industry order. The implementation thread's line 525
requires saved per-resource request timing, following the correction about
ESI expiry. The current thread's lines 366–378 establish batching: regional
orders/blueprints/small routes first, then history/policies/SSO/notices, with
combined testing. No new implementation scope is inferred from this audit.

When continuing after compaction, consult the original relevant messages
before changing an agreed requirement or updating this plan. A compressed
handoff is a navigation aid, not a replacement for the transcript. Preserve
explicit corrections; do not revive an assistant proposal the user corrected.
If an original record is unavailable, identify that limitation instead of
claiming it was reviewed. Check current source and logs separately for status.

The continuation transcript for this handoff is session
`01a1e453-af3c-7d50-b825-4217347d2bdd`, under the local sessions directory for
2026/11/16. It contains the user's audit-after-testing correction, strict-scope
instruction, checkpoint approval, three-day work review, and preference to end
long chats rather than rely on compaction. The three-day review read original
November 14–16 project messages, including a continuation started November 12,
and cross-checked commits and saved test results. It changed no application code.

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
| Skills and standings, Trading and Industry | Committed/pushed in b329c8c. Seventeen checks passed at 19:00:02 Central; real seller/Industry acquisition, save, restoration/reuse verified. | Live matching-context cross-consumer sharing remains unobserved; its synthetic scenario passed. Buyer login fails with invalid_grant and needs reauthentication. |
| Reputation retention and manual/seven-day refresh | Committed/pushed in b329c8c. Seller/Industry retain last good rep beyond its update deadline and on failure. Its synthetic policy scenarios passed in the current 40/40 run. | Real manual rep-control behavior remains unobserved; fold it into final live checks. |
| Private structures and discovery | Authorization-scoped books/metadata and discovery provider implemented; synthetic isolation/restart checks passed. Real books persist/reuse and Browse-to-Tracking sharing passed. | Live metadata/discovery sharing, authorization changes and unexpired private-book restart reuse remain unobserved. |
| Regional books, blueprints and smaller routes | Implemented locally and passed 40/40. Both 404 regressions verified fixed. Real regional save/reuse, blueprint save/restart reuse and contract-list restart reuse observed. | Unexpired regional restart reuse and remaining live metadata/auth cases. Contract item errors remain distinct from list-cache success. |
| History, consumer policies, SSO and notices | 52/52 passed at 08:31:08 Central. Real restart restored 27 snapshots / 4,498 history rows with original age/expiry and reused ten holdings histories without acquisition. Scanner/Jita and Stock Market live evidence is recorded above. | Notice readability and the specific remaining live cases are parked in the deferred list. Future executable notice verification remains pending before distribution. General ESI audit remains planned. |

Latest wallet evidence: run `16552-1794789044` acquired eligible replacements
around 18:32 Central and passed all eleven offline checks at 18:32:08. After
restart, run `25944-1794789218` restored all five snapshots at 18:33:47 and
reused transactions, journal, orders and order history at 18:34:40 without
requests to those routes. Original ages/absolute expiry were preserved; balance
alone was expired and fetched again. The public underbid fallback's earlier
eighteen pre-expiry repeats were a historical direct-path gap. Shared per-type
snapshots now address that path, with repeat/restart/page-recovery scenarios
passing in the current suite. See [verification evidence](ESI_DIAGNOSTICS.md).

## Immediate continuation

1. **Done: batch 1 and its 40-check gate.** The user completed the combined
   walkthrough; its evidence remains valid. No extra standalone rerun is due.
2. **Written and pushed: batch 2 in b26f1d8; 52/52 now passed.**
   Recent history, scanner/Jita/Stock Market policies, SSO cooldowns and the
   accessible notice are already implemented. The runner already contains
   52 scenarios. Do not implement or extend this batch again as the next step.
3. **Done: recorded IDE checks and real history restart/reuse.** Preserve
   these results. Caleb has set the remaining fix/check items aside; do not
   restart a blanket test round or implement deferred work automatically.
4. **Next planned assessment: the requested general ESI compliance audit.**
   Carry the deferred cases and classifications above. Do not silently
   implement findings or claim phase-1 closure from synthetic tests alone.

Keep the agreed scope bounded. New findings stay separate unless they block
the requested behavior. The Trade Refresh scheduler's 20-minute floor still
requires separate discussion before changing it; do not add an hourly wallet
delay or reopen deferred auth/versioning work. Broader Stock Market workflow
review follows ESI closeout, and broader Industry review follows Stock Market.

Buyer reauthentication and live character/discovery-sharing checks remain open
verification items. Preserve the separation between an invalid buyer login,
successful seller/Industry persistence, and unexercised authorization cases.

## Remaining phase-1 work, in order

The five-stage implementation sequence agreed in the original conversation is
retained here as design/history. Current checkpoint, deferred list and Immediate
continuation above govern what to do next. The detailed findings are code-review work items within that
sequence, not newly agreed product behavior or proof of runtime failures.

### 1. Finish shared acquisition, persistence and direct/manual coverage

**Wallet restart/reuse and the eleven-check accounting checkpoint passed.**
Continue protecting that behavior as the provider is extended. Live character
changes and discovery sharing remain unobserved; do not mark those complete.

The passed inventory fixtures compare fetched, reused and restored wallet
records through the actual sync: FIFO lots/basis, listings/partial fills,
completed/cancelled orders, fees/taxes and realized P&L, including repeat sync,
disk reload, delayed journal records and partial journal-refresh failures.
Late-arriving purchases and other unrepresented linking cases remain separate
coverage gaps. Preserve the existing accounting results while extending them;
the original eight cache checks alone were not accounting verification.
The wallet constructor's compatibility assertion for `ESIWallet(None)` passed
in the 18:32 eleven-check run.

The initial ESI/cache checkpoint was committed and pushed as `46c76f4`.
The subsequent shared-character/accounting/rep work was committed and pushed
as `b329c8c` on 2026-11-15, retaining `APP_VERSION` 3.67 live. It adds three
isolated in-app scenarios: complete order lifecycle across FIFO lots; delayed
journal fee/tax backfill with independent expiry; and partial journal-refresh
failure/recovery. Each compares fetched, reused and restored wallet data through
the actual inventory sync, then repeats sync and reloads inventory from disk.
These three accounting scenarios passed in the user's 18:32 in-app run.
Late-arriving purchase records and other delayed-linking cases remain outside
these three scenarios; their pass is not exhaustive coverage.

Provider coverage already implemented (retain it while finishing the next batch):

- **Skills and standings:** persist full raw responses and ESI expiry; share
  matching authorization contexts between trading and Industry. Keep derived
  calculations local and preserve the existing user-triggered acquisition.
  Original shared-provider chunk passed seventeen checks and normal live
  seller/Industry acquisition/restart/reuse. The newer reputation policy is
  committed/pushed in `b329c8c`; its isolated scenarios passed in the 29-check
  run. Normal rep-control verification remains open.
  Skills retain their existing one-hour non-forced preference; rep uses the
  user-selected seven-day/manual policy. Explicit force may bypass a consumer
  preference only after server freshness permits it. Skills follow
  Cache-Control where supplied (ESI's event-driven route); original Expires,
  Cache-Control, Date and Age metadata remain saved. This is a route-specific
  interpretation of server caching, not a new wallet delay or scheduler change.
- **Private structures:** isolate books and metadata by authorization context
  before sharing Browse Orders, discovery and Industry lookups. Implemented
  locally in `core/esi_route_cache.py`; legacy unscoped files are ignored,
  cache reads check current authorization, and no private book is written into
  the public regional store. Real book persistence/reuse and Tracking sharing
  passed; metadata and live authorization cases remain open.
- **Other direct paths:** protect Tracking's per-type order fallback and the
  station/system metadata calls with reusable payloads, expiry and request
  deduplication. A later-page 404 must not accept accumulated partial orders.
  These paths are implemented locally with exact query keys, page staging,
  validation, atomic saves and original expiry. The structure Tracking fix
  passed live; the expanded small-route scenarios passed in the 40-check run.
- **Orders:** make regional/structure disk replacement atomic and report save
  failures. Retain successful pages across retry/restart, check all page counts,
  shapes and consistency metadata, and respect every page's expiry. Missing
  validators must not silently establish a complete consistent multi-page
  snapshot. Existing whole-snapshot retries can refetch successful page 1 too
  soon; preserve the working low-bandwidth retry behavior while closing this.
  **Implemented locally in the combined batch:** successful regional pages
  persist/reuse independently; every page's count/shape/validator/expiry is
  checked before whole-book acceptance. Whole-book replacement is atomic,
  failures retain disk data and retry saving without another acquisition.
- **Blueprints:** deduplicate simultaneous pulls, review authorization scoping,
  validate all pages/rows before replacing the database, and distinguish a
  database-write failure from a valid empty result. **Implemented locally:**
  shared raw snapshots/pages use the roster's full auth context; per-DB/character
  locks serialize application, complete row validation precedes replacement,
  and failed transactions report failure and retain the previous rows. A valid
  empty result still succeeds. DB/save retries reuse the accepted raw data;
  context changes cannot inherit an earlier character-only cooldown.

Finish the smaller route inventory too: contract cross-caller deduplication,
name/ID failure isolation and negative caching, remaining universe metadata,
and concurrent route/jump-count misses. These do not all require the same TTL;
choose reuse appropriate to each resource. Do not declare every route covered
because the public Industry and wallet families passed.
**Implemented locally in this batch:** exact contract pages share requests,
transactional persistence and cooldowns across callers/loops; mixed bad name/ID
batches split without poisoning valid results, with persistent negatives;
region/type/stargate data and route/jump-count misses use shared snapshots.
Private structure-name search also uses exact auth/query keys. The combined
40-check IDE run passed; unexercised live routes remain separately recorded.

### 2. Preserve recent history data and report durable completion honestly

**Implemented locally, runtime pending:** the shared recent-history provider
saves validated full per-region/type responses transactionally in a separate
SQLite store. It restores original acquisition/expiry/headers, shares in-flight
work across loops, and retries failed saves without another acquisition.
History reads merge the recent overlay with archives; ESI wins on overlapping
dates. Holdings freshening reports per-item accepted/durable/failed results.
A saved hub timestamp alone no longer suppresses missing payload acquisition.

Persist the recent ESI market-history overlay with acquisition/expiry metadata,
separately from archive data. Restore it with the saved freshness state.
Replace the current timestamp-only success behavior and propagate per-item
failure/partial results; a swallowed fetch error must not print FETCHED and
advance the successful-refresh clock. Preserve the previous good data on
fetch, parse or write failure. Keep memory acceptance distinct from disk save.

### 3. Apply the agreed scanner, Jita and Stock Market policies

**Implemented locally, runtime pending:** dedicated Jita-reference provider
checks the strict 24-hour bound. Direct and cross-hub Jita targets cannot use
the old unbounded legacy fallback. Stock Market load/open/manual paths use
one hour from saved acquisition, including scanner updates. All regional
callers recheck shared ESI freshness inside the acquisition lock, even when
forced. No scheduler interval or Industry cadence was changed.

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

**Implemented locally, runtime pending:** Trading and Industry token/verify
requests share a serialized SSO gate. Numeric/date Retry-After applies across
endpoints and survives restart; failed grants have isolated retry delays.
Saved cooldown data contains hashed request identities and deadlines, never
credentials. A Data use / CCP dialog reads the disclosure from source or the
PyInstaller bundle; the spec now includes it. Acquisition claims were revised
to distinguish policy/implementation from remaining runtime coverage. No
public executable was built in this batch.

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

The expanded forty-check runner passed on 2026-11-16 at 06:11:55 Central,
including regional orders, blueprints, smaller routes and both corrected 404
regressions. Remaining live cases include rep controls, metadata, authorization
changes and unexpired regional/private-book cross-restart reuse. Steps 2–4
are implemented locally; their twelve new checks bring the runner to 52.
Step 5 and live verification remain pending before phase closeout.
The Stock Market/Industry follow-on order remains unchanged.

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
8. Restore saved reputation for the matching seller/Industry context even
   after its refresh deadline. Reuse before seven days on ordinary use; request
   an eligible update at seven days or explicitly. A failed update retains the
   previous rep and original acquisition time. Cached social modifiers remain
   available for effective rep, and context changes do not expose old rep.

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
