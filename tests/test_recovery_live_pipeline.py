from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.extractors import ExtractionResult
from idx_digest.recovery_live_pipeline import LiveRecoveryPipeline
from idx_digest.recovery_runner import CurrentDisclosure, CurrentFile, RecoveryManifestRecord
from idx_digest.recovery_runner import RecoveryCaps, RecoveryManifest, RecoveryRunner


RAW_ID = "20260827094727-003/AV/VIII/2026-CSC_id-id"
EXTERNAL_ID = f"idx-web-{RAW_ID}"
PDF_BODY = b"controlled recovery attachment"
PDF_HASH = hashlib.sha256(PDF_BODY).hexdigest()


def _payload() -> dict[str, object]:
    return {
        "ResultCount": 1,
        "Replies": [
            {
                "pengumuman": {
                    "Id2": RAW_ID,
                    "TglPengumuman": "2026-08-27T09:47:27",
                    "JudulPengumuman": "Perubahan Anggaran Dasar Perseroan",
                    "Kode_Emiten": "ARTA",
                    "JenisPengumuman": "STOCK",
                },
                "attachments": [
                    {
                        "PDFFilename": "akta.pdf",
                        "OriginalFilename": "akta.pdf",
                        "FullSavePath": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/akta.pdf",
                        "IsAttachment": True,
                    }
                ],
            }
        ],
    }


class FakeSummarizer:
    model = "synthetic-model"
    announcement_prompt_version = "synthetic-announcement"

    def summarize_document(self, **kwargs):
        return {
            "ticker": kwargs["ticker"],
            "summary": "Synthetic document summary",
            "chunk_count": 1,
        }

    def summarize_announcement(self, *, announcement, documents, stream=False):
        return {
            "ticker": announcement["ticker"],
            "announcement_id": announcement["id2"],
            "announced_at": announcement["announced_at"],
            "title": announcement["title"],
            "executive_summary": "Synthetic announcement summary",
            "category": "regulatory",
            "material_facts": ["Synthetic fact"],
            "financial_figures": [],
            "corporate_actions": [],
            "expansion_projects": [],
            "management_or_control_changes": [],
            "capital_structure_events": [],
            "listing_or_regulatory_events": ["Synthetic regulatory event"],
            "analytical_scenarios": [],
            "dates_and_deadlines": [],
            "risks_or_uncertainties": [],
            "possible_investor_relevance": [],
            "limitations": [],
        }

    def close(self):
        return None


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
        synapse_daily_max_download_bytes=10_000_000,
        idx_request_delay_seconds=0,
        idx_429_jitter_seconds=0,
    )


def _current(record: RecoveryManifestRecord) -> CurrentDisclosure:
    return CurrentDisclosure(
        disclosure_id=record.disclosure_id,
        external_id=record.expected_external_id,
        ticker=record.ticker,
        title="Perubahan Anggaran Dasar Perseroan",
        processing_status=record.expected_status,
        updated_at=record.expected_updated_at,
        is_stock_scope=True,
        declared_attachment_count=record.declared_attachment_count,
        attachment_hashes=record.expected_attachment_hashes,
    )


def _record() -> RecoveryManifestRecord:
    return RecoveryManifestRecord(
        disclosure_id=uuid4(),
        expected_status="DISCOVERED",
        expected_updated_at=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc),
        expected_external_id=EXTERNAL_ID,
        ticker="ARTA",
        bucket="C",
        declared_attachment_count=1,
        expected_attachment_hashes=(PDF_HASH,),
        intended_recovery_action="bounded live recovery",
        recovery_allowed=True,
    )


