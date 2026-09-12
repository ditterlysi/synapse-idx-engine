from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from idx_digest.recovery_runner import (
    CurrentDisclosure,
    CurrentFile,
    DownloadedArtifact,
    ExtractedArtifact,
    RecoveryCaps,
    RecoveryHooks,
    RecoveryManifest,
    RecoveryManifestRecord,
    RecoveryRunner,
    RecoverySnapshot,
    SnapshotRecoveryStore,
)
from typer.testing import CliRunner


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
HASH_2 = "b" * 64


def _record(
    disclosure_id: UUID,
    *,
    status: str = "PARTIAL",
    bucket: str = "C",
    count: int = 1,
    hashes: tuple[str, ...] = (HASH,),
    allowed: bool = False,
) -> RecoveryManifestRecord:
    return RecoveryManifestRecord(
        disclosure_id=disclosure_id,
        expected_status=status,
        expected_updated_at=NOW,
        expected_external_id=f"idx-web-{disclosure_id}",
        ticker="TEST",
        bucket=bucket,
        declared_attachment_count=count,
        expected_attachment_hashes=hashes,
        intended_recovery_action="resume",
        recovery_allowed=allowed,
    )


def _current(record: RecoveryManifestRecord, *, status: str | None = None, **changes: object) -> CurrentDisclosure:
    return CurrentDisclosure(
        disclosure_id=record.disclosure_id,
        external_id=record.expected_external_id,
        ticker=record.ticker,
        processing_status=status or record.expected_status,
        updated_at=record.expected_updated_at,
        is_stock_scope=True,
        declared_attachment_count=record.declared_attachment_count,
        attachment_hashes=record.expected_attachment_hashes,
        **changes,
    )


def _manifest(record: RecoveryManifestRecord) -> RecoveryManifest:
    return RecoveryManifest(records=(record,))


class FakeStore:
    def __init__(self, current: CurrentDisclosure, files: tuple[CurrentFile, ...] = ()) -> None:
        self.current = current
        self.files = list(files)
        self.calls: list[tuple[str, object]] = []
        self.fail_commit = False
        self.mutate_on_second_fetch = False
        self.fetch_count = 0

    def fetch_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        self.fetch_count += 1
        if self.mutate_on_second_fetch and self.fetch_count == 2:
            self.current = self.current.model_copy(update={"updated_at": NOW + timedelta(seconds=1)})
        return self.current if self.current.disclosure_id == disclosure_id else None

    def list_files(self, disclosure_id: UUID):
        return tuple(item for item in self.files if item.disclosure_id == disclosure_id)

    def create_retry_run(self, manifest: RecoveryManifest) -> str:
        self.calls.append(("create_retry_run", manifest.manifest_id))
        return "retry-run-1"

    def finish_retry_run(self, run_id: str, report) -> None:
        self.calls.append(("finish_retry_run", run_id))

    def update_processing_status(self, disclosure_id: UUID, status: str) -> None:
        self.calls.append(("status", status))
        self.current = self.current.model_copy(update={"processing_status": status})

    def upsert_file(self, disclosure_id: UUID, file: CurrentFile) -> None:
        self.calls.append(("upsert_file", file.source_url))
        self.files.append(file)

    def commit_analysis(self, disclosure_id: UUID, analysis: object) -> None:
        self.calls.append(("commit_analysis", analysis))
        if self.fail_commit:
            raise RuntimeError("RPC unavailable")
        self.current = self.current.model_copy(update={"analysis_present": True})


def _valid_file(record: RecoveryManifestRecord, tmp_path: Path) -> CurrentFile:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    # Hash is checked against the manifest; use the matching content digest in
    # tests that exercise cache reuse instead of the default fixture hash.
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    return CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )


def _hooks(*, fail_commit: bool = False) -> RecoveryHooks:
    def download(current, index, expected_hash):
        return DownloadedArtifact("https://idx.example/new.pdf", Path("new.pdf"), expected_hash or HASH)

    def extract(current, file, artifact):
        return ExtractedArtifact("text", "c" * 64, "pdf")

    return RecoveryHooks(
        download_attachment=download,
        extract_attachment=extract,
        analyze_document=lambda current, file, text: {"file": file.source_url, "text": text},
        analyze_announcement=lambda current, documents: {"documents": list(documents)},
    )


