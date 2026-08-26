# IDX Website Checkpoint Incident — 2026-08-21 Recovery

## Status

**Closed — Production Revalidated on 2026-08-26.**

The checkpoint data-loss mechanism is fixed, the affected source window has been reconciled, the Gemini backlog was cleared, and a later natural scheduled daily run has now proven the repaired collector in production.

The AI backlog caused by Gemini free-tier quota exhaustion was cleared on 2026-08-24 after quota became available again. Retry run `60227561-e58f-4eb4-92ae-b3b6eb81c90c` processed all six pending stock-scope disclosures successfully.

Final production validation was provided by the natural GitHub Actions schedule on 2026-08-26:

- GitHub Actions workflow: `IDX Daily Collector`, run #8 (`32895256845`);
- trigger: `schedule` (not manual);
- production ingestion run: `99740537-a86f-4e8c-ace8-6f52b98ffc29`;
- terminal status: `PARTIAL / SOURCE_COVERAGE_UNPROVEN`, the expected non-authoritative website-source status;
- `processingOk=true`;
- source requests: 50;
- request-budget deferral: `true`;
- issuer disclosures processed: 18;
- canonical disclosures created: 17;
- files extracted: 46;
- analyses completed: 18;
- durable checkpoint advanced from 115 to 133 seen source IDs;
- `latestAnnouncedAt` advanced to `2026-08-24T10:54:29+07:00`;
- production stock-scope state after the run: 133/133 `READY`, 0 non-`READY`;
- duplicate non-null `idx_announcement_id` groups: 0.

The source still reached its configured 50-request budget, but the repaired behavior preserved all fully completed work, deferred the in-progress row, committed the durable checkpoint, and returned a healthy processing result instead of failing the entire run. This is the production acceptance condition for the request-budget defect.

The 43 quarantined legacy non-stock disclosures remain preserved for backend audit and excluded from authenticated stock-scope reads.

## Incident summary

A validation run requested a narrow timestamp interval from the IDX `GetAnnouncement` endpoint. The endpoint accepts calendar-day date filters, so it returned rows from outside the requested timestamp range. The old collector appended raw announcement IDs to checkpoint state before applying the exact timestamp filter. As a result, valid same-day announcements that were never processed could still be marked as seen.

The poisoned durable checkpoint eventually contained 306 IDs. The affected reconciliation target is the 166 checkpoint IDs whose embedded announcement timestamps fall between 2026-08-20 22:10:47 and 2026-08-22 20:41:25 Asia/Jakarta.

Historical poisoned source-state row `73a062d9-15e0-48a5-b824-956ef1e38d4c` is intentionally preserved as audit evidence and must not be rewritten.

## Correctness fixes

### Checkpoint window semantics

PR #26, merged as `db34c96fe43a4c096777814746b10ff5dfcfa252`:

- apply the exact requested timestamp window before checkpoint inclusion;
- checkpoint only successfully normalized/processed source IDs;
- expose requested-window diagnostics;
- regression-test same-day out-of-window rows so they cannot poison checkpoint state.

### Non-issuer rows

PR #31, merged as `252fd72b5f973fe70974df0f01d0b23fefe4e976`:

- skip exchange/market rows with no issuer ticker;
- keep bounded diagnostics;
- never checkpoint skipped non-issuer IDs;
- preserve fail-closed behavior for an issuer row with a ticker but missing title.

### Ticker contract parity

PR #32, merged as `51210d84075f66c438d75d4374a9e10033875aa2`:

- enforce the Synapse ticker contract before publishing;
- skip unsupported ticker formats such as fund/security codes containing unsupported punctuation;
- never checkpoint skipped IDs.

### Recovery concurrency

PR #33, merged as `97ce7a614466dc27581481ed513eb6a72312f75e`:

- remove the PR-trigger cancellation path that could cancel an active recovery;
- use a dedicated recovery trigger and non-cancelling concurrency group.

### Stale attachment 404 and explicit non-stock products

PR #35, merged as `1f25a2beb6d683207fba1254817d44c755aecf90`:

- classify an official IDX attachment HTTP 404 separately;
- defer that announcement without checkpointing it so a later pass can retry it;
- continue processing other announcements in the source window;
- skip explicit non-stock products when IDX metadata flags ETF, DIRE, DINFRA, EBA, or SPEI;
- retain fail-closed handling for malformed metadata and unexpected transport failures.

A 2026-08-24 exact-window validation proved the 404 fix reached the AI stage successfully. The failure at that point was Gemini quota exhaustion rather than IDX collection.

### Gemini run-level circuit breaker

PR #36, merged as `73f0032052eeca9f6cfbb9f853db04a69a92d573`:

