# Synapse IDX Engine

**Status:** foundation / integration in progress  
**Version:** 0.16.0  
**Product owner:** Synapse

This repository is being evolved from the original IDX Disclosure Digest prototype into the separate Python runtime for **Synapse IDX Disclosure Intelligence**.

The Python package remains `idx_digest` during the transition to avoid a risky all-at-once module rename. The project/distribution identity is now `synapse-idx-engine`.

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

The engine must **not** receive a Supabase service-role key. It writes through a narrow internal API protected by `SYNAPSE_INGESTION_SECRET`.

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

Scheduled Synapse mode is intentionally disabled by default.

When enabled after E2E approval it must enforce:

```text
HTTP-only source transport
incremental collection only
no automatic historical backfill
no per-ticker mass fan-out
hard source-request cap
hard attachment cap
hard download-byte cap
hard AI-document cap
hard run-duration cap
stop/backoff on rate limiting
stop on access protection; no CAPTCHA bypass
no proxy/IP rotation
```

Generic/manual research commands remain separate from scheduled Synapse mode.

## Current milestone

Foundation includes:

- Synapse project identity;
- daily policy and budget primitives;
- strict relevance contract;
- narrow authenticated Synapse API client;
- `synapse-idx-engine doctor` command;
- CI/tests for guardrails.

Live daily collection is **not wired yet**. The intended order is:

1. merge this foundation;
2. implement `/api/internal/idx/*` in main Synapse;
3. verify auth + relevance contract;
4. add engine metadata upsert/run/coverage client methods;
5. manual narrow-range E2E;
6. only then wire daily command;
7. only after repeated successful E2E enable GitHub Actions schedule.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
synapse-idx-engine doctor
```

`doctor` does not contact IDX or Synapse.

## Rename repository

After this branch is stable, the GitHub repository can be renamed from:

```text
IDX_scraper
```

to:

```text
synapse-idx-engine
```

GitHub keeps repository history and redirects the old repository URL after a rename. Local clones should still update `origin` afterward for clarity.
