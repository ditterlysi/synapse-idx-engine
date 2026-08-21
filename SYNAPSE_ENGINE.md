# Synapse IDX Engine

**Status:** pipeline wiring complete / activation gated  
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

The engine now contains the offline-verified wiring layer between the existing local pipeline/cache and the typed Synapse Internal API.

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
- offline end-to-end tests using temporary SQLite plus fake pipeline/API clients;
- `synapse-idx-engine doctor` command.

Not enabled yet:

- live IDX execution through the Synapse runner;
- production engine writes to Synapse;
- daily CLI activation;
- GitHub Actions schedule;
- automatic historical backfill.

## Next gates

1. merge the offline pipeline-wiring PR after CI is fully green;
2. configure the existing `SYNAPSE_INGESTION_SECRET` in the engine runtime without exposing it in Git or chat;
3. run one controlled narrow-range live E2E against the already-verified Synapse Internal API;
4. verify run/disclosure/file/analysis/coverage state in Synapse and clean up any synthetic test data;
5. repeat controlled E2E until stable;
6. expose the gated daily command;
7. only after repeated successful live runs add the ~03:00 WIB GitHub Actions schedule.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
synapse-idx-engine doctor
```

`doctor` does not contact IDX or Synapse.