def test_valid_partial_record_plans_ai_retry() -> None:
    record = _record(uuid4(), count=0, hashes=())
    store = SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),)))
    report = RecoveryRunner(store).run(_manifest(record), dry_run=True)
    assert report.ok is True
    assert report.records[0].action == "ATTACHMENT_DOWNLOAD"
    assert report.mutations == 0


def test_valid_discovered_record_plans_bounded_download() -> None:
    record = _record(uuid4(), status="DISCOVERED")
    store = SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),)))
    report = RecoveryRunner(store).run(_manifest(record), dry_run=True)
    assert report.ok is True
    assert report.source_requests == 1
    assert report.attachments_considered == 1
    result = report.records[0]
    assert result.ticker == record.ticker
    assert result.before_status == "DISCOVERED"
    assert result.source_requests == 1
    assert result.attachments_selected == 1
    assert result.files_downloaded == 1
    assert result.ai_document_calls == 1
    assert result.announcement_analysis == "NOT_RUN"


def test_stale_extracting_record_is_inspected_without_reset() -> None:
    record = _record(uuid4(), status="EXTRACTING")
    store = SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),)))
    report = RecoveryRunner(store).run(_manifest(record), dry_run=True)
    assert report.records[0].action == "ATTACHMENT_DOWNLOAD"
    assert report.records[0].action != "DISCOVERED"


def test_id_mismatch_is_skipped() -> None:
    record = _record(uuid4())
    current = _current(record).model_copy(update={"disclosure_id": uuid4()})

    class WrongIdStore(FakeStore):
        def fetch_disclosure(self, disclosure_id: UUID):
            return self.current

    report = RecoveryRunner(WrongIdStore(current)).run(_manifest(record), dry_run=True)
    assert report.skipped == 1
    assert "disclosure_id mismatch" in report.records[0].reasons


def test_external_id_mismatch_is_skipped() -> None:
    record = _record(uuid4())
    current = _current(record).model_copy(update={"external_id": "other"})
    report = RecoveryRunner(SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))).run(
        _manifest(record), dry_run=True
    )
    assert "external_id mismatch" in report.records[0].reasons
    assert report.records[0].outcome == "PRECONDITION_FAILED"


def test_updated_at_concurrency_change_is_skipped() -> None:
    record = _record(uuid4())
    current = _current(record).model_copy(update={"updated_at": NOW + timedelta(minutes=1)})
    report = RecoveryRunner(SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))).run(
        _manifest(record), dry_run=True
    )
    assert "updated_at concurrency guard changed" in report.records[0].reasons
    assert report.records[0].outcome == "PRECONDITION_FAILED"


def test_stock_scope_mismatch_is_precondition_failure() -> None:
    record = _record(uuid4())
    current = _current(record).model_copy(update={"is_stock_scope": False})
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))
    ).run(_manifest(record), dry_run=True)
    assert report.records[0].outcome == "PRECONDITION_FAILED"
    assert "disclosure is outside stock scope" in report.records[0].reasons


def test_attachment_metadata_mismatch_is_precondition_failure() -> None:
    record = _record(uuid4())
    current = _current(record).model_copy(update={"attachment_hashes": (HASH_2,)})
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))
    ).run(_manifest(record), dry_run=True)
    assert report.records[0].outcome == "PRECONDITION_FAILED"
    assert "attachment hash metadata changed" in report.records[0].reasons


def test_partial_attachment_hash_metadata_remains_a_valid_precondition() -> None:
    record = _record(uuid4(), count=2, hashes=(HASH,))
    current = _current(record)
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))
    ).run(_manifest(record), dry_run=True)
    assert report.records[0].outcome == "PLANNED"


