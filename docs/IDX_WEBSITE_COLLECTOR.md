# Automated IDX Website Collector

This document describes the guarded network source used to discover new public IDX disclosures and its production scheduling gates.

## Current phase

The collector has passed two production gates before scheduling:

- **Phase A — collector MVP:** a controlled real BACA disclosure was discovered, its official attachment was downloaded and extracted, Gemini analysis completed, Synapse publish succeeded, and an immediate same-window repeat produced zero new disclosures, downloads, or AI calls.
- **Phase B — durable reliability:** a fresh ephemeral runner restored the Synapse-backed checkpoint, repeated the bounded window using metadata requests only, produced zero duplicate work, and `health` reported healthy without contacting IDX or AI.
- **Phase C — daily scheduler:** `.github/workflows/daily.yml` and `synapse-idx-website daily` provide a guarded recurring path. The workflow remains inert unless the repository variable `IDX_DAILY_ENABLED` is exactly `true`.

Website collection remains non-authoritative for coverage in every phase.

## Source design

- HTTP only; no browser fallback.
- Official HTTPS IDX hosts only.
- One request stream; no concurrency or per-ticker fan-out.
- At least 10 seconds between source requests in the guarded collector runtime, plus jitter.
- Maximum two retries for transport/5xx failures.
- HTTP 403, 429, auth/proxy challenges, HTML WAF/CAPTCHA/Turnstile responses, and redirects stop the run.
- No proxy rotation, CAPTCHA solving, cookie farming, TLS impersonation, or browser challenge handling.
- Collection windows are capped at 48 hours.
- Pagination is capped and fails closed when the reported result set cannot be reconstructed safely.
- Attachments are cached by source URL hash and downloaded sequentially.
- Checkpoints keep a bounded set of recently observed IDX IDs.
- A checkpoint is committed only after Synapse processing succeeds.
- Failed or partial processing never advances the durable checkpoint.
- Website collection never claims authoritative coverage and never commits coverage.

## Incremental and recovery behavior

The public endpoint exposes calendar-date filters, while the adapter applies exact timestamp filtering locally. The durable checkpoint stores recently observed raw IDX IDs so overlapping windows are safe.

A scheduled run chooses its window as follows:

1. use a conservative **30-hour minimum lookback**;
2. compare that with `latestAnnouncedAt - IDX_INCREMENTAL_OVERLAP_DAYS` from the durable checkpoint;
3. choose the earlier boundary so recovery can extend farther back when needed;
4. cap the result at **48 hours**, which is the source contract maximum;
5. deduplicate by the durable seen-ID checkpoint before staging attachments or invoking AI.

This means a routine run always has overlap, while a stale checkpoint can stretch recovery up to 48 hours without enabling historical backfill.

If publishing, extraction, or AI analysis fails, the old checkpoint remains intact. A later successful run can retry the bounded overlap. Cached attachment files can be reused when they are available on the same runner, but correctness does not depend on ephemeral filesystem state.

## Manual collector command

Manual collection remains separately gated and does not imply that scheduling is enabled:

```bash
synapse-idx-website collect \
  --start '2026-08-22T18:00:00+07:00' \
  --end '2026-08-22T20:00:00+07:00' \
  --enable-source \
  --confirm-publish
```

## Scheduled command

One scheduled iteration is:

```bash
SYNAPSE_DAILY_ENABLED=true synapse-idx-website daily --confirm-schedule
```

The command also validates `DailyPolicy`, requires the Synapse internal API credentials, restores durable source state, uses `runMode=DAILY`, and keeps authoritative coverage disabled.

## GitHub Actions schedule

`.github/workflows/daily.yml` declares:

- cron `0 20 * * *`, which is **03:00 Asia/Jakarta** the next calendar day;
- manual `workflow_dispatch` for controlled verification;
- one production concurrency group with `cancel-in-progress: false`;
- Gemini as the scheduled AI provider;
- HTTP-only transport;
- historical backfill disabled;
- ticker fan-out disabled;
- a 50-minute workflow timeout;
- a GitHub Issue alert when the job fails.

The scheduled job itself has an additional repository-level kill switch:

```text
IDX_DAILY_ENABLED=true
```

If that repository variable is missing or has any other value, the job is skipped and no IDX request is made.

Required GitHub Actions secrets are:

- `GEMINI_API_KEY`
- `SYNAPSE_INTERNAL_BASE_URL`
- `SYNAPSE_INGESTION_SECRET`

## Health check

`health` reads only Synapse source state:

```bash
synapse-idx-website health
```

It does not contact IDX or the AI provider. A failed scheduled run is also visible in the Synapse ingestion audit trail and triggers a GitHub Issue containing the Actions run URL.

## Production gate after merge

Do not treat the existence of the cron file as successful rollout. The release gate is:

1. merge Phase C with CI green;
2. confirm all three Actions secrets are configured;
3. set repository variable `IDX_DAILY_ENABLED=true`;
4. observe two consecutive scheduled runs;
5. verify no duplicate disclosure processing, no unexpected attachment/AI replay, durable checkpoint advancement only on success, and no authoritative coverage commit.

Only after those two scheduled runs pass should the recurring collector be considered fully released.
