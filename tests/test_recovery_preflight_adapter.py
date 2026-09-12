from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.recovery_live_pipeline import LiveRecoveryPipeline
from idx_digest.recovery_preflight_adapter import SynapseRecoveryPreflightStore
from idx_digest.recovery_write_adapter import SynapseRecoveryWriteStore
from idx_digest.recovery_runner import (
    RecoveryCaps,
    RecoveryExecutionError,
    RecoveryManifest,
    RecoveryManifestRecord,
    RecoveryReadError,
    RecoveryRunner,
)


DISCLOSURE_ID = UUID("11111111-1111-4111-8111-111111111111")
ANALYSIS_ID = UUID("22222222-2222-4222-8222-222222222222")
FILE_ID = UUID("33333333-3333-4333-8333-333333333333")
HASH = "a" * 64
UPDATED_AT = "2026-09-11T01:02:03Z"


class _NoopSummarizer:
    model = "diagnostic-model"
    announcement_prompt_version = "diagnostic-prompt"

    def close(self) -> None:
        return None


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def _payload(*, files=None, analysis_state=None, disclosure=None) -> dict[str, object]:
    return {
        "sourceId": "idx-website",
        "disclosure": {
            "disclosureId": str(DISCLOSURE_ID),
            "externalId": "idx-web-123",
            "sourceId": "idx-website",
            "ticker": "BBRI",
            "title": "Disclosure title",
            "processingStatus": "PARTIAL",
            "updatedAt": UPDATED_AT,
            "isStockScope": True,
            "declaredAttachmentCount": 1,
            "attachmentHashes": [HASH],
            **(disclosure or {}),
        },
        "files": files or [],
        "analysisState": analysis_state
        or {
            "hasAnalysis": False,
            "analysisCount": 0,
            "activeAnalysisId": None,
            "activeAnalysisPresent": False,
            "claimsCount": 0,
            "numbersCount": 0,
            "datesCount": 0,
        },
    }


def test_exact_id_adapter_fetches_and_maps_file_analysis_state(tmp_path) -> None:
    requests: list[httpx.Request] = []
    body = _payload(
        files=[
            {
                "fileId": str(FILE_ID),
                "sourceUrl": "https://idx.example/document.pdf",
                "sha256": HASH,
                "downloadStatus": "DOWNLOADED",
                "extractionStatus": "EXTRACTED",
                "extractedTextHash": HASH,
                "extractedTextRef": "storage://text/one",
            }
        ],
        analysis_state={
            "hasAnalysis": True,
            "analysisCount": 1,
            "activeAnalysisId": str(ANALYSIS_ID),
            "activeAnalysisPresent": True,
            "claimsCount": 4,
            "numbersCount": 3,
            "datesCount": 2,
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/preflight"
        assert request.headers["authorization"] == "Bearer test-secret"
        return httpx.Response(200, json=body, request=request)

    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        current = store.fetch_disclosure(DISCLOSURE_ID)
        files = store.list_files(DISCLOSURE_ID)
        analysis = store.analysis_state(DISCLOSURE_ID)
        store.list_files(DISCLOSURE_ID)

    assert current is not None
    assert current.external_id == "idx-web-123"
    assert current.title == "Disclosure title"
    assert current.analysis_present is True
    assert len(files) == 1
    assert files[0].file_id == str(FILE_ID)
    assert files[0].extracted_text_ref == "storage://text/one"
    assert analysis.claims_count == 4
    assert analysis.numbers_count == 3
    assert analysis.dates_count == 2
    assert len(requests) == 1


def test_refresh_disclosure_invalidates_cached_preflight(tmp_path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload(), request=request)

    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        store.fetch_disclosure(DISCLOSURE_ID)
        store.refresh_disclosure(DISCLOSURE_ID)

    assert len(requests) == 2


def test_invalid_uuid_is_rejected_before_network(tmp_path) -> None:
    calls: list[httpx.Request] = []
    with SynapseRecoveryPreflightStore(
        _settings(tmp_path),
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500)),
    ) as store:
        with pytest.raises(RecoveryReadError, match="valid UUID") as raised:
            store.fetch_disclosure("not-a-uuid")  # type: ignore[arg-type]

    assert raised.value.code == "INVALID_UUID"
    assert calls == []


