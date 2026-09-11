"""Bounded, per-disclosure IDX recovery orchestration.

This module deliberately does not know how to discover IDX disclosures.  A
manifest is the complete input boundary and a small data-store/processing hook
interface keeps recovery separate from discovery, checkpoint, watermark and
coverage state.  The command-line entry point uses the read-only snapshot
adapter below; an execution adapter can be supplied by a future, explicitly
approved pilot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol, Sequence
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .synapse_contract import ProcessingStatus

IDX_WEBSITE_SOURCE_ID = "idx-website"
PendingStatus = Literal["DISCOVERED", "EXTRACTING", "PARTIAL"]
Bucket = Literal["A", "B", "C", "D", "E", "F"]
RecoveryAction = Literal[
    "AI_ONLY_RETRY",
    "EXTRACTION_RESUME",
    "ATTACHMENT_DOWNLOAD",
    "METADATA_PROCESS_RESUME",
    "NOOP",
    "HELD",
    "MANUAL_REVIEW",
]


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class RecoveryModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
    )


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def _hashes(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("attachment hashes must be 64-character hexadecimal digests")
        result.append(normalized)
    return tuple(result)


class RecoveryManifestRecord(RecoveryModel):
    """One immutable allowlisted disclosure and its optimistic-concurrency guard."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )

    disclosure_id: UUID
    expected_status: PendingStatus
    expected_updated_at: datetime
    expected_external_id: str = Field(min_length=1, max_length=200)
    ticker: str = Field(min_length=1, max_length=10)
    bucket: Bucket
    declared_attachment_count: int = Field(ge=0, le=100)
    expected_attachment_hashes: tuple[str, ...] = ()
    intended_recovery_action: str = Field(min_length=1, max_length=80)
    recovery_allowed: bool = False
    source_id: str = IDX_WEBSITE_SOURCE_ID

    @field_validator("expected_updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return _aware(value, "expected_updated_at")

    @field_validator("expected_external_id")
    @classmethod
    def normalize_external_id(cls, value: str) -> str:
        if not value:
            raise ValueError("expected_external_id must not be empty")
        return value

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.upper()
        if not normalized:
            raise ValueError("ticker must not be empty")
        return normalized

    @field_validator("expected_attachment_hashes", mode="before")
    @classmethod
    def normalize_hashes(cls, value: Iterable[str]) -> tuple[str, ...]:
        return _hashes(value or ())

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if value != IDX_WEBSITE_SOURCE_ID:
            raise ValueError(f"source_id must be {IDX_WEBSITE_SOURCE_ID}")
        return value

    @property
    def initial_status(self) -> PendingStatus:
        return self.expected_status

    @property
    def initial_updated_at(self) -> datetime:
        return self.expected_updated_at


class RecoveryManifest(RecoveryModel):
    """Frozen manifest; records are the only database rows a run may inspect."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )

    run_type: Literal["RETRY"] = "RETRY"
    manifest_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    records: tuple[RecoveryManifestRecord, ...] = Field(min_length=1, max_length=100)

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        return _aware(value, "created_at")

    @model_validator(mode="after")
    def validate_unique_identity(self) -> "RecoveryManifest":
        disclosure_ids = [str(item.disclosure_id) for item in self.records]
        external_ids = [item.expected_external_id for item in self.records]
        if len(disclosure_ids) != len(set(disclosure_ids)):
            raise ValueError("manifest contains duplicate disclosure_id values")
        if len(external_ids) != len(set(external_ids)):
            raise ValueError("manifest contains duplicate external_id values")
        return self

    @property
    def digest(self) -> str:
        payload = self.model_dump(mode="json", by_alias=True)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class CurrentDisclosure(RecoveryModel):
    """Read-only current state returned by a Synapse adapter or snapshot."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    disclosure_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    ticker: str = Field(min_length=1, max_length=10)
    processing_status: ProcessingStatus
    updated_at: datetime
    is_stock_scope: bool
    declared_attachment_count: int = Field(ge=0, le=100)
    attachment_hashes: tuple[str, ...] = ()
    source_id: str = IDX_WEBSITE_SOURCE_ID
    analysis_present: bool = False

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        return _aware(value, "updated_at")

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    @field_validator("attachment_hashes", mode="before")
    @classmethod
    def normalize_attachment_hashes(cls, value: Iterable[str]) -> tuple[str, ...]:
        return _hashes(value or ())

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if value != IDX_WEBSITE_SOURCE_ID:
            raise ValueError(f"source_id must be {IDX_WEBSITE_SOURCE_ID}")
        return value


class CurrentFile(RecoveryModel):
    """Durable file metadata plus an optional local cache path."""

    file_id: str | None = None
    disclosure_id: UUID
    source_url: str = Field(min_length=1, max_length=2000)
    sha256: str | None = None
    download_status: Literal["PENDING", "DOWNLOADED", "SKIPPED", "FAILED"] = "PENDING"
    extraction_status: Literal["PENDING", "EXTRACTED", "SKIPPED", "FAILED"] = "PENDING"
    extracted_text_hash: str | None = None
    extracted_text_ref: str | None = None
    local_path: Path | None = None

    @field_validator("sha256", "extracted_text_hash")
    @classmethod
    def normalize_optional_hash(cls, value: str | None) -> str | None:
        return None if value is None else _hashes((value,))[0]


class RecoverySnapshot(RecoveryModel):
    """Offline read-only input for the Phase 2B-1 CLI gate."""

    disclosures: tuple[CurrentDisclosure, ...] = Field(default_factory=tuple)
    files: tuple[CurrentFile, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def validate_files(self) -> "RecoverySnapshot":
        known = {item.disclosure_id for item in self.disclosures}
        disclosure_ids = [item.disclosure_id for item in self.disclosures]
        if len(disclosure_ids) != len(set(disclosure_ids)):
            raise ValueError("snapshot contains duplicate disclosure_id values")
        unknown = [item.disclosure_id for item in self.files if item.disclosure_id not in known]
        if unknown:
            raise ValueError(f"snapshot contains files for unknown disclosures: {unknown!r}")
        return self

    def disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        return next((item for item in self.disclosures if item.disclosure_id == disclosure_id), None)

    def files_for(self, disclosure_id: UUID) -> tuple[CurrentFile, ...]:
        return tuple(item for item in self.files if item.disclosure_id == disclosure_id)


class RecoveryCaps(RecoveryModel):
    """Explicit caps; the defaults are deliberately sized for a pilot dry-run."""

    max_records: int = Field(default=12, ge=1, le=100)
    max_source_requests: int = Field(default=12, ge=0, le=100)
    max_attachments: int = Field(default=20, ge=0, le=100)
    max_ai_documents: int = Field(default=20, ge=0, le=100)


class RecoveryPreflight(RecoveryModel):
    disclosure_id: UUID
    outcome: Literal["READY", "SKIP", "HELD", "ALREADY_READY"]
    action: RecoveryAction
    reasons: tuple[str, ...] = ()
    attachments_needed: int = 0
    attachments_considered: int = 0
    source_requests_needed: int = 0
    files_reused: int = 0
    ai_documents_needed: int = 0
    announcement_analyses_needed: int = 0


class RecoveryRecordResult(RecoveryModel):
    disclosure_id: UUID
    outcome: Literal["PLANNED", "READY", "SKIPPED", "HELD", "FAILED", "ALREADY_READY"]
    action: RecoveryAction
    reasons: tuple[str, ...] = ()


class RecoveryRunReport(RecoveryModel):
    manifest_id: UUID
    manifest_digest: str
    run_type: Literal["RETRY"] = "RETRY"
    dry_run: bool
    ok: bool
    run_id: str | None = None
    planned_records: int = 0
    attempted: int = 0
    skipped: int = 0
    held: int = 0
    ready: int = 0
    source_requests: int = 0
    attachments_considered: int = 0
    files_reused: int = 0
    files_downloaded: int = 0
    extraction_count: int = 0
    ai_document_requests: int = 0
    announcement_analyses: int = 0
    errors: tuple[str, ...] = ()
    records: tuple[RecoveryRecordResult, ...] = ()
    mutations: int = 0
    checkpoint_touched: bool = False
    watermark_touched: bool = False
    coverage_touched: bool = False


class RecoveryRunnerError(RuntimeError):
    """Base class for a safely stoppable recovery run."""


class RecoveryPreflightError(RecoveryRunnerError):
    pass


class RecoveryCapExceeded(RecoveryRunnerError):
    pass


class RecoveryHashMismatch(RecoveryRunnerError):
    pass


class RecoveryExecutionError(RecoveryRunnerError):
    pass


class RecoveryStore(Protocol):
    def fetch_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None: ...

    def list_files(self, disclosure_id: UUID) -> Sequence[CurrentFile]: ...

    def create_retry_run(self, manifest: RecoveryManifest) -> str: ...

    def finish_retry_run(self, run_id: str, report: RecoveryRunReport) -> None: ...

    def update_processing_status(self, disclosure_id: UUID, status: ProcessingStatus) -> None: ...

    def upsert_file(self, disclosure_id: UUID, file: CurrentFile) -> None: ...

    def commit_analysis(self, disclosure_id: UUID, analysis: object) -> None: ...


@dataclass(frozen=True)
class DownloadedArtifact:
    source_url: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class ExtractedArtifact:
    text: str
    text_hash: str
    method: str = "existing"

    def __post_init__(self) -> None:
        normalized = self.text_hash.strip().lower()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("extracted text hash must be a 64-character hexadecimal digest")
        object.__setattr__(self, "text_hash", normalized)


@dataclass
class RecoveryHooks:
    """Adapters to the existing downloader, extractors and validated Synapse writes."""

    download_attachment: Callable[[CurrentDisclosure, int, str | None], DownloadedArtifact] | None = None
    extract_attachment: (
        Callable[[CurrentDisclosure, CurrentFile | None, DownloadedArtifact], ExtractedArtifact] | None
    ) = None
    analyze_document: Callable[[CurrentDisclosure, CurrentFile, str], object] | None = None
    analyze_announcement: Callable[[CurrentDisclosure, Sequence[object]], object] | None = None


@dataclass
class _Plan:
    preflight: RecoveryPreflight
    files: tuple[CurrentFile, ...] = ()
    valid_cached_files: tuple[CurrentFile, ...] = ()
    download_hashes: tuple[str, ...] = ()


def _download_hashes(
    expected_hashes: tuple[str, ...],
    files: Sequence[CurrentFile],
    attachments_needed: int,
) -> tuple[str, ...]:
    if attachments_needed <= 0:
        return ()
    represented = [file.sha256 for file in files if file.sha256]
    candidates = [digest for digest in expected_hashes if digest not in represented]
    if len(candidates) < attachments_needed:
        candidates.extend(expected_hashes)
    return tuple(candidates[:attachments_needed])


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _cache_is_valid(file: CurrentFile, expected_hashes: tuple[str, ...]) -> bool:
    if file.download_status != "DOWNLOADED" or file.extraction_status != "EXTRACTED":
        return False
    if file.local_path is not None:
        if not file.local_path.exists() or not file.local_path.is_file():
            return False
        actual = _file_sha256(file.local_path)
        if file.sha256 and actual != file.sha256:
            raise RecoveryHashMismatch(f"{file.source_url}: local cache hash mismatch")
        if expected_hashes and actual not in expected_hashes:
            raise RecoveryHashMismatch(f"{file.source_url}: cache hash is not in manifest")
        return True
    # A durable extracted-text reference means the existing pipeline can reuse
    # the artifact without another source request.  There is no local path to
    # hash in this case; the DB hash remains the audit value.
    return bool(file.extracted_text_ref and file.sha256 and file.sha256 in expected_hashes)


class SnapshotRecoveryStore:
    """Read-only store used by CLI dry-runs; every mutation method fails closed."""

    def __init__(self, snapshot: RecoverySnapshot) -> None:
        self.snapshot = snapshot

    def fetch_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        return self.snapshot.disclosure(disclosure_id)

    def list_files(self, disclosure_id: UUID) -> Sequence[CurrentFile]:
        return self.snapshot.files_for(disclosure_id)

    def _read_only(self, *_args: object, **_kwargs: object) -> None:
        raise RecoveryExecutionError("snapshot store is read-only")

    create_retry_run = _read_only
    finish_retry_run = _read_only
    update_processing_status = _read_only
    upsert_file = _read_only
    commit_analysis = _read_only


class RecoveryRunner:
    """Plan or execute explicit per-ID recovery without discovery side effects."""

    def __init__(
        self,
        store: RecoveryStore,
        *,
        hooks: RecoveryHooks | None = None,
        caps: RecoveryCaps | None = None,
    ) -> None:
        self.store = store
        self.hooks = hooks or RecoveryHooks()
        self.caps = caps or RecoveryCaps()

    def _preflight(self, record: RecoveryManifestRecord) -> _Plan:
        current = self.store.fetch_disclosure(record.disclosure_id)
        if current is None:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="SKIP",
                    action="MANUAL_REVIEW",
                    reasons=("disclosure row not found",),
                )
            )

        reasons: list[str] = []
        if current.disclosure_id != record.disclosure_id:
            reasons.append("disclosure_id mismatch")
        if current.external_id != record.expected_external_id:
            reasons.append("external_id mismatch")
        if current.ticker != record.ticker:
            reasons.append("ticker mismatch")
        if current.source_id != IDX_WEBSITE_SOURCE_ID:
            reasons.append("source_id is not idx-website")
        if current.processing_status != record.expected_status:
            reasons.append("processing_status changed since manifest")
        if current.updated_at != record.expected_updated_at:
            reasons.append("updated_at concurrency guard changed")
        if not current.is_stock_scope:
            reasons.append("disclosure is outside stock scope")
        if current.declared_attachment_count != record.declared_attachment_count:
            reasons.append("declared attachment count changed")
        if tuple(current.attachment_hashes) != tuple(record.expected_attachment_hashes):
            reasons.append("attachment hash metadata changed")
        if len(record.expected_attachment_hashes) != record.declared_attachment_count:
            reasons.append("manifest attachment count/hash metadata is inconsistent")

        if reasons:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="SKIP",
                    action="MANUAL_REVIEW",
                    reasons=tuple(reasons),
                )
            )
        if record.bucket in {"E", "F"} and not record.recovery_allowed:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="HELD",
                    action="HELD",
                    reasons=(f"bucket {record.bucket} requires explicit recovery_allowed=true",),
                )
            )

        files = tuple(self.store.list_files(record.disclosure_id))
        if any(file.disclosure_id != record.disclosure_id for file in files):
            reasons.append("stored file belongs to another disclosure")
        if len(files) > record.declared_attachment_count:
            reasons.append("stored file count exceeds declared attachment count")
        if len({file.source_url for file in files}) != len(files):
            reasons.append("duplicate stored attachment source_url")
        if reasons:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="SKIP",
                    action="MANUAL_REVIEW",
                    reasons=tuple(reasons),
                ),
                files=files,
            )
        # A completed analysis is a safe idempotent no-op.  The identity and
        # updated_at checks above still apply, so a concurrent READY update is
        # never silently accepted.
        if current.analysis_present:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="ALREADY_READY",
                    action="NOOP",
                    reasons=("record already has a committed analysis",),
                ),
                files=files,
            )

        valid: list[CurrentFile] = []
        cache_errors: list[str] = []
        for file in files:
            try:
                if _cache_is_valid(file, record.expected_attachment_hashes):
                    valid.append(file)
            except RecoveryHashMismatch as exc:
                cache_errors.append(str(exc))
        if cache_errors:
            return _Plan(
                RecoveryPreflight(
                    disclosure_id=record.disclosure_id,
                    outcome="SKIP",
                    action="MANUAL_REVIEW",
                    reasons=tuple(cache_errors),
                ),
                files=files,
            )

        staged = [
            file
            for file in files
            if file not in valid
            and file.download_status == "DOWNLOADED"
            and file.local_path is not None
            and file.local_path.exists()
            and file.local_path.is_file()
        ]
        unavailable = [file for file in files if file not in valid and file not in staged]
        missing = max(record.declared_attachment_count - len(valid) - len(staged), 0)
        source_needed = missing + len(unavailable)
        if valid and not source_needed:
            action: RecoveryAction = "AI_ONLY_RETRY"
        elif source_needed:
            action = "ATTACHMENT_DOWNLOAD"
        elif staged:
            action = "EXTRACTION_RESUME"
        elif record.expected_status == "EXTRACTING":
            action = "METADATA_PROCESS_RESUME"
        elif files:
            action = "EXTRACTION_RESUME"
        else:
            action = "ATTACHMENT_DOWNLOAD"

        attachments_needed = source_needed if action in {"ATTACHMENT_DOWNLOAD", "METADATA_PROCESS_RESUME"} else 0
        considered = len(files) + attachments_needed
        source_requests = attachments_needed
        ai_documents = len(valid) if action == "AI_ONLY_RETRY" else max(considered, 0)
        announcement = 0 if action == "NOOP" else 1
        return _Plan(
            RecoveryPreflight(
                disclosure_id=record.disclosure_id,
                outcome="READY",
                action=action,
                attachments_needed=attachments_needed,
                attachments_considered=considered,
                source_requests_needed=source_requests,
                files_reused=len(valid),
                ai_documents_needed=ai_documents,
                announcement_analyses_needed=announcement,
            ),
            files=files,
            valid_cached_files=tuple(valid),
            download_hashes=_download_hashes(record.expected_attachment_hashes, files, attachments_needed),
        )

    def _cap_error(self, message: str) -> RecoveryRunReport:
        return RecoveryRunReport(
            manifest_id=self._manifest.manifest_id,
            manifest_digest=self._manifest.digest,
            dry_run=self._dry_run,
            ok=False,
            planned_records=len(self._manifest.records),
            errors=(message,),
        )

    def run(self, manifest: RecoveryManifest, *, dry_run: bool = True) -> RecoveryRunReport:
        self._manifest = manifest
        self._dry_run = dry_run
        if manifest.run_type != "RETRY":
            return self._cap_error("recovery run must use mode RETRY")
        if len(manifest.records) > self.caps.max_records:
            return self._cap_error(f"record cap exceeded: {len(manifest.records)} > {self.caps.max_records}")

        plans = [self._preflight(record) for record in manifest.records]
        source_requests = sum(plan.preflight.source_requests_needed for plan in plans)
        attachments = sum(plan.preflight.attachments_considered for plan in plans)
        ai_documents = sum(plan.preflight.ai_documents_needed for plan in plans)
        if source_requests > self.caps.max_source_requests:
            return self._cap_error(f"source-request cap exceeded: {source_requests} > {self.caps.max_source_requests}")
        if attachments > self.caps.max_attachments:
            return self._cap_error(f"attachment cap exceeded: {attachments} > {self.caps.max_attachments}")
        if ai_documents > self.caps.max_ai_documents:
            return self._cap_error(f"AI-document cap exceeded: {ai_documents} > {self.caps.max_ai_documents}")

        records: list[RecoveryRecordResult] = []
        errors: list[str] = []
        skipped = held = ready = attempted = mutations = 0
        files_reused = sum(plan.preflight.files_reused for plan in plans)
        files_downloaded = sum(
            plan.preflight.attachments_needed
            for plan in plans
            if plan.preflight.action in {"ATTACHMENT_DOWNLOAD", "METADATA_PROCESS_RESUME"}
        )
        extraction_count = sum(
            plan.preflight.attachments_considered
            for plan in plans
            if plan.preflight.action in {"ATTACHMENT_DOWNLOAD", "METADATA_PROCESS_RESUME", "EXTRACTION_RESUME"}
        )
        run_id: str | None = None
        execution_candidates = sum(plan.preflight.outcome == "READY" for plan in plans)
        if not dry_run and execution_candidates:
            try:
                # The RETRY run is opened only after every manifest record has
                # passed read-only preflight and all caps have been checked.
                run_id = self.store.create_retry_run(manifest)
            except Exception as exc:
                return RecoveryRunReport(
                    manifest_id=manifest.manifest_id,
                    manifest_digest=manifest.digest,
                    dry_run=False,
                    ok=False,
                    planned_records=len(manifest.records),
                    errors=(f"could not create RETRY run: {type(exc).__name__}: {exc}",),
                    checkpoint_touched=False,
                    watermark_touched=False,
                    coverage_touched=False,
                )
        for plan, record in zip(plans, manifest.records, strict=True):
            preflight = plan.preflight
            if preflight.outcome == "SKIP":
                skipped += 1
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="SKIPPED",
                        action=preflight.action,
                        reasons=preflight.reasons,
                    )
                )
                continue
            if preflight.outcome == "HELD":
                held += 1
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="HELD",
                        action="HELD",
                        reasons=preflight.reasons,
                    )
                )
                continue
            if preflight.outcome == "ALREADY_READY":
                ready += 1
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="ALREADY_READY",
                        action="NOOP",
                        reasons=preflight.reasons,
                    )
                )
                continue

            attempted += 1
            if dry_run:
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="PLANNED",
                        action=preflight.action,
                    )
                )
                continue

            try:
                self._execute_record(record, plan)
                ready += 1
                mutations += 1
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="READY",
                        action=preflight.action,
                    )
                )
            except Exception as exc:  # fail the record, preserve resumability
                errors.append(f"{record.expected_external_id}: {type(exc).__name__}: {exc}")
                # Leave the row in the existing resumable PARTIAL state when a
                # stage fails.  A future invocation must create a fresh
                # manifest snapshot; it must never reset an EXTRACTING row by
                # force or lose the already committed file metadata.
                try:
                    current = self.store.fetch_disclosure(record.disclosure_id)
                    if current is not None and current.processing_status in {"EXTRACTING", "ANALYZING"}:
                        self.store.update_processing_status(record.disclosure_id, "PARTIAL")
                except Exception as status_exc:
                    errors.append(
                        f"{record.expected_external_id}: could not preserve PARTIAL state: "
                        f"{type(status_exc).__name__}: {status_exc}"
                    )
                records.append(
                    RecoveryRecordResult(
                        disclosure_id=record.disclosure_id,
                        outcome="FAILED",
                        action=preflight.action,
                        reasons=(str(exc),),
                    )
                )
                break

        report = RecoveryRunReport(
            manifest_id=manifest.manifest_id,
            manifest_digest=manifest.digest,
            dry_run=dry_run,
            ok=not errors and not any(item.outcome in {"SKIPPED", "HELD", "FAILED"} for item in records),
            run_id=run_id,
            planned_records=len(manifest.records),
            attempted=attempted,
            skipped=skipped,
            held=held,
            ready=ready,
            source_requests=source_requests,
            attachments_considered=attachments,
            files_reused=files_reused,
            files_downloaded=files_downloaded,
            extraction_count=extraction_count,
            ai_document_requests=ai_documents,
            announcement_analyses=sum(plan.preflight.announcement_analyses_needed for plan in plans),
            errors=tuple(errors),
            records=tuple(records),
            mutations=0 if dry_run else mutations,
        )
        if not dry_run and run_id is not None:
            try:
                self.store.finish_retry_run(run_id, report)
            except Exception as exc:
                report = report.model_copy(
                    update={
                        "ok": False,
                        "errors": (*report.errors, f"could not finish RETRY run: {type(exc).__name__}: {exc}"),
                    }
                )
        return report

    def _execute_record(self, record: RecoveryManifestRecord, plan: _Plan) -> None:
        current = self.store.fetch_disclosure(record.disclosure_id)
        if current is None:
            raise RecoveryPreflightError("disclosure disappeared during execution")
        if current.updated_at != record.expected_updated_at or current.processing_status != record.expected_status:
            raise RecoveryPreflightError("disclosure changed after preflight")
        action = plan.preflight.action
        if action == "NOOP":
            return
        if self.hooks.analyze_document is None or self.hooks.analyze_announcement is None:
            raise RecoveryExecutionError("analysis hooks are required for AI recovery")

        # Status transitions are deliberately explicit and stay within the
        # existing processing-status API.  There is no path here for coverage,
        # checkpoint, watermark, discovery-cursor, or stock-scope writes.
        self.store.update_processing_status(record.disclosure_id, "EXTRACTING")
        files_to_analyze: list[CurrentFile] = list(plan.valid_cached_files)

        if action in {"ATTACHMENT_DOWNLOAD", "METADATA_PROCESS_RESUME"}:
            if self.hooks.download_attachment is None:
                raise RecoveryExecutionError("download hook is required for attachment recovery")
            if self.hooks.extract_attachment is None:
                raise RecoveryExecutionError("extraction hook is required for attachment recovery")
            for index, expected_hash in enumerate(plan.download_hashes):
                artifact = self.hooks.download_attachment(current, index, expected_hash)
                if not artifact.path.exists() or not artifact.path.is_file():
                    raise RecoveryExecutionError(f"{artifact.source_url}: downloaded artifact is unavailable")
                actual = _file_sha256(artifact.path)
                declared = artifact.sha256.lower()
                if declared != actual or actual != expected_hash:
                    raise RecoveryHashMismatch(f"{artifact.source_url}: downloaded hash mismatch")
                file = CurrentFile(
                    disclosure_id=record.disclosure_id,
                    source_url=artifact.source_url,
                    sha256=actual,
                    download_status="DOWNLOADED",
                    extraction_status="PENDING",
                    local_path=artifact.path,
                )
                extracted = self.hooks.extract_attachment(current, file, artifact)
                file = file.model_copy(
                    update={
                        "extraction_status": "EXTRACTED",
                        "extracted_text_hash": extracted.text_hash,
                    }
                )
                # The existing upsert endpoint is keyed by disclosure + source
                # URL, so duplicate file rows cannot be fabricated here.
                if not any(existing.source_url == file.source_url for existing in plan.files):
                    self.store.upsert_file(record.disclosure_id, file)
                files_to_analyze.append(file)
            for file in plan.files:
                if file in plan.valid_cached_files:
                    continue
                if file.download_status != "DOWNLOADED" or file.local_path is None:
                    continue
                if not file.local_path.exists() or not file.local_path.is_file():
                    raise RecoveryExecutionError(f"{file.source_url}: staged attachment is unavailable")
                artifact = DownloadedArtifact(
                    source_url=file.source_url,
                    path=file.local_path,
                    sha256=file.sha256 or _file_sha256(file.local_path),
                )
                extracted = self.hooks.extract_attachment(current, file, artifact)
                updated = file.model_copy(
                    update={
                        "extraction_status": "EXTRACTED",
                        "extracted_text_hash": extracted.text_hash,
                    }
                )
                self.store.upsert_file(record.disclosure_id, updated)
                files_to_analyze.append(updated)
        elif action == "EXTRACTION_RESUME":
            if self.hooks.extract_attachment is None:
                raise RecoveryExecutionError("extraction hook is required for extraction recovery")
            files_to_analyze = list(plan.valid_cached_files)
            for file in plan.files:
                if file in plan.valid_cached_files:
                    continue
                if file.local_path is None or not file.local_path.exists():
                    raise RecoveryExecutionError(f"{file.source_url}: extraction artifact is unavailable")
                artifact = DownloadedArtifact(
                    source_url=file.source_url,
                    path=file.local_path,
                    sha256=file.sha256 or _file_sha256(file.local_path),
                )
                extracted = self.hooks.extract_attachment(current, file, artifact)
                updated = file.model_copy(
                    update={
                        "download_status": "DOWNLOADED",
                        "extraction_status": "EXTRACTED",
                        "extracted_text_hash": extracted.text_hash,
                    }
                )
                self.store.upsert_file(record.disclosure_id, updated)
                files_to_analyze.append(updated)

        self.store.update_processing_status(record.disclosure_id, "ANALYZING")
        documents: list[object] = []
        for file in files_to_analyze:
            text = file.extracted_text_ref or ""
            documents.append(self.hooks.analyze_document(current, file, text))
        announcement = self.hooks.analyze_announcement(current, documents)
        self.store.commit_analysis(record.disclosure_id, announcement)
        self.store.update_processing_status(record.disclosure_id, "READY")


def load_manifest(path: Path) -> RecoveryManifest:
    return RecoveryManifest.model_validate_json(path.read_text(encoding="utf-8"))


def load_snapshot(path: Path) -> RecoverySnapshot:
    return RecoverySnapshot.model_validate_json(path.read_text(encoding="utf-8"))


__all__ = [
    "CurrentDisclosure",
    "CurrentFile",
    "DownloadedArtifact",
    "ExtractedArtifact",
    "RecoveryCaps",
    "RecoveryCapExceeded",
    "RecoveryExecutionError",
    "RecoveryHashMismatch",
    "RecoveryHooks",
    "RecoveryManifest",
    "RecoveryManifestRecord",
    "RecoveryPreflightError",
    "RecoveryPreflight",
    "RecoveryRunner",
    "RecoveryRunnerError",
    "RecoverySnapshot",
    "SnapshotRecoveryStore",
    "load_manifest",
    "load_snapshot",
]
