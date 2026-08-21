import json

import httpx

from idx_digest.config import Settings
from idx_digest.synapse_client import SynapseClient
from idx_digest.synapse_contract import (
    AnalysisClaim,
    AnalysisImportantDate,
    AnalysisKeyNumber,
    CommitAnalysisRequest,
    CoverageCommitRequest,
    CreateRunRequest,
    DisclosureFileUpsertItem,
    DisclosureFilesUpsertRequest,
    DisclosureUpsertItem,
    DisclosureUpsertRequest,
    StructuredAnalysis,
    UpdateProcessingStatusRequest,
    UpdateRunRequest,
)


RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
DISCLOSURE_ID = "70f28dd7-09f2-4936-92c8-01c22d1a1e95"
FILE_ID = "a15b1438-61b0-4bd9-9cf0-5a831d3f531f"
ANALYSIS_ID = "2cb247cf-9697-4c92-9bcc-075b6c783916"
COVERAGE_ID = "1948f033-22d4-4e74-8289-5a40c2c47f90"


def _settings() -> Settings:
    return Settings(
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def test_create_run_and_relevance_use_camel_case_contract() -> None:
    seen: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen[request.url.path] = payload
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.url.path == "/api/internal/idx/relevance":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"ticker": "BBRI", "isPortfolio": False, "isWatchlist": True, "priority": 1},
                        {"ticker": "ANTM", "isPortfolio": False, "isWatchlist": False, "priority": 3},
                    ]
                },
            )
        raise AssertionError(f"unexpected path: {request.url.path}")

    with SynapseClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        run = client.create_run(
            CreateRunRequest(
                mode="DAILY",
                requested_from="2026-08-21T00:00:00Z",
                requested_to="2026-08-21T01:00:00Z",
                engine_version="0.16.0",
            )
        )
        relevance = client.resolve_relevance([" bbri ", "ANTM", "BBRI"])

    assert run.run_id == RUN_ID
    assert relevance.items[0].is_watchlist is True
    assert relevance.items[0].priority == 1
    assert seen["/api/internal/idx/runs"] == {
        "mode": "DAILY",
        "requestedFrom": "2026-08-21T00:00:00Z",
        "requestedTo": "2026-08-21T01:00:00Z",
        "engineVersion": "0.16.0",
    }
    assert seen["/api/internal/idx/relevance"] == {"tickers": ["BBRI", "ANTM"]}


