# IDX Disclosure Digest

IDX Disclosure Digest collects IDX disclosure metadata and attachments, extracts their text, and builds isolated per-issuer research summaries. It includes a CLI, a local browser-based workspace, durable SQLite checkpoints, recovery tools, shareable exports, a source-neutral Synapse ingestion path, and a production-tested guarded IDX website collector.

## Current production status — 2026-08-22

The guarded Synapse website collector has completed its Phase A/B/C rollout and is active behind repository/runtime kill switches.

Production baseline:

- public IDX website metadata discovery works through the guarded HTTP-only adapter;
- official attachments download sequentially;
- native extraction + Tesseract OCR fallback are available on GitHub-hosted runners;
- structured Gemini analysis publishes into Synapse;
- durable source checkpoint is stored through Synapse internal APIs;
- checkpoint only advances after successful processing;
- two controlled production daily runs passed processing and idempotency gates;
- scheduled target is 20:00 UTC / 03:00 Asia/Jakarta;
- website collection remains intentionally non-authoritative for complete market coverage.

Engine release history for this production path:

- PR #20 — automated IDX collector MVP;
- PR #21 — durable collector reliability;
- PR #22 — guarded daily scheduler;
- PR #23 — production hardening (`set -o pipefail` and Tesseract system dependencies).

## What it does

- Queries IDX announcements for exact Jakarta-time intervals in legacy/manual research tooling.
- Supports all listed issuers or one ticker in the legacy research path.
- Downloads PDF, XLSX, DOCX, HTML, and text attachments.
- Extracts native text first and uses Indonesian/English OCR for sparse PDF pages.
- Selects primary financial-statement sources and records keep/skip decisions.
- Deduplicates announcements, attachments, and already-completed model work.
- Produces document, announcement, and company-window summaries without mixing issuers.
- Saves files, summaries, prompts, audits, progress, and recovery state locally for the legacy research workflow.
- Provides a source-neutral `DisclosureSource` ingestion boundary for Synapse.
- Provides a guarded HTTP-only `idx-website` source with durable Synapse-backed checkpoints and non-authoritative coverage semantics.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m playwright install chromium
cp .env.example .env
python verify_install.py
idx-digest gui
```

Add `OPENROUTER_API_KEY` to `.env` before using the legacy OpenRouter-backed research summaries. OCR also requires Tesseract and the Indonesian language pack.

See [INSTALLATION.md](INSTALLATION.md) for full installation and troubleshooting instructions.

## Synapse source-neutral ingestion

`manual-import` exercises the source-neutral Synapse path without contacting IDX for source collection:

```text
local/approved source adapter
        ↓
extraction
        ↓
configured AI provider
        ↓
strict schema validation
        ↓
Synapse Internal API
```

AI selection is handled by `src/idx_digest/ai_provider.py`:

```env
# Direct Gemini
AI_PROVIDER=gemini
GEMINI_API_KEY=...
GEMINI_MODEL=gemini-3.5-flash-lite

# Or OpenRouter compatibility
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
OPENROUTER_PROVIDER=deepinfra
```

Only the selected provider's API key is required. See [docs/MANUAL_IMPORT.md](docs/MANUAL_IMPORT.md) and [docs/SOURCE_ADAPTERS.md](docs/SOURCE_ADAPTERS.md).

## Guarded IDX website collector

The production website adapter uses the public `ListedCompany/GetAnnouncement` path and official IDX attachment hosts through an HTTP-only client. It fails closed on access protection, does not use browser/proxy/CAPTCHA bypasses, never claims authoritative coverage, and commits its durable checkpoint only after successful processing.

Manual bounded run:

```bash
synapse-idx-website collect \
  --start '2026-08-22T18:00:00+07:00' \
  --end '2026-08-22T20:00:00+07:00' \
  --enable-source \
  --confirm-publish