def test_live_pipeline_reuses_selector_and_counts_metadata_plus_download_requests(tmp_path: Path) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("GetAnnouncement"):
            return httpx.Response(200, json=_payload(), headers={"content-type": "application/json"}, request=request)
        return httpx.Response(200, content=PDF_BODY, headers={"content-type": "application/pdf"}, request=request)

    record = _record()
    with LiveRecoveryPipeline(
        _settings(tmp_path),
        max_source_requests=12,
        summarizer_factory=lambda settings: FakeSummarizer(),
        extractor=lambda path, content_type, settings: ExtractionResult("Synthetic text", "text"),
        transport=httpx.MockTransport(handler),
        request_delay_seconds=0,
        request_jitter_seconds=0,
    ) as pipeline:
        plan = pipeline.prepare_source(_current(record), record, ())
        assert plan.selected_attachment_count == 1
        assert plan.metadata_requests == 1
        assert plan.planned_download_requests == 1
        artifact = pipeline.download_attachment(_current(record), 0, PDF_HASH)
        assert artifact.cache_hit is False
        assert artifact.path.exists()
        assert pipeline.source_request_count == 2

    assert requests.count("/primary/ListedCompany/GetAnnouncement") == 1
    assert len(requests) == 2


def test_runner_enforces_actual_source_request_estimate_before_retry_run(tmp_path: Path) -> None:
    record = _record()
    current = _current(record)

    class Store:
        def __init__(self) -> None:
            self.run_created = False

        def fetch_disclosure(self, disclosure_id):
            return current if disclosure_id == record.disclosure_id else None

        def list_files(self, disclosure_id):
            return ()

        def create_retry_run(self, manifest):
            self.run_created = True
            return "run"

        def finish_retry_run(self, run_id, report):
            return None

        def update_processing_status(self, disclosure_id, status):
            return None

        def upsert_file(self, disclosure_id, file):
            return None

        def commit_analysis(self, disclosure_id, analysis):
            return None

    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=_payload(), headers={"content-type": "application/json"}, request=request)

    with LiveRecoveryPipeline(
        _settings(tmp_path),
        max_source_requests=12,
        summarizer_factory=lambda settings: FakeSummarizer(),
        transport=httpx.MockTransport(handler),
        request_delay_seconds=0,
        request_jitter_seconds=0,
    ) as pipeline:
        store = Store()
        report = RecoveryRunner(
            store,
            hooks=pipeline.hooks(),
            caps=RecoveryCaps(max_records=1, max_source_requests=1, max_attachments=20, max_ai_documents=20),
        ).run(RecoveryManifest(records=(record,)), dry_run=True)

    assert report.ok is False
    assert "source-request cap exceeded" in report.errors[0]
    assert store.run_created is False
    assert requests == ["/primary/ListedCompany/GetAnnouncement"]


def test_live_pipeline_resolves_existing_api_file_to_local_cache_without_source_request(tmp_path: Path) -> None:
    record = _record()
    record = record.model_copy(update={"expected_attachment_hashes": (PDF_HASH,)})
    current = _current(record)
    with LiveRecoveryPipeline(
        _settings(tmp_path),
        max_source_requests=12,
        summarizer_factory=lambda settings: FakeSummarizer(),
        transport=httpx.MockTransport(lambda request: pytest.fail("source must not be called")),
        request_delay_seconds=0,
        request_jitter_seconds=0,
    ) as pipeline:
        cache_path = pipeline._cache_path(current, type("Attachment", (), {
            "source_url": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/akta.pdf",
            "filename": "akta.pdf",
        })())
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(PDF_BODY)
        file = CurrentFile(
            disclosure_id=record.disclosure_id,
            source_url="https://www.idx.co.id/StaticData/NewsAndAnnouncement/akta.pdf",
            sha256=PDF_HASH,
            download_status="DOWNLOADED",
            extraction_status="EXTRACTED",
            extracted_text_ref=None,
            local_path=None,
        )
        resolved = pipeline.resolve_local_file(current, file)
        assert resolved.local_path == cache_path

        class Store:
            def fetch_disclosure(self, disclosure_id):
                return current

            def list_files(self, disclosure_id):
                return (file,)

        report = RecoveryRunner(
            Store(),
            hooks=pipeline.hooks(),
            caps=RecoveryCaps(max_records=1, max_source_requests=12, max_attachments=20, max_ai_documents=20),
        ).run(RecoveryManifest(records=(record,)), dry_run=True)

    assert report.ok is True
    assert report.source_requests == 0
    assert report.files_reused == 1
    assert report.records[0].action == "AI_ONLY_RETRY"
