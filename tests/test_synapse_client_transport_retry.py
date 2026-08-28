from __future__ import annotations

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.synapse_client import SynapseClient
from idx_digest.synapse_contract import (
    CreateRunRequest,
    DisclosureUpsertItem,
    DisclosureUpsertRequest,
)


RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
DISCLOSURE_ID = "70f28dd7-09f2-4936-92c8-01c22d1a1e95"


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def test_disclosure_upsert_retries_one_transport_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("synthetic read timeout", request=request)
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

    request = DisclosureUpsertRequest(
        run_id=RUN_ID,
        items=[
            DisclosureUpsertItem(
                idx_announcement_id="SYNTHETIC-1",
                ticker="BBRI",
                announced_at="2026-08-28T09:00:00Z",
                title="Synthetic disclosure",
                source_url="https://example.com/disclosure",
            )
        ],
    )

    with SynapseClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        response = client.upsert_disclosures(request)

    assert calls == 2
    assert response.items[0].disclosure_id == DISCLOSURE_ID


def test_create_run_does_not_retry_ambiguous_transport_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic read timeout", request=request)

    request = CreateRunRequest(
        mode="DAILY",
        requested_from="2026-08-28T08:00:00Z",
        requested_to="2026-08-28T09:00:00Z",
        engine_version="0.16.0",
    )

    with SynapseClient(_settings(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout, match="synthetic read timeout"):
            client.create_run(request)

    assert calls == 1
