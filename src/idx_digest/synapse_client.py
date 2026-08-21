from __future__ import annotations

from urllib.parse import urlparse

import httpx

from .config import Settings
from .synapse_contract import (
    CommitAnalysisRequest,
    CommitAnalysisResponse,
    CoverageCommitRequest,
    CoverageCommitResponse,
    CreateRunRequest,
    CreateRunResponse,
    DisclosureFilesUpsertRequest,
    DisclosureFilesUpsertResponse,
    DisclosureUpsertRequest,
    DisclosureUpsertResponse,
    RelevanceRequest,
    RelevanceResponse,
    SynapseModel,
    UpdateProcessingStatusRequest,
    UpdateProcessingStatusResponse,
    UpdateRunRequest,
    UpdateRunResponse,
)


class SynapseClientConfigurationError(ValueError):
    pass


class SynapseClient:
    """Narrow engine-to-Synapse client.

    The engine intentionally receives only an ingestion secret. It never needs a
    Supabase service-role key or direct write access to arbitrary product tables.
    """

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        base_url = settings.synapse_internal_base_url.strip().rstrip("/")
        secret = settings.synapse_ingestion_secret.get_secret_value().strip()
        self._validate_base_url(base_url)
        if not secret:
            raise SynapseClientConfigurationError("SYNAPSE_INGESTION_SECRET is required")

        self._client = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {secret}",
                "Accept": "application/json",
                "User-Agent": "SynapseIDXEngine/0.16.0",
            },
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    @staticmethod
    def _validate_base_url(base_url: str) -> None:
        if not base_url:
            raise SynapseClientConfigurationError("SYNAPSE_INTERNAL_BASE_URL is required")
        parsed = urlparse(base_url)
        if not parsed.hostname:
            raise SynapseClientConfigurationError("SYNAPSE_INTERNAL_BASE_URL must include a hostname")
        if parsed.scheme == "https":
            return
        if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return
        raise SynapseClientConfigurationError("Synapse internal API must use HTTPS outside local development")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SynapseClient":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @staticmethod
    def _payload(request: SynapseModel) -> dict[str, object]:
        return request.model_dump(mode="json", by_alias=True, exclude_none=True)

    def _request_json(self, method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        response = self._client.request(method, path, json=payload)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Synapse API returned a non-object JSON response")
        return data

    def create_run(self, request: CreateRunRequest) -> CreateRunResponse:
        data = self._request_json("POST", "/api/internal/idx/runs", self._payload(request))
        return CreateRunResponse.model_validate(data)

    def update_run(self, run_id: str, request: UpdateRunRequest) -> UpdateRunResponse:
        data = self._request_json("PATCH", f"/api/internal/idx/runs/{run_id}", self._payload(request))
        return UpdateRunResponse.model_validate(data)

    def resolve_relevance(self, tickers: list[str]) -> RelevanceResponse:
        request = RelevanceRequest(tickers=tickers)
        data = self._request_json("POST", "/api/internal/idx/relevance", self._payload(request))
        return RelevanceResponse.model_validate(data)

    def upsert_disclosures(self, request: DisclosureUpsertRequest) -> DisclosureUpsertResponse:
        data = self._request_json("POST", "/api/internal/idx/disclosures/upsert", self._payload(request))
        return DisclosureUpsertResponse.model_validate(data)

    def upsert_files(
        self,
        disclosure_id: str,
        request: DisclosureFilesUpsertRequest,
    ) -> DisclosureFilesUpsertResponse:
        data = self._request_json(
            "POST",
            f"/api/internal/idx/disclosures/{disclosure_id}/files/upsert",
            self._payload(request),
        )
        return DisclosureFilesUpsertResponse.model_validate(data)

    def commit_analysis(self, disclosure_id: str, request: CommitAnalysisRequest) -> CommitAnalysisResponse:
        data = self._request_json(
            "POST",
            f"/api/internal/idx/disclosures/{disclosure_id}/analysis",
            self._payload(request),
        )
        return CommitAnalysisResponse.model_validate(data)

    def update_processing_status(
        self,
        disclosure_id: str,
        request: UpdateProcessingStatusRequest,
    ) -> UpdateProcessingStatusResponse:
        data = self._request_json(
            "POST",
            f"/api/internal/idx/disclosures/{disclosure_id}/status",
            self._payload(request),
        )
        return UpdateProcessingStatusResponse.model_validate(data)

    def commit_coverage(self, request: CoverageCommitRequest) -> CoverageCommitResponse:
        data = self._request_json("POST", "/api/internal/idx/coverage/commit", self._payload(request))
        return CoverageCommitResponse.model_validate(data)
