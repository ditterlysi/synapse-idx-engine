from __future__ import annotations

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.daily_guardrails import DailyBudgetExceeded, DailyPolicy, DailyPolicyError, DailyRunBudget
from idx_digest.synapse_client import SynapseClient, SynapseClientConfigurationError
from idx_digest.synapse_contract import TickerRelevance


def test_daily_policy_defaults_are_conservative() -> None:
    settings = Settings(_env_file=None)
    policy = DailyPolicy.from_settings(settings)
    assert settings.synapse_daily_enabled is False
    assert policy.transport == "http"
    assert policy.request_delay_seconds >= 0.5
    assert policy.allow_historical_backfill is False
    assert policy.allow_ticker_fanout is False


def test_daily_policy_rejects_browser_transport_and_fanout() -> None:
    with pytest.raises(DailyPolicyError):
        DailyPolicy.from_settings(Settings(_env_file=None, synapse_daily_transport="browser"))
    with pytest.raises(DailyPolicyError):
        DailyPolicy.from_settings(Settings(_env_file=None, synapse_daily_allow_ticker_fanout=True))


def test_daily_budget_fails_before_exceeding_source_request_cap() -> None:
    policy = DailyPolicy.from_settings(Settings(_env_file=None, synapse_daily_max_source_requests=2))
    budget = DailyRunBudget(policy)
    budget.consume_source_request()
    budget.consume_source_request()
    with pytest.raises(DailyBudgetExceeded):
        budget.consume_source_request()
    assert budget.snapshot()["source_requests"] == 2


def test_portfolio_contract_requires_p0() -> None:
    item = TickerRelevance(ticker=" bbri ", is_portfolio=True, is_watchlist=True, priority=0)
    assert item.ticker == "BBRI"
    with pytest.raises(ValueError):
        TickerRelevance(ticker="BBRI", is_portfolio=True, is_watchlist=True, priority=1)


def test_synapse_client_rejects_insecure_remote_url() -> None:
    settings = Settings(
        _env_file=None,
        synapse_internal_base_url="http://example.com",
        synapse_ingestion_secret="secret",
    )
    with pytest.raises(SynapseClientConfigurationError):
        SynapseClient(settings)


def test_synapse_client_sends_narrow_bearer_contract() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-secret"
        assert request.url.path == "/api/internal/idx/relevance"
        return httpx.Response(
            200,
            json={"items": [{"ticker": "ANTM", "is_portfolio": True, "is_watchlist": False, "priority": 0}]},
        )

    settings = Settings(
        _env_file=None,
        synapse_internal_base_url="https://synapse.example.com",
        synapse_ingestion_secret="test-secret",
    )
    with SynapseClient(settings, transport=httpx.MockTransport(handler)) as client:
        response = client.resolve_relevance(["antm", "ANTM"])
    assert len(response.items) == 1
    assert response.items[0].ticker == "ANTM"
    assert response.items[0].priority == 0
