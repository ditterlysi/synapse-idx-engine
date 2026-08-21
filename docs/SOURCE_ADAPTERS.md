# Disclosure Source Adapters

Synapse ingestion is source-agnostic. Upstream providers must normalize their data into the contract in `idx_digest.source_contract` before any downstream extraction, AI analysis, or Synapse publishing is allowed.

## Safety rules

1. A source must never be treated as complete merely because it returned zero or a finite number of rows.
2. `SourceWindowResult.complete=True` is valid only when explicit coverage evidence spans the entire requested window.
3. The manual manifest adapter is offline-only and never performs network requests.
4. Manual attachment paths must be relative to the manifest directory and may not escape it, including through path traversal or resolved symlinks.
5. Manual manifests cannot establish authoritative ingestion coverage by default. Explicit completeness attestation is opt-in at construction time and is intended for controlled fixtures/testing until a product policy defines otherwise.
6. The existing IDX website collector is not a Synapse automated source. It remains isolated while the source-compliance hold is active.
7. A future licensed source, such as an approved IDX Data Reference integration, must implement the same normalized contract and its own completeness evidence.

## Normalized contract

`DisclosureSource.collect_window(start_at, end_at)` returns `SourceWindowResult` containing:

- `source_id`
- requested start/end timestamps
- normalized `SourceDisclosure` rows
- explicit `complete` state
- optional coverage start/end evidence
- source diagnostics

Each disclosure contains:

- upstream `external_id`
- ticker
- timezone-aware announcement timestamp
- title
- optional subject/type/source URL
- zero or more normalized attachments
- source metadata

Each attachment can carry a local path or an authorized source URL. The manual adapter requires a local path and computes SHA-256 from the file bytes.

## Manual manifest v1

Schema identifier:

```text
synapse-source-manifest-v1
```

Example:

```json
{
  "schemaVersion": "synapse-source-manifest-v1",
  "coverage": {
    "complete": false,
    "startAt": "2026-08-21T09:00:00+07:00",
    "endAt": "2026-08-21T11:00:00+07:00"
  },
  "disclosures": [
    {
      "externalId": "manual-example-1",
      "ticker": "BBRI",
      "announcedAt": "2026-08-21T10:00:00+07:00",
      "title": "Example disclosure",
      "subject": "Example subject",
      "disclosureType": "MATERIAL_INFORMATION",
      "sourceUrl": "https://example.invalid/disclosure/manual-example-1",
      "attachments": [
        {
          "path": "files/example.pdf",
          "filename": "example.pdf",
          "contentType": "application/pdf"
        }
      ]
    }
  ]
}
```

The directory layout for that example is:

```text
import-bundle/
  manifest.json
  files/
    example.pdf
```

The adapter resolves `files/example.pdf` relative to `manifest.json`. Absolute paths and `../` escapes are rejected.

## Completeness behavior

Default construction:

```python
source = ManualManifestSource("import-bundle/manifest.json")
```

Even if the manifest says `coverage.complete=true`, the returned source result remains `complete=false` and exposes diagnostics showing that the attestation was suppressed.

Controlled test/fixture construction can explicitly opt in:

```python
source = ManualManifestSource(
    "import-bundle/manifest.json",
    allow_complete_attestation=True,
)
```

In that mode the manifest must provide timezone-aware `coverage.startAt` and `coverage.endAt`, and they must fully contain the requested collection window. Otherwise the contract rejects the result.

## Next integration step

The next focused phase should connect `DisclosureSource` to a source-neutral ingestion runner that can:

1. collect normalized disclosures from an adapter;
2. stage local attachments without downloading them again;
3. run the existing extraction/AI pipeline;
4. publish through the existing Synapse Internal API;
5. commit coverage only when the source result is authoritative and proves the requested window.

The first executable integration should use `ManualManifestSource` so the downstream pipeline can be tested end-to-end without crawling the IDX website. A licensed provider adapter can then replace the manual source without changing the Synapse product/API contract.
