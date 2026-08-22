from __future__ import annotations

import hashlib
from datetime import datetime

from idx_digest.source_contract import SourceAttachment, SourceDisclosure
from idx_digest.source_ingestion import SourceIngestionRunner


def test_disclosure_item_publishes_attachment_hash_without_local_path(tmp_path) -> None:
    attachment_path = tmp_path / "disclosure.txt"
    attachment_path.write_text("same disclosure body", encoding="utf-8")
    expected_hash = hashlib.sha256(b"same disclosure body").hexdigest()
    disclosure = SourceDisclosure(
        external_id="manual-reconcile-1",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-07-23T03:56:00+07:00"),
        title="Material disclosure",
        source_url="https://issuer.example/disclosure",
        attachments=(
            SourceAttachment(
                filename="disclosure.txt",
                local_path=attachment_path,
                source_url="https://issuer.example/disclosure.pdf",
                content_type="text/plain",
            ),
        ),
    )

    item = SourceIngestionRunner._disclosure_item("manual-manifest", disclosure)

    assert item.raw_metadata["sourceId"] == "manual-manifest"
    assert item.raw_metadata["sourceExternalId"] == "manual-reconcile-1"
    assert item.raw_metadata["sourceAttachmentSha256s"] == [expected_hash]
    assert "local_path" not in str(item.raw_metadata).lower()
    assert str(tmp_path) not in str(item.raw_metadata)


def test_disclosure_item_reuses_verified_hash_and_deduplicates_it(tmp_path) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    verified_hash = "a" * 64
    disclosure = SourceDisclosure(
        external_id="manual-reconcile-2",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-07-23T03:56:00+07:00"),
        title="Material disclosure",
        attachments=(
            SourceAttachment(filename="first.txt", local_path=first_path, sha256=verified_hash),
            SourceAttachment(filename="second.txt", local_path=second_path, sha256=verified_hash),
        ),
    )

    item = SourceIngestionRunner._disclosure_item("manual-manifest", disclosure)

    assert item.raw_metadata["sourceAttachmentCount"] == 2
    assert item.raw_metadata["sourceAttachmentSha256s"] == [verified_hash]
