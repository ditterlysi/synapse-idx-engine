from __future__ import annotations

import json

import httpx

from idx_digest.config import Settings
from idx_digest.source_state_client import (
    SourceStateSynapseClient,
    checkpoint_from_payload,
    checkpoint_payload,
)
from idx_digest.sources.idx_website import IdxWebsiteCheckpoint
from idx_digest.synapse_contract import CreateRunRequest, UpdateRunRequest


RUN_ID = "11111111-1111-4111-8111-111111111111"
OLD_RUN_ID = "22222222-2222-4222-8222-222222222222"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def test_checkpoint_payload_round_trip() -> None:
    checkpoint = IdxWebsiteCheckpoint(("IDX-1", "IDX-2"), "2026-08-21T22:10:47+07:00")
    payload = checkpoint_payload(checkpoint)
    assert payload["schemaVersion"] == "synapse-idx-website-checkpoint-v1"
    assert checkpoint_from_payload(payload) == checkpoint


def test_source_state_client_registers_run_and_uses_real_source_request_count(tmp_path) -> None:
    requests: list[tuple[str, str, dict[str, object] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/api/internal/idx/source-state":
            return httpx.Response(
                200,
                json={"sourceId": "idx-website", "latestAttempt": None, "checkpoint": None},
            )
        if request.method == "POST" and request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/register":
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        if request.method == "PATCH" and request.url.path == f"/api/internal/idx/runs/{RUN_ID}":
            return httpx.Response(
                200,
                json={"runId": RUN_ID, "status": "PARTIAL", "completedAt": "2026-08-22T11:00:00Z"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        source_request_counter=lambda: 7,
        transport=httpx.MockTransport(handler),
    )
    try:
        created = client.create_run(CreateRunRequest(mode="MANUAL_BACKFILL"))
        client.update_run(
            created.run_id,
            UpdateRunRequest(
                status="PARTIAL",
                completed_at="2026-08-22T11:00:00Z",
                source_requests=0,
            ),
        )
    finally:
        client.close()

    assert requests[0][:2] == ("GET", "/api/internal/idx/source-state")
    assert requests[2][2] == {"action": "REGISTER", "runId": RUN_ID, "sourceId": "idx-website"}
    assert requests[3][2]["sourceRequests"] == 7


def test_create_run_recovers_stale_source_run_before_starting_new_run(tmp_path) -> None:
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
                        "started_at": "2020-01-01T00:00:00Z",
                    },
                    "checkpoint": None,
                },
            )
        if request.method == "PATCH" and request.url.path == f"/api/internal/idx/runs/{OLD_RUN_ID}":
            return httpx.Response(
                200,
                json={
                    "runId": OLD_RUN_ID,
                    "status": "FAILED",
                    "completedAt": body["completedAt"],
                },
            )
        if request.method == "POST" and request.url.path == "/api/internal/idx/runs":
            return httpx.Response(201, json={"runId": RUN_ID})
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/register":
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )
    try:
        created = client.create_run(CreateRunRequest(mode="DAILY"))
    finally:
        client.close()

    assert created.run_id == RUN_ID
    assert [item[:2] for item in requests] == [
        ("GET", "/api/internal/idx/source-state"),
        ("PATCH", f"/api/internal/idx/runs/{OLD_RUN_ID}"),
        ("POST", "/api/internal/idx/runs"),
        ("POST", "/api/internal/idx/source-state/register"),
    ]
    recovery_payload = requests[1][2]
    assert recovery_payload is not None
    assert recovery_payload["status"] == "FAILED"
    assert recovery_payload["errorCode"] == "STALE_RUN_RECOVERED"
    assert recovery_payload["completedAt"]


def test_source_state_client_reads_and_commits_checkpoint(tmp_path) -> None:
    checkpoint = IdxWebsiteCheckpoint(("IDX-1",), "2026-08-21T22:10:47+07:00")
    committed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/internal/idx/source-state":
            return httpx.Response(
                200,
                json={
                    "sourceId": "idx-website",
                    "latestAttempt": None,
                    "checkpoint": checkpoint_payload(checkpoint),
                },
            )
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/commit":
            committed.update(json.loads(request.content))
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )
    try:
        state = client.get_source_state()
        restored = checkpoint_from_payload(state["checkpoint"])
        client.commit_source_state(
            run_id=RUN_ID,
            processing_ok=True,
            source_transport="http-only",
            source_complete=False,
            coverage_committed=False,
            checkpoint=restored,
        )
    finally:
        client.close()

    assert restored == checkpoint
    assert committed["processingOk"] is True
    assert committed["checkpoint"]["seenIds"] == ["IDX-1"]
    assert committed["checkpointProgressPreserved"] is False


def test_failed_processing_cannot_advance_checkpoint_without_explicit_progress_flag(tmp_path) -> None:
    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    try:
        try:
            client.commit_source_state(
                run_id=RUN_ID,
                processing_ok=False,
                source_transport="http-only",
                source_complete=False,
                coverage_committed=False,
                checkpoint=IdxWebsiteCheckpoint(("IDX-1",), None),
            )
        except ValueError as exc:
            assert "explicitly preserved" in str(exc)
        else:
            raise AssertionError("expected ValueError")
    finally:
        client.close()


def test_failed_processing_can_preserve_explicit_completed_checkpoint_progress(tmp_path) -> None:
    committed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/internal/idx/source-state/commit":
            committed.update(json.loads(request.content))
            return httpx.Response(200, json={"runId": RUN_ID, "sourceId": "idx-website"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )
    try:
        client.commit_source_state(
            run_id=RUN_ID,
            processing_ok=False,
            source_transport="http-only",
            source_complete=False,
            coverage_committed=False,
            checkpoint=IdxWebsiteCheckpoint(("IDX-READY",), "2026-08-21T22:10:47+07:00"),
            checkpoint_progress_preserved=True,
        )
    finally:
        client.close()

    assert committed["processingOk"] is False
    assert committed["coverageCommitted"] is False
    assert committed["checkpointProgressPreserved"] is True
    assert committed["checkpoint"]["seenIds"] == ["IDX-READY"]


def test_partial_checkpoint_progress_rejects_invalid_combinations(tmp_path) -> None:
    client = SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    try:
        invalid = (
            dict(processing_ok=True, coverage_committed=False, checkpoint=IdxWebsiteCheckpoint(("IDX-1",), None)),
            dict(processing_ok=False, coverage_committed=False, checkpoint=None),
            dict(processing_ok=False, coverage_committed=True, checkpoint=IdxWebsiteCheckpoint(("IDX-1",), None)),
        )
        for case in invalid:
            try:
                client.commit_source_state(
                    run_id=RUN_ID,
                    source_transport="http-only",
                    source_complete=False,
                    checkpoint_progress_preserved=True,
                    **case,
                )
            except ValueError:
                pass
            else:
                raise AssertionError(f"expected ValueError for {case!r}")
    finally:
        client.close()