def test_partial_attachment_metadata_allows_unknown_cached_hash(tmp_path: Path) -> None:
    record = _record(uuid4(), count=2, hashes=(HASH,))
    path = tmp_path / "unknown.pdf"
    path.write_bytes(b"unknown cached attachment")
    import hashlib

    unknown_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/unknown.pdf",
        sha256=unknown_digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://unknown",
        local_path=path,
    )
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),), files=(file,)))
    ).run(_manifest(record), dry_run=True)

    assert report.records[0].outcome == "PLANNED"
    assert report.records[0].action == "ATTACHMENT_DOWNLOAD"
    assert report.files_reused == 1
    assert report.source_requests == 1


def test_status_changed_after_manifest_is_skipped() -> None:
    record = _record(uuid4())
    current = _current(record, status="FAILED")
    report = RecoveryRunner(SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))).run(
        _manifest(record), dry_run=True
    )
    assert "processing_status changed since manifest" in report.records[0].reasons


@pytest.mark.parametrize("bucket", ["E", "F"])
def test_held_buckets_are_rejected(bucket: str) -> None:
    record = _record(uuid4(), bucket=bucket)
    current = _current(record)
    report = RecoveryRunner(SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,)))).run(
        _manifest(record), dry_run=True
    )
    assert report.held == 1
    assert report.records[0].outcome == "HELD"


def test_valid_cache_hash_is_reused_without_source_request(tmp_path: Path) -> None:
    record = _record(uuid4(), count=1, hashes=(HASH_2,))
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (digest,)})
    current = _current(record)
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )
    store = SnapshotRecoveryStore(RecoverySnapshot(disclosures=(current,), files=(file,)))
    report = RecoveryRunner(store).run(_manifest(record), dry_run=True)
    assert report.files_reused == 1
    assert report.source_requests == 0
    assert report.records[0].action == "AI_ONLY_RETRY"


def test_invalid_cache_hash_is_rejected(tmp_path: Path) -> None:
    record = _record(uuid4())
    path = tmp_path / "document.pdf"
    path.write_bytes(b"wrong")
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=HASH,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        local_path=path,
    )
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),), files=(file,)))
    ).run(_manifest(record), dry_run=True)
    assert report.skipped == 1
    assert "local cache hash mismatch" in report.records[0].reasons[0]


def test_downloaded_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    record = _record(uuid4(), status="DISCOVERED")
    path = tmp_path / "wrong.pdf"
    path.write_bytes(b"wrong")
    fake = FakeStore(_current(record))
    hooks = _hooks()
    hooks.download_attachment = lambda current, index, expected: DownloadedArtifact(
        "https://idx.example/wrong.pdf", path, expected or HASH
    )
    report = RecoveryRunner(fake, hooks=hooks).run(_manifest(record), dry_run=False)
    assert report.ok is False
    assert "downloaded hash mismatch" in report.errors[0]


def test_existing_file_is_not_duplicated(tmp_path: Path) -> None:
    record = _record(uuid4())
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (digest,)})
    current = _current(record)
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )
    fake = FakeStore(current, (file,))
    report = RecoveryRunner(fake, hooks=_hooks()).run(_manifest(record), dry_run=False)
    assert report.ready == 1
    assert not [call for call in fake.calls if call[0] == "upsert_file"]
    result = report.records[0]
    assert result.before_status == "PARTIAL"
    assert result.after_status == "READY"
    assert result.files_reused == 1
    assert result.ai_document_calls == 1
    assert result.announcement_analysis == "SUCCEEDED"
    assert result.db_commits == 4


def test_live_execution_refreshes_preflight_before_first_mutation() -> None:
    record = _record(uuid4(), count=0, hashes=())

    class RefreshStore(FakeStore):
        def __init__(self) -> None:
            super().__init__(_current(record))
            self.refresh_count = 0

        def refresh_disclosure(self, disclosure_id: UUID):
            self.refresh_count += 1
            return self.fetch_disclosure(disclosure_id)

    store = RefreshStore()
    report = RecoveryRunner(store, hooks=_hooks()).run(_manifest(record), dry_run=False)

    assert report.ok is True
    assert store.refresh_count == 1
    assert store.calls[0] == ("create_retry_run", report.manifest_id)


