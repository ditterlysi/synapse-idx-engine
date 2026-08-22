from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings
from .sources.idx_website import CHECKPOINT_SCHEMA, IdxWebsiteCheckpoint
from .synapse_client import SynapseClient
from .synapse_contract import CreateRunRequest, CreateRunResponse, UpdateRunRequest, UpdateRunResponse


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def checkpoint_from_payload(payload: object) -> IdxWebsiteCheckpoint:
    if payload is None:
        return IdxWebsiteCheckpoint()
    if not isinstance(payload, dict) or payload.get("schemaVersion") != CHECKPOINT_SCHEMA:
        raise ValueError("Synapse returned an invalid IDX website checkpoint schema")
    seen = payload.get("seenIds") or []
    latest = payload.get("latestAnnouncedAt")
    if not isinstance(seen, list) or len(seen) > 1000:
        raise ValueError("Synapse returned an invalid IDX website checkpoint seenIds")
    if any(not isinstance(item, str) or not item for item in seen):
        raise ValueError("Synapse returned an invalid IDX website checkpoint id")
    if latest is not None and not isinstance(latest, str):
        raise ValueError("Synapse returned an invalid latestAnnouncedAt")
    return IdxWebsiteCheckpoint(tuple(seen), latest)


def checkpoint_payload(checkpoint: IdxWebsiteCheckpoint) -> dict[str, object]:
    return {
        "schemaVersion": CHECKPOINT_SCHEMA,
        "seenIds": list(checkpoint.seen_ids[-1000:]),
        "latestAnnouncedAt": checkpoint.latest_announced_at,
        "committedAt": _now_iso(),
    }


class SourceStateSynapseClient(SynapseClient):
    """Synapse client that tags source runs and persists source reliability state."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_id: str,
        source_request_counter: Callable[[], int] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(settings, transport=transport)
        self.source_id = source_id
        self.source_request_counter = source_request_counter

    def _request_without_model(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        response = self._client.request(method, path, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Synapse source-state API returned a non-object response")
        return data

    def get_source_state(self) -> dict[str, Any]:
        source_id = quote(self.source_id, safe="")
        return self._request_without_model("GET", f"/api/internal/idx/source-state?sourceId={source_id}")

    def create_run(self, request: CreateRunRequest) -> CreateRunResponse:
        response = super().create_run(request)
        self._request_without_model(
            "POST",
            "/api/internal/idx/source-state/register",
            {"action": "REGISTER", "runId": response.run_id, "sourceId": self.source_id},
        )
        return response

    def update_run(self, run_id: str, request: UpdateRunRequest) -> UpdateRunResponse:
        if self.source_request_counter is not None and request.source_requests is not None:
            request = request.model_copy(update={"source_requests": max(0, int(self.source_request_counter()))})
        return super().update_run(run_id, request)

    def commit_source_state(
        self,
        *,
        run_id: str,
        processing_ok: bool,
        source_transport: str,
        source_complete: bool,
        coverage_committed: bool,
        checkpoint: IdxWebsiteCheckpoint | None,
    ) -> dict[str, Any]:
        if not processing_ok and checkpoint is not None:
            raise ValueError("failed processing must not advance the source checkpoint")
        payload: dict[str, object] = {
            "action": "COMMIT",
            "runId": run_id,
            "sourceId": self.source_id,
            "processingOk": processing_ok,
            "sourceTransport": source_transport,
            "sourceComplete": source_complete,
            "coverageCommitted": coverage_committed,
            "checkpoint": checkpoint_payload(checkpoint) if checkpoint is not None else None,
        }
        return self._request_without_model("POST", "/api/internal/idx/source-state/commit", payload)
