# Automated IDX Website Collector

This document describes the guarded network source used to discover new public IDX disclosures and its production scheduling/recovery behavior.

## Current phase — production released

The collector has passed all rollout phases:

- **Phase A — collector MVP:** controlled real disclosure discovery, official attachment download/extraction, Gemini analysis, Synapse publish, and duplicate-free immediate rerun.
- **Phase B — durable reliability:** fresh ephemeral runner restored Synapse-backed checkpoint, repeated the bounded window with no duplicate work, and `health` worked without IDX/AI access.
- **Phase C — daily scheduler:** `.github/workflows/daily.yml` and `synapse-idx-website daily` are merged and production-enabled behind kill switches. Two controlled production runs passed processing + idempotency gates.

Website collection remains intentionally **non-authoritative for complete coverage** in every phase.

## Source design

- HTTP only; no browser fallback.
- Official HTTPS IDX hosts only.
- One request stream; no concurrency or per-ticker fanout.
- At least 10 seconds between guarded source requests, plus jitter.
- Maximum two retries for transport/5xx failures.
- HTTP 403, 429, auth/proxy challenges, HTML WAF/CAPTCHA/Turnstile responses, and protection redirects stop the run.
- No proxy rotation, CAPTCHA solving, cookie farming, TLS impersonation, or challenge bypass.
- Collection windows capped at 48 hours.
- Pagination capped and fails closed when result reconstruction is not safe.
- Attachments downloaded sequentially.
- Checkpoint stores a bounded set of recently observed IDX IDs.
- Checkpoint committed only after Synapse processing succeeds.
- Failed or processing-partial work never advances the durable checkpoint.
- Website collection never commits authoritative coverage.

## Incremental and recovery behavior

The public endpoint exposes calendar-date filters while the adapter applies exact timestamp filtering locally. The durable checkpoint stores recent raw IDX IDs, making overlapping windows safe.

Scheduled window selection:

1. conservative **30-hour minimum lookback**;
2. compare with `latestAnnouncedAt - IDX_INCREMENTAL_OVERLAP_DAYS` from durable state;
3. choose the earlier boundary for recovery;
4. cap at **48 hours**;
5. deduplicate against durable seen IDs before attachment/AI work.

If publishing, extraction, or AI analysis fails, the old checkpoint remains intact. A later successful run retries through the bounded overlap.

Correctness does not depend on ephemeral runner filesystem state.

## Manual collector command

```bash
synapse-idx-website collect \
  --start '2026-08-22T18:00:00+07:00' \
  --end '2026-08-22T20:00:00+07:00' \
  --enable-source \
  --confirm-publish
```

Manual collection remains separately gated and does not enable scheduling.

## Scheduled command

```bash
SYNAPSE_DAILY_ENABLED=true synapse-idx-website daily --confirm-schedule
```

The command validates `DailyPolicy`, requires Synapse internal credentials, restores durable source state, uses `runMode=DAILY`, and keeps authoritative coverage disabled.

## GitHub Actions schedule

`.github/workflows/daily.yml` declares:

- cron `0 20 * * *` = **03:00 Asia/Jakarta** next calendar day;
- `workflow_dispatch` for controlled verification;
- one production concurrency group with `cancel-in-progress: false`;
- Gemini as scheduled AI provider;
- HTTP-only transport;
- historical backfill disabled;
- ticker fanout disabled;
- 50-minute workflow timeout;
- Tesseract OCR + English/Indonesian language packs installed on runner;
- `set -o pipefail` so collector failures propagate through `tee`.

Repository-level kill switch:

```text
IDX_DAILY_ENABLED=true
```

If missing or not exactly `true`, the job is skipped and no IDX request is made.

Required Actions secrets:

- `GEMINI_API_KEY`
- `SYNAPSE_INTERNAL_BASE_URL`
- `SYNAPSE_INGESTION_SECRET`

## Production rollout evidence

### Validation run #1 — successful processing

```text
runId: 401b3277-12f7-4962-a620-9b01d94000c1
ok: true
processingOk: true
sourceRequests: 9
checkpointCommitted: true
attachmentsStaged: 7
filesExtracted: 7
documentsAnalyzed: 7
analysesCompleted: 4
partialDisclosures: 0
errors: []
```

### Validation run #2 — durable idempotency

```text
runId: cc0ce9cd-1b82-45ed-abdc-568b929bdd7e
ok: true
sourceRequests: 2
checkpointCommitted: true
disclosuresAvailable: 0
disclosuresCreated: 0
attachmentsStaged: 0
filesExtracted: 0
documentsAnalyzed: 0
analysesCompleted: 0
partialDisclosures: 0
```

The second overlapping run proves the durable checkpoint suppresses duplicate disclosure, attachment, extraction, and AI work.

## Understanding `PARTIAL`

Do not equate the raw run status `PARTIAL` with processing failure.

The website source intentionally refuses to claim full authoritative market coverage. A healthy completed production run can therefore persist:

```text
status = PARTIAL
error_code = SOURCE_COVERAGE_UNPROVEN
error_message = Source processing succeeded but the requested window is not authoritative.
metadata.sourceState.processingOk = true
metadata.sourceState.sourceComplete = false
```

This means:

- processing succeeded;
- durable checkpoint may advance;
- no authoritative coverage is committed.

A genuine processing failure instead uses signals such as:

```text
SOURCE_PROCESSING_PARTIAL
processingOk = false
FAILED
BLOCKED
```

Synapse product-health classification is expected to distinguish these cases while preserving the raw audit status.

## Rollout incidents and operational lessons

### Shell pipeline failure masking

Original scheduled command used a pipe to `tee`, allowing `tee` exit 0 to hide collector failure. Fixed in PR #23 with:

```bash
set -o pipefail
```

### Missing Tesseract

A real production run reached OCR but the GitHub runner did not have the system Tesseract binary. PR #23 added Tesseract and language packs.

### Synapse source-state 404

A scheduled retry failed before contacting IDX because Synapse production returned 404 for the durable source-state endpoint. The route existed in source; production had a stale build/deployment. Rebuilding and redeploying current Synapse `main` fixed it.

### GitHub Issue alert limitation

The workflow currently attempts to create a GitHub Issue when the collector fails, but repository Issues are disabled. The Issue step therefore cannot serve as a reliable production alert right now.

Collector correctness, workflow failure propagation, and checkpoint safety do not depend on Issue creation. Follow-up should either enable Issues or add another alert channel.

## Health check

```bash
synapse-idx-website health
```

`health` reads only Synapse source state and does not contact IDX or the AI provider.

## Production gates — completed

- [x] scheduler code merged with CI green;
- [x] Actions secrets configured;
- [x] `IDX_DAILY_ENABLED=true` enabled;
- [x] two consecutive production `workflow_dispatch` runs passed;
- [x] duplicate processing/AI replay absent on second run;
- [x] checkpoint advances only after successful processing;
- [x] no authoritative website coverage committed;
- [x] OCR dependencies available on runner;
- [x] collector failures propagate through shell pipeline.

## Remaining monitoring

- [ ] observe first natural 03:00 Asia/Jakarta cron after production release;
- [ ] verify continued low-request idempotent behavior on no-new-disclosure days;
- [ ] monitor 403/429/protection responses;
- [ ] establish a reliable failure-alert channel while repository Issues remain disabled.
