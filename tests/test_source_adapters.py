from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from idx_digest.source_contract import SourceContractError, SourceDisclosure, SourceWindowResult
from idx_digest.sources.manual_manifest import MANUAL_MANIFEST_SCHEMA, ManualManifestError, ManualManifestSource


JAKARTA = ZoneInfo("Asia/Jakarta")


def _write_manifest(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_manifest() -> dict:
    return {
        "schemaVersion": MANUAL_MANIFEST_SCHEMA,
        "coverage": {
            "complete": True,
            "startAt": "2026-08-21T09:00:00+07:00",
            "endAt": "2026-08-21T11:00:00+07:00",
        },
        "disclosures": [
            {
                "externalId": "manual-1",
                "ticker": "bbri",
                "announcedAt": "2026-08-21T10:00:00+07:00",
                "title": "Material disclosure",
                "subject": "Example",
                "disclosureType": "MATERIAL_INFORMATION",
                "sourceUrl": "https://example.invalid/disclosure/manual-1",
                "attachments": [],
            },
            {
                "externalId": "manual-outside",
                "ticker": "ANTM",
                "announcedAt": "2026-08-21T12:00:00+07:00",
                "title": "Outside window",
                "attachments": [],
            },
        ],
    }


def test_source_contract_rejects_naive_disclosure_timestamp() -> None:
    with pytest.raises(SourceContractError, match="announced_at must be timezone-aware"):
        SourceDisclosure(
            external_id="x",
            ticker="BBRI",
            announced_at=datetime(2026, 8, 21, 10, 0),
            title="Disclosure",
        )


def test_complete_source_result_requires_proven_coverage() -> None:
    start = datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA)
    end = datetime(2026, 8, 21, 10, 0, tzinfo=JAKARTA)
    with pytest.raises(SourceContractError, match="must prove the entire requested window"):
        SourceWindowResult(
            source_id="test",
            requested_start=start,
            requested_end=end,
            complete=True,
        )


def test_source_result_rejects_disclosure_outside_requested_window() -> None:
    start = datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA)
    end = datetime(2026, 8, 21, 10, 0, tzinfo=JAKARTA)
    disclosure = SourceDisclosure(
        external_id="outside",
        ticker="BBRI",
        announced_at=datetime(2026, 8, 21, 10, 1, tzinfo=JAKARTA),
        title="Outside",
    )
    with pytest.raises(SourceContractError, match="outside the requested window"):
        SourceWindowResult(
            source_id="test",
            requested_start=start,
            requested_end=end,
            disclosures=(disclosure,),
        )


def test_manual_manifest_is_offline_filtered_and_non_authoritative_by_default(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    attachment = docs / "report.pdf"
    attachment.write_bytes(b"offline fixture")
    payload = _base_manifest()
    payload["disclosures"][0]["attachments"] = [
        {
            "path": "docs/report.pdf",
            "filename": "report.pdf",
            "contentType": "application/pdf",
        }
    ]
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    result = source.collect_window(
        start_at=datetime(2026, 8, 21, 9, 30, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 21, 10, 30, tzinfo=JAKARTA),
    )

    assert result.source_id == "manual-manifest"
    assert result.complete is False
    assert result.coverage_start is None
    assert result.coverage_end is None
    assert result.diagnostics["networkAccess"] is False
    assert result.diagnostics["completeAttestationSuppressed"] is True
    assert result.diagnostics["manifestDisclosures"] == 2
    assert result.diagnostics["matchedDisclosures"] == 1
    assert result.diagnostics["outsideRequestedWindow"] == 1

    disclosure = result.disclosures[0]
    assert disclosure.external_id == "manual-1"
    assert disclosure.ticker == "BBRI"
    assert disclosure.attachments[0].local_path == attachment.resolve()
    assert disclosure.attachments[0].sha256 == hashlib.sha256(b"offline fixture").hexdigest()


def test_manual_manifest_can_expose_explicit_attested_coverage_when_opted_in(tmp_path: Path) -> None:
    source = ManualManifestSource(
        _write_manifest(tmp_path, _base_manifest()),
        allow_complete_attestation=True,
    )
    result = source.collect_window(
        start_at=datetime(2026, 8, 21, 9, 30, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 21, 10, 30, tzinfo=JAKARTA),
    )

    assert result.complete is True
    assert result.proves_requested_window() is True
    assert result.diagnostics["completeAttestationAllowed"] is True


def test_manual_manifest_rejects_coverage_that_does_not_cover_requested_window(tmp_path: Path) -> None:
    source = ManualManifestSource(
        _write_manifest(tmp_path, _base_manifest()),
        allow_complete_attestation=True,
    )
    with pytest.raises(SourceContractError, match="must prove the entire requested window"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 8, 30, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 10, 30, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_naive_requested_window(tmp_path: Path) -> None:
    source = ManualManifestSource(_write_manifest(tmp_path, _base_manifest()))
    with pytest.raises(ManualManifestError, match="start_at must be timezone-aware"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_reversed_requested_window(tmp_path: Path) -> None:
    source = ManualManifestSource(_write_manifest(tmp_path, _base_manifest()))
    with pytest.raises(ManualManifestError, match="end_at must be greater"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_attachment_path_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.pdf"
    outside.write_bytes(b"do not read")
    payload = _base_manifest()
    payload["disclosures"][0]["attachments"] = [{"path": "../outside.pdf"}]
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    with pytest.raises(ManualManifestError, match="may not escape"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_absolute_attachment_path(tmp_path: Path) -> None:
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fixture")
    payload = _base_manifest()
    payload["disclosures"][0]["attachments"] = [{"path": str(attachment.resolve())}]
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    with pytest.raises(ManualManifestError, match="must be relative"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_duplicate_external_ids(tmp_path: Path) -> None:
    payload = _base_manifest()
    payload["disclosures"].append(
        {
            "externalId": "manual-1",
            "ticker": "TLKM",
            "announcedAt": "2026-08-21T10:15:00+07:00",
            "title": "Duplicate id",
            "attachments": [],
        }
    )
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    with pytest.raises(ManualManifestError, match="duplicate disclosure externalId"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_attachment_hash_mismatch(tmp_path: Path) -> None:
    attachment = tmp_path / "report.pdf"
    attachment.write_bytes(b"fixture")
    payload = _base_manifest()
    payload["disclosures"][0]["attachments"] = [
        {"path": "report.pdf", "sha256": "0" * 64}
    ]
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    with pytest.raises(ManualManifestError, match="sha256 does not match"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )


def test_manual_manifest_rejects_naive_announcement_timestamp(tmp_path: Path) -> None:
    payload = _base_manifest()
    payload["disclosures"][0]["announcedAt"] = "2026-08-21T10:00:00"
    source = ManualManifestSource(_write_manifest(tmp_path, payload))

    with pytest.raises(ManualManifestError, match="explicit timezone"):
        source.collect_window(
            start_at=datetime(2026, 8, 21, 9, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 21, 11, 0, tzinfo=JAKARTA),
        )
