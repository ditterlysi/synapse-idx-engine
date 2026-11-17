"""The daily rules-only relevance lookup must come from Synapse, not only flags.

Before this fix the scheduled workflow passed no portfolio or watchlist flags,
so every disclosure resolved to in_portfolio=false, in_watchlist=false and the
High Impact feed stayed empty. These tests pin the mapping from the internal
relevance response.
"""

from __future__ import annotations

import json

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.source_state_client import SourceStateSynapseClient


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
    )


def _client(tmp_path, payload: dict, requests: list) -> SourceStateSynapseClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        return httpx.Response(200, json=payload)

    return SourceStateSynapseClient(
        _settings(tmp_path),
        source_id="idx-website",
        transport=httpx.MockTransport(handler),
    )


def test_resolve_relevance_maps_portfolio_and_watchlist(tmp_path) -> None:
    requests: list = []
    client = _client(
        tmp_path,
        {
            "items": [
                {"ticker": "BBCA", "isPortfolio": True, "isWatchlist": True},
                {"ticker": "TOWR", "isPortfolio": True, "isWatchlist": False},
                {"ticker": "AKPI", "isPortfolio": False, "isWatchlist": False},
            ]
        },
        requests,
    )

    result = client.resolve_relevance(["bbca", "TOWR", "AKPI", "BBCA"])

    assert result["BBCA"] == {"in_portfolio": True, "in_watchlist": True}
    assert result["TOWR"] == {"in_portfolio": True, "in_watchlist": False}
    assert result["AKPI"] == {"in_portfolio": False, "in_watchlist": False}
    method, path, body = requests[0]
    assert method == "POST"
    assert path == "/api/internal/idx/relevance"
    assert body == {"tickers": ["AKPI", "BBCA", "TOWR"]}, "tickers are deduped and sorted"


def test_resolve_relevance_skips_network_for_empty_input(tmp_path) -> None:
    requests: list = []
    client = _client(tmp_path, {"items": []}, requests)

    assert client.resolve_relevance(["  ", ""]) == {}
    assert requests == []


def test_resolve_relevance_rejects_a_payload_without_items(tmp_path) -> None:
    client = _client(tmp_path, {"oops": True}, [])

    with pytest.raises(ValueError, match="no items"):
        client.resolve_relevance(["BBCA"])
