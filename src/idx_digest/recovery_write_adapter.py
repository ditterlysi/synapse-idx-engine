"""Manifest-bounded live writes for the existing Synapse IDX ingestion APIs.

The adapter deliberately composes the already authenticated read-only preflight
store and the existing Synapse write client.  It does not discover disclosures,
touch source state, or expose any wildcard write operation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

import httpx

from .config import Settings
from .recovery_preflight_adapter import SynapseRecoveryPreflightStore
from .recovery_runner import (
    CurrentDisclosure,
    CurrentFile,
    RecoveryCaps,
    RecoveryExecutionError,
    RecoveryManifest,
    RecoveryRunReport,
    RecoveryStore,
)
from .synapse_client import SynapseClient
from .synapse_contract import (
    CommitAnalysisRequest,
    CreateRunRequest,
    DisclosureFilesUpsertRequest,
    DisclosureFileUpsertItem,
    ProcessingStatus,
    UpdateProcessingStatusRequest,
    UpdateRunRequest,
)

MAX_LIVE_RECOVERY_RECORDS = 4
MAX_LIVE_SOURCE_REQUESTS = 12
MAX_LIVE_ATTACHMENTS = 20
MAX_LIVE_AI_DOCUMENTS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _filename(source_url: str) -> str:
    value = unquote(Path(urlparse(source_url).path).name).strip()
    return value or "idx-attachment.bin"


def _suffix(filename: str) -> str | None:
    suffix = Path(filename).suffix.lower().lstrip(".")
    return suffix if suffix and len(suffix) <= 30 else None


def _approved_records(manifest: RecoveryManifest) -> list[dict[str, str]]:
    return [
        {
            "disclosureId": str(record.disclosure_id),
            "externalId": record.expected_external_id,
            "ticker": record.ticker,
        }
        for record in manifest.records
    ]


def _terminal_status(report: RecoveryRunReport) -> str:
    if report.ok:
        return "COMPLETE"
    # A recovery run that has durably changed a disclosure/file but did not
    # finish its analysis is resumable work, not an all-or-nothing failure.
    if report.db_commits > 0 or report.files_downloaded > 0 or report.files_reused > 0:
        return "PARTIAL"
    return "FAILED"


def _recovery_metadata(
    manifest: RecoveryManifest,
    *,
    caps: RecoveryCaps,
    audit_phase: str,
    report: RecoveryRunReport | None = None,
) -> dict[str, object]:
    recovery: dict[str, object] = {
        "kind": "idx-pending-recovery",
        "phase": audit_phase,
        "runtimeMode": "EXECUTE_LIVE",
        "manifestId": str(manifest.manifest_id),
        "manifestDigest": manifest.digest,
        "approvedRecords": _approved_records(manifest),
        "caps": {
            "maxRecords": caps.max_records,
            "maxSourceRequests": caps.max_source_requests,
            "maxAttachments": caps.max_attachments,
            "maxAIDocuments": caps.max_ai_documents,
        },
    }
    if report is not None:
        failed = sum(item.outcome in {"FAILED", "PRECONDITION_FAILED"} for item in report.records)
        recovery["result"] = {
            "recordsAttempted": report.attempted,
            "recordsSucceeded": report.ready,
            "recordsSkipped": report.skipped,
            "recordsFailed": failed,
            "metricsScope": report.metrics_scope,
            "retryRunCreated": report.retry_run_created,
            "finalizationStatus": report.finalization_status,
            "mutationsThisInvocation": report.mutations_this_invocation,
            "sourceRequests": report.source_requests,
            "attachmentsSelected": report.attachments_considered,
            "filesReused": report.files_reused,
            "filesDownloaded": report.files_downloaded,
            "extractionCount": report.extraction_count,
            "aiDocumentCalls": report.ai_document_requests,
            "announcementAnalyses": report.announcement_analyses,
            "dbCommits": report.db_commits,
            "recordResults": [item.model_dump(mode="json", by_alias=True) for item in report.records],
            "errors": list(report.errors),
            "finalStatus": _terminal_status(report),
        }
    return {"recovery": recovery}


class SynapseRecoveryWriteStore(RecoveryStore):
    """Write adapter restricted to one immutable RETRY manifest."""

    def __init__(
        self,
        settings: Settings,
        manifest: RecoveryManifest,
        *,
        caps: RecoveryCaps,
        audit_phase: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if manifest.run_type != "RETRY":
            raise RecoveryExecutionError("live recovery requires a RETRY manifest")
        if not manifest.records:
            raise RecoveryExecutionError("live recovery manifest must contain at least one record")
        if len(manifest.records) > MAX_LIVE_RECOVERY_RECORDS:
            raise RecoveryExecutionError("live recovery record cap exceeded")
        if any(not record.recovery_allowed for record in manifest.records):
            raise RecoveryExecutionError("every live recovery record must be explicitly approved")
        normalized_audit_phase = audit_phase.strip()
        if not normalized_audit_phase:
            raise RecoveryExecutionError("live recovery audit phase is required")

        self._manifest = manifest
        self._manifest_id = manifest.manifest_id
        self._manifest_digest = manifest.digest
        self._allowed_ids = frozenset(record.disclosure_id for record in manifest.records)
        self._caps = caps
        self._audit_phase = normalized_audit_phase
        self._preflight = SynapseRecoveryPreflightStore(settings, transport=transport)
        self._client = SynapseClient(settings, transport=transport)
        self._run_id: str | None = None

    def close(self) -> None:
        self._preflight.close()
        self._client.close()

    def __enter__(self) -> "SynapseRecoveryWriteStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _assert_allowed(self, disclosure_id: UUID) -> None:
        if disclosure_id not in self._allowed_ids:
            raise RecoveryExecutionError("disclosure is outside the immutable recovery manifest")

    def fetch_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        self._assert_allowed(disclosure_id)
        return self._preflight.fetch_disclosure(disclosure_id)

    def refresh_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        self._assert_allowed(disclosure_id)
        return self._preflight.refresh_disclosure(disclosure_id)

    def list_files(self, disclosure_id: UUID):
        self._assert_allowed(disclosure_id)
        return self._preflight.list_files(disclosure_id)

    def analysis_state(self, disclosure_id: UUID):
        self._assert_allowed(disclosure_id)
        return self._preflight.analysis_state(disclosure_id)

    def create_retry_run(self, manifest: RecoveryManifest) -> str:
        if self._run_id is not None:
            raise RecoveryExecutionError("RETRY run already created")
        if manifest.manifest_id != self._manifest_id or manifest.digest != self._manifest_digest:
            raise RecoveryExecutionError("live recovery manifest changed after adapter initialization")
        if frozenset(record.disclosure_id for record in manifest.records) != self._allowed_ids:
            raise RecoveryExecutionError("live recovery manifest record allowlist changed")

        response = self._client.create_run(
            CreateRunRequest(
                mode="RETRY",
                engine_version="0.16.0-recovery-bounded-v1",
                metadata=_recovery_metadata(
                    manifest,
                    caps=self._caps,
                    audit_phase=self._audit_phase,
                ),
            )
        )
        self._run_id = response.run_id
        return response.run_id

    def finish_retry_run(self, run_id: str, report: RecoveryRunReport) -> None:
        if self._run_id != run_id:
            raise RecoveryExecutionError("unknown RETRY run id")
        final_status = _terminal_status(report)
        self._client.update_run(
            run_id,
            UpdateRunRequest(
                status=final_status,
                completed_at=_now_iso(),
                announcements_found=report.planned_records,
                files_downloaded=report.files_downloaded,
                files_extracted=report.extraction_count,
                analyses_completed=report.announcement_analyses,
                source_requests=report.source_requests,
                error_code=None if report.ok else f"RECOVERY_RUN_{final_status}",
                error_message=None if report.ok else (report.errors[0] if report.errors else "bounded recovery failed"),
                metadata=_recovery_metadata(
                    self._manifest_for_metadata(report),
                    caps=self._caps,
                    audit_phase=self._audit_phase,
                    report=report,
                ),
            ),
        )

    def _manifest_for_metadata(self, report: RecoveryRunReport) -> RecoveryManifest:
        manifest = self._manifest
        if manifest.manifest_id != report.manifest_id:
            raise RecoveryExecutionError("recovery manifest identity is unavailable")
        return manifest

    def update_processing_status(self, disclosure_id: UUID, status: ProcessingStatus) -> None:
        self._assert_allowed(disclosure_id)
        response = self._client.update_processing_status(
            str(disclosure_id),
            UpdateProcessingStatusRequest(processing_status=status),
        )
        if response.disclosure_id != str(disclosure_id) or response.processing_status != status:
            raise RecoveryExecutionError("Synapse returned an unexpected processing-status response")
        self._preflight.invalidate(disclosure_id)

    def upsert_file(self, disclosure_id: UUID, file: CurrentFile) -> None:
        self._assert_allowed(disclosure_id)
        if file.disclosure_id != disclosure_id:
            raise RecoveryExecutionError("file disclosure id does not match manifest disclosure")
        filename = _filename(file.source_url)
        size_bytes = None
        if file.local_path is not None and file.local_path.exists() and file.local_path.is_file():
            size_bytes = file.local_path.stat().st_size
        response = self._client.upsert_files(
            str(disclosure_id),
            DisclosureFilesUpsertRequest(
                files=[
                    DisclosureFileUpsertItem(
                        source_url=file.source_url,
                        original_filename=filename,
                        normalized_filename=filename,
                        file_extension=_suffix(filename),
                        sha256=file.sha256,
                        size_bytes=size_bytes,
                        selected_for_analysis=True,
                        selection_category="recovery",
                        selection_reason="bounded immutable-manifest recovery",
                        download_status=file.download_status,
                        extraction_status=file.extraction_status,
                        extraction_method="recovery" if file.extraction_status == "EXTRACTED" else None,
                        extracted_text_hash=file.extracted_text_hash,
                        extracted_text_ref=file.extracted_text_ref,
                        downloaded_at=_now_iso() if file.download_status == "DOWNLOADED" else None,
                        extracted_at=_now_iso() if file.extraction_status == "EXTRACTED" else None,
                    )
                ]
            ),
        )
        if not any(item.source_url == file.source_url for item in response.files):
            raise RecoveryExecutionError("Synapse did not confirm the recovery file upsert")
        self._preflight.invalidate(disclosure_id)

    def commit_analysis(self, disclosure_id: UUID, analysis: object) -> None:
        self._assert_allowed(disclosure_id)
        if not isinstance(analysis, CommitAnalysisRequest):
            raise RecoveryExecutionError(
                "analysis hook must return the existing CommitAnalysisRequest contract"
            )
        response = self._client.commit_analysis(str(disclosure_id), analysis)
        if not response.promoted:
            raise RecoveryExecutionError("Synapse did not promote the committed analysis")
        self._preflight.invalidate(disclosure_id)


__all__ = [
    "MAX_LIVE_AI_DOCUMENTS",
    "MAX_LIVE_ATTACHMENTS",
    "MAX_LIVE_RECOVERY_RECORDS",
    "MAX_LIVE_SOURCE_REQUESTS",
    "SynapseRecoveryWriteStore",
]