- after one terminal Gemini HTTP 429, stop further AI work for remaining non-READY disclosures in the run;
- leave those disclosures retryable as `PARTIAL`;
- report `AI_RATE_LIMITED` instead of producing a retry storm.

Recovery run `df4a9c73-e00d-4bf9-9c16-5c1ac311e2a1` proved the circuit breaker: one candidate reached the exhausted Gemini quota and seven remaining candidates were deferred without repeated AI calls.

### Daily request-budget progress preservation

PR #40, merged as `1d4d79a9d0083e0409333f4619816a70e24295ab`:

- type IDX request-budget exhaustion separately from unrelated source failures;
- preserve fully staged disclosures when the source-request budget is reached later in the candidate loop;
- do not publish or checkpoint the partially staged current row;
- stop later candidates for that run and leave them retryable;
- allow successful processing to commit progress so the next daily run resumes from a newer checkpoint;
- keep the configured request limit unchanged.

The 2026-08-26 natural cron reached exactly 50 source requests, set `requestBudgetDeferred=true`, processed 18 issuer disclosures, committed the checkpoint from 115 to 133 seen IDs, and completed with `processingOk=true`. It did **not** regress to `FAILED / SOURCE_RUN_FAILED`. This production run revalidated the fix.

## Official non-stock audit and quarantine

A metadata-only audit reread official IDX announcement metadata for 2026-08-20 through 2026-08-23. It performed no Synapse writes, attachment downloads, or AI calls.

Results:

- 2026-08-20: 140 metadata rows, 47 explicit non-stock rows;
- 2026-08-21: 159 metadata rows, 48 explicit non-stock rows;
- 2026-08-22: 5 metadata rows, 0 explicit non-stock rows;
- 2026-08-23: 4 metadata rows, 0 explicit non-stock rows;
- total: 95 source IDs flagged by official IDX metadata as ETF/DIRE/DINFRA/EBA/SPEI.

Those 95 source IDs reconcile to 43 canonical disclosure rows that had been ingested as `READY` before explicit product filtering was added. They are retained for backend audit rather than deleted.

Synapse PR #50, merged as `9f3f8ff2675b41b3f8be69b5341e8ca78110ed0f`, added `idx_disclosures.is_stock_scope` and quarantined those 43 canonical rows. Authenticated parent disclosure reads are restricted by RLS to `is_stock_scope = true`.

Synapse PR #51, merged as `eaf0aac0a4d6abc99c4afdc432377eeb39912fdb`, extended the same stock-scope restriction to authenticated reads of disclosure files, tags, analyses, claims, numbers, and dates. Service-role/backend access remains available for audit and reconciliation.

## Final reconciliation of the 166 poisoned target IDs

The reconciliation uses canonical announcement IDs plus Synapse `sourceAliases`, because multiple source announcements may correctly map to one canonical disclosure.

After the successful AI retry, the target classification is:

| Classification | Source IDs |
| --- | ---: |
| Stock-scope `READY` | 113 |
| Official IDX non-stock product IDs | 48 |
| Intentional current-source exclusions | 3 |
| Historical IDs no longer present in current IDX metadata | 2 |
| **Total** | **166** |

The three intentional source exclusions are:

- `20260821163757-Peng-PK-00059/BEI.PLP/08-2026_id-id` — exchange announcement with no issuer ticker;
- `20260821195331-Peng-P-00949/BEI.PP2/08-2026_id-id` — `R-ABFII`, exchange announcement for additional ETF listing;
- `20260821200235-Peng-P-00953/BEI.PP2/08-2026_id-id` — `R-ABFII`, exchange announcement for additional ETF listing.

The two unresolved historical IDs are:

- `20260821215318-BXS/0/021/003/2026_id-id`;
- `20260821224527-BXS/08/021/007/2026_id-id`.

Both existed in the historical poisoned checkpoint but are absent from the current full-day IDX metadata response. They are **not** classified as non-stock. The `BXS` identifier family is also used by a valid BACA disclosure (`BXS/08/021/005/2026`), so these two IDs remain explicitly unresolved rather than being discarded. They are not in the rebuilt checkpoint; if IDX returns either ID again, the collector can process it normally.

## Post-quota AI retry

The bounded branch-local retry package was triggered once after the expected Gemini quota reset window. It was not merged into `main`.

Run `60227561-e58f-4eb4-92ae-b3b6eb81c90c`:

- started: 2026-08-24 15:00:20 WIB;
- completed: 2026-08-24 15:06:07 WIB;
- source announcements found: 6;
- new canonical disclosures: 0, because all six rows already existed as retryable `PARTIAL` records;
- files downloaded/extracted: 15/15;
- analyses completed: 6/6;
- source requests: 19;
- terminal status: `PARTIAL / SOURCE_COVERAGE_UNPROVEN`, which is the expected non-authoritative website-source status and is not a processing failure.

