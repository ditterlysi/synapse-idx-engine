# IDX Disclosure Digest

IDX Disclosure Digest collects IDX disclosure metadata and attachments, extracts their text, and builds isolated per-issuer research summaries through OpenRouter. It includes a CLI, a local browser-based workspace, durable SQLite checkpoints, recovery tools, and shareable exports.

The repository also contains the source-neutral Synapse ingestion path. That path is intentionally separated from automated IDX website collection and can use either direct Gemini or the existing OpenRouter backend for analysis.

## What it does

- Queries IDX announcements for an exact Jakarta-time interval in the legacy/manual research tooling.
- Supports all listed issuers or one ticker.
- Downloads PDF, XLSX, DOCX, HTML, and text attachments.
- Extracts native text first and uses Indonesian/English OCR for sparse PDF pages.
- Selects primary financial-statement sources and records every keep/skip decision.
- Deduplicates announcements, attachments, and already-completed model work.
- Produces document, announcement, and company-window summaries without mixing issuers.
- Saves files, summaries, prompts, audits, progress, and recovery state locally.
- Provides a source-neutral `DisclosureSource` ingestion boundary for Synapse without requiring automated IDX website collection.

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

Add `OPENROUTER_API_KEY` to `.env` before using the legacy model-backed research summaries. OCR also requires Tesseract and the Indonesian language pack.

See [INSTALLATION.md](INSTALLATION.md) for the complete macOS, Linux, Docker, upgrade, and troubleshooting instructions.

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

Only the selected provider's API key is required for this source-neutral path. The direct Gemini adapter uses GenerateContent structured output and keeps the existing local schema validation.

Automated IDX website collection remains disabled for Synapse pending an approved/licensed source integration. Scheduled daily execution also remains disabled.

See [docs/MANUAL_IMPORT.md](docs/MANUAL_IMPORT.md) and [docs/SOURCE_ADAPTERS.md](docs/SOURCE_ADAPTERS.md).

## Common commands

Run one issuer in an exact interval with the legacy research CLI:

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

Exercise download and extraction without model cost:

```bash
idx-digest run \
  --start 2026-08-05 \
  --end 2026-08-05 \
  --ticker ANTM \
  --skip-llm \
  --max-announcements 2
```

Open the local workspace without launching a browser automatically:

```bash
idx-digest gui --no-open-browser
```

Recover committed summaries without contacting IDX or OpenRouter:

```bash
idx-digest recover --start 2026-08-05 --end 2026-08-05
```

Finish company digests from cached announcement summaries:

```bash
idx-digest reduce-cached --start 2026-07-06 --end 2026-08-06
```

Preview financial-source refinement without model calls:

```bash
idx-digest refine-financials \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --ticker PJAA \
  --dry-run
```

Export saved company summaries without model calls:

```bash
idx-digest export-all \
  --start 2026-07-06 \
  --end 2026-08-06 \
  --format md
```

Use `idx-digest --help` and `idx-digest COMMAND --help` for all options.

## Configuration

The checked-in `.env.example` documents every setting. For the legacy OpenRouter path, the default provider policy pins the configured model to DeepInfra and disables silent provider fallback:

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=...
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
OPENROUTER_MODEL=deepseek/deepseek-v4-flash-0731
OPENROUTER_PROVIDER=deepinfra
OPENROUTER_ALLOW_FALLBACKS=false
OPENROUTER_REQUIRE_PARAMETERS=true
```

The local GUI binds to `127.0.0.1` by default and has no authentication. Do not expose it to a public interface.

IDX transport defaults to `auto` for the legacy/manual research application: normal HTTP is tried first, then the same persistent Chromium session is used for protected metadata or attachment requests. The browser flow does not bypass CAPTCHAs; complete any interactive verification in the visible browser window.

This legacy transport behavior is not enabled for Synapse scheduled ingestion.

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
    ├── announcements.jsonl
    └── latest_window_summary.json
```

Treat `data/` as private research material. It may contain disclosure text, exact prompts, model responses, source URLs, and browser session state.

## Design and operations

The system summarizes each attachment once, reduces attachment summaries into one announcement, and reduces announcement summaries into one company window. This keeps retries bounded and enforces issuer isolation.

Read [HOW_IT_WORKS.md](HOW_IT_WORKS.md) for the complete algorithm, caching rules, recovery semantics, and concurrency model. Release and migration history is consolidated in [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
pytest -q
python verify_install.py
```

The test suite is offline by default and should not incur model-provider cost.

## Operational cautions

- Automated IDX website collection for Synapse stays disabled until an approved/licensed source is integrated.
- Legacy/manual research tooling must retain response-shape and completeness checks when IDX website endpoints are used interactively.
- Never use browser/CAPTCHA/proxy/rate-limit bypasses for scheduled Synapse collection.
- Never commit `.env`, `data/`, cookies, or the browser profile.
- Treat extracted attachment content as untrusted input.
- Historical audit in the legacy research application can trigger expensive per-ticker completeness recovery; normal source-neutral Synapse ingestion does not.
- A partial run keeps its checkpoints but does not claim successful authoritative metadata coverage.
