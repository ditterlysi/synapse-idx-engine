from __future__ import annotations

import httpx

from idx_digest.config import Settings
from idx_digest.source_state_client import SourceStateSynapseClient
from idx_digest.synapse_client import SynapseClient
from idx_digest.synapse_contract import (
    CommitAnalysisResponse,
    DisclosureUpsertItem,
    DisclosureUpsertRequest,
)


RUN_ID = "11111111-1111-4111-8111-111111111111"
DISCLOSURE_ID = "22222222-2222-4222-8222-222222222222"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def test_ready_upsert_is_immediately_checkpoint_eligible(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/internal/idx/disclosures/upsert"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "idxAnnouncementId": "idx-web-READY-1",
                        "disclosureId": DISCLOSURE_ID,
                        "created": False,
                        "processingStatus": "READY",
                    }
                ]
            },
        )

    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )
    try:
        client.upsert_disclosures(
            DisclosureUpsertRequest(
                run_id=RUN_ID,
                items=[
                    DisclosureUpsertItem(
                        idx_announcement_id="idx-web-READY-1",
                        ticker="BBRI",
                        announced_at="2026-08-27T10:00:00+07:00",
                        title="Ready fixture",
                    )
                ],
            )
        )
    finally:
        client.close()

    assert client.checkpoint_eligible_external_ids == {"idx-web-READY-1"}


def test_successful_analysis_commit_becomes_checkpoint_eligible(tmp_path, monkeypatch) -> None:
    def fake_commit_analysis(self, disclosure_id, request):
        assert disclosure_id == DISCLOSURE_ID
        return CommitAnalysisResponse(analysis_id="analysis-id", promoted=True)

    monkeypatch.setattr(SynapseClient, "commit_analysis", fake_commit_analysis)
    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    client._external_id_by_disclosure_id[DISCLOSURE_ID] = "idx-web-NEW-1"
    try:
        client.commit_analysis(DISCLOSURE_ID, object())
    finally:
        client.close()

    assert client.checkpoint_eligible_external_ids == {"idx-web-NEW-1"}
