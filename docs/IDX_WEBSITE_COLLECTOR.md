# Automated IDX Website Collector MVP

This document describes the guarded network source used to discover new public IDX disclosures with minimal routine operator involvement.

## Scope

The MVP reads the public IDX `ListedCompany/GetAnnouncement` JSON endpoint, stages official IDX attachments locally, and passes normalized disclosures into the existing source-neutral extraction, AI, and Synapse publishing pipeline.

The production scheduler is intentionally **not enabled** by this phase.

## Design

- HTTP only; no browser fallback.
- Official HTTPS IDX hosts only.
- One request stream; no concurrency or per-ticker fan-out.
- Default delay is at least 10 seconds between source requests in the CLI.
- Small random jitter is added between requests.
- Maximum two retries for transport/5xx failures.
- HTTP 403, 429, auth/proxy challenges, HTML WAF/CAPTCHA/Turnstile responses, and redirects stop the run.
- No proxy rotation, CAPTCHA solving, cookie farming, TLS impersonation, or browser challenge handling.
- Collection windows are capped at 48 hours.
- Pagination is capped at 10 pages of at most 100 rows each.
- Attachments are cached by source URL hash and downloaded sequentially.
- Checkpoints keep a bounded set of recently observed IDX IDs.
- A checkpoint is committed only after the Synapse ingestion result reports successful processing.
- Website collection never claims authoritative coverage and never commits coverage.

## Incremental behavior

A run queries the requested date range because the public endpoint exposes calendar-date filters. Exact timestamps are filtered locally. The checkpoint stores recently observed raw IDX IDs so overlapping collection windows can be used safely:

1. query a bounded overlapping window;
2. ignore IDs already present in the checkpoint;
3. stage only attachments for unseen IDs;
4. extract and analyze through the existing source-neutral runner;
5. publish to Synapse;
6. commit the new checkpoint only after processing succeeds.

If publishing or AI analysis fails, the old checkpoint remains intact, so a later run can retry the disclosure. Cached attachment files can be reused without another attachment request.

## One-shot command

The command requires two explicit acknowledgements and does not enable any scheduler:

```bash
synapse-idx-website collect \
  --start '2026-08-22T18:00:00+07:00' \
  --end '2026-08-22T20:00:00+07:00' \
  --enable-source \
  --confirm-publish
```

Runtime configuration still requires the selected AI provider and the Synapse internal API credentials used by source-neutral ingestion.

## Current state

This phase provides the collector code, CLI wiring, checkpoint/cache behavior, and offline tests. A controlled live probe should be performed before any recurring workflow is created. A recurring schedule remains a separate production gate.