```

Read-only health check with no IDX or AI request:

```bash
synapse-idx-website health
```

Scheduled iteration:

```bash
SYNAPSE_DAILY_ENABLED=true synapse-idx-website daily --confirm-schedule
```

`.github/workflows/daily.yml` targets `20:00 UTC` / `03:00 Asia/Jakarta`. The production job is skipped unless repository variable `IDX_DAILY_ENABLED` is exactly `true`.

### Production validation evidence

Successful processing run:

```text
runId: 401b3277-12f7-4962-a620-9b01d94000c1
ok: true
processingOk: true
sourceRequests: 9
checkpointCommitted: true
filesExtracted: 7
analysesCompleted: 4
partialDisclosures: 0
```

Immediate overlapping idempotency run:

```text
runId: cc0ce9cd-1b82-45ed-abdc-568b929bdd7e
ok: true
sourceRequests: 2
checkpointCommitted: true
disclosuresAvailable: 0
attachmentsStaged: 0
documentsAnalyzed: 0
analysesCompleted: 0
```

This proves the durable checkpoint prevents attachment/extraction/AI replay for already-processed disclosures.

### Expected `PARTIAL` run status

The website source intentionally reports `sourceComplete=false` because it does not claim authoritative market-wide coverage. A successful run can therefore persist:

```text
status = PARTIAL
error_code = SOURCE_COVERAGE_UNPROVEN
processingOk = true
```

This is expected coverage semantics, not a processing failure. Real processing failure is represented separately, for example `SOURCE_PROCESSING_PARTIAL` / `processingOk=false`.

## Production incidents and fixes

- **Masked shell failure:** `... | tee ...` originally hid collector exit code. Fixed with `set -o pipefail`.
- **Missing Tesseract:** GitHub-hosted runner initially failed OCR. Workflow now installs Tesseract plus English and Indonesian language packs.
- **Synapse source-state 404:** production Synapse had a stale deployment artifact. Rebuilding/deploying current Synapse `main` restored the internal endpoint.
- **Failure issue alert:** workflow contains a GitHub Issue alert step, but repository Issues are currently disabled. Do not rely on Issue creation as the alert channel. Collector correctness/checkpoint safety is independent of this step.

See [docs/IDX_WEBSITE_COLLECTOR.md](docs/IDX_WEBSITE_COLLECTOR.md) for rollout, recovery, and operational details.

## Common legacy research commands

Run one issuer:

```bash
idx-digest run \
  --start '2026-08-05T21:00:00+07:00' \
  --end '2026-08-05T23:59:59+07:00' \
  --ticker ANTM
```

Run all listed issuers for whole dates:

```bash
idx-digest run --start 2026-08-01 --end 2026-08-05
```

Exercise download/extraction without model cost:

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --skip-llm \
  --max-announcements 2
```

Open the local workspace:

```bash
idx-digest gui --no-open-browser
```

Recover committed summaries without contacting IDX/provider:

```bash
idx-digest recover --start 2026-08-05 --end 2026-08-05
```

Use `idx-digest --help` and `idx-digest COMMAND --help` for all options.

## Configuration

The checked-in `.env.example` documents settings. Never commit real credentials.

The local GUI binds to `127.0.0.1` by default and has no authentication. Do not expose it publicly.

Legacy/manual research transport may use a visible persistent Chromium session for protected source access. It does not bypass CAPTCHAs. This browser behavior is **not enabled for scheduled Synapse ingestion**.

## Output

```text
data/
├── idx_digest.sqlite3
├── last_run.json
├── prompts.json
├── browser-profile/
├── logs/
├── traces/
├── runs/<RUN_ID>/
├── raw/<TICKER>/<ANNOUNCEMENT_ID>/
├── text/<TICKER>/<SHA256>.txt
├── share/
└── companies/<TICKER>/
```

Treat `data/` as private research material.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python verify_install.py
```

The test suite is offline by default and should not incur model-provider cost.

## Operational cautions

- Keep the scheduled collector behind both `IDX_DAILY_ENABLED=true` and runtime `SYNAPSE_DAILY_ENABLED=true`.
- Never use browser/CAPTCHA/proxy/rate-limit bypasses for scheduled Synapse collection.
- Keep historical backfill and per-ticker fanout disabled in scheduled mode.
- Never commit `.env`, `data/`, cookies, or browser profile state.
- Treat extracted attachment content as untrusted input.
- Website source coverage is non-authoritative; successful processing must not be confused with authoritative coverage.
- Failed/processing-partial runs must never advance the durable checkpoint.
- Observe the first natural 03:00 WIB cron after release and establish a reliable failure-alert channel.