def test_retry_after_partial_interruption_is_resumable(tmp_path: Path) -> None:
    record = _record(uuid4())
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (digest,)})
    current = _current(record)
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )
    fake = FakeStore(current, (file,))
    fake.fail_commit = True
    first = RecoveryRunner(fake, hooks=_hooks()).run(_manifest(record), dry_run=False)
    assert first.records[0].outcome == "FAILED"
    assert fake.current.processing_status == "PARTIAL"
    fake.fail_commit = False
    second = RecoveryRunner(fake, hooks=_hooks()).run(_manifest(record), dry_run=False)
    assert second.ready == 1


def test_process_interruption_after_run_creation_finalizes_and_preserves_report() -> None:
    record = _record(uuid4(), count=0, hashes=())
    fake = FakeStore(_current(record))

    class InterruptingRunner(RecoveryRunner):
        def _execute_record(self, record, plan, metrics):
            raise KeyboardInterrupt("simulated stop")

    with pytest.raises(KeyboardInterrupt, match="simulated stop") as raised:
        InterruptingRunner(fake, hooks=_hooks()).run(_manifest(record), dry_run=False)

    report = raised.value.recovery_report
    assert report.run_id == "retry-run-1"
    assert report.retry_run_created is True
    assert report.finalization_status == "SUCCEEDED"
    assert report.metrics_scope == "CURRENT_INVOCATION"
    assert report.mutations_this_invocation == 2  # run create + finalize
    assert fake.calls == [("create_retry_run", report.manifest_id), ("finish_retry_run", "retry-run-1")]


def test_process_interruption_after_status_and_file_persist_keeps_partial_metrics(tmp_path: Path) -> None:
    record = _record(uuid4(), status="DISCOVERED", count=2, hashes=(HASH, HASH_2))
    fake = FakeStore(_current(record))
    first_path = tmp_path / "first.pdf"
    first_path.write_bytes(b"first")
    first_hash = __import__("hashlib").sha256(b"first").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (first_hash, HASH_2)})
    fake.current = _current(record)
    calls = 0

    def download(current, index, expected_hash):
        nonlocal calls
        calls += 1
        if calls == 1:
            return DownloadedArtifact("https://idx.example/first.pdf", first_path, first_hash)
        raise KeyboardInterrupt("interrupted after first file")

    hooks = _hooks()
    hooks.download_attachment = download
    with pytest.raises(KeyboardInterrupt, match="interrupted after first file") as raised:
        RecoveryRunner(fake, hooks=hooks).run(_manifest(record), dry_run=False)

    report = raised.value.recovery_report
    assert report.run_id == "retry-run-1"
    assert report.attempted == 1
    assert report.files_downloaded == 1
    assert report.extraction_count == 1
    assert report.ai_document_requests == 0
    assert report.db_commits == 2  # status + one persisted file
    assert report.mutations_this_invocation == 4  # run create + two record writes + finalize
    assert report.finalization_status == "SUCCEEDED"
    assert fake.current.processing_status == "EXTRACTING"
    assert len(fake.files) == 1


def test_live_precondition_failure_reports_invocation_scoped_zero_mutations() -> None:
    record = _record(uuid4(), allowed=True)
    current = _current(record, status="FAILED")
    report = RecoveryRunner(FakeStore(current)).run(_manifest(record), dry_run=False)

    assert report.run_id is None
    assert report.retry_run_created is False
    assert report.mutations_this_invocation == 0
    assert report.metrics_scope == "CURRENT_INVOCATION"


