from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .config import Settings
from .daily_guardrails import DailyPolicy, DailyRunBudget
from .db import Database
from .incremental import CoverageRange, normalize_coverage_ranges
from .synapse_client import SynapseClient
from .synapse_contract import (
    CoverageCommitRequest,
    CreateRunRequest,
    DisclosureFilesUpsertRequest,
    DisclosureFileUpsertItem,
    DisclosureUpsertItem,
    DisclosureUpsertRequest,
    UpdateProcessingStatusRequest,
    UpdateRunRequest,
)
from .synapse_mapper import build_commit_request
from .synapse_runtime import (
    BudgetedSummarizerProxy,
    ConservativeAttachmentDownloader,
    ConservativeIDXClient,
    ConservativeRuntime,
    bind_runtime,
)

DISCLOSURE_BATCH_SIZE = 20


def _engine_version() -> str:
    try:
        return version("synapse-idx-engine")
    except PackageNotFoundError:
        return "0.16.0"


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Synapse pipeline windows must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _chunks(values: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _text_hash(path_value: str | None) -> str | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _size_bytes(path_value: str | None) -> int | None:
    if not path_value:
        return None
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return None
    return path.stat().st_size


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


@dataclass
class PublishStats:
    announcements_available: int = 0
    announcements_created: int = 0
    files_published: int = 0
    files_downloaded: int = 0
    files_extracted: int = 0
    analyses_completed: int = 0
    partial_disclosures: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class SynapseRunResult:
    run_id: str
    status: str
    report: dict[str, Any]
    publish: PublishStats
    budget: dict[str, int | float]
    coverage_committed: bool


@contextmanager
def _patched_pipeline_dependencies() -> Iterator[None]:
    """Swap only the dependencies used by Pipeline.run, then restore them."""
    from . import pipeline as pipeline_module

    original_client = pipeline_module.IDXClient
    original_downloader = pipeline_module.AttachmentDownloader
    pipeline_module.IDXClient = ConservativeIDXClient
    pipeline_module.AttachmentDownloader = ConservativeAttachmentDownloader
    try:
        yield
    finally:
        pipeline_module.IDXClient = original_client
        pipeline_module.AttachmentDownloader = original_downloader


class SynapsePublisher:
    def __init__(self, settings: Settings, client: Any):
        self.settings = settings
        self.client = client

    def _window_announcements(
        self,
        db: Database,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> list[dict[str, Any]]:
        start_value = start_at.isoformat()
        end_value = end_at.isoformat()
        with db.connect() as conn:
            ticker_rows = conn.execute(
                """
                SELECT DISTINCT ticker
                FROM announcements
                WHERE announced_at BETWEEN ? AND ?
                ORDER BY ticker
                """,
                (start_value, end_value),
            ).fetchall()
        announcements: list[dict[str, Any]] = []
        for ticker_row in ticker_rows:
            bundle = db.company_audit_bundle(str(ticker_row["ticker"]), start_value, end_value)
            announcements.extend(bundle.get("announcements") or [])
        announcements.sort(
            key=lambda item: (str(item.get("announced_at") or ""), str(item.get("id2") or ""))
        )
        return announcements

    @staticmethod
    def _disclosure_item(item: dict[str, Any]) -> DisclosureUpsertItem:
        raw: dict[str, Any] = {}
        try:
            parsed = json.loads(str(item.get("raw_json") or "{}"))
            if isinstance(parsed, dict):
                raw = parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            raw = {}
        announcement = raw.get("pengumuman") if isinstance(raw.get("pengumuman"), dict) else {}
        metadata: dict[str, object] = {
            "source": "IDX",
            "announcementNo": str(
                item.get("announcement_no") or announcement.get("NoPengumuman") or ""
            )
            or None,
            "legacyAnalysisMode": str(item.get("summary_analysis_mode") or "") or None,
            "legacyPromptVersion": str(item.get("summary_prompt_version") or "") or None,
        }
        metadata = {key: value for key, value in metadata.items() if value is not None}
        return DisclosureUpsertItem(
            idx_announcement_id=str(item["id2"]),
            ticker=str(item["ticker"]),
            announced_at=str(item["announced_at"]),
            title=str(item.get("title") or "IDX disclosure"),
            subject=str(item.get("subject") or "").strip() or None,
            disclosure_type=str(item.get("announcement_type") or "").strip() or None,
            source_url=None,
            raw_metadata=metadata,
        )

    @staticmethod
    def _file_item(row: dict[str, Any]) -> DisclosureFileUpsertItem | None:
        source_url = str(row.get("url") or "").strip()
        if not _valid_http_url(source_url):
            return None
        filename = str(row.get("original_filename") or "").strip() or None
        suffix = Path(filename or urlparse(source_url).path).suffix.lower().lstrip(".") or None
        local_path = str(row.get("local_path") or "").strip() or None
        text_path = str(row.get("extracted_text_path") or "").strip() or None
        sha256 = str(row.get("sha256") or "").strip().lower() or None
        if sha256 and len(sha256) != 64:
            sha256 = None
        extraction_error = str(row.get("extraction_error") or "").strip() or None
        downloaded = bool(local_path and sha256)
        extracted = bool(text_path and not extraction_error)
        extraction_status = "EXTRACTED" if extracted else ("FAILED" if extraction_error else "PENDING")
        return DisclosureFileUpsertItem(
            source_url=source_url,
            original_filename=filename,
            normalized_filename=filename,
            content_type=str(row.get("content_type") or "").strip() or None,
            file_extension=suffix,
            sha256=sha256,
            size_bytes=_size_bytes(local_path),
            selected_for_analysis=bool(row.get("selected_for_analysis")),
            selection_category=str(row.get("selection_category") or "").strip() or None,
            selection_reason=_truncate(str(row.get("selection_reason") or "").strip(), 1000) or None,
            download_status="DOWNLOADED" if downloaded else "PENDING",
            extraction_status=extraction_status,
            extraction_method=str(row.get("extraction_method") or "").strip() or None,
            extracted_text_hash=_text_hash(text_path),
            extracted_text_ref=None,
            extraction_error=_truncate(extraction_error, 1000) if extraction_error else None,
            downloaded_at=str(row.get("downloaded_at") or "").strip() or None,
            extracted_at=str(row.get("extracted_at") or "").strip() or None,
        )

    def publish_window(
        self,
        *,
        db: Database,
        run_id: str,
        start_at: datetime,
        end_at: datetime,
    ) -> PublishStats:
        stats = PublishStats()
        local_items = self._window_announcements(db, start_at=start_at, end_at=end_at)
        stats.announcements_available = len(local_items)
        if not local_items:
            return stats

        disclosure_ids: dict[str, str] = {}
        for batch in _chunks(local_items, DISCLOSURE_BATCH_SIZE):
            response = self.client.upsert_disclosures(
                DisclosureUpsertRequest(
                    run_id=run_id,
                    items=[self._disclosure_item(item) for item in batch],
                )
            )
            for result in response.items:
                disclosure_ids[result.idx_announcement_id] = result.disclosure_id
                if result.created:
                    stats.announcements_created += 1

        for item in local_items:
            announcement_id = str(item["id2"])
            disclosure_id = disclosure_ids.get(announcement_id)
            if not disclosure_id:
                stats.errors.append(f"{announcement_id}: Synapse disclosure id missing after upsert")
                stats.partial_disclosures += 1
                continue

            file_items: list[DisclosureFileUpsertItem] = []
            invalid_selected_source = False
            attachment_hashes: list[str] = []
            for row in item.get("attachments") or []:
                if not isinstance(row, dict):
                    continue
                mapped = self._file_item(row)
                if mapped is None:
                    if bool(row.get("selected_for_analysis")):
                        invalid_selected_source = True
                    continue
                file_items.append(mapped)
                if mapped.sha256:
                    attachment_hashes.append(mapped.sha256)
                if mapped.download_status == "DOWNLOADED":
                    stats.files_downloaded += 1
                if mapped.extraction_status == "EXTRACTED":
                    stats.files_extracted += 1

            try:
                for file_batch in _chunks(file_items, 100):
                    response = self.client.upsert_files(
                        disclosure_id,
                        DisclosureFilesUpsertRequest(files=file_batch),
                    )
                    stats.files_published += len(response.files)

                summary = item.get("announcement_summary")
                if invalid_selected_source:
                    raise ValueError("selected source file has an unsupported/non-HTTP URL")
                if not isinstance(summary, dict):
                    raise ValueError("validated announcement summary is not available")

                self.client.update_processing_status(
                    disclosure_id,
                    UpdateProcessingStatusRequest(processing_status="ANALYZING"),
                )
                request = build_commit_request(
                    ticker=str(item["ticker"]),
                    title=str(item.get("title") or ""),
                    announcement_id=announcement_id,
                    summary=summary,
                    analysis_mode=str(item.get("summary_analysis_mode") or "") or None,
                    model=str(item.get("summary_model") or self.settings.openrouter_model),
                    provider=self.settings.openrouter_provider,
                    prompt_version=str(item.get("summary_prompt_version") or "announcement-v3"),
                    attachment_hashes=attachment_hashes,
                )
                self.client.commit_analysis(disclosure_id, request)
                stats.analyses_completed += 1
            except Exception as exc:
                stats.partial_disclosures += 1
                stats.errors.append(f"{announcement_id}: {exc}")
                try:
                    self.client.update_processing_status(
                        disclosure_id,
                        UpdateProcessingStatusRequest(processing_status="PARTIAL"),
                    )
                except Exception as status_exc:
                    stats.errors.append(f"{announcement_id}: could not mark PARTIAL: {status_exc}")

        return stats


def _report_proves_window(report: dict[str, Any], start_at: datetime, end_at: datetime) -> bool:
    if report.get("scrape_complete") is not True or report.get("metadata_deferred_ranges"):
        return False
    ranges: list[CoverageRange] = []
    for item in report.get("metadata_coverage_after") or []:
        if not isinstance(item, dict):
            continue
        try:
            ranges.append(
                CoverageRange(
                    datetime.fromisoformat(str(item["start_at"])),
                    datetime.fromisoformat(str(item["end_at"])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return any(
        value.start <= start_at and end_at <= value.end
        for value in normalize_coverage_ranges(ranges)
    )


class SynapsePipelineRunner:
    """Gated one-window runner; no CLI or schedule invokes this automatically."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[Settings], Any] = SynapseClient,
        pipeline_factory: Callable[[Settings], Any] | None = None,
        runtime_factory: Callable[[DailyPolicy, DailyRunBudget], ConservativeRuntime] = ConservativeRuntime,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.pipeline_factory = pipeline_factory
        self.runtime_factory = runtime_factory

    def _pipeline(self, settings: Settings) -> Any:
        if self.pipeline_factory is not None:
            return self.pipeline_factory(settings)
        from .pipeline import Pipeline

        return Pipeline(settings)

    def run_window(self, *, start_at: datetime, end_at: datetime) -> SynapseRunResult:
        if start_at.tzinfo is None or end_at.tzinfo is None:
            raise ValueError("start_at and end_at must be timezone-aware")
        if end_at <= start_at:
            raise ValueError("end_at must be greater than start_at")

        policy = DailyPolicy.from_settings(self.settings)
        runtime_settings = policy.apply_to_runtime(self.settings)
        local_tz = ZoneInfo(runtime_settings.app_timezone)
        local_start = start_at.astimezone(local_tz)
        local_end = end_at.astimezone(local_tz)
        budget = DailyRunBudget(policy)
        runtime = self.runtime_factory(policy, budget)
        requested_from = _utc_iso(start_at)
        requested_to = _utc_iso(end_at)

        with self.client_factory(runtime_settings) as client:
            run_id = client.create_run(
                CreateRunRequest(
                    mode="DAILY",
                    requested_from=requested_from,
                    requested_to=requested_to,
                    engine_version=_engine_version(),
                )
            ).run_id
            pipeline = None
            report: dict[str, Any] = {}
            publish = PublishStats()
            try:
                with bind_runtime(runtime), _patched_pipeline_dependencies():
                    pipeline = self._pipeline(runtime_settings)
                    summarizer = getattr(pipeline, "summarizer", None)
                    if summarizer is not None:
                        pipeline.summarizer = BudgetedSummarizerProxy(summarizer, budget)
                    report = pipeline.run(
                        start_at=local_start,
                        end_at=local_end,
                        ticker=None,
                        keyword="",
                        max_announcements=None,
                        attachment_policy="smart",
                        instrument_scope="stocks",
                        metadata_mode="incremental",
                    )
                    publish = SynapsePublisher(runtime_settings, client).publish_window(
                        db=pipeline.db,
                        run_id=run_id,
                        start_at=local_start,
                        end_at=local_end,
                    )
            except Exception as exc:
                client.update_run(
                    run_id,
                    UpdateRunRequest(
                        status="FAILED",
                        completed_at=_now_iso(),
                        source_requests=budget.source_requests,
                        error_code="ENGINE_RUN_FAILED",
                        error_message=_truncate(str(exc) or type(exc).__name__, 1000),
                    ),
                )
                raise
            finally:
                if pipeline is not None:
                    try:
                        pipeline.close()
                    except Exception:
                        pass

            pipeline_complete = (
                report.get("status") == "completed" and report.get("scrape_complete") is True
            )
            coverage_proven = _report_proves_window(report, local_start, local_end)
            complete = pipeline_complete and not publish.errors and coverage_proven
            status = "COMPLETE" if complete else "PARTIAL"
            error_code = None
            error_message = None
            if not complete:
                reasons: list[str] = []
                if not pipeline_complete:
                    reasons.append("local pipeline did not complete cleanly")
                if publish.errors:
                    reasons.append("; ".join(publish.errors[:4]))
                if not coverage_proven:
                    reasons.append("requested window is not fully proven by local metadata coverage")
                error_code = "ENGINE_RUN_PARTIAL"
                error_message = _truncate(" | ".join(reasons) or "partial Synapse engine run", 1000)

            client.update_run(
                run_id,
                UpdateRunRequest(
                    status=status,
                    completed_at=_now_iso(),
                    announcements_found=int(report.get("metadata_announcements_collected") or 0),
                    announcements_new=publish.announcements_created,
                    files_downloaded=publish.files_downloaded,
                    files_extracted=publish.files_extracted,
                    analyses_completed=publish.analyses_completed,
                    source_requests=budget.source_requests,
                    error_code=error_code,
                    error_message=error_message,
                ),
            )

            coverage_committed = False
            if status == "COMPLETE":
                client.commit_coverage(
                    CoverageCommitRequest(
                        run_id=run_id,
                        scope="ALL",
                        covered_from=requested_from,
                        covered_to=requested_to,
                    )
                )
                coverage_committed = True

            return SynapseRunResult(
                run_id=run_id,
                status=status,
                report=report,
                publish=publish,
                budget=budget.snapshot(),
                coverage_committed=coverage_committed,
            )
