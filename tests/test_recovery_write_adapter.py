from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.recovery_runner import (
    RecoveryCaps,
    CurrentFile,
    RecoveryManifest,
    RecoveryManifestRecord,
    RecoveryRecordResult,
    RecoveryRunReport,
    RecoveryExecutionError,
)
from idx_digest.recovery_write_adapter import SynapseRecoveryWriteStore
from idx_digest.synapse_contract import (
    AnalysisClaim,
    CommitAnalysisRequest,
    StructuredAnalysis,
)


NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
ANALYSIS_ID = "2cb247cf-9697-4c92-9bcc-075b6c783916"
CAPS = RecoveryCaps(max_records=1, max_source_requests=9, max_attachments=7, max_ai_documents=6)


def _record(disclosure_id: UUID | None = None) -> RecoveryManifestRecord:
    disclosure_id = disclosure_id or uuid4()
    return RecoveryManifestRecord(
        disclosure_id=disclosure_id,
        expected_status="PARTIAL",
        expected_updated_at=NOW,
        expected_external_id=f"idx-web-{disclosure_id}",
        ticker="TEST",
        bucket="C",
        declared_attachment_count=1,
        expected_attachment_hashes=(HASH,),
        intended_recovery_action="resume",
        recovery_allowed=True,
    )


def _preflight(record: RecoveryManifestRecord) -> dict[str, object]:
    return {
        "sourceId": "idx-website",
        "disclosure": {
            "disclosureId": str(record.disclosure_id),
            "externalId": record.expected_external_id,
            "sourceId": "idx-website",
            "ticker": record.ticker,
            "title": "Synthetic disclosure",
            "processingStatus": record.expected_status,
            "updatedAt": record.expected_updated_at.isoformat().replace("+00:00", "Z"),
            "isStockScope": True,
            "declaredAttachmentCount": record.declared_attachment_count,
            "attachmentHashes": list(record.expected_attachment_hashes),
        },
        "files": [],
        "analysisState": {
            "hasAnalysis": False,
            "analysisCount": 0,
            "activeAnalysisId": None,
            "activeAnalysisPresent": False,
            "claimsCount": 0,
            "numbersCount": 0,
            "datesCount": 0,
        },
    }


def _settings() -> Settings:
    return Settings(
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def _analysis() -> CommitAnalysisRequest:
    return CommitAnalysisRequest(
        provider="synthetic",
        model="e2e-test",
        schema_version="1.0",
        prompt_version="1.0",
        taxonomy_version="0.1",
        input_hash="b" * 64,
        analysis=StructuredAnalysis(
            ticker="TEST",
            primary_category="OTHER",
            tags=["OTHER"],
            materiality="ROUTINE",
            impact="NEUTRAL",
            confidence=1.0,
            executive_summary="Synthetic summary",
            why_it_matters="Synthetic validation",
            material_facts=[AnalysisClaim(claim_type="EXPLICIT_FACT", text="Synthetic fact")],
        ),
    )


def test_writer_uses_existing_per_id_paths_and_persists_recovery_metrics(tmp_path: Path) -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        seen.append((request.method, request.url.path, payload))
        if request.method == "GET":
            return httpx.Response(200, json=_preflight(record))
        if request.url.path == "/api/internal/idx/runs":
            assert payload["mode"] == "RETRY"
            assert payload["metadata"]["recovery"]["manifestId"] == str(manifest.manifest_id)
            assert payload["metadata"]["recovery"]["approvedRecords"][0]["ticker"] == "TEST"
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.url.path == f"/api/internal/idx/runs/{RUN_ID}":
            assert payload["metadata"]["recovery"]["result"]["recordsSucceeded"] == 1
            return httpx.Response(200, json={"runId": RUN_ID, "status": "COMPLETE", "completedAt": "2026-09-10T12:01:00Z"})
        if request.url.path == f"/api/internal/idx/disclosures/{record.disclosure_id}/status":
            return httpx.Response(200, json={"disclosureId": str(record.disclosure_id), "processingStatus": payload["processingStatus"], "readyAt": None})
        if request.url.path == f"/api/internal/idx/disclosures/{record.disclosure_id}/files/upsert":
            return httpx.Response(200, json={"files": [{"fileId": str(uuid4()), "sourceUrl": payload["files"][0]["sourceUrl"]}]})
        if request.url.path == f"/api/internal/idx/disclosures/{record.disclosure_id}/analysis":
            return httpx.Response(200, json={"analysisId": ANALYSIS_ID, "promoted": True})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with SynapseRecoveryWriteStore(
        _settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(handler)
    ) as store:
        run_id = store.create_retry_run(manifest)
        assert run_id == RUN_ID
        store.update_processing_status(record.disclosure_id, "EXTRACTING")
        file_path = tmp_path / "document.pdf"
        file_path.write_bytes(b"document")
        store.upsert_file(
            record.disclosure_id,
            CurrentFile(
                disclosure_id=record.disclosure_id,
                source_url="https://www.idx.id/document.pdf",
                sha256=HASH,
                download_status="DOWNLOADED",
                extraction_status="EXTRACTED",
                extracted_text_hash="c" * 64,
                extracted_text_ref="cache://document",
                local_path=file_path,
            ),
        )
        store.commit_analysis(record.disclosure_id, _analysis())
        report = RecoveryRunReport(
            manifest_id=manifest.manifest_id,
            manifest_digest=manifest.digest,
            dry_run=False,
            ok=True,
            run_id=run_id,
            planned_records=1,
            attempted=1,
            ready=1,
            source_requests=1,
            attachments_considered=1,
            files_downloaded=1,
            extraction_count=1,
            ai_document_requests=1,
            announcement_analyses=1,
            records=(RecoveryRecordResult(disclosure_id=record.disclosure_id, outcome="READY", action="ATTACHMENT_DOWNLOAD"),),
            mutations=1,
        )
        store.finish_retry_run(run_id, report)

    paths = [path for _method, path, _payload in seen]
    create_payload = next(payload for method, path, payload in seen if method == "POST" and path == "/api/internal/idx/runs")
    finish_payload = next(payload for method, path, payload in seen if method == "PATCH" and path == f"/api/internal/idx/runs/{RUN_ID}")
    for payload in (create_payload, finish_payload):
        recovery = payload["metadata"]["recovery"]
        assert recovery["phase"] == "2B-5A"
        assert recovery["runtimeMode"] == "EXECUTE_LIVE"
        assert recovery["caps"] == {
            "maxRecords": 1,
            "maxSourceRequests": 9,
            "maxAttachments": 7,
            "maxAIDocuments": 6,
        }
    assert f"/api/internal/idx/disclosures/{record.disclosure_id}/status" in paths
    assert f"/api/internal/idx/disclosures/{record.disclosure_id}/files/upsert" in paths
    assert f"/api/internal/idx/disclosures/{record.disclosure_id}/analysis" in paths
    assert "/api/internal/idx/coverage/commit" not in paths
    assert all("test-secret" not in json.dumps(payload) for _method, _path, payload in seen)


def test_writer_rejects_ids_outside_manifest_without_http() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))
    with SynapseRecoveryWriteStore(_settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(lambda _request: pytest.fail("HTTP must not be called"))) as store:
        with pytest.raises(RecoveryExecutionError, match="outside the immutable recovery manifest"):
            store.update_processing_status(uuid4(), "PARTIAL")


