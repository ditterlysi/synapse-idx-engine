from __future__ import annotations

import json
from datetime import datetime

import pytest

from idx_digest.sources.issuer_public_snapshot import (
    ISSUER_PUBLIC_SNAPSHOT_KIND,
    IssuerPublicSnapshotError,
    IssuerPublicSnapshotSource,
)


def _manifest(
    tmp_path,
    *,
    source_page="https://bri.co.id/web/guest/announcement",
    issuer_hosts=None,
    coverage_complete=False,
    ticker="BBRI",
    external_id="issuer-public-bbri-20260702-1459-affiliated-asset-purchase",
    attachment_url="https://bri.co.id/web/guest/api/files?path=%2Fofficial.pdf",
):
    attachment = tmp_path / "disclosure.txt"
    attachment.write_text("Offline fixture for issuer public snapshot adapter tests.", encoding="utf-8")
    payload = {
        "schemaVersion": "synapse-source-manifest-v1",
        "metadata": {
            "sourceKind": ISSUER_PUBLIC_SNAPSHOT_KIND,
            "issuerTicker": "BBRI",
            "issuerHosts": issuer_hosts or ["bri.co.id"],
            "sourcePage": source_page,
            "capturedAt": "2026-07-02T15:10:00+07:00",
        },
        "coverage": {
            "complete": coverage_complete,
            "startAt": "2026-07-02T14:30:00+07:00",
            "endAt": "2026-07-02T15:30:00+07:00",
        },
        "disclosures": [
            {
                "externalId": external_id,
                "ticker": ticker,
                "announcedAt": "2026-07-02T14:59:00+07:00",
                "title": "Transaksi Afiliasi - Pembelian Aset",
                "disclosureType": "AFFILIATED_TRANSACTION",
                "attachments": [
                    {
                        "path": "disclosure.txt",
                        "filename": "official.pdf",
                        "contentType": "text/plain",
                        "sourceUrl": attachment_url,
                    }
                ],
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _window():
    return (
        datetime.fromisoformat("2026-07-02T14:30:00+07:00"),
        datetime.fromisoformat("2026-07-02T15:30:00+07:00"),
    )


def test_issuer_snapshot_adds_provenance_and_never_claims_coverage(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(_manifest(tmp_path))

    result = source.collect_window(start_at=start_at, end_at=end_at)

    assert result.source_id == "issuer-public-snapshot"
    assert result.complete is False
    assert result.coverage_start is None
    assert result.coverage_end is None
    assert result.diagnostics["networkAccess"] is False
    assert result.diagnostics["authoritativeCoverageAllowed"] is False
    assert result.diagnostics["issuerTicker"] == "BBRI"
    assert result.diagnostics["issuerHosts"] == ["bri.co.id"]
    assert result.diagnostics["officialIssuerSourcePage"] == "https://bri.co.id/web/guest/announcement"
    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.external_id.startswith("issuer-public-")
    assert disclosure.ticker == "BBRI"
    assert disclosure.source_url == "https://bri.co.id/web/guest/announcement"
    assert disclosure.metadata["issuerSnapshotAuthoritativeCoverage"] is False
    assert disclosure.metadata["issuerSnapshotHosts"] == ["bri.co.id"]
    assert disclosure.attachments[0].source_url.startswith("https://bri.co.id/")
    assert disclosure.attachments[0].local_path is not None


def test_issuer_snapshot_allows_declared_subdomains(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(
        _manifest(
            tmp_path,
            source_page="https://investor.example.co.id/disclosures",
            issuer_hosts=["example.co.id"],
            attachment_url="https://cdn.example.co.id/docs/disclosure.pdf",
        )
    )

    result = source.collect_window(start_at=start_at, end_at=end_at)

    assert result.disclosures[0].attachments[0].source_url == "https://cdn.example.co.id/docs/disclosure.pdf"


def test_issuer_snapshot_rejects_cross_host_attachment(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(
        _manifest(tmp_path, attachment_url="https://unrelated.example/disclosure.pdf")
    )

    with pytest.raises(IssuerPublicSnapshotError, match="host must match metadata.issuerHosts"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_issuer_snapshot_rejects_non_https_source_page(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(
        _manifest(tmp_path, source_page="http://bri.co.id/web/guest/announcement")
    )

    with pytest.raises(IssuerPublicSnapshotError, match="HTTPS URL"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_issuer_snapshot_rejects_authoritative_coverage_claim(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(_manifest(tmp_path, coverage_complete=True))

    with pytest.raises(IssuerPublicSnapshotError, match="may not claim authoritative coverage"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_issuer_snapshot_requires_matching_ticker(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(_manifest(tmp_path, ticker="TLKM"))

    with pytest.raises(IssuerPublicSnapshotError, match="does not match metadata.issuerTicker"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_issuer_snapshot_requires_issuer_public_external_id_prefix(tmp_path) -> None:
    start_at, end_at = _window()
    source = IssuerPublicSnapshotSource(_manifest(tmp_path, external_id="manual-bbri-event"))

    with pytest.raises(IssuerPublicSnapshotError, match="issuer-public-"):
        source.collect_window(start_at=start_at, end_at=end_at)
