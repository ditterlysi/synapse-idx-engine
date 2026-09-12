from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import quote
from uuid import UUID

import httpx

from .config import Settings
from .recovery_runner import (
    CurrentDisclosure,
    CurrentFile,
    RecoveryExecutionError,
    RecoveryReadError,
    RecoveryStore,
)
from .synapse_client import create_synapse_internal_http_client
from .synapse_contract import RecoveryPreflightAnalysisState, RecoveryPreflightResponse


class SynapseRecoveryPreflightStore(RecoveryStore):
    """Read-only exact-ID adapter for the Phase 2B-3A preflight gate.

    This store deliberately exposes no live execution capability.  Every write
    method fails closed, while the HTTP client uses the same Bearer secret and
    base URL validation as the existing Synapse ingestion client.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = create_synapse_internal_http_client(settings, transport=transport)
        self._responses: dict[UUID, RecoveryPreflightResponse] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SynapseRecoveryPreflightStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def invalidate(self, disclosure_id: UUID | str) -> None:
        """Drop one cached preflight after a successful write."""

        self._responses.pop(self._disclosure_uuid(disclosure_id), None)

    def refresh_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        """Fetch one disclosure again for the final pre-mutation guard."""

        self.invalidate(disclosure_id)
        return self.fetch_disclosure(disclosure_id)

    @staticmethod
    def _disclosure_uuid(disclosure_id: UUID | str) -> UUID:
        if isinstance(disclosure_id, UUID):
            return disclosure_id
        try:
            return UUID(str(disclosure_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise RecoveryReadError("INVALID_UUID", "disclosure id must be a valid UUID") from exc

    def _fetch(self, disclosure_id: UUID | str) -> RecoveryPreflightResponse:
        normalized_id = self._disclosure_uuid(disclosure_id)
        cached = self._responses.get(normalized_id)
        if cached is not None:
            return cached

        path = f"/api/internal/idx/disclosures/{quote(str(normalized_id), safe='')}/preflight"
        try:
            response = self._client.get(path)
        except httpx.TransportError as exc:
            # Keep the outward error classified as an internal API failure, but
            # retain the transport class and safe exception text for local
            # diagnostics.  httpx transport exceptions do not contain request
            # headers, so the ingestion credential is never included here.
            detail = str(exc).strip() or "no transport detail"
            raise RecoveryReadError(
                "INTERNAL_API_ERROR",
                f"Synapse preflight request failed ({type(exc).__name__}: {detail})",
            ) from exc

        if response.status_code >= 400:
            code = "INTERNAL_API_ERROR"
            if response.status_code == 401:
                code = "UNAUTHORIZED"
            elif response.status_code == 404:
                code = "NOT_FOUND"
            elif response.status_code == 400:
                code = "INVALID_UUID"
            elif response.status_code == 409:
                code = "WRONG_SOURCE"
            raise RecoveryReadError(code, f"Synapse preflight returned HTTP {response.status_code}")

        try:
            payload = response.json()
            result = RecoveryPreflightResponse.model_validate(payload)
        except (ValueError, TypeError) as exc:
            raise RecoveryReadError("INTERNAL_API_ERROR", "Synapse preflight returned invalid JSON") from exc
        self._responses[normalized_id] = result
        return result

    def fetch_disclosure(self, disclosure_id: UUID) -> CurrentDisclosure | None:
        response = self._fetch(disclosure_id)
        disclosure = response.disclosure
        return CurrentDisclosure(
            disclosure_id=disclosure.disclosure_id,
            external_id=disclosure.external_id,
            ticker=disclosure.ticker,
            title=disclosure.title,
            processing_status=disclosure.processing_status,
            updated_at=disclosure.updated_at,
            is_stock_scope=disclosure.is_stock_scope,
            declared_attachment_count=disclosure.declared_attachment_count,
            attachment_hashes=tuple(disclosure.attachment_hashes),
            source_id=disclosure.source_id,
            analysis_present=response.analysis_state.active_analysis_present,
        )

    def list_files(self, disclosure_id: UUID) -> Sequence[CurrentFile]:
        response = self._fetch(disclosure_id)
        return tuple(
            CurrentFile(
                file_id=str(file.file_id),
                disclosure_id=response.disclosure.disclosure_id,
                source_url=file.source_url,
                sha256=file.sha256,
                download_status=file.download_status,
                extraction_status=file.extraction_status,
                extracted_text_hash=file.extracted_text_hash,
                extracted_text_ref=file.extracted_text_ref,
            )
            for file in response.files
        )

    def analysis_state(self, disclosure_id: UUID) -> RecoveryPreflightAnalysisState:
        return self._fetch(disclosure_id).analysis_state

    @staticmethod
    def _read_only(*_args: object, **_kwargs: object) -> None:
        raise RecoveryExecutionError("read-only preflight adapter cannot execute recovery writes")

    create_retry_run = _read_only
    finish_retry_run = _read_only
    update_processing_status = _read_only
    upsert_file = _read_only
    commit_analysis = _read_only


__all__ = ["SynapseRecoveryPreflightStore"]
