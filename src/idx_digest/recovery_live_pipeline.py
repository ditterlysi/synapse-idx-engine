"""Per-ID recovery hooks backed by the existing IDX ingestion components.

The live runner is intentionally a narrow composition layer.  It resolves one
manifest external id with :class:`IdxWebsiteSource`, applies the production
attachment selector, uses the existing polite IDX transport for downloads,
reuses the canonical extractors and configured AI provider, and leaves all
checkpoint/watermark/coverage state outside the recovery boundary.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlparse

from .ai_provider import resolve_ai_provider
from .config import Settings
from .extractors import extract_document
from .idx_polite_http import CURRENT_IDX_BASE_URL, PoliteFetchClient
from .recovery_runner import (
    CurrentDisclosure,
    CurrentFile,
    DownloadedArtifact,
    ExtractedArtifact,
    RecoveryExecutionError,
    RecoveryHooks,
    RecoveryManifestRecord,
    RecoverySourcePlan,
)
from .sources.idx_website import FileCheckpointStore, IdxWebsiteSource
from .synapse_mapper import build_commit_request


@dataclass(frozen=True)
class _PreparedDisclosure:
    source_disclosure: Any
    download_queue: tuple[Any, ...]


class LiveRecoveryPipeline:
    """Build explicit hooks for a bounded, serial live recovery run."""

    def __init__(
        self,
        settings: Settings,
        *,
        max_source_requests: int,
        max_download_bytes: int | None = None,
        request_delay_seconds: float | None = None,
        request_jitter_seconds: float | None = None,
        summarizer_factory: Callable[[Settings], Any] | None = None,
        extractor: Callable[[Path, str, Settings], Any] = extract_document,
        transport: Any = None,
    ) -> None:
        self.settings = settings
        self.extractor = extractor
        self.client = PoliteFetchClient(
            base_url=CURRENT_IDX_BASE_URL,
            user_agent=settings.idx_user_agent,
            request_delay_seconds=(
                max(10.0, settings.synapse_daily_request_delay_seconds)
                if request_delay_seconds is None
                else request_delay_seconds
            ),
            request_jitter_seconds=(
                max(2.0, settings.synapse_daily_request_jitter_seconds)
                if request_jitter_seconds is None
                else request_jitter_seconds
            ),
            max_retries=2,
            max_requests=max_source_requests,
            max_download_bytes_total=max_download_bytes or settings.synapse_daily_max_download_bytes,
            max_run_seconds=settings.synapse_daily_max_run_seconds,
            transport=transport,
        )
        recovery_dir = settings.data_dir / "recovery" / "live"
        self.source = IdxWebsiteSource(
            self.client,
            checkpoint_store=FileCheckpointStore(recovery_dir / "unused-checkpoint.json"),
            staging_dir=recovery_dir / "staging",
            page_size=min(settings.idx_page_size, 100),
            max_pages=1,
            wide_page_size=min(settings.idx_wide_page_probe_size, 1000),
            max_wide_page_size=min(settings.idx_wide_page_probe_max_size, 2000),
            timezone_name=settings.app_timezone,
        )
        self._prepared: dict[str, _PreparedDisclosure] = {}
        self._hashes_by_disclosure: dict[str, list[str]] = {}
        self._runtime = resolve_ai_provider(settings)
        self._summarizer = (
            summarizer_factory or self._runtime.summarizer_factory
        )(self._runtime.settings)

    @property
    def source_request_count(self) -> int:
        return self.client.request_count

    def close(self) -> None:
        close = getattr(self._summarizer, "close", None)
        if callable(close):
            close()
        self.client.close()

    def __enter__(self) -> "LiveRecoveryPipeline":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _cache_path(self, disclosure: CurrentDisclosure, attachment: Any) -> Path:
        source_url = str(attachment.source_url)
        suffix = Path(str(attachment.filename)).suffix.lower()
        if not suffix or len(suffix) > 12:
            suffix = ".bin"
        key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        return self.settings.data_dir / "raw" / disclosure.ticker / disclosure.external_id / f"{key}{suffix}"

    def resolve_local_file(self, current: CurrentDisclosure, file: Any) -> Any:
        """Attach the canonical local cache references to API file metadata.

        The Synapse preflight response intentionally contains durable metadata,
        not workstation paths.  Resolving the path locally keeps a resume
        state-aware and prevents a valid extracted file from being downloaded a
        second time.  No network or database operation occurs here.
        """

        if file.local_path is not None:
            return file
        source_url = str(file.source_url)
        suffix = Path(urlparse(source_url).path).suffix.lower()
        if not suffix or len(suffix) > 12:
            suffix = ".bin"
        key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
        raw_path = self.settings.data_dir / "raw" / current.ticker / current.external_id / f"{key}{suffix}"
        text_path = (
            self.settings.data_dir / "text" / current.ticker / f"{file.sha256}.txt"
            if file.sha256
            else None
        )
        updates: dict[str, object] = {}
        if raw_path.exists() and raw_path.is_file():
            updates["local_path"] = raw_path
        if (
            file.extraction_status == "EXTRACTED"
            and text_path is not None
            and text_path.exists()
            and text_path.is_file()
        ):
            updates["extracted_text_ref"] = str(text_path)
        return file.model_copy(update=updates) if updates else file

    def prepare_source(
        self,
        current: CurrentDisclosure,
        record: RecoveryManifestRecord,
        files: Sequence[CurrentFile],
    ) -> RecoverySourcePlan:
        before = self.client.request_count
        source_disclosure = self.source.resolve_exact_disclosure(current.external_id, current.ticker)
        metadata_requests = self.client.request_count - before
        selected = tuple(source_disclosure.attachments)
        existing_urls = {file.source_url for file in files}
        queue = tuple(attachment for attachment in selected if attachment.source_url not in existing_urls)
        planned_downloads = 0
        for attachment in queue:
            path = self._cache_path(current, attachment)
            if path.exists() and path.is_file() and path.stat().st_size > 0:
                try:
                    actual = self._digest(path)
                except OSError:
                    actual = ""
                if actual and (
                    len(record.expected_attachment_hashes) < record.declared_attachment_count
                    or actual in set(record.expected_attachment_hashes)
                ):
                    continue
            planned_downloads += 1
        self._prepared[str(current.disclosure_id)] = _PreparedDisclosure(source_disclosure, queue)
        self._hashes_by_disclosure[str(current.disclosure_id)] = [
            file.sha256 for file in files if file.sha256
        ]
        return RecoverySourcePlan(
            selected_attachment_count=len(selected),
            metadata_requests=metadata_requests,
            planned_download_requests=planned_downloads,
        )

    def download_attachment(
        self,
        current: CurrentDisclosure,
        index: int,
        expected_hash: str | None,
    ) -> DownloadedArtifact:
        prepared = self._prepared.get(str(current.disclosure_id))
        if prepared is None or index >= len(prepared.download_queue):
            raise RecoveryExecutionError("source attachment plan is missing or exhausted")
        attachment = prepared.download_queue[index]
        path = self._cache_path(current, attachment)
        cache_hit = path.exists() and path.is_file() and path.stat().st_size > 0
        if cache_hit:
            digest = self._digest(path)
            self._hashes_by_disclosure.setdefault(str(current.disclosure_id), []).append(digest)
            return DownloadedArtifact(str(attachment.source_url), path, digest, cache_hit=True)
        self.client.download(str(attachment.source_url), path)
        digest = self._digest(path)
        self._hashes_by_disclosure.setdefault(str(current.disclosure_id), []).append(digest)
        return DownloadedArtifact(str(attachment.source_url), path, digest, cache_hit=False)

    def extract_attachment(
        self,
        current: CurrentDisclosure,
        file: CurrentFile | None,
        artifact: DownloadedArtifact,
    ) -> ExtractedArtifact:
        if file is None:
            raise RecoveryExecutionError("extraction requires current file metadata")
        content_type = mimetypes.guess_type(str(artifact.path))[0] or "application/octet-stream"
        result = self.extractor(artifact.path, content_type, self.settings)
        text_hash = hashlib.sha256(result.text.encode("utf-8", errors="replace")).hexdigest()
        text_path = self.settings.data_dir / "text" / current.ticker / f"{artifact.sha256}.txt"
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text(result.text, encoding="utf-8")
        return ExtractedArtifact(result.text, text_hash, getattr(result, "method", "existing"))

    def analyze_document(self, current: CurrentDisclosure, file: CurrentFile, text: str) -> object:
        if not text.strip():
            raise RecoveryExecutionError(f"extracted text is unavailable for {file.source_url}")
        prepared = self._prepared.get(str(current.disclosure_id))
        filename = Path(file.source_url).name or "idx-attachment"
        if prepared is not None:
            for attachment in (*prepared.source_disclosure.attachments, *prepared.download_queue):
                if attachment.source_url == file.source_url:
                    filename = attachment.filename
                    break
        return self._summarizer.summarize_document(
            ticker=current.ticker,
            filename=filename,
            text=text,
            stream=False,
            source_url=file.source_url,
            announcement_id=current.external_id,
        )

    def analyze_announcement(self, current: CurrentDisclosure, documents: Sequence[object]) -> object:
        prepared = self._prepared.get(str(current.disclosure_id))
        source_disclosure = prepared.source_disclosure if prepared is not None else None
        announcement = {
            "id2": current.external_id,
            "ticker": current.ticker,
            "title": current.title or current.external_id,
            "source_url": getattr(source_disclosure, "source_url", None),
            "announced_at": getattr(getattr(source_disclosure, "announced_at", None), "isoformat", lambda: None)(),
        }
        summary = self._summarizer.summarize_announcement(
            announcement=announcement,
            documents=list(documents),
            stream=False,
        )
        prompt_version = getattr(self._summarizer, "announcement_prompt_version", "legacy-announcement")
        provider = self._runtime.provider
        model = getattr(self._summarizer, "model", self._runtime.model)
        hashes = self._hashes_by_disclosure.get(str(current.disclosure_id), [])
        return build_commit_request(
            ticker=current.ticker,
            title=current.title or current.external_id,
            announcement_id=current.external_id,
            summary=summary,
            analysis_mode="full",
            model=model,
            provider=provider,
            prompt_version=prompt_version,
            attachment_hashes=hashes,
        )

    def hooks(self) -> RecoveryHooks:
        return RecoveryHooks(
            download_attachment=self.download_attachment,
            extract_attachment=self.extract_attachment,
            analyze_document=self.analyze_document,
            analyze_announcement=self.analyze_announcement,
            prepare_source=self.prepare_source,
            resolve_local_file=self.resolve_local_file,
            source_request_counter=lambda: self.client.request_count,
        )


__all__ = ["LiveRecoveryPipeline"]
