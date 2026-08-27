from __future__ import annotations

import json

import httpx

from idx_digest.config import Settings
from idx_digest.source_state_client import SourceStateSynapseClient
from idx_digest.sources.idx_website import IdxWebsiteCheckpoint


RUN_ID = "11111111-1111-4111-8111-111111111111"


def test_partial_checkpoint_falls_back_safely_for_legacy_synapse_contract(tmp_path) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/internal/idx/source-state/commit"
        body = json.loads(request.content)
        payloads.append(body)
        if len(payloads) == 1:
            return httpx.Response(400, json={"code": "INVALID_REQUEST"})
        return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})

    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )
    client = SourceStateSynapseClient(
        settings,
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = client.commit_source_state(
            run_id=RUN_ID,
            processing_ok=False,
            source_transport="http-only",
            source_complete=False,
            coverage_committed=False,
            checkpoint=IdxWebsiteCheckpoint(("IDX-READY",), "2026-08-27T10:00:00+07:00"),
            checkpoint_progress_preserved=True,
        )
    finally:
        client.close()

    assert result["runId"] == RUN_ID
    assert payloads[0]["checkpointProgressPreserved"] is True
    assert payloads[0]["checkpoint"]["seenIds"] == ["IDX-READY"]
    assert "checkpointProgressPreserved" not in payloads[1]
    assert payloads[1]["processingOk"] is False
    assert payloads[1]["checkpoint"] is None
