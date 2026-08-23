from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import Settings
from .daily_guardrails import DailyPolicy, DailyRunBudget
from .extractors import ExtractionResult, extract_document
from .source_contract import DisclosureSource, SourceAttachment, SourceContractError, SourceDisclosure
from .summarizer import OpenRouterSummarizer
from .synapse_client import SynapseClient
from .synapse_contract import (
    CoverageCommitRequest,
    CreateRunRequest,
    DisclosureFilesUpsertRequest,
    DisclosureFileUpsertItem,
    DisclosureUpsertItem,
    DisclosureUpsertRequest,
    RunMode,
    UpdateProcessingStatusRequest,
    UpdateRunRequest,
)
from .synapse_mapper import build_commit_request

DISCLOSURE_BATCH_SIZE = 20
FILE_BATCH_SIZE = 100


def _engine_version() -> str:
    try:
        return version("synapse-idx-engine")
    except PackageNotFoundError:
        return "0.16.0"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source ingestion windows must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _chunks(values: list[Any], size: int):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _http_url(value: str | None) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SourceContractError(f"source URL must use http(s): {candidate!r}")
    return candidate


def _content_type(attachment: SourceAttachment) -> str:
    supplied = (attachment.content_type or "").strip()
    if supplied:
        return supplied
    guessed, _encoding = mimetypes.guess_type(attachment.filename)
    return guessed or "application/octet-stream"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


@dataclass
class SourcePublishStats:
    disclosures_available: int = 0
    disclosures_created: int = 0
    disclosures_skipped_ready: int = 0
    attachments_staged: int = 0
    files_published: int = 0
    files_extracted: int = 0
    documents_analyzed: int = 0
    analyses_completed: int = 0
    partial_disclosures: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SourceIngestionResult:
    run_id: str
    status: str
    source_id: str
    source_complete: bool
    source_diagnostics: dict[str, Any]
    publish: SourcePublishStats
    budget: dict[str, int]
    coverage_committed: bool
    processing_ok: bool