def test_resume_reuses_one_extracted_file_and_downloads_only_missing_attachment(tmp_path: Path) -> None:
    first_path = tmp_path / "existing.pdf"
    first_path.write_bytes(b"existing")
    first_hash = __import__("hashlib").sha256(b"existing").hexdigest()
    record = _record(uuid4(), count=2, hashes=(first_hash, HASH_2), allowed=True)
    existing = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/existing.pdf",
        sha256=first_hash,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://existing",
        local_path=first_path,
    )
    downloaded = tmp_path / "missing.pdf"
    downloaded.write_bytes(b"missing")
    missing_hash = __import__("hashlib").sha256(b"missing").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (first_hash, missing_hash)})
    current = _current(record)
    fake = FakeStore(current, (existing,))
    calls: list[int] = []
    hooks = _hooks()

    def download(current, index, expected_hash):
        calls.append(index)
        return DownloadedArtifact("https://idx.example/missing.pdf", downloaded, missing_hash)

    hooks.download_attachment = download
    report = RecoveryRunner(fake, hooks=hooks).run(_manifest(record), dry_run=False)

    assert report.ready == 1
    assert report.records[0].files_reused == 1
    assert report.records[0].files_downloaded == 1
    assert calls == [0]
    assert len(fake.files) == 2
    assert len({file.source_url for file in fake.files}) == 2


def test_source_request_cap_stops_before_execution() -> None:
    record = _record(uuid4(), status="DISCOVERED")
    current = _current(record)
    fake = FakeStore(current)
    report = RecoveryRunner(fake, caps=RecoveryCaps(max_source_requests=0)).run(_manifest(record), dry_run=False)
    assert report.ok is False
    assert "source-request cap exceeded" in report.errors[0]
    assert not fake.calls


def test_attachment_cap_stops_before_execution() -> None:
    record = _record(uuid4(), status="DISCOVERED")
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),))),
        caps=RecoveryCaps(max_attachments=0),
    ).run(_manifest(record), dry_run=True)
    assert "attachment cap exceeded" in report.errors[0]


def test_ai_cap_stops_before_execution(tmp_path: Path) -> None:
    record = _record(uuid4())
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (digest,)})
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )
    report = RecoveryRunner(
        SnapshotRecoveryStore(RecoverySnapshot(disclosures=(_current(record),), files=(file,))),
        caps=RecoveryCaps(max_ai_documents=0),
    ).run(_manifest(record), dry_run=True)
    assert "AI-document cap exceeded" in report.errors[0]


def test_db_rpc_failure_is_reported_and_run_is_resumable(tmp_path: Path) -> None:
    record = _record(uuid4())
    path = tmp_path / "document.pdf"
    path.write_bytes(b"valid")
    import hashlib

    digest = hashlib.sha256(b"valid").hexdigest()
    record = record.model_copy(update={"expected_attachment_hashes": (digest,)})
    file = CurrentFile(
        disclosure_id=record.disclosure_id,
        source_url="https://idx.example/document.pdf",
        sha256=digest,
        download_status="DOWNLOADED",
        extraction_status="EXTRACTED",
        extracted_text_ref="text://document",
        local_path=path,
    )
    fake = FakeStore(_current(record), (file,))
    fake.fail_commit = True
    report = RecoveryRunner(fake, hooks=_hooks()).run(_manifest(record), dry_run=False)
    assert report.ok is False
    assert "RPC unavailable" in report.errors[0]


def test_checkpoint_method_is_never_called() -> None:
    record = _record(uuid4(), count=0, hashes=())

    class Guarded(SnapshotRecoveryStore):
        def commit_checkpoint(self):
            raise AssertionError("checkpoint must not be touched")

    report = RecoveryRunner(Guarded(RecoverySnapshot(disclosures=(_current(record),)))).run(
        _manifest(record), dry_run=True
    )
    assert report.checkpoint_touched is False


def test_watermark_method_is_never_called() -> None:
    record = _record(uuid4(), count=0, hashes=())

    class Guarded(SnapshotRecoveryStore):
        def advance_watermark(self):
            raise AssertionError("watermark must not be touched")

    report = RecoveryRunner(Guarded(RecoverySnapshot(disclosures=(_current(record),)))).run(
        _manifest(record), dry_run=True
    )
    assert report.watermark_touched is False


