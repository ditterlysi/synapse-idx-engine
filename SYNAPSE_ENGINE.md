# Synapse IDX Engine

**Status:** pipeline wiring complete / manual live E2E gated  
**Version:** 0.16.0  
**Product owner:** Synapse

This repository is the separate Python runtime for **Synapse IDX Disclosure Intelligence**.

The Python package remains `idx_digest` during the transition to avoid a risky all-at-once module rename. The project/distribution identity is `synapse-idx-engine`.

## Repository boundary

### This repository owns

- disclosure metadata collection;
- incremental coverage logic;
- attachment selection/download;
- PDF/XLSX/DOCX/HTML extraction;
- OCR fallback;
- exact/near duplicate suppression;
- structured AI analysis;
- conservative daily-run budgets;
- engine observability/recovery;
- authenticated calls to the Synapse internal IDX API.

### Main `synapse` repository owns

- user auth;
- Supabase product database;
- portfolio/watchlist source of truth;
- P0–P4 relevance calculation;
- normalized disclosure feed/detail APIs;
- read/bookmark state;
- desktop/PWA UI;
- notifications later.

The engine must **not** receive a Supabase service-role key. It writes through the narrow internal API protected by `SYNAPSE_INGESTION_SECRET`.

## Priority contract

Synapse returns issuer relevance to this engine:

```text
P0 current open portfolio position (remaining qty > 0)
P1 watchlist, when not P0
P2 material non-watchlist disclosure
P3 normal disclosure
P4 routine / administrative
```

Portfolio status is decided by Synapse. The engine must not infer portfolio ownership from historical BUY presence.

## Conservative scheduled policy

Scheduled Synapse mode remains intentionally disabled until controlled live E2E approval.

The gated Synapse runner enforces:

```text
HTTP-only source transport
incremental collection only
no automatic historical backfill
no per-ticker mass fan-out
no browser fallback
no wide-page rescue
hard source-request cap
hard attachment cap
hard download-byte cap
hard AI-document cap
hard run-duration cap
stop immediately on HTTP 429
stop immediately on HTTP 403/access protection
no CAPTCHA bypass
no proxy/IP rotation
```

Generic/manual research commands remain separate and keep their existing behavior.

## Current milestone

The engine contains the offline-verified wiring layer between the existing local pipeline/cache and the typed Synapse Internal API.

Implemented:

- Synapse project identity;
- daily policy and budget primitives;
- strict relevance and ingestion contracts;
- narrow authenticated Synapse API client;
- conservative HTTP-only runtime wrappers;
- existing pipeline dependency injection without changing generic/manual behavior;
- run creation/update publishing;
- disclosure metadata upsert;
- file/extraction-state upsert;
- compatibility mapping from legacy `announcement-v3` summaries into the Synapse taxonomy;
- deterministic compatibility materiality mapping;
- deliberately `UNCLEAR` directional impact until native Synapse analysis provides explicit impact reasoning;
- PARTIAL fallback when validated analysis is missing or publishing fails;
- coverage commit only after a clean local run, clean publish, and proven requested-window coverage;
- compensation back to PARTIAL when the final coverage commit fails;
- thread-safe conservative budget counters;
- offline end-to-end tests using temporary SQLite plus fake pipeline/API clients;
- `synapse-idx-engine doctor` command;
- `synapse-idx-engine api-check` command;
- bounded manual `synapse-idx-engine e2e` command for the controlled live gate.

Not enabled yet:

- scheduled daily CLI execution;
- GitHub Actions schedule;
- automatic historical backfill.

No controlled live IDX E2E should be considered approved until the manual command has completed successfully and the resulting Synapse records have been verified.

## Manual live E2E gate

The `e2e` command is intentionally stricter than future scheduled defaults. It requires:

- `SYNAPSE_DAILY_ENABLED=true` as an explicit live-run kill switch;
- `SYNAPSE_INTERNAL_BASE_URL`;
- `SYNAPSE_INGESTION_SECRET`;
- `OPENROUTER_API_KEY`;
- `--confirm-live-idx`;
- explicit ISO timestamps with timezone offset or `Z`;
- a window of at most two hours;
- start/end within one Asia/Jakarta calendar date.

The command additionally clamps the run to at most 12 source requests, 20 attachments, 100 MB downloaded, 20 AI documents, 15 minutes, and concurrency 2. It never creates or enables a schedule.

Example shape only — never commit secret values:

```bash
synapse-idx-engine api-check -t BBRI
synapse-idx-engine e2e \
  --start "2026-08-21T20:00:00+07:00" \
  --end "2026-08-21T21:00:00+07:00" \
  --confirm-live-idx
```

## Next gates

1. merge the bounded manual-E2E command after CI is fully green;
2. configure the existing `SYNAPSE_INGESTION_SECRET` in the local/runtime environment without exposing it in Git or chat;
3. run `api-check` to confirm the authenticated boundary without contacting IDX;
4. run one controlled two-hour-or-less live E2E window;
5. verify run/disclosure/file/analysis/coverage state in Synapse and clean up any synthetic test data if used;
6. repeat controlled E2E until stable;
7. expose the gated scheduled daily execution;
8. only after repeated successful live runs add the ~03:00 WIB GitHub Actions schedule.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
synapse-idx-engine doctor
```

`doctor` does not contact IDX or Synapse.