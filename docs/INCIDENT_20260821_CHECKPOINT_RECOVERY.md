# IDX Website Checkpoint Incident — 2026-08-21 Recovery

## Status

The checkpoint data-loss mechanism is fixed and the affected source window has been reconciled. Production is **not yet considered fully validated** because six stock-scope disclosures remain `PARTIAL` while the Gemini free-tier request quota is exhausted, and the next natural scheduled daily run still needs to complete successfully with the current code.

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
- skip that announcement for the current pass without checkpointing it, allowing a later retry;
- continue processing other announcements in the source window;
- skip explicit non-stock products when IDX metadata flags ETF, DIRE, DINFRA, EBA, or SPEI;
- retain fail-closed handling for access protection, malformed metadata, redirects, and other unexpected transport errors.

A 2026-08-24 exact-window validation proved the 404 fix reached the AI stage successfully. The remaining failure was Gemini quota exhaustion rather than IDX collection.

### Gemini run-level circuit breaker

PR #36, merged as `73f0032052eeca9f6cfbb9f853db04a69a92d573`:

- after one terminal Gemini HTTP 429, stop further AI/extraction work for remaining non-READY disclosures in the run;
- leave those disclosures retryable as `PARTIAL`;
- report `AI_RATE_LIMITED` rather than a generic processing error.

Recovery run `df4a9c73-e00d-4bf9-9c16-5c1ac311e2a1` proved the circuit breaker: eight candidates were available, one attempted Gemini and received 429, and the remaining seven were deferred without an AI retry storm.

## Official non-stock audit

A metadata-only audit reread official IDX announcement metadata for 2026-08-20 through 2026-08-23. It performed no Synapse writes, attachment downloads, or AI calls.

Results:

- 2026-08-20: 140 metadata rows, 47 explicit non-stock rows;
- 2026-08-21: 159 metadata rows, 48 explicit non-stock rows;
- 2026-08-22: 5 metadata rows, 0 explicit non-stock rows;
- 2026-08-23: 4 metadata rows, 0 explicit non-stock rows;
- total: 95 source IDs flagged by official IDX metadata as ETF/DIRE/DINFRA/EBA/SPEI.

Those 95 source IDs reconcile to 43 canonical disclosure rows that had already been ingested as `READY` before explicit product filtering was added. They are retained for backend audit rather than deleted.

Synapse PR #50, merged as `9f3f8ff2675b41b3f8be69b5341e8ca78110ed0f`, added `idx_disclosures.is_stock_scope` and quarantined exactly those 43 canonical rows. Authenticated user reads are restricted by RLS to `is_stock_scope = true`; service-role/backend access retains all rows for audit and reconciliation.

Production counts immediately after the migration:

- 159 total disclosures;
- 116 stock-scope disclosures;
- 43 quarantined non-stock disclosures;
- 110 stock-scope `READY`;
- 6 stock-scope `PARTIAL`.

## Final reconciliation of the 166 poisoned target IDs

The final classification uses canonical announcement IDs plus Synapse `sourceAliases`, because multiple source announcements may correctly reconcile to one canonical disclosure.

| Classification | Source IDs |
| --- | ---: |
| Stock-scope `READY` | 108 |
| Stock-scope `PARTIAL` awaiting AI retry | 5 |
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

## Remaining stock-scope PARTIAL disclosures

Five affected-window stock disclosures remain retryable because Gemini quota is exhausted:

- BUKA — `idx-web-20260821212216-1052/BL/CORSEC/SURAT/VIII/2026_id-id`;
- MEJA — `idx-web-20260821213506-011/HDK/SP/VIII/2026_id-id`;
- MKNT — `idx-web-20260821215844-151/MKNT-OJK/VIII/2026_id-id`;
- WMPP — `idx-web-20260821221241-077.34/B/SKet/WMP-CS/VIII/2026_id-id`;
- PICO — `idx-web-20260821224612-038/PIC/CS/VIII-2026_id-id`.

A sixth stock-scope PARTIAL, TUGU (`idx-web-20260823092742-140/S/01/PD-ATPI/VIII/2026_id-id`), was discovered by the 2026-08-24 exact-window validation and is outside the original 166-ID incident target.

Gemini reported the free-tier `generate_content_free_tier_requests` quota at its 500-request limit for `gemini-3.5-flash-lite`. These rows must remain outside checkpoint state until processing reaches `READY`.

## Durable checkpoint rebuild

The active source-state holder is run `a3b3d603-b978-4fe8-8641-c44877d2b0c5`.

The durable checkpoint was rebuilt from all current `READY`, stock-scope `idx-web-*` canonical IDs plus their stock-scope `sourceAliases`:

- `seenIds`: 109 unique source IDs;
- `latestAnnouncedAt`: `2026-08-22T18:46:14Z` (2026-08-23 01:46:14 WIB);
- non-stock intersection: 0;
- PARTIAL intersection: 0;
- rebuild marker: `stock-scope-hygiene-20260824`.

The two unresolved BXS IDs are recorded separately in source-state incident metadata and intentionally excluded from `seenIds`.

## Operational cleanup

- PR #29, the old one-off execution PR, was closed without merge after the reconciliation audit.
- The temporary `idx-recovery-20260821.yml` workflow is removed from `main` during incident closeout.
- Failed, cancelled, and poisoned historical ingestion/source-state rows are preserved as evidence.
- `.github/workflows/daily.yml` is unchanged.

## Exit criteria for production validation

Do not label the collector fully production-validated again until all of the following are true:

1. Gemini quota is available and the six stock-scope PARTIAL disclosures are retried to a terminal healthy state, or any genuine document-specific failure is separately explained.
2. The rebuilt 109-ID checkpoint remains free of non-stock and PARTIAL IDs.
3. A natural scheduled daily run executes with the current source code, does not fail on stale attachments/non-stock metadata, and exhibits healthy processing semantics.
4. Any reappearance of either unresolved BXS source ID is investigated and processed rather than silently checkpointed.
