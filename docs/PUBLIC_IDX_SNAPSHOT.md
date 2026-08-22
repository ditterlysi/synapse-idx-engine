# Official IDX Public Snapshot Source

`IdxPublicSnapshotSource` is an offline, non-authoritative source adapter for disclosure bundles captured from the official IDX disclosure page.

It exists for development and explicit manual ingestion when public disclosure metadata and attachment files are already available, without enabling automated IDX website crawling or any scheduled collector.

## Source boundary

The snapshot must identify one of the official IDX disclosure pages:

- `https://www.idx.id/en/listed-companies/disclosure/`
- `https://www.idx.id/id/perusahaan-tercatat/keterbukaan-informasi/`

The engine does **not** request either page. It only validates the recorded provenance and reads local files supplied in the bundle.

The adapter always reports:

```text
sourceId = idx-public-snapshot
networkAccess = false
complete = false
authoritativeCoverageAllowed = false
```

A public webpage snapshot therefore cannot advance Synapse ingestion coverage, even if the underlying metadata came from IDX.

## Manifest format

The bundle deliberately reuses the existing safe `synapse-source-manifest-v1` format. Add root-level snapshot metadata:

```json
{
  "schemaVersion": "synapse-source-manifest-v1",
  "metadata": {
    "sourceKind": "idx-public-snapshot-v1",
    "sourcePage": "https://www.idx.id/en/listed-companies/disclosure/",
    "capturedAt": "2026-07-11T00:30:00+07:00"
  },
  "coverage": {
    "complete": false
  },
  "disclosures": []
}
```

`metadata.capturedAt` must include an explicit timezone.

`coverage.complete=true` is rejected. The adapter does not accept a completeness attestation from a public-page snapshot.

## Disclosure IDs

Every snapshot disclosure must use the reserved namespace:

```text
idx-public-
```

Prefer a stable identifier visible in IDX metadata or filenames. For example, a filename containing announcement number `32111091` can use:

```text
idx-public-32111091
```

This avoids collisions with future licensed/authoritative source identifiers.

## Example from the public IDX listing

The official disclosure listing has shown metadata such as:

```text
Ticker: SUPR
Announced at: 2026-07-10 23:34:49 WIB
Title: The Signing of the Amendment to the Facility Agreement between Protelindo, Iforte, STP, BIT, IFEN, and IBST with PT Bank Mizuho Indonesia
Attachment filename: 20260710_SUPR_Laporan Informasi dan Fakta Material_32111091_lamp1.pdf
```

A local snapshot bundle for that row can look like:

```json
{
  "schemaVersion": "synapse-source-manifest-v1",
  "metadata": {
    "sourceKind": "idx-public-snapshot-v1",
    "sourcePage": "https://www.idx.id/en/listed-companies/disclosure/",
    "capturedAt": "2026-07-11T00:30:00+07:00"
  },
  "coverage": {
    "complete": false
  },
  "disclosures": [
    {
      "externalId": "idx-public-32111091",
      "ticker": "SUPR",
      "announcedAt": "2026-07-10T23:34:49+07:00",
      "title": "The Signing of the Amendment to the Facility Agreement between Protelindo, Iforte, STP, BIT, IFEN, and IBST with PT Bank Mizuho Indonesia",
      "disclosureType": "MATERIAL_INFORMATION",
      "attachments": [
        {
          "path": "files/20260710_SUPR_Laporan Informasi dan Fakta Material_32111091_lamp1.pdf",
          "filename": "20260710_SUPR_Laporan Informasi dan Fakta Material_32111091_lamp1.pdf",
          "contentType": "application/pdf"
        }
      ]
    }
  ]
}
```

The file under `files/` must already have been saved locally. Normal `ManualManifestSource` protections still apply: absolute paths and path traversal are rejected, file existence is checked, and optional SHA-256 values are verified.

## Why this is non-authoritative

A page snapshot can show real official IDX disclosure rows but it does not prove that every disclosure in a requested time interval was captured. Pagination, page state, timing, and snapshot boundaries can all make the set incomplete.

Therefore this adapter is suitable for:

- real-document parser development;
- AI taxonomy and materiality testing;
- explicit user-triggered ingestion of selected disclosures;
- cross-checking IDX metadata with issuer-hosted documents.

It is **not** suitable for:

- production coverage advancement;
- claiming an interval is complete;
- scheduled 03:00 WIB collection;
- background crawling of IDX pages;
- browser/CAPTCHA/proxy/rate-limit bypass.

A future licensed or otherwise approved source may implement the same `DisclosureSource` contract and provide explicit completeness evidence.