# Issuer Public Snapshot

`issuer-public-snapshot` is the durable, offline source adapter for disclosures captured from an issuer's official public website.

It is intentionally **not a crawler**. The adapter performs zero source-network requests. A human or an explicitly controlled preparation step must first save the official documents next to the manifest. The CLI then validates the staged files, extracts them locally, runs the configured AI provider, and publishes through the Synapse Internal API.

## Safety model

- source adapter network access: **off**
- authoritative coverage: **never**
- coverage commit: **off**
- scheduler: **off**
- external ID prefix: `issuer-public-`
- one manifest is bound to one `issuerTicker`
- issuer page and attachment URLs must use HTTPS
- URL hosts must match the explicit `metadata.issuerHosts` allowlist (declared subdomains are allowed)
- attachments must already exist inside the manifest directory tree and cannot escape it
- every attachment requires an official `sourceUrl`

This source is useful for issuer-hosted PDFs or other public files when an exact public timestamp and provenance can be verified, but it does not establish that every exchange disclosure in an interval was captured.

## Manifest shape

The underlying schema remains `synapse-source-manifest-v1` with additional root metadata:

```json
{
  "schemaVersion": "synapse-source-manifest-v1",
  "metadata": {
    "sourceKind": "issuer-public-snapshot-v1",
    "issuerTicker": "BBRI",
    "issuerHosts": ["bri.co.id"],
    "sourcePage": "https://bri.co.id/web/guest/announcement",
    "capturedAt": "2026-07-02T15:10:00+07:00"
  },
  "coverage": {
    "complete": false,
    "startAt": "2026-07-02T14:30:00+07:00",
    "endAt": "2026-07-02T15:30:00+07:00"
  },
  "disclosures": [
    {
      "externalId": "issuer-public-bbri-20260702-1459-example",
      "ticker": "BBRI",
      "announcedAt": "2026-07-02T14:59:00+07:00",
      "title": "Example official issuer disclosure",
      "disclosureType": "MATERIAL_INFORMATION",
      "attachments": [
        {
          "path": "files/disclosure.pdf",
          "filename": "disclosure.pdf",
          "contentType": "application/pdf",
          "sourceUrl": "https://bri.co.id/path/to/disclosure.pdf"
        }
      ]
    }
  ]
}
```

`coverage.complete=true` is rejected. `capturedAt` and every disclosure timestamp must include an explicit timezone.

## CLI

```bash
synapse-issuer-public-snapshot \
  --manifest ./issuer-snapshot/manifest.json \
  --start '2026-07-02T14:30:00+07:00' \
  --end '2026-07-02T15:30:00+07:00' \
  --confirm-publish
```

The window must be explicit, no more than two hours, and remain within one Asia/Jakarta calendar date. `--confirm-publish` is mandatory because the command can call the configured AI provider and write to Synapse.

A successful issuer-public import is normally reported as `PARTIAL` with `processingOk=true`, `coverageCommitted=false`, `coverageAuthoritative=false`, `sourceNetworkAccess=false`, and `scheduleEnabled=false`. `PARTIAL` is expected because issuer-public snapshots do not prove full exchange coverage.