The six rows completed with `google-gemini / gemini-3.5-flash-lite` and valid active analyses:

- BUKA — `idx-web-20260821212216-1052/BL/CORSEC/SURAT/VIII/2026_id-id`;
- MEJA — `idx-web-20260821213506-011/HDK/SP/VIII/2026_id-id`;
- MKNT — `idx-web-20260821215844-151/MKNT-OJK/VIII/2026_id-id`;
- WMPP — `idx-web-20260821221241-077.34/B/SKet/WMP-CS/VIII/2026_id-id`;
- PICO — `idx-web-20260821224612-038/PIC/CS/VIII-2026_id-id`;
- TUGU — `idx-web-20260823092742-140/S/01/PD-ATPI/VIII/2026_id-id`.

BUKA, MEJA, and TUGU each have three published file records; MKNT, WMPP, and PICO each have two. No stock-scope `idx-web-*` row remains non-READY after the retry.

## Durable checkpoint rebuild

The recovery checkpoint was rebuilt in retry run `60227561-e58f-4eb4-92ae-b3b6eb81c90c` from all current `READY`, stock-scope `idx-web-*` canonical IDs plus valid stock-scope `sourceAliases`:

- canonical stock-scope READY `idx-web-*` rows: 112;
- `seenIds`: 115 unique source IDs;
- `latestAnnouncedAt`: `2026-08-23T02:27:42Z` (2026-08-23 09:27:42 WIB);
- non-stock intersection: 0;
- non-READY intersection: 0;
- rebuild marker: `post-ai-retry-stock-scope-ready-20260824`.

That recovery checkpoint was then superseded normally by the successful natural daily run on 2026-08-26. The active durable checkpoint after production revalidation contains 133 seen source IDs and `latestAnnouncedAt = 2026-08-24T10:54:29+07:00`.

The earlier safe 109-ID checkpoint in run `a3b3d603-b978-4fe8-8641-c44877d2b0c5` remains historical evidence. The poisoned 306-ID source-state row also remains untouched.

The two unresolved BXS IDs remain separately recorded as incident evidence and intentionally excluded from the recovery `seenIds`; if either reappears from IDX it remains eligible for normal processing.

## Production revalidation

The final acceptance run was not a manual backfill or workflow dispatch. GitHub Actions invoked the normal scheduled workflow from `main` on 2026-08-26.

Production ingestion run `99740537-a86f-4e8c-ace8-6f52b98ffc29` proved all remaining acceptance properties:

1. scheduler execution was natural (`event=schedule`);
2. the source budget reached 50 without aborting and discarding completed work;
3. `processingOk=true` and there were no publish errors or partial disclosures;
4. the run ended `PARTIAL / SOURCE_COVERAGE_UNPROVEN`, which is expected because the website adapter intentionally does not claim authoritative coverage;
5. the durable checkpoint committed and advanced from 115 to 133 seen source IDs;
6. 18 analyses completed and 17 new canonical disclosures were created;
7. all 133 stock-scope disclosures were `READY` after the run;
8. stock-scope non-`READY` remained 0;
9. duplicate non-null `idx_announcement_id` groups remained 0.

**Verdict: Production Revalidated. Incident closed.**

The remaining question of whether 50 source requests per day provide enough sustained throughput to catch up with future IDX volume is a separate capacity/operations concern, not an unresolved correctness defect from this incident.

## Operational cleanup

- PR #29, the old one-off execution PR, was closed without merge.
- The temporary `idx-recovery-20260821.yml` workflow was removed from `main`.
- The bounded AI retry package was validated with Ruff and the full pytest suite, executed only from its ops branch, and was never merged into `main`.
- Failed, cancelled, and poisoned historical ingestion/source-state rows are preserved as evidence.
- The production schedule remains `0 20 * * *` (20:00 UTC / approximately 03:00 Asia/Jakarta). Later observability-only workflow summary changes do not alter collector scheduling or ingestion semantics.

## Exit criteria for production validation

1. **Complete:** all six previously pending stock-scope disclosures are `READY` with active analyses and files.
2. **Complete:** the rebuilt 115-ID recovery checkpoint contained no quarantined non-stock or non-READY source IDs.
3. **Complete:** a later natural scheduled daily run executed with the repaired source code and checkpoint without a real source/processing failure, while preserving progress at the 50-request budget.
4. **Ongoing invariant:** if either unresolved BXS source ID reappears, it must be investigated and processed rather than silently checkpointed.

All incident exit criteria are satisfied. Capacity monitoring and the unresolved-BXS invariant continue as normal operations.