def test_coverage_method_is_never_called() -> None:
    record = _record(uuid4(), count=0, hashes=())

    class Guarded(SnapshotRecoveryStore):
        def commit_coverage(self):
            raise AssertionError("coverage must not be touched")

    report = RecoveryRunner(Guarded(RecoverySnapshot(disclosures=(_current(record),)))).run(
        _manifest(record), dry_run=True
    )
    assert report.coverage_touched is False


def test_dry_run_makes_zero_mutations_and_network_hooks_are_not_called() -> None:
    record = _record(uuid4(), status="DISCOVERED")
    fake = FakeStore(_current(record))
    calls: list[str] = []
    hooks = RecoveryHooks(download_attachment=lambda *args: calls.append("download"))
    report = RecoveryRunner(fake, hooks=hooks).run(_manifest(record), dry_run=True)
    assert report.ok is True
    assert report.mutations == 0
    assert fake.calls == []
    assert calls == []


def test_live_precondition_failure_stops_before_later_record() -> None:
    first = _record(uuid4())
    second = _record(uuid4())
    current = {
        first.disclosure_id: _current(first, status="FAILED"),
        second.disclosure_id: _current(second),
    }

    class MultiStore(FakeStore):
        def __init__(self) -> None:
            self.calls = []
            self.fetch_count = 0
            self.current = current[first.disclosure_id]

        def fetch_disclosure(self, disclosure_id: UUID):
            self.fetch_count += 1
            return current.get(disclosure_id)

    store = MultiStore()
    report = RecoveryRunner(store).run(
        RecoveryManifest(records=(first, second)),
        dry_run=False,
    )

    assert report.ok is False
    assert report.records[0].outcome == "PRECONDITION_FAILED"
    assert len(report.records) == 1
    assert not [call for call in store.calls if call[0] == "create_retry_run"]


def test_record_observer_receives_serial_results() -> None:
    first = _record(uuid4(), count=0, hashes=())
    second = _record(uuid4(), count=0, hashes=())
    observed: list[UUID] = []

    class MultiStore(FakeStore):
        def __init__(self) -> None:
            self.calls = []
            self.fetch_count = 0
            self.current = None
            self.files = []

        def fetch_disclosure(self, disclosure_id: UUID):
            self.fetch_count += 1
            return _current(first if disclosure_id == first.disclosure_id else second)

    multi = MultiStore()
    report = RecoveryRunner(multi).run(
        RecoveryManifest(records=(first, second)),
        dry_run=True,
        on_record_result=lambda result: observed.append(result.disclosure_id),
    )

    assert report.ok is True
    assert observed == [first.disclosure_id, second.disclosure_id]


def test_manifest_is_immutable_and_rejects_duplicate_ids() -> None:
    record = _record(uuid4(), count=0, hashes=())
    with pytest.raises(ValueError, match="duplicate disclosure_id"):
        RecoveryManifest(records=(record, record))
    with pytest.raises(ValueError):
        record.ticker = "OTHER"  # type: ignore[misc]


def test_cli_recover_pending_requires_dry_run(tmp_path: Path) -> None:
    from idx_digest.idx_website_cli import app

    result = CliRunner().invoke(app, ["recover-pending", "--manifest", "m.json", "--snapshot", "s.json"])
    assert result.exit_code != 0
    assert result.exception is not None


def test_cli_recover_pending_uses_only_offline_snapshot(tmp_path: Path) -> None:
    from idx_digest.idx_website_cli import app

    record = _record(uuid4(), status="DISCOVERED")
    manifest_path = tmp_path / "manifest.json"
    snapshot_path = tmp_path / "snapshot.json"
    manifest_path.write_text(_manifest(record).model_dump_json(by_alias=True), encoding="utf-8")
    snapshot_path.write_text(
        RecoverySnapshot(disclosures=(_current(record),)).model_dump_json(by_alias=True),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "recover-pending",
            "--manifest",
            str(manifest_path),
            "--snapshot",
            str(snapshot_path),
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert '"dryRun": true' in result.output
    assert '"sourceRequests": 1' in result.output