class SourceIngestionRunner:
    """Publish normalized disclosures without coupling to a website collector.

    The current implementation requires attachments to be staged locally by the
    source adapter. It reuses the existing extraction, announcement-v3 analysis,
    compatibility mapper, and narrow Synapse Internal API client.
    """

    def __init__(
        self,
        settings: Settings,
        source: DisclosureSource,
        *,
        client_factory: Callable[[Settings], Any] = SynapseClient,
        summarizer_factory: Callable[[Settings], Any] = OpenRouterSummarizer,
        extractor: Callable[[Path, str, Settings], ExtractionResult] = extract_document,
        run_mode: RunMode = "MANUAL_BACKFILL",
        allow_coverage_commit: bool = False,
        require_external_id_prefix: str | None = None,
    ) -> None:
        self.settings = settings
        self.source = source
        self.client_factory = client_factory
        self.summarizer_factory = summarizer_factory
        self.extractor = extractor
        self.run_mode = run_mode
        self.allow_coverage_commit = allow_coverage_commit
        self.require_external_id_prefix = require_external_id_prefix

    def _validate_disclosure(self, disclosure: SourceDisclosure) -> None:
        if self.require_external_id_prefix and not disclosure.external_id.startswith(
            self.require_external_id_prefix
        ):
            raise SourceContractError(
                f"source disclosure external_id must start with {self.require_external_id_prefix!r}"
            )
        _http_url(disclosure.source_url)
        for attachment in disclosure.attachments:
            if attachment.local_path is None:
                raise SourceContractError(
                    f"{disclosure.external_id}: current source-neutral runner requires staged local attachments"
                )
            _http_url(attachment.source_url)

    @staticmethod
    def _disclosure_attachment_hashes(disclosure: SourceDisclosure) -> list[str]:
        hashes: list[str] = []
        for attachment in disclosure.attachments:
            digest = attachment.sha256
            path = attachment.local_path
            if digest is None and path is not None and path.exists() and path.is_file():
                digest = _file_sha256(path)
            if digest:
                hashes.append(digest.lower())
        return list(dict.fromkeys(hashes))

    @staticmethod
    def _disclosure_item(source_id: str, disclosure: SourceDisclosure) -> DisclosureUpsertItem:
        source_url = _http_url(disclosure.source_url)
        attachment_hashes = SourceIngestionRunner._disclosure_attachment_hashes(disclosure)
        return DisclosureUpsertItem(
            idx_announcement_id=disclosure.external_id,
            ticker=disclosure.ticker,
            announced_at=disclosure.announced_at.isoformat(),
            title=disclosure.title,
            subject=(disclosure.subject or "").strip() or None,
            disclosure_type=(disclosure.disclosure_type or "").strip() or None,
            source_url=source_url,
            raw_metadata={
                "sourceId": source_id,
                "sourceExternalId": disclosure.external_id,
                "sourceAttachmentCount": len(disclosure.attachments),
                "sourceAttachmentSha256s": attachment_hashes,
            },
        )

    @staticmethod
    def _announcement(disclosure: SourceDisclosure) -> dict[str, Any]:
        return {
            "id2": disclosure.external_id,
            "ticker": disclosure.ticker,
            "announced_at": disclosure.announced_at.isoformat(),
            "title": disclosure.title,
            "subject": disclosure.subject,
            "announcement_type": disclosure.disclosure_type,
            "source_url": disclosure.source_url,
        }

    def _process_attachment(
        self,
        *,
        settings: Settings,
        budget: DailyRunBudget,
        summarizer: Any,
        disclosure: SourceDisclosure,
        attachment: SourceAttachment,
        stats: SourcePublishStats,
    ) -> tuple[dict[str, Any], DisclosureFileUpsertItem | None, str]:
        path = attachment.local_path
        if path is None or not path.exists() or not path.is_file():
            raise SourceContractError(
                f"{disclosure.external_id}: staged attachment is unavailable: {attachment.filename}"
            )

        budget.consume_attachment()
        size_bytes = path.stat().st_size
        budget.consume_download_bytes(size_bytes)
        stats.attachments_staged += 1

        content_type = _content_type(attachment)
        digest = attachment.sha256 or _file_sha256(path)
        extracted = self.extractor(path, content_type, settings)
        stats.files_extracted += 1
        extracted_hash = _text_sha256(extracted.text)

        budget.consume_ai_document()
        source_url = _http_url(attachment.source_url)
        document_summary = summarizer.summarize_document(
            ticker=disclosure.ticker,
            filename=attachment.filename,
            text=extracted.text,
            stream=False,
            source_url=source_url,
            announcement_id=disclosure.external_id,
        )
        stats.documents_analyzed += 1

        document = {
            "url": source_url or "",
            "filename": attachment.filename,
            "extraction_error": None,
            "summary": document_summary,
        }

        file_item = None
        if source_url:
            suffix = path.suffix.lower().lstrip(".") or None
            file_item = DisclosureFileUpsertItem(
                source_url=source_url,
                original_filename=attachment.filename,
                normalized_filename=attachment.filename,
                content_type=content_type,
                file_extension=suffix,
                sha256=digest,
                size_bytes=size_bytes,
                selected_for_analysis=True,
                selection_category="source-adapter",
                selection_reason="Staged by an approved source adapter and analyzed locally.",
                download_status="DOWNLOADED",
                extraction_status="EXTRACTED",
                extraction_method=extracted.method,
                extracted_text_hash=extracted_hash,
                extracted_text_ref=None,
                extracted_at=_now_iso(),
            )
        return document, file_item, digest

    def run_window(self, *, start_at: datetime, end_at: datetime) -> SourceIngestionResult:
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError("start_at must be timezone-aware")
        if end_at.tzinfo is None or end_at.utcoffset() is None:
            raise ValueError("end_at must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be greater than start_at")

        policy = DailyPolicy.from_settings(self.settings)
        runtime_settings = policy.apply_to_runtime(self.settings)
        budget = DailyRunBudget(policy)
        requested_from = _utc_iso(start_at)
        requested_to = _utc_iso(end_at)

        with self.client_factory(runtime_settings) as client:
            run_id = client.create_run(
                CreateRunRequest(
                    mode=self.run_mode,
                    requested_from=requested_from,
                    requested_to=requested_to,
                    engine_version=_engine_version(),
                )
            ).run_id

            summarizer = None
            stats = SourcePublishStats()
            try:
                source_result = self.source.collect_window(start_at=start_at, end_at=end_at)
                if source_result.requested_start != start_at or source_result.requested_end != end_at:
                    raise SourceContractError("source result requested window does not match the runner window")
                for disclosure in source_result.disclosures:
                    self._validate_disclosure(disclosure)

                stats.disclosures_available = len(source_result.disclosures)
                disclosure_ids: dict[str, str] = {}
                disclosure_statuses: dict[str, str | None] = {}
                local_items = list(source_result.disclosures)
                for batch in _chunks(local_items, DISCLOSURE_BATCH_SIZE):
                    response = client.upsert_disclosures(
                        DisclosureUpsertRequest(
                            run_id=run_id,
                            items=[self._disclosure_item(source_result.source_id, item) for item in batch],
                        )
                    )
                    for item in response.items:
                        disclosure_ids[item.idx_announcement_id] = item.disclosure_id
                        disclosure_statuses[item.idx_announcement_id] = getattr(
                            item, "processing_status", None
                        )
                        if item.created:
                            stats.disclosures_created += 1

                if any(disclosure_statuses.get(item.external_id) != "READY" for item in local_items):
                    summarizer = self.summarizer_factory(runtime_settings)

                for disclosure in local_items:
                    disclosure_id = disclosure_ids.get(disclosure.external_id)
                    if not disclosure_id:
                        stats.partial_disclosures += 1
                        stats.errors.append(
                            f"{disclosure.external_id}: Synapse disclosure id missing after upsert"
                        )
                        continue
                    if disclosure_statuses.get(disclosure.external_id) == "READY":
                        stats.disclosures_skipped_ready += 1
                        continue
                    try:
                        if not disclosure.attachments:
                            raise SourceContractError("source disclosure has no staged attachments")

                        client.update_processing_status(
                            disclosure_id,
                            UpdateProcessingStatusRequest(processing_status="EXTRACTING"),
                        )

                        documents: list[dict[str, Any]] = []
                        file_items: list[DisclosureFileUpsertItem] = []
                        attachment_hashes: list[str] = []
                        for attachment in disclosure.attachments:
                            document, file_item, digest = self._process_attachment(
                                settings=runtime_settings,
                                budget=budget,
                                summarizer=summarizer,
                                disclosure=disclosure,
                                attachment=attachment,
                                stats=stats,
                            )
                            documents.append(document)
                            attachment_hashes.append(digest)
                            if file_item is not None:
                                file_items.append(file_item)

                        for file_batch in _chunks(file_items, FILE_BATCH_SIZE):
                            response = client.upsert_files(
                                disclosure_id,
                                DisclosureFilesUpsertRequest(files=file_batch),
                            )
                            stats.files_published += len(response.files)

                        client.update_processing_status(
                            disclosure_id,
                            UpdateProcessingStatusRequest(processing_status="ANALYZING"),
                        )
                        announcement_summary = summarizer.summarize_announcement(
                            announcement=self._announcement(disclosure),
                            documents=documents,
                            stream=False,
                        )
                        request = build_commit_request(
                            ticker=disclosure.ticker,
                            title=disclosure.title,
                            announcement_id=disclosure.external_id,
                            summary=announcement_summary,
                            analysis_mode="source_adapter",
                            model=runtime_settings.openrouter_model,
                            provider=runtime_settings.openrouter_provider,
                            prompt_version=getattr(
                                summarizer,
                                "announcement_prompt_version",
                                "announcement-v3",
                            ),
                            attachment_hashes=attachment_hashes,
                        )
                        client.commit_analysis(disclosure_id, request)
                        stats.analyses_completed += 1
                    except Exception as exc:
                        stats.partial_disclosures += 1
                        stats.errors.append(
                            f"{disclosure.external_id}: {_truncate(str(exc) or type(exc).__name__, 700)}"
                        )
                        try:
                            client.update_processing_status(
                                disclosure_id,
                                UpdateProcessingStatusRequest(processing_status="PARTIAL"),
                            )
                        except Exception as status_exc:
                            stats.errors.append(
                                f"{disclosure.external_id}: could not mark PARTIAL: "
                                f"{_truncate(str(status_exc), 400)}"
                            )

                processing_ok = not stats.errors and stats.partial_disclosures == 0
                source_complete = source_result.complete and source_result.proves_requested_window()
                complete = processing_ok and source_complete and self.allow_coverage_commit
                status = "COMPLETE" if complete else "PARTIAL"

                error_code = None
                error_message = None
                if not complete:
                    if not processing_ok:
                        error_code = "SOURCE_PROCESSING_PARTIAL"
                        error_message = _truncate("; ".join(stats.errors[:4]), 1000)
                    elif not source_complete:
                        error_code = "SOURCE_COVERAGE_UNPROVEN"
                        error_message = "Source processing succeeded but the requested window is not authoritative."
                    else:
                        error_code = "SOURCE_COVERAGE_NOT_AUTHORIZED"
                        error_message = "Source processing succeeded but this runner is not authorized to commit coverage."

                client.update_run(
                    run_id,
                    UpdateRunRequest(
                        status=status,
                        completed_at=_now_iso(),
                        announcements_found=stats.disclosures_available,
                        announcements_new=stats.disclosures_created,
                        files_downloaded=stats.attachments_staged,
                        files_extracted=stats.files_extracted,
                        analyses_completed=stats.analyses_completed,
                        source_requests=budget.source_requests,
                        error_code=error_code,
                        error_message=error_message,
                    ),
                )

                coverage_committed = False
                if status == "COMPLETE":
                    try:
                        client.commit_coverage(
                            CoverageCommitRequest(
                                run_id=run_id,
                                scope="ALL",
                                covered_from=requested_from,
                                covered_to=requested_to,
                            )
                        )
                        coverage_committed = True
                    except Exception as exc:
                        status = "PARTIAL"
                        client.update_run(
                            run_id,
                            UpdateRunRequest(
                                status="PARTIAL",
                                completed_at=_now_iso(),
                                announcements_found=stats.disclosures_available,
                                announcements_new=stats.disclosures_created,
                                files_downloaded=stats.attachments_staged,
                                files_extracted=stats.files_extracted,
                                analyses_completed=stats.analyses_completed,
                                source_requests=budget.source_requests,
                                error_code="COVERAGE_COMMIT_FAILED",
                                error_message=_truncate(f"coverage commit failed: {exc}", 1000),
                            ),
                        )

                return SourceIngestionResult(
                    run_id=run_id,
                    status=status,
                    source_id=source_result.source_id,
                    source_complete=source_complete,
                    source_diagnostics=dict(source_result.diagnostics),
                    publish=stats,
                    budget=budget.snapshot(),
                    coverage_committed=coverage_committed,
                    processing_ok=processing_ok,
                )
            except Exception as exc:
                try:
                    client.update_run(
                        run_id,
                        UpdateRunRequest(
                            status="FAILED",
                            completed_at=_now_iso(),
                            announcements_found=stats.disclosures_available,
                            announcements_new=stats.disclosures_created,
                            files_downloaded=stats.attachments_staged,
                            files_extracted=stats.files_extracted,
                            analyses_completed=stats.analyses_completed,
                            source_requests=budget.source_requests,
                            error_code="SOURCE_RUN_FAILED",
                            error_message=_truncate(str(exc) or type(exc).__name__, 1000),
                        ),
                    )
                except Exception:
                    pass
                raise
            finally:
                if summarizer is not None:
                    try:
                        summarizer.close()
                    except Exception:
                        pass
