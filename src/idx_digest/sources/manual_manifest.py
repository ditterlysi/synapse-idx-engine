from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from dateutil.parser import isoparse

from ..source_contract import (
    SourceAttachment,
    SourceContractError,
    SourceDisclosure,
    SourceWindowResult,
)

MANUAL_MANIFEST_SCHEMA = "synapse-source-manifest-v1"


class ManualManifestError(SourceContractError):
    """Raised when an offline manual manifest is malformed or unsafe."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManualManifestError(f"{label} must be a JSON object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ManualManifestError(f"{label} must be a JSON array")
    return value


def _text(value: Any, label: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ManualManifestError(f"{label} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ManualManifestError(f"{label} must not be empty")
    return normalized or None


def _aware_datetime(value: Any, label: str) -> datetime:
    raw = _text(value, label)
    assert raw is not None
    try:
        parsed = isoparse(raw)
    except (TypeError, ValueError) as exc:
        raise ManualManifestError(f"{label} must be a valid ISO timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManualManifestError(f"{label} must include an explicit timezone offset or Z")
    return parsed


def _validate_window(start_at: datetime, end_at: datetime) -> None:
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ManualManifestError("start_at must be timezone-aware")
    if end_at.tzinfo is None or end_at.utcoffset() is None:
        raise ManualManifestError("end_at must be timezone-aware")
    if end_at <= start_at:
        raise ManualManifestError("end_at must be greater than start_at")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ManualManifestSource:
    """Offline-only disclosure source backed by a user-provided JSON manifest.

    Attachment paths must be relative to the manifest directory and may not
    escape it. The adapter never performs network requests. Manual completeness
    attestations are ignored by default so development imports cannot
    accidentally prove production ingestion coverage.
    """

    source_id = "manual-manifest"

    def __init__(self, manifest_path: Path | str, *, allow_complete_attestation: bool = False):
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.allow_complete_attestation = allow_complete_attestation

    @property
    def base_dir(self) -> Path:
        return self.manifest_path.parent

    def _load(self) -> dict[str, Any]:
        if not self.manifest_path.exists() or not self.manifest_path.is_file():
            raise ManualManifestError(f"manifest does not exist: {self.manifest_path}")
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManualManifestError(f"could not read manifest: {exc}") from exc
        root = _mapping(payload, "manifest")
        schema = _text(root.get("schemaVersion"), "schemaVersion")
        if schema != MANUAL_MANIFEST_SCHEMA:
            raise ManualManifestError(
                f"unsupported schemaVersion {schema!r}; expected {MANUAL_MANIFEST_SCHEMA!r}"
            )
        return root

    def _resolve_attachment_path(self, value: Any, label: str) -> Path:
        raw = _text(value, label)
        assert raw is not None
        candidate = Path(raw)
        if candidate.is_absolute():
            raise ManualManifestError(f"{label} must be relative to the manifest directory")
        resolved = (self.base_dir / candidate).resolve()
        try:
            resolved.relative_to(self.base_dir)
        except ValueError as exc:
            raise ManualManifestError(f"{label} may not escape the manifest directory") from exc
        if not resolved.exists() or not resolved.is_file():
            raise ManualManifestError(f"attachment file does not exist: {candidate.as_posix()}")
        return resolved

    def _attachment(self, value: Any, *, disclosure_index: int, attachment_index: int) -> SourceAttachment:
        label = f"disclosures[{disclosure_index}].attachments[{attachment_index}]"
        item = _mapping(value, label)
        local_path = self._resolve_attachment_path(item.get("path"), f"{label}.path")
        digest = _sha256(local_path)
        expected_digest = _text(item.get("sha256"), f"{label}.sha256", required=False)
        if expected_digest is not None and expected_digest.lower() != digest:
            raise ManualManifestError(f"{label}.sha256 does not match the local attachment")
        filename = _text(item.get("filename"), f"{label}.filename", required=False) or local_path.name
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ManualManifestError(f"{label}.metadata must be a JSON object")
        return SourceAttachment(
            filename=filename,
            local_path=local_path,
            source_url=_text(item.get("sourceUrl"), f"{label}.sourceUrl", required=False),
            content_type=_text(item.get("contentType"), f"{label}.contentType", required=False),
            sha256=digest,
            metadata=metadata,
        )

    def _disclosure(self, value: Any, *, index: int) -> SourceDisclosure:
        label = f"disclosures[{index}]"
        item = _mapping(value, label)
        attachments_raw = item.get("attachments") or []
        attachments = tuple(
            self._attachment(attachment, disclosure_index=index, attachment_index=attachment_index)
            for attachment_index, attachment in enumerate(_list(attachments_raw, f"{label}.attachments"))
        )
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ManualManifestError(f"{label}.metadata must be a JSON object")
        return SourceDisclosure(
            external_id=_text(item.get("externalId"), f"{label}.externalId") or "",
            ticker=_text(item.get("ticker"), f"{label}.ticker") or "",
            announced_at=_aware_datetime(item.get("announcedAt"), f"{label}.announcedAt"),
            title=_text(item.get("title"), f"{label}.title") or "",
            subject=_text(item.get("subject"), f"{label}.subject", required=False),
            disclosure_type=_text(item.get("disclosureType"), f"{label}.disclosureType", required=False),
            source_url=_text(item.get("sourceUrl"), f"{label}.sourceUrl", required=False),
            attachments=attachments,
            metadata=metadata,
        )

    def _coverage(self, root: dict[str, Any]) -> tuple[bool, datetime | None, datetime | None, dict[str, Any]]:
        raw = root.get("coverage")
        if raw is None:
            return False, None, None, {"manifestCoveragePresent": False}
        item = _mapping(raw, "coverage")
        claimed_complete = item.get("complete") is True
        diagnostics: dict[str, Any] = {
            "manifestCoveragePresent": True,
            "manifestClaimedComplete": claimed_complete,
            "completeAttestationAllowed": self.allow_complete_attestation,
        }
        if not claimed_complete or not self.allow_complete_attestation:
            diagnostics["completeAttestationSuppressed"] = claimed_complete and not self.allow_complete_attestation
            return False, None, None, diagnostics
        start_at = _aware_datetime(item.get("startAt"), "coverage.startAt")
        end_at = _aware_datetime(item.get("endAt"), "coverage.endAt")
        return True, start_at, end_at, diagnostics

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        _validate_window(start_at, end_at)
        root = self._load()
        raw_disclosures = _list(root.get("disclosures"), "disclosures")
        parsed = [self._disclosure(value, index=index) for index, value in enumerate(raw_disclosures)]
        ids = [item.external_id for item in parsed]
        if len(ids) != len(set(ids)):
            raise ManualManifestError("manifest contains duplicate disclosure externalId values")

        matched = [item for item in parsed if start_at <= item.announced_at <= end_at]
        matched.sort(key=lambda item: (item.announced_at, item.external_id))
        complete, coverage_start, coverage_end, coverage_diagnostics = self._coverage(root)
        diagnostics = {
            "adapter": self.source_id,
            "networkAccess": False,
            "manifest": self.manifest_path.name,
            "manifestDisclosures": len(parsed),
            "matchedDisclosures": len(matched),
            "outsideRequestedWindow": len(parsed) - len(matched),
            **coverage_diagnostics,
        }
        return SourceWindowResult(
            source_id=self.source_id,
            requested_start=start_at,
            requested_end=end_at,
            disclosures=tuple(matched),
            complete=complete,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            diagnostics=diagnostics,
        )