def test_full_write_client_matches_final_synapse_boundary() -> None:
    seen: dict[str, tuple[str, dict[str, object]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen[request.url.path] = (request.method, payload)
        path = request.url.path
        if path == f"/api/internal/idx/runs/{RUN_ID}":
            return httpx.Response(
                200,
                json={"runId": RUN_ID, "status": "COMPLETE", "completedAt": "2026-08-21T01:01:00Z"},
            )
        if path == "/api/internal/idx/disclosures/upsert":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "idxAnnouncementId": "SYNTHETIC-1",
                            "disclosureId": DISCLOSURE_ID,
                            "created": True,
                        }
                    ]
                },
            )
        if path == f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/files/upsert":
            return httpx.Response(
                200,
                json={"files": [{"fileId": FILE_ID, "sourceUrl": "https://example.com/test.pdf"}]},
            )
        if path == f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/analysis":
            return httpx.Response(200, json={"analysisId": ANALYSIS_ID, "promoted": True})
        if path == f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/status":
            return httpx.Response(
                200,
                json={"disclosureId": DISCLOSURE_ID, "processingStatus": "ANALYZING", "readyAt": None},
            )
        if path == "/api/internal/idx/coverage/commit":
            return httpx.Response(201, json={"coverageId": COVERAGE_ID, "created": True})
        raise AssertionError(f"unexpected path: {path}")

    with SynapseClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        updated = client.update_run(
            RUN_ID,
            UpdateRunRequest(
                status="COMPLETE",
                completed_at="2026-08-21T01:01:00Z",
                announcements_found=1,
                announcements_new=1,
                files_downloaded=1,
                files_extracted=1,
                analyses_completed=1,
                source_requests=2,
            ),
        )
        disclosures = client.upsert_disclosures(
            DisclosureUpsertRequest(
                run_id=RUN_ID,
                items=[
                    DisclosureUpsertItem(
                        idx_announcement_id="SYNTHETIC-1",
                        ticker="zzzz",
                        announced_at="2026-08-21T00:30:00Z",
                        title="Synthetic disclosure",
                        source_url="https://example.com/disclosure",
                        raw_metadata={"synthetic": True},
                    )
                ],
            )
        )
        files = client.upsert_files(
            DISCLOSURE_ID,
            DisclosureFilesUpsertRequest(
                files=[
                    DisclosureFileUpsertItem(
                        source_url="https://example.com/test.pdf",
                        normalized_filename="test.pdf",
                        content_type="application/pdf",
                        sha256="a" * 64,
                        selected_for_analysis=True,
                        download_status="DOWNLOADED",
                        extraction_status="EXTRACTED",
                        extraction_method="native",
                        extracted_text_hash="b" * 64,
                    )
                ]
            ),
        )
        status = client.update_processing_status(
            DISCLOSURE_ID,
            UpdateProcessingStatusRequest(processing_status="ANALYZING"),
        )
        analysis = client.commit_analysis(
            DISCLOSURE_ID,
            CommitAnalysisRequest(
                provider="synthetic",
                model="e2e-test",
                schema_version="1.0",
                prompt_version="1.0",
                taxonomy_version="0.1",
                input_hash="c" * 64,
                analysis=StructuredAnalysis(
                    ticker="zzzz",
                    primary_category="OTHER",
                    tags=["OTHER", "OTHER"],
                    materiality="ROUTINE",
                    impact="NEUTRAL",
                    confidence=1.0,
                    executive_summary="Synthetic summary",
                    why_it_matters="Synthetic validation only",
                    material_facts=[
                        AnalysisClaim(
                            claim_type="EXPLICIT_FACT",
                            text="Synthetic fact",
                            source_file_id=FILE_ID,
                            source_page=1,
                        )
                    ],
                    key_numbers=[
                        AnalysisKeyNumber(
                            metric="Synthetic value",
                            value_numeric=1,
                            source_file_id=FILE_ID,
                            source_page=1,
                        )
                    ],
                    important_dates=[
                        AnalysisImportantDate(
                            event_type="TEST",
                            event_date="2026-08-21",
                            description="Synthetic date",
                            source_file_id=FILE_ID,
                            source_page=1,
                        )
                    ],
                ),
            ),
        )
        coverage = client.commit_coverage(
            CoverageCommitRequest(
                run_id=RUN_ID,
                scope="ALL",
                covered_from="2026-08-21T00:00:00Z",
                covered_to="2026-08-21T01:00:00Z",
            )
        )

    assert updated.status == "COMPLETE"
    assert disclosures.items[0].disclosure_id == DISCLOSURE_ID
    assert files.files[0].file_id == FILE_ID
    assert status.processing_status == "ANALYZING"
    assert analysis.analysis_id == ANALYSIS_ID and analysis.promoted is True
    assert coverage.coverage_id == COVERAGE_ID and coverage.created is True

    _, run_payload = seen[f"/api/internal/idx/runs/{RUN_ID}"]
    assert run_payload["completedAt"] == "2026-08-21T01:01:00Z"
    assert run_payload["announcementsFound"] == 1
    assert "completed_at" not in run_payload

    _, disclosure_payload = seen["/api/internal/idx/disclosures/upsert"]
    item = disclosure_payload["items"][0]
    assert item["idxAnnouncementId"] == "SYNTHETIC-1"
    assert item["announcedAt"] == "2026-08-21T00:30:00Z"
    assert item["rawMetadata"] == {"synthetic": True}

    _, file_payload = seen[f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/files/upsert"]
    assert file_payload["files"][0]["selectedForAnalysis"] is True
    assert file_payload["files"][0]["extractedTextHash"] == "b" * 64

    _, analysis_payload = seen[f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/analysis"]
    assert analysis_payload["schemaVersion"] == "1.0"
    assert analysis_payload["analysis"]["primaryCategory"] == "OTHER"
    assert analysis_payload["analysis"]["tags"] == ["OTHER"]
    assert analysis_payload["analysis"]["materialFacts"][0]["sourceFileId"] == FILE_ID

    _, status_payload = seen[f"/api/internal/idx/disclosures/{DISCLOSURE_ID}/status"]
    assert status_payload == {"processingStatus": "ANALYZING"}

    _, coverage_payload = seen["/api/internal/idx/coverage/commit"]
    assert coverage_payload == {
        "runId": RUN_ID,
        "scope": "ALL",
        "coveredFrom": "2026-08-21T00:00:00Z",
        "coveredTo": "2026-08-21T01:00:00Z",
    }
