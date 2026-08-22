# Manual Disclosure Import

`manual-import` is the first executable source-neutral ingestion path for Synapse IDX Disclosure Intelligence. It exists so the downstream extraction, AI, taxonomy mapping, and Synapse publishing flow can be exercised without crawling the IDX website or using any automated website-internal endpoint.

## What it does

Given a local `synapse-source-manifest-v1` bundle, the command:

1. reads and validates the manifest through `ManualManifestSource`;
2. resolves only local attachment paths beneath the manifest directory;
3. verifies attachment existence and SHA-256 where supplied;
4. stages the local bytes without downloading them again;
5. extracts PDF/XLSX/DOCX/HTML/text through the existing extractor stack;
6. runs document + announcement summaries through the configured AI provider;
7. maps the validated `announcement-v3` summary through the conservative Synapse compatibility taxonomy;
8. publishes disclosure metadata, optional source-backed file metadata, and structured analysis through the authenticated Synapse Internal API;
9. leaves automated IDX website collection and scheduled daily execution disabled.

The engine still never receives a Supabase service-role credential.

## AI provider abstraction

The source-neutral path resolves its analysis backend through `ai_provider.py` rather than selecting a provider inside the CLI or mutating shared settings.

Supported backends:

- `AI_PROVIDER=gemini` — direct Gemini Developer API through `GeminiSummarizer`;
- `AI_PROVIDER=openrouter` — the existing OpenRouter compatibility backend.

Provider resolution returns an isolated runtime settings copy plus the summarizer factory and provenance identity. This preserves the current `SourceIngestionRunner` compatibility contract while ensuring Synapse records the provider/model that actually executed the analysis.

The direct Gemini adapter deliberately reuses the existing prompt rendering, retry policy, audit persistence, concurrency gates, and local JSON-schema validation. Only the upstream HTTP request/response shape changes. Gemini structured output is sent through GenerateContent using `responseMimeType=application/json` and `responseJsonSchema`.

The controlled production E2E was verified with `gemini-3.5-flash-lite`, including a READY disclosure, VALID analysis, and provenance `google-gemini / gemini-3.5-flash-lite`.

## Required configuration

Direct Gemini:

```env
AI_PROVIDER=gemini
GEMINI_API_KEY=<your Gemini Developer API key>
GEMINI_MODEL=gemini-3.5-flash-lite
SYNAPSE_INTERNAL_BASE_URL=https://<your-synapse-worker-origin>
SYNAPSE_INGESTION_SECRET=<same engine secret configured on Synapse>
```

OpenRouter compatibility backend:

```env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=<your OpenRouter key>
OPENROUTER_MODEL=<configured model>
OPENROUTER_PROVIDER=<pinned provider>
SYNAPSE_INTERNAL_BASE_URL=https://<your-synapse-worker-origin>
SYNAPSE_INGESTION_SECRET=<same engine secret configured on Synapse>
```

Only the selected provider's API key is required. `SYNAPSE_DAILY_ENABLED` does **not** need to be enabled for a manual import. Manual import is an explicit user-triggered offline-source flow, not the scheduled source collector.

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

This provider work does not change the current source-policy hold:

- automated IDX website collection remains disabled;
- no browser fallback is enabled for scheduled collection;
- no CAPTCHA bypass;
- no proxy/IP rotation;
- no 403/429 evasion;
- no per-ticker mass fan-out;
- no GitHub Actions daily schedule is enabled.

The existing website-coupled live E2E pipeline remains dormant and OpenRouter-only while it is under compliance hold. A future approved/licensed provider should implement `DisclosureSource` and stage normalized attachments into the source-neutral runner. Only a source that provides authoritative completeness evidence, plus an explicitly coverage-authorized runner, may commit production coverage.
