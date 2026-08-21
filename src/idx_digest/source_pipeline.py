from __future__ import annotations

import hashlib
import mimetypes
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .config import Settings
from .daily_guardrails import DailyPolicy, DailyRunBudget
from .source_contract import DisclosureSource, SourceAttachment, SourceDisclosure, SourceWindowResult
from .synapse_client import SynapseClient
from .synapse_contract import CreateRunRequest, UpdateRunRequest
from .synapse_pipeline import PublishStats, SynapsePublisher, _engine_version, _now_iso, _truncate
from .synapse_runtime import BudgetedSummarizerProxy

SOURCE_IMPORT_MAX_DISCLOSURES = 20
SOURCE_IMPORT_MAX_ATTACHMENTS = 20
SOURCE_IMPORT_MAX_LOCAL_BYTES = 100_000_000
SOURCE_IMPORT_MAX_AI_DOCUMENTS = 20
SOURCE_IMPORT_MAX_RUN_SECONDS = 900


class SourcePipelineError(RuntimeError):
    """Raised when normalized source input cannot be processed safely."""


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourcePipelineError("source import timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_reference(source_id: str, disclosure_id: str, index: int, attachment: SourceAttachment) -> str:
    digest = attachment.sha256 or "unhashed"
    return (
        "synapse-local://"
        f"{quote(source_id, safe='')}/{quote(disclosure_id, safe='')}/{index}-{digest}"
    )


def _attachment_reference(
    source_id: str,
    disclosure: SourceDisclosure,
    index: int,
    attachment: SourceAttachment,
) -> str:
    source_url = (attachment.source_url or "").strip()
    return source_url or _local_reference(source_id, disclosure.external_id, index, attachment)


def _idx_attachment(
    source_id: str,
    disclosure: SourceDisclosure,
    index: int,
    attachment: SourceAttachment,
) -> tuple[dict[str, Any], str]:
    reference = _attachment_reference(source_id, disclosure, index, attachment)
    payload: dict[str, Any] = {
        "FullSavePath": reference,
        "OriginalFilename": attachment.filename,
        "PDFFilename": attachment.filename,
        "IsAttachment": True,
        "SynapseSourceUrl": attachment.source_url,
        "SynapseSourceMetadata": dict(attachment.metadata),
    }
    if attachment.content_type:
        payload["ContentType"] = attachment.content_type
    return payload, reference


def source_disclosure_to_idx(
    source_id: str,
    disclosure: SourceDisclosure,
) -> tuple[dict[str, Any], dict[str, SourceAttachment]]:
    """Bridge normalized data into the legacy pipeline's internal metadata shape."""
    attachments: list[dict[str, Any]] = []
    attachment_map: dict[str, SourceAttachment] = {}
    for index, attachment in enumerate(disclosure.attachments):
        payload, reference = _idx_attachment(source_id, disclosure, index, attachment)
        if reference in attachment_map:
            raise SourcePipelineError(
                f"duplicate attachment source reference {reference!r} in disclosure {disclosure.external_id}"
            )
        attachment_map[reference] = attachment
        attachments.append(payload)

    metadata = dict(disclosure.metadata)
    raw = {
        "pengumuman": {
            "Id2": disclosure.external_id,
            "Kode_Emiten": disclosure.ticker,
            "TglPengumuman": disclosure.announced_at.isoformat(),
            "JudulPengumuman": disclosure.title,
            "NoPengumuman": str(metadata.get("announcementNo") or ""),
            "JenisPengumuman": disclosure.disclosure_type or "",
            "PerihalPengumuman": disclosure.subject or "",
        },
        "attachments": attachments,
        "synapseSource": {
            "id": source_id,
            "sourceUrl": disclosure.source_url,
            "metadata": metadata,
        },
    }
    return raw, attachment_map


class SourceIDXBridge:
    """IDXClient-compatible facade backed entirely by one normalized source result."""

    def __init__(self, source_result: SourceWindowResult):
        self.source_result = source_result
        self.browser = None
        self._raw_items: list[tuple[SourceDisclosure, dict[str, Any]]] = []
        self.attachments: dict[str, SourceAttachment] = {}
        for disclosure in source_result.disclosures:
            raw, mapped = source_disclosure_to_idx(source_result.source_id, disclosure)
            collisions = self.attachments.keys() & mapped.keys()
            if collisions:
                raise SourcePipelineError(
                    f"duplicate attachment source references across disclosures: {sorted(collisions)!r}"
                )
            self.attachments.update(mapped)
            self._raw_items.append((disclosure, raw))

    def collect_announcements(
        self,
        start_at: datetime,
        end_at: datetime,
        ticker: str | None = None,
        keyword: str = "",
        emiten_type: str = "s",
        *,
        allow_ticker_fallback: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        del emiten_type, allow_ticker_fallback
        ticker_value = ticker.strip().upper() if ticker else None
        keyword_value = keyword.strip().casefold()
        items = [
            raw
            for disclosure, raw in self._raw_items
            if start_at <= disclosure.announced_at <= end_at
            and (ticker_value is None or disclosure.ticker == ticker_value)
            and (
                not keyword_value
                or keyword_value in disclosure.title.casefold()
                or keyword_value in (disclosure.subject or "").casefold()
            )
        ]
        complete = bool(
            self.source_result.complete
            and self.source_result.coverage_start is not None
            and self.source_result.coverage_end is not None
            and self.source_result.coverage_start <= start_at
            and end_at <= self.source_result.coverage_end
        )
        return items, {
            "complete": complete,
            "strategy": f"source-adapter:{self.source_result.source_id}",
            "collected": len(items),
            "reported_total": len(items),
            "source": self.source_result.source_id,
            "source_diagnostics": dict(self.source_result.diagnostics),
        }

    def stock_master_tickers(self):
        return None

    def browser_transport(self):
        raise SourcePipelineError("source-neutral local import never uses browser transport")

    def close(self) -> None:
        return None


class LocalSourceAttachmentDownloader:
    """AttachmentDownloader-compatible local-file reader with no network client."""

    def __init__(
        self,
        attachments: dict[str, SourceAttachment],
        budget: DailyRunBudget,
        *,
        observer: Any = None,
    ):
        self.attachments = attachments
        self.budget = budget
        self.observer = observer

    def download(
        self,
        *,
        ticker: str,
        announcement_id: str,
        url: str,
        original_filename: str,
    ) -> tuple[Path, str, str]:
        del ticker, announcement_id, original_filename
        attachment = self.attachments.get(url)
        if attachment is None:
            raise SourcePipelineError(f"unknown local source attachment reference: {url}")
        if attachment.local_path is None:
            raise SourcePipelineError(f"source attachment {url} has no local_path")
        path = Path(attachment.local_path).resolve()
        if not path.exists() or not path.is_file():
            raise SourcePipelineError(f"local source attachment no longer exists: {path}")

        size = path.stat().st_size
        self.budget.consume_attachment()
        self.budget.consume_download_bytes(size)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if attachment.sha256 and digest != attachment.sha256:
            raise SourcePipelineError(f"local source attachment changed after manifest validation: {path.name}")
        content_type = attachment.content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path, digest, content_type

    def close(self) -> None:
        return None


@contextmanager
def patched_source_dependencies(
    source_result: SourceWindowResult,
    budget: DailyRunBudget,
) -> Iterator[SourceIDXBridge]:
    """Patch only Pipeline's source/downloader dependencies, then restore them."""
    from . import pipeline as pipeline_module

    bridge = SourceIDXBridge(source_result)
    original_client = pipeline_module.IDXClient
    original_downloader = pipeline_module.AttachmentDownloader

    def source_client_factory(_settings: Settings, observer: Any = None):
        del observer
        return bridge

    def downloader_factory(
        _settings: Settings,
        *,
        browser_transport_factory: Any = None,
        observer: Any = None,
    ):
        del browser_transport_factory
        return LocalSourceAttachmentDownloader(bridge.attachments, budget, observer=observer)

    pipeline_module.IDXClient = source_client_factory  # type: ignore[assignment]
    pipeline_module.AttachmentDownloader = downloader_factory  # type: ignore[assignment]
    try:
        yield bridge
    finally:
        pipeline_module.IDXClient = original_client
        pipeline_module.AttachmentDownloader = original_downloader


@dataclass
class SourceRunResult:
    run_id: str
    status: str
    source: SourceWindowResult
    report: dict[str, Any]
    publish: PublishStats
    budget: dict[str, int | float]
    coverage_committed: bool = False


class SourcePipelineRunner:
    """Run a normalized local source through the existing extraction/AI/publish pipeline."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: Callable[[Settings], Any] = SynapseClient,
        pipeline_factory: Callable[[Settings], Any] | None = None,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.pipeline_factory = pipeline_factory

    def _pipeline(self, settings: Settings) -> Any:
        if self.pipeline_factory is not None:
            return self.pipeline_factory(settings)
        from .pipeline import Pipeline

        return Pipeline(settings)

    def _runtime_settings(self) -> Settings:
        return self.settings.model_copy(
            update={
                "idx_transport": "http",
                "synapse_daily_max_attachments": min(
                    self.settings.synapse_daily_max_attachments, SOURCE_IMPORT_MAX_ATTACHMENTS
                ),
                "synapse_daily_max_download_bytes": min(
                    self.settings.synapse_daily_max_download_bytes, SOURCE_IMPORT_MAX_LOCAL_BYTES
                ),
                "synapse_daily_max_ai_documents": min(
                    self.settings.synapse_daily_max_ai_documents, SOURCE_IMPORT_MAX_AI_DOCUMENTS
                ),
                "synapse_daily_max_run_seconds": min(
                    self.settings.synapse_daily_max_run_seconds, SOURCE_IMPORT_MAX_RUN_SECONDS
                ),
                "llm_concurrency": min(self.settings.llm_concurrency, 2),
                "llm_per_announcement_concurrency": min(self.settings.llm_per_announcement_concurrency, 2),
                "extraction_workers": min(self.settings.extraction_workers, 2),
            }
        )

    @staticmethod
    def _preflight(source_result: SourceWindowResult) -> None:
        if len(source_result.disclosures) > SOURCE_IMPORT_MAX_DISCLOSURES:
            raise SourcePipelineError(
                f"source import contains {len(source_result.disclosures)} disclosures; "
                f"maximum is {SOURCE_IMPORT_MAX_DISCLOSURES}"
            )
        attachments = [
            attachment
            for disclosure in source_result.disclosures
            for attachment in disclosure.attachments
        ]
        if len(attachments) > SOURCE_IMPORT_MAX_ATTACHMENTS:
            raise SourcePipelineError(
                f"source import contains {len(attachments)} attachments; maximum is {SOURCE_IMPORT_MAX_ATTACHMENTS}"
            )
        total_bytes = 0
        for attachment in attachments:
            if attachment.local_path is None:
                raise SourcePipelineError(
                    "current source-neutral import runner requires local_path for every attachment"
                )
            path = Path(attachment.local_path).resolve()
            if not path.exists() or not path.is_file():
                raise SourcePipelineError(f"local source attachment does not exist: {path}")
            total_bytes += path.stat().st_size
        if total_bytes > SOURCE_IMPORT_MAX_LOCAL_BYTES:
            raise SourcePipelineError(
                f"source import local bytes {total_bytes} exceed maximum {SOURCE_IMPORT_MAX_LOCAL_BYTES}"
            )

    def run_window(
        self,
        *,
        source: DisclosureSource,
        start_at: datetime,
        end_at: datetime,
    ) -> SourceRunResult:
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise SourcePipelineError("start_at must be timezone-aware")
        if end_at.tzinfo is None or end_at.utcoffset() is None:
            raise SourcePipelineError("end_at must be timezone-aware")
        if end_at <= start_at:
            raise SourcePipelineError("end_at must be greater than start_at")

        source_result = source.collect_window(start_at=start_at, end_at=end_at)
        self._preflight(source_result)
        runtime_settings = self._runtime_settings()
        local_tz = ZoneInfo(runtime_settings.app_timezone)
        local_start = start_at.astimezone(local_tz)
        local_end = end_at.astimezone(local_tz)
        policy = DailyPolicy.from_settings(runtime_settings)
        budget = DailyRunBudget(policy)
        requested_from = _utc_iso(start_at)
        requested_to = _utc_iso(end_at)

        with self.client_factory(runtime_settings) as client:
            run_id = client.create_run(
                CreateRunRequest(
                    mode="MANUAL_BACKFILL",
                    requested_from=requested_from,
                    requested_to=requested_to,
                    engine_version=_engine_version(),
                )
            ).run_id
            pipeline = None
            report: dict[str, Any] = {}
            publish = PublishStats()
            try:
                with patched_source_dependencies(source_result, budget):
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
                        attachment_policy="all_supported",
                        instrument_scope=f"source:{source_result.source_id}",
                        metadata_mode="historical_audit",
                    )
                    publish = SynapsePublisher(runtime_settings, client).publish_window(
                        db=pipeline.db,
                        run_id=run_id,
                        start_at=local_start,
                        end_at=local_end,
                    )
            except Exception as exc:
                try:
                    client.update_run(
                        run_id,
                        UpdateRunRequest(
                            status="FAILED",
                            completed_at=_now_iso(),
                            source_requests=0,
                            error_code="SOURCE_IMPORT_FAILED",
                            error_message=_truncate(str(exc) or type(exc).__name__, 1000),
                        ),
                    )
                except Exception:
                    pass
                raise
            finally:
                if pipeline is not None:
                    try:
                        pipeline.close()
                    except Exception:
                        pass

            pipeline_clean = report.get("status") == "completed" and not report.get("errors")
            source_authoritative = source_result.complete and source_result.proves_requested_window()
            complete = pipeline_clean and not publish.errors and source_authoritative
            status = "COMPLETE" if complete else "PARTIAL"
            reasons: list[str] = []
            if not pipeline_clean:
                reasons.append("local source pipeline did not complete cleanly")
            if publish.errors:
                reasons.append("; ".join(publish.errors[:4]))
            if not source_authoritative:
                reasons.append("source did not provide authoritative coverage for the requested window")
            error_code = None if complete else "SOURCE_IMPORT_PARTIAL"
            error_message = None if complete else _truncate(" | ".join(reasons), 1000)

            client.update_run(
                run_id,
                UpdateRunRequest(
                    status=status,
                    completed_at=_now_iso(),
                    announcements_found=len(source_result.disclosures),
                    announcements_new=publish.announcements_created,
                    files_downloaded=publish.files_downloaded,
                    files_extracted=publish.files_extracted,
                    analyses_completed=publish.analyses_completed,
                    source_requests=0,
                    error_code=error_code,
                    error_message=error_message,
                ),
            )

            # Manual/local source runs intentionally never write production coverage.
            # A future licensed adapter must add an explicit policy gate before this changes.
            return SourceRunResult(
                run_id=run_id,
                status=status,
                source=source_result,
                report=report,
                publish=publish,
                budget=budget.snapshot(),
                coverage_committed=False,
            )
