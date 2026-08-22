from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dateutil.parser import isoparse

from ..source_contract import SourceDisclosure, SourceWindowResult
from .manual_manifest import ManualManifestError, ManualManifestSource

IDX_PUBLIC_SNAPSHOT_KIND = "idx-public-snapshot-v1"
IDX_PUBLIC_EXTERNAL_ID_PREFIX = "idx-public-"
OFFICIAL_IDX_HOSTS = {"idx.id", "www.idx.id", "idx.co.id", "www.idx.co.id"}


class IdxPublicSnapshotError(ManualManifestError):
    """Raised when an offline IDX public-page snapshot is malformed or unsafe."""


def _aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IdxPublicSnapshotError(f"{label} must be a non-empty ISO timestamp")
    try:
        parsed = isoparse(value.strip())
    except (TypeError, ValueError) as exc:
        raise IdxPublicSnapshotError(f"{label} must be a valid ISO timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IdxPublicSnapshotError(f"{label} must include an explicit timezone offset or Z")
    return parsed


def _official_idx_page(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdxPublicSnapshotError("metadata.sourcePage must be a non-empty URL")
    raw = value.strip()
    parsed = urlparse(raw)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_IDX_HOSTS:
        raise IdxPublicSnapshotError("metadata.sourcePage must be an official HTTPS IDX page")
    path = parsed.path.rstrip("/").lower()
    allowed_path = (
        path.endswith("/en/listed-companies/disclosure")
        or path.endswith("/id/perusahaan-tercatat/keterbukaan-informasi")
    )
    if not allowed_path:
        raise IdxPublicSnapshotError("metadata.sourcePage must point to the official IDX disclosure page")
    return raw


class IdxPublicSnapshotSource:
    """Offline wrapper for disclosure bundles captured from the official IDX page.

    This adapter deliberately performs zero network requests. It reuses the
    existing safe local-manifest parser for attachment path/hash validation, then
    adds IDX-specific provenance rules. A public webpage snapshot can never prove
    authoritative completeness, so coverage is always suppressed.
    """

    source_id = "idx-public-snapshot"

    def __init__(self, manifest_path: Path | str):
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.delegate = ManualManifestSource(self.manifest_path, allow_complete_attestation=False)

    def _snapshot_metadata(self) -> tuple[str, datetime]:
        if not self.manifest_path.exists() or not self.manifest_path.is_file():
            raise IdxPublicSnapshotError(f"manifest does not exist: {self.manifest_path}")
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IdxPublicSnapshotError(f"could not read snapshot manifest: {exc}") from exc
        if not isinstance(payload, dict):
            raise IdxPublicSnapshotError("manifest must be a JSON object")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise IdxPublicSnapshotError("metadata must be a JSON object")
        if metadata.get("sourceKind") != IDX_PUBLIC_SNAPSHOT_KIND:
            raise IdxPublicSnapshotError(
                f"metadata.sourceKind must equal {IDX_PUBLIC_SNAPSHOT_KIND!r}"
            )

        coverage = payload.get("coverage")
        if isinstance(coverage, dict) and coverage.get("complete") is True:
            raise IdxPublicSnapshotError("IDX public snapshots may not claim authoritative coverage")

        source_page = _official_idx_page(metadata.get("sourcePage"))
        captured_at = _aware_timestamp(metadata.get("capturedAt"), "metadata.capturedAt")
        return source_page, captured_at

    @staticmethod
    def _normalize_disclosure(
        disclosure: SourceDisclosure,
        *,
        source_page: str,
        captured_at: datetime,
    ) -> SourceDisclosure:
        if not disclosure.external_id.startswith(IDX_PUBLIC_EXTERNAL_ID_PREFIX):
            raise IdxPublicSnapshotError(
                f"IDX public snapshot externalId must start with {IDX_PUBLIC_EXTERNAL_ID_PREFIX!r}"
            )
        metadata = {
            **dict(disclosure.metadata),
            "snapshotSourcePage": source_page,
            "snapshotCapturedAt": captured_at.isoformat(),
            "snapshotAuthoritativeCoverage": False,
        }
        return replace(
            disclosure,
            source_url=disclosure.source_url or source_page,
            metadata=metadata,
        )

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        source_page, captured_at = self._snapshot_metadata()
        delegated = self.delegate.collect_window(start_at=start_at, end_at=end_at)
        disclosures = tuple(
            self._normalize_disclosure(item, source_page=source_page, captured_at=captured_at)
            for item in delegated.disclosures
        )
        diagnostics = {
            **dict(delegated.diagnostics),
            "adapter": self.source_id,
            "delegateAdapter": delegated.source_id,
            "networkAccess": False,
            "officialIdxSourcePage": source_page,
            "snapshotCapturedAt": captured_at.isoformat(),
            "authoritativeCoverageAllowed": False,
        }
        return SourceWindowResult(
            source_id=self.source_id,
            requested_start=delegated.requested_start,
            requested_end=delegated.requested_end,
            disclosures=disclosures,
            complete=False,
            coverage_start=None,
            coverage_end=None,
            diagnostics=diagnostics,
        )
