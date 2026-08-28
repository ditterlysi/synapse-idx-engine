from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx

from idx_digest.config import Settings
from idx_digest.source_state_client import SourceStateSynapseClient
from idx_digest.synapse_contract import CreateRunRequest


RUN_ID = "11111111-1111-4111-8111-111111111111"
OLD_RUN_ID = "22222222-2222-4222-8222-222222222222"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def _started_minutes_ago(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def test_create_run_recovers_source_row_after_85_minute_budget_grace(tmp_path) -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/internal/idx/source-state":
            return httpx.Response(
                200,
                json={
                    "sourceId": "idx-website",
                    "latestAttempt": {
                        "id": OLD_RUN_ID,
                        "status": "RUNNING",
                        "started_at": _started_minutes_ago(86),
                    },
                    "checkpoint": None,
                },
            )
        if request.method == "PATCH" and request.url.path == f"/api/internal/idx/runs/{OLD_RUN_ID}":
            return httpx.Response(
                200,
                json={"runId": OLD_RUN_ID, "status": "FAILED", "completedAt": body["completedAt"]},
            )
        if request.method == "POST" and request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/register":
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.create_run(CreateRunRequest(mode="DAILY"))

    assert [item[:2] for item in requests] == [
        ("GET", "/api/internal/idx/source-state"),
        ("PATCH", f"/api/internal/idx/runs/{OLD_RUN_ID}"),
        ("POST", "/api/internal/idx/runs"),
        ("POST", "/api/internal/idx/source-state/register"),
    ]
    assert requests[1][2]["errorCode"] == "STALE_RUN_RECOVERED"


def test_create_run_does_not_recover_recent_source_row_inside_grace_window(tmp_path) -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/api/internal/idx/source-state":
            return httpx.Response(
                200,
                json={
                    "sourceId": "idx-website",
                    "latestAttempt": {
                        "id": OLD_RUN_ID,
                        "status": "RUNNING",
                        "started_at": _started_minutes_ago(84),
                    },
                    "checkpoint": None,
                },
            )
        if request.method == "POST" and request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/register":
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    ) as client:
        client.create_run(CreateRunRequest(mode="DAILY"))

    assert ("PATCH", f"/api/internal/idx/runs/{OLD_RUN_ID}") not in requests
