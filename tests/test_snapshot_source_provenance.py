from __future__ import annotations

from datetime import datetime

from idx_digest.snapshot_ingestion import (
    SnapshotSourceIngestionRunner,
    snapshot_source_provenance,
)
from idx_digest.source_contract import SourceAttachment, SourceDisclosure


def _disclosure(*, metadata: dict[str, object]) -> SourceDisclosure:
    return SourceDisclosure(
        external_id="issuer-public-bbri-event",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-07-02T14:59:00+07:00"),
        title="Official issuer disclosure",
        source_url="https://bri.co.id/web/guest/announcement",
        attachments=(
            SourceAttachment(
                filename="official.pdf",
                source_url="https://bri.co.id/docs/official.pdf",
                sha256="a" * 64,
            ),
        ),
        metadata=metadata,
    )


def test_issuer_snapshot_persists_only_allowlisted_provenance() -> None:
    disclosure = _disclosure(
        metadata={
            "issuerSnapshotSourcePage": "https://bri.co.id/web/guest/announcement",
            "issuerSnapshotCapturedAt": "2026-07-02T15:10:00+07:00",
            "issuerSnapshotAuthoritativeCoverage": False,
            "issuerSnapshotHosts": ["bri.co.id", "cdn.bri.co.id"],
            "localPath": "C:/secret/work/disclosure.pdf",
            "apiKey": "must-not-persist",
            "arbitraryNested": {"password": "must-not-persist"},
        }
    )

    item = SnapshotSourceIngestionRunner._disclosure_item("issuer-public-snapshot", disclosure)

    assert item.raw_metadata["sourceId"] == "issuer-public-snapshot"
    assert item.raw_metadata["sourceAttachmentSha256s"] == ["a" * 64]
    assert item.raw_metadata["sourceProvenance"] == {
        "sourcePage": "https://bri.co.id/web/guest/announcement",
        "capturedAt": "2026-07-02T15:10:00+07:00",
        "authoritativeCoverage": False,
        "issuerHosts": ["bri.co.id", "cdn.bri.co.id"],
    }
    serialized = str(item.raw_metadata)
    assert "localPath" not in serialized
    assert "must-not-persist" not in serialized
    assert "password" not in serialized


def test_idx_snapshot_persists_only_idx_provenance() -> None:
    provenance = snapshot_source_provenance(
        "idx-public-snapshot",
        {
            "snapshotSourcePage": "https://www.idx.id/en/listed-companies/disclosure/",
            "snapshotCapturedAt": "2026-07-11T00:30:00+07:00",
            "snapshotAuthoritativeCoverage": False,
            "issuerSnapshotHosts": ["should-not-copy.example"],
            "secret": "nope",
        },
    )

    assert provenance == {
        "sourcePage": "https://www.idx.id/en/listed-companies/disclosure/",
        "capturedAt": "2026-07-11T00:30:00+07:00",
        "authoritativeCoverage": False,
    }


def test_unknown_source_does_not_persist_arbitrary_metadata() -> None:
    disclosure = _disclosure(metadata={"token": "secret", "localPath": "/tmp/file"})

    item = SnapshotSourceIngestionRunner._disclosure_item("manual-manifest", disclosure)

    assert "sourceProvenance" not in item.raw_metadata
    assert "token" not in str(item.raw_metadata)
    assert "localPath" not in str(item.raw_metadata)


def test_authoritative_true_is_never_copied() -> None:
    provenance = snapshot_source_provenance(
        "issuer-public-snapshot",
        {
            "issuerSnapshotSourcePage": "https://example.com/disclosures",
            "issuerSnapshotCapturedAt": "2026-07-11T00:30:00+07:00",
            "issuerSnapshotAuthoritativeCoverage": True,
            "issuerSnapshotHosts": ["example.com"],
        },
    )

    assert "authoritativeCoverage" not in provenance