def test_writer_requires_explicit_audit_phase() -> None:
    manifest = RecoveryManifest(records=(_record(),))
    with pytest.raises(RecoveryExecutionError, match="audit phase is required"):
        SynapseRecoveryWriteStore(
            _settings(),
            manifest,
            caps=CAPS,
            audit_phase=" ",
            transport=httpx.MockTransport(lambda _request: pytest.fail("HTTP must not be called")),
        )


def test_writer_rejects_manifest_digest_change() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))
    changed = RecoveryManifest(records=(record.model_copy(update={"ticker": "OTHER"}),))
    with SynapseRecoveryWriteStore(_settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(lambda _request: pytest.fail("HTTP must not be called"))) as store:
        with pytest.raises(RecoveryExecutionError, match="manifest changed"):
            store.create_retry_run(changed)


def test_writer_requires_existing_analysis_contract() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))
    with SynapseRecoveryWriteStore(_settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(lambda _request: pytest.fail("HTTP must not be called"))) as store:
        with pytest.raises(RecoveryExecutionError, match="CommitAnalysisRequest contract"):
            store.commit_analysis(record.disclosure_id, {"analysis": "wrong"})


def test_writer_rejects_duplicate_retry_run_creation() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_preflight(record))
        return httpx.Response(201, json={"runId": RUN_ID})

    with SynapseRecoveryWriteStore(
        _settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(handler)
    ) as store:
        store.create_retry_run(manifest)
        with pytest.raises(RecoveryExecutionError, match="already created"):
            store.create_retry_run(manifest)


def test_writer_rejects_unexpected_status_response() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_preflight(record))
        if request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={"disclosureId": str(record.disclosure_id), "processingStatus": "PARTIAL", "readyAt": None},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with SynapseRecoveryWriteStore(
        _settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(handler)
    ) as store:
        store.create_retry_run(manifest)
        with pytest.raises(RecoveryExecutionError, match="unexpected processing-status"):
            store.update_processing_status(record.disclosure_id, "EXTRACTING")


def test_writer_rejects_unpromoted_analysis() -> None:
    record = _record()
    manifest = RecoveryManifest(records=(record,))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_preflight(record))
        if request.url.path.endswith("/analysis"):
            return httpx.Response(200, json={"analysisId": ANALYSIS_ID, "promoted": False})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    with SynapseRecoveryWriteStore(
        _settings(), manifest, caps=CAPS, audit_phase="2B-5A", transport=httpx.MockTransport(handler)
    ) as store:
        with pytest.raises(RecoveryExecutionError, match="did not promote"):
            store.commit_analysis(record.disclosure_id, _analysis())
