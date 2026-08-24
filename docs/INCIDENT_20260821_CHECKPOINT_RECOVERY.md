# IDX Website Checkpoint Incident — 2026-08-21 Recovery

## Status

The checkpoint data-loss mechanism is fixed and the affected source window has been reconciled.

The AI backlog caused by Gemini free-tier quota exhaustion was cleared on 2026-08-24 after quota became available again. Retry run `60227561-e58f-4eb4-92ae-b3b6eb81c90c` processed all six pending stock-scope disclosures successfully.

Current production state after the retry:

- 159 total disclosures;
- 116 stock-scope disclosures;
- 116 stock-scope `READY`;
- 0 stock-scope non-`READY`;
- 43 quarantined legacy non-stock disclosures;
- 155 canonical `idx-web-*` rows with 155 distinct announcement IDs;
- 112 stock-scope `idx-web-*` canonical rows, all `READY`, all with active analyses and file records.

The durable website checkpoint now contains 115 unique stock-scope READY source IDs, including valid `sourceAliases`, and has zero intersection with quarantined non-stock or non-READY rows.

Production is **not yet labeled fully revalidated** only because a later natural scheduled daily run must still execute successfully with the current code and checkpoint.

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

The active source-state holder is now retry run `60227561-e58f-4eb4-92ae-b3b6eb81c90c`.

The durable checkpoint is rebuilt from all current `READY`, stock-scope `idx-web-*` canonical IDs plus valid stock-scope `sourceAliases`:

- canonical stock-scope READY `idx-web-*` rows: 112;
- `seenIds`: 115 unique source IDs;
- `latestAnnouncedAt`: `2026-08-23T02:27:42Z` (2026-08-23 09:27:42 WIB);
- non-stock intersection: 0;
- non-READY intersection: 0;
- rebuild marker: `post-ai-retry-stock-scope-ready-20260824`.

The earlier safe 109-ID checkpoint in run `a3b3d603-b978-4fe8-8641-c44877d2b0c5` remains historical evidence but is superseded by the newer source-state row. The poisoned 306-ID source-state row remains untouched.

The two unresolved BXS IDs remain separately recorded as incident evidence and intentionally excluded from `seenIds`.

## Operational cleanup

- PR #29, the old one-off execution PR, was closed without merge.
- The temporary `idx-recovery-20260821.yml` workflow was removed from `main`.
- The bounded AI retry package was validated with Ruff and the full pytest suite, executed only from its ops branch, and was never merged into `main`.
- Failed, cancelled, and poisoned historical ingestion/source-state rows are preserved as evidence.
- `.github/workflows/daily.yml` remains unchanged.

## Exit criteria for production validation

1. **Complete:** all six previously pending stock-scope disclosures are `READY` with active analyses and files.
2. **Complete:** the rebuilt 115-ID checkpoint contains no quarantined non-stock or non-READY source IDs.
3. **Pending:** a later natural scheduled daily run must execute with the current source code and checkpoint without a real source/processing failure.
4. **Ongoing invariant:** if either unresolved BXS source ID reappears, it must be investigated and processed rather than silently checkpointed.

Until criterion 3 is observed, the incident recovery is complete but the collector remains pending final natural-cron production revalidation.
