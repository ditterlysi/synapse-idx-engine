from __future__ import annotations

import json
from datetime import datetime

import pytest

from idx_digest.sources.idx_public_snapshot import (
    IDX_PUBLIC_SNAPSHOT_KIND,
    IdxPublicSnapshotError,
    IdxPublicSnapshotSource,
)


def _manifest(tmp_path, *, source_page="https://www.idx.id/en/listed-companies/disclosure/", coverage_complete=False):
    attachment = tmp_path / "disclosure.txt"
    attachment.write_text("Offline fixture for IDX public snapshot adapter tests.", encoding="utf-8")
    payload = {
        "schemaVersion": "synapse-source-manifest-v1",
        "metadata": {
            "sourceKind": IDX_PUBLIC_SNAPSHOT_KIND,
            "sourcePage": source_page,
            "capturedAt": "2026-07-11T00:30:00+07:00",
        },
        "coverage": {
            "complete": coverage_complete,
            "startAt": "2026-07-10T22:00:00+07:00",
            "endAt": "2026-07-11T01:00:00+07:00",
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
                        "path": "disclosure.txt",
                        "filename": "20260710_SUPR_Laporan Informasi dan Fakta Material_32111091_lamp1.pdf",
                        "contentType": "text/plain",
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
        datetime.fromisoformat("2026-07-10T22:00:00+07:00"),
        datetime.fromisoformat("2026-07-11T01:00:00+07:00"),
    )


def test_public_snapshot_adds_idx_provenance_and_never_claims_coverage(tmp_path) -> None:
    start_at, end_at = _window()
    source = IdxPublicSnapshotSource(_manifest(tmp_path))

    result = source.collect_window(start_at=start_at, end_at=end_at)

    assert result.source_id == "idx-public-snapshot"
    assert result.complete is False
    assert result.coverage_start is None
    assert result.coverage_end is None
    assert result.diagnostics["networkAccess"] is False
    assert result.diagnostics["authoritativeCoverageAllowed"] is False
    assert result.diagnostics["officialIdxSourcePage"] == "https://www.idx.id/en/listed-companies/disclosure/"
    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.external_id == "idx-public-32111091"
    assert disclosure.ticker == "SUPR"
    assert disclosure.source_url == "https://www.idx.id/en/listed-companies/disclosure/"
    assert disclosure.metadata["snapshotAuthoritativeCoverage"] is False
    assert disclosure.attachments[0].local_path is not None


def test_public_snapshot_rejects_non_idx_source_page(tmp_path) -> None:
    start_at, end_at = _window()
    source = IdxPublicSnapshotSource(
        _manifest(tmp_path, source_page="https://example.com/en/listed-companies/disclosure/")
    )

    with pytest.raises(IdxPublicSnapshotError, match="official HTTPS IDX page"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_public_snapshot_rejects_authoritative_coverage_claim(tmp_path) -> None:
    start_at, end_at = _window()
    source = IdxPublicSnapshotSource(_manifest(tmp_path, coverage_complete=True))

    with pytest.raises(IdxPublicSnapshotError, match="may not claim authoritative coverage"):
        source.collect_window(start_at=start_at, end_at=end_at)


def test_public_snapshot_requires_idx_public_external_id_prefix(tmp_path) -> None:
    path = _manifest(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["disclosures"][0]["externalId"] = "32111091"
    path.write_text(json.dumps(payload), encoding="utf-8")
    start_at, end_at = _window()

    with pytest.raises(IdxPublicSnapshotError, match="idx-public-"):
        IdxPublicSnapshotSource(path).collect_window(start_at=start_at, end_at=end_at)