def test_transport_failure_preserves_safe_exception_diagnostic(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection failure", request=request)

    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        with pytest.raises(RecoveryReadError, match="ConnectError: synthetic connection failure") as raised:
            store.fetch_disclosure(DISCLOSURE_ID)

    assert raised.value.code == "INTERNAL_API_ERROR"
    assert "Bearer" not in str(raised.value)
    assert "test-secret" not in str(raised.value)


def test_live_orchestration_preflight_stops_before_source_or_retry_write(tmp_path) -> None:
    body = _payload()
    record = RecoveryManifestRecord(
        disclosure_id=DISCLOSURE_ID,
        expected_status="PARTIAL",
        expected_updated_at=datetime(2026, 9, 11, 1, 2, 3, tzinfo=timezone.utc),
        expected_external_id="idx-web-123",
        ticker="BBRI",
        bucket="C",
        declared_attachment_count=1,
        expected_attachment_hashes=(HASH,),
        intended_recovery_action="diagnostic preflight",
        recovery_allowed=True,
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/preflight"
        return httpx.Response(200, json=body, request=request)

    manifest = RecoveryManifest(records=(record,))
    with LiveRecoveryPipeline(
        _settings(tmp_path),
        max_source_requests=12,
        summarizer_factory=lambda settings: _NoopSummarizer(),
        transport=httpx.MockTransport(handler),
        request_delay_seconds=0,
        request_jitter_seconds=0,
    ) as pipeline, SynapseRecoveryWriteStore(
        _settings(tmp_path),
        manifest,
        caps=RecoveryCaps(max_records=1, max_source_requests=12, max_attachments=20, max_ai_documents=20),
        audit_phase="2B-5A",
        transport=httpx.MockTransport(handler),
    ) as store:
        hooks = pipeline.hooks()
        # Keep the exact live hook composition but stop before IDX source
        # resolution so this diagnostic remains preflight-only.
        hooks.prepare_source = None
        report = RecoveryRunner(
            store,
            hooks=hooks,
        ).run(manifest, dry_run=True)

    assert report.ok is True
    assert report.run_id is None
    assert report.mutations == 0
    assert report.records[0].outcome == "PLANNED"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "UNAUTHORIZED"), (404, "NOT_FOUND"), (409, "WRONG_SOURCE"), (500, "INTERNAL_API_ERROR")],
)
def test_http_errors_are_distinguished(tmp_path, status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code}}, request=request)

    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        with pytest.raises(RecoveryReadError) as raised:
            store.fetch_disclosure(DISCLOSURE_ID)
    assert raised.value.code == code


def test_not_found_is_safe_skip_for_runner(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND"}}, request=request)

    record = RecoveryManifestRecord(
        disclosure_id=DISCLOSURE_ID,
        expected_status="PARTIAL",
        expected_updated_at=datetime(2026, 9, 11, 1, 2, 3, tzinfo=timezone.utc),
        expected_external_id="idx-web-123",
        ticker="BBRI",
        bucket="C",
        declared_attachment_count=1,
        expected_attachment_hashes=(HASH,),
        intended_recovery_action="resume",
        recovery_allowed=True,
    )
    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        report = RecoveryRunner(store).run(RecoveryManifest(records=(record,)), dry_run=True)

    assert report.skipped == 1
    assert report.records[0].outcome == "PRECONDITION_FAILED"
    assert "NOT_FOUND" in report.records[0].reasons[0]
    assert report.mutations == 0


def test_adapter_is_read_only_and_runner_preflight_parses_response(tmp_path) -> None:
    body = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    record = RecoveryManifestRecord(
        disclosure_id=DISCLOSURE_ID,
        expected_status="PARTIAL",
        expected_updated_at=datetime(2026, 9, 11, 1, 2, 3, tzinfo=timezone.utc),
        expected_external_id="idx-web-123",
        ticker="BBRI",
        bucket="C",
        declared_attachment_count=1,
        expected_attachment_hashes=(HASH,),
        intended_recovery_action="resume",
        recovery_allowed=True,
    )
    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        report = RecoveryRunner(store).run(RecoveryManifest(records=(record,)), dry_run=True)
        assert report.ok is True
        assert report.records[0].outcome == "PLANNED"
        with pytest.raises(RecoveryExecutionError, match="read-only"):
            store.create_retry_run(RecoveryManifest(records=(record,)))

    assert report.checkpoint_touched is False
    assert report.watermark_touched is False
    assert report.coverage_touched is False
    assert report.mutations == 0


def test_adapter_rejects_invalid_response_state(tmp_path) -> None:
    body = _payload(disclosure={"sourceId": "other-source"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    with SynapseRecoveryPreflightStore(
        _settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as store:
        with pytest.raises(RecoveryReadError, match="invalid JSON"):
            store.fetch_disclosure(DISCLOSURE_ID)
