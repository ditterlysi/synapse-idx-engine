# Changelog

All notable changes and migration requirements are consolidated here. Upgrades preserve `.env`, SQLite data, downloaded attachments, extracted text, prompt profiles, run history, and the browser profile unless an entry explicitly says otherwise.

Use [INSTALLATION.md](INSTALLATION.md) for the current upgrade procedure. Historical archive/patch extraction commands have been removed because they no longer describe the current source tree.

## 0.16.0

### Synapse source-neutral integration

- Added the authenticated Synapse Internal API client and conservative publishing boundary.
- Added source-neutral `DisclosureSource` contracts with explicit completeness evidence.
- Added the offline `synapse-source-manifest-v1` adapter with local path/hash safeguards.
- Added the source-neutral local-staging ingestion runner that reuses the existing extraction, OpenRouter announcement-v3 analysis, conservative Synapse taxonomy mapper, and Internal API publishing path.
- Added guarded `synapse-idx-engine manual-import` for controlled offline-source end-to-end development.
- Manual imports require explicit publish confirmation, `manual-` external-ID namespacing, and bounded two-hour windows.
- Manual imports remain non-authoritative for production coverage by default; successful processing can therefore report `processingOk=true` while the ingestion run remains `PARTIAL` and `coverageCommitted=false`.
- Local attachment paths are never published. File metadata is sent only when a real HTTP(S) source URL exists; no URL is fabricated.
- Reused conservative attachment, byte, AI-document, and runtime budgets for manual source processing.
- Automated IDX public-website/internal-endpoint collection remains disabled under the source-compliance hold. No browser/CAPTCHA/proxy/rate-limit bypass or schedule was enabled.
- Added offline runner and CLI regression tests plus operational documentation in `docs/MANUAL_IMPORT.md`.

## 0.15.5

### Coverage-aware incremental runs

- Replaced end-only no-op logic with normalized, scope-specific metadata coverage ranges.
- Subtracts known coverage from the exact requested interval and queries every missing gap.
- Supports backward, forward, and disjoint gaps.
- Stops new coverage at the poll-start snapshot.
- Imports only completed historical reports that prove both boundaries.
- Rejects false no-op, partial, filtered, capped, reducer, and refiner reports as coverage evidence.
- Keeps the legacy watermark for compatibility without treating it as proof of an unknown start.
- Leaves stock-master fanout disabled for normal incremental gaps; historical audit retains it.

Database migration is additive. Existing archives and summaries remain intact. Model, prompt, schema, and output ceilings are unchanged.

## 0.15.4

### Incremental boundary and profile deletion

- Distinguished the last successful poll boundary from the latest announcement timestamp.
- Added adaptive wide-page recovery up to a configured maximum.
- Added permanent deletion for non-Main isolated profiles, blocked during active work.
- Clarified that a blank ticker means all companies.

The Main profile cannot be deleted. This release's end-only no-op behavior was superseded by 0.15.5 coverage ranges.

## 0.15.3

### Incremental watermark mode

- Made forward-moving incremental retrieval the normal mode.
- Added a configurable overlap for recent disclosures.
- Reserved expensive per-ticker completeness reconstruction for historical audit.
- Prevented failed or partial runs from advancing trusted progress.

The watermark remains as compatibility and diagnostic state after 0.15.5.

## 0.15.2

### Fast completeness recovery

- Added a verified wide-page probe for inconsistent IDX offset pagination.
- Avoided unreliable intra-day splitting because the endpoint accepts calendar-day request bounds.
- Retained paced per-ticker reconstruction as historical last-resort recovery.

## 0.15.1

### IDX throttling hotfix

- Separated HTTP 429 handling from browser-verification handling.
- Honored `Retry-After` where supplied and added bounded cooldown with jitter.
- Paced historical per-ticker recovery with burst rests.
- Updated installation verification to check required prompt names rather than a fixed count.

## 0.15.0

### Incremental intelligence and recovery

- Added pagination completeness guards and partial-run reporting.
- Added fingerprinted company-window caches and dirty-company scope.
- Added deterministic single-announcement promotion.
- Streamed company checkpoints progressively to the GUI.
- Added phase-aware ETA and network recovery watchdog behavior.
- Added concurrency presets without changing generation ceilings.
- Distinguished active-run settings from next-run settings.
- Added Public Expose specialist analysis, financial-sheet ranking, routine ownership audit detail, and stock-master guardrails.

Database migrations are additive. No data reset is required.

## 0.14.4

### Financial fairness hotfix

- Recognized standalone `LK` statement PDFs as primary evidence.
