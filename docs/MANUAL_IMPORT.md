# Manual Disclosure Import

`manual-import` is the first executable source-neutral ingestion path for Synapse IDX Disclosure Intelligence. It exists so the downstream extraction, AI, taxonomy mapping, and Synapse publishing flow can be exercised without crawling the IDX website or using any automated website-internal endpoint.

## What it does

Given a local `synapse-source-manifest-v1` bundle, the command:

1. reads and validates the manifest through `ManualManifestSource`;
2. resolves only local attachment paths beneath the manifest directory;
3. verifies attachment existence and SHA-256 where supplied;
4. stages the local bytes without downloading them again;
5. extracts PDF/XLSX/DOCX/HTML/text through the existing extractor stack;
6. runs the existing document + announcement summaries through the configured AI provider;
7. maps the validated `announcement-v3` summary through the conservative Synapse compatibility taxonomy;
8. publishes disclosure metadata, optional source-backed file metadata, and structured analysis through the authenticated Synapse Internal API;
9. leaves automated IDX website collection and scheduled daily execution disabled.

The engine still never receives a Supabase service-role credential.

## AI provider

The source-neutral manual path now supports two analysis transports:

- `AI_PROVIDER=gemini` — direct Gemini Developer API. This is the recommended controlled-E2E path.
- `AI_PROVIDER=openrouter` — existing compatibility path through OpenRouter.

The direct Gemini adapter deliberately reuses the existing Synapse prompt rendering, retry policy, audit persistence, concurrency gates, and local JSON-schema validation. Only the upstream HTTP request/response shape changes.

For the controlled free-tier path, use the stable `gemini-3.5-flash-lite` model. It supports structured outputs and is suited to document parsing/data extraction.

## Required configuration

Recommended direct Gemini configuration:

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=<your Gemini Developer API key>
GEMINI_MODEL=gemini-3.5-flash-lite
SYNAPSE_INTERNAL_BASE_URL=https://<your-synapse-worker-origin>
SYNAPSE_INGESTION_SECRET=<same engine secret configured on Synapse>
```

OpenRouter remains available when explicitly selected:

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=<your OpenRouter key>
```

`SYNAPSE_DAILY_ENABLED` does **not** need to be enabled for a manual import. Manual import is an explicit user-triggered offline-source flow, not the scheduled source collector.

Do not commit secrets or a populated `.env` file.

## Manifest requirements

See [`SOURCE_ADAPTERS.md`](./SOURCE_ADAPTERS.md) for the complete `synapse-source-manifest-v1` schema.

Additional manual-import rule:

```text
externalId must start with manual-
```

Examples:

```text
manual-bbri-20260821-001
manual-antm-capex-20260821
```

This namespace guard reduces the risk that a development/manual record collides with a future authoritative source's globally unique disclosure ID.

Attachment paths must remain relative to the manifest directory. Absolute paths and path traversal are rejected by the adapter.

## Run a bounded import

The window must use explicit timezone-aware ISO timestamps, remain within one Asia/Jakarta calendar date, and be no longer than two hours.

Example:

```bash
synapse-idx-engine manual-import \
  --manifest ./import-bundle/manifest.json \
  --start '2026-08-21T09:00:00+07:00' \
  --end '2026-08-21T11:00:00+07:00' \
  --confirm-publish
```

`--confirm-publish` is required because the command can call the configured AI provider and write disclosure/analysis data to the configured Synapse environment.

## Expected status semantics

A normal successful manual import reports approximately:

```json
{
  "ok": true,
  "status": "PARTIAL",
  "processingOk": true,
  "sourceId": "manual-manifest",
  "sourceComplete": false,
  "coverageCommitted": false,
  "coverageAuthoritative": false,
  "scheduleEnabled": false
}
```

`PARTIAL` is intentional here. It does **not** mean extraction or AI necessarily failed. It means the manual source is not authoritative proof that every disclosure in the requested production window was collected.

The CLI constructs `ManualManifestSource(..., allow_complete_attestation=False)` and `SourceIngestionRunner(..., allow_coverage_commit=False)`. Therefore a normal manual import cannot advance production ingestion coverage even if a manifest claims `coverage.complete=true`.

Controlled unit/fixture tests may explicitly enable completeness attestation and coverage authorization to verify the generic runner's COMPLETE path. The production manual CLI does not expose those switches.

## Attachment provenance

A local attachment does not need an HTTP source URL in order to be extracted and analyzed.

If an attachment has no valid `sourceUrl`:

- the local bytes are still extracted and analyzed;
- no fake HTTP URL is invented;
- no `idx_disclosure_files` row is published for that attachment, because the Synapse file contract requires an HTTP(S) source URL;
- the compatibility analysis can still be committed because legacy claim mapping intentionally does not fabricate `sourceFileId` provenance.

If a real HTTP(S) `sourceUrl` is supplied, the runner publishes file metadata with the computed file hash, extracted-text hash, content type, size, and extraction method. Local filesystem paths are never sent to Synapse.

## Resource limits

Manual import reuses the same conservative budget primitives and the stricter bounded-E2E caps:

```text
max 20 staged attachments
max 100 MB staged bytes
max 20 AI documents
max 15 minutes
LLM concurrency <= 2
extraction workers <= 2
```

The offline manifest itself performs zero source-network requests.

## Compliance boundary

This command does not change the current source-policy hold:

- automated IDX website collection remains disabled;
- no browser fallback is enabled for scheduled collection;
- no CAPTCHA bypass;
- no proxy/IP rotation;
- no 403/429 evasion;
- no per-ticker mass fan-out;
- no GitHub Actions daily schedule is enabled.

A future approved/licensed provider should implement `DisclosureSource` and stage normalized attachments into the same runner. Only a source that provides authoritative completeness evidence, plus an explicitly coverage-authorized runner, may commit production coverage.
