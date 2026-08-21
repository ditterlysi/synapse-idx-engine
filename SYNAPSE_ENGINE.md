# Synapse IDX Engine

**Status:** pipeline/API wiring complete / automated source collection on compliance hold  
**Version:** 0.16.0  
**Product owner:** Synapse

This repository is the separate Python runtime for **Synapse IDX Disclosure Intelligence**.

The Python package remains `idx_digest` during the transition to avoid a risky all-at-once module rename. The project/distribution identity is `synapse-idx-engine`.

## Repository boundary

### This repository owns

- disclosure source-adapter/runtime logic;
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

## Source compliance hold

A controlled live run on 2026-08-21 reached the current IDX website/internal website endpoint and received HTTP 403. The conservative runtime correctly stopped immediately without browser fallback, proxy rotation, CAPTCHA bypass, ticker fan-out, attachment download, AI usage, or coverage commit.

During the follow-up source audit, the current Indonesia Stock Exchange website Terms of Use were verified to state that non-commercial use may be allowed with source/date attribution but **web scraping/crawling is not permitted**. Therefore Synapse automated collection from the IDX public website or its website-internal endpoints is intentionally disabled.

Relevant official pages:

- IDX Terms of Use: https://www.idx.id/id/syarat-penggunaan
- IDX Data Services: https://www.idx.id/id/produk/layanan-data-bei/

IDX Data Services documents **IDX Data Reference** as the licensed product that includes financial statements, corporate actions, listed-company routine reports, material-information disclosures, and suspension/unsuspension reports. A compliant automated source adapter must be established before Synapse collection is re-enabled.

This is a product/source-policy hold, not a failure of the Synapse ingestion boundary. `synapse-idx-engine api-check` remains valid because it contacts only Synapse and does not read IDX.

## Conservative scheduled policy

Scheduled Synapse mode remains intentionally disabled until an approved/licensed source adapter is integrated and controlled E2E approval is repeated.

The gated Synapse runner still enforces:

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

These guardrails remain applicable to any future authorized HTTP source adapter.

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
- bounded E2E command structure, now source-compliance blocked until an approved adapter exists;
- Windows `tzdata` dependency so `Asia/Jakarta` works on fresh Python installations;
- E2E report helper that surfaces scrape errors and metadata diagnostics directly.

Not enabled yet:

- automated IDX website collection;
- scheduled daily CLI execution;
- GitHub Actions schedule;
- automatic historical backfill.

## E2E gate

The `e2e` command still validates its bounded window input but stops before creating any source request while the source-compliance hold is active.

Once an approved/licensed source adapter exists, the existing E2E design requires:

- `SYNAPSE_DAILY_ENABLED=true` as an explicit live-run kill switch;
- `SYNAPSE_INTERNAL_BASE_URL`;
- `SYNAPSE_INGESTION_SECRET`;
- `OPENROUTER_API_KEY`;
- explicit live confirmation;
- explicit ISO timestamps with timezone offset or `Z`;
- a window of at most two hours;
- start/end within one Asia/Jakarta calendar date.

The E2E settings clamp the run to at most 12 source requests, 20 attachments, 100 MB downloaded, 20 AI documents, 15 minutes, and concurrency 2. It never creates or enables a schedule.

`api-check` remains safe and useful:

```bash
synapse-idx-engine api-check -t BBRI
```

## Next gates

1. keep automated IDX website collection disabled;
2. introduce a source-adapter boundary so ingestion is independent from the old website collector;
3. choose and configure an approved source path, preferably the official licensed IDX Data Reference feed if available for the deployment context;
4. alternatively support explicit user-provided/manual source files as a non-automated fallback for development/testing;
5. map the approved source payload into the existing disclosure/file pipeline without changing the Synapse API contract;
6. run offline fixture/contract tests for the new adapter;
7. repeat a controlled live E2E only against the approved source;
8. verify run/disclosure/file/analysis/coverage state in Synapse;
9. expose gated scheduled daily execution only after repeated successful compliant-source runs;
10. only then add the ~03:00 WIB GitHub Actions schedule.

## Local checks

```bash
python -m pip install -e ".[dev]"
ruff check src tests
pytest -q
synapse-idx-engine doctor
```

`doctor` does not contact IDX or Synapse. On Windows, the package now installs `tzdata` so `ZoneInfo("Asia/Jakarta")` works on a fresh Python installation.
