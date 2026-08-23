from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from idx_digest.idx_polite_http import (
    IdxAccessProtectionError,
    IdxResourceNotFoundError,
    PoliteFetchClient,
)


def _client(handler, *, max_retries=2):
    return PoliteFetchClient(
        request_delay_seconds=0,
        request_jitter_seconds=0,
        max_retries=max_retries,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def test_polite_client_stops_on_403_without_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"blocked": True}, request=request)

    with _client(handler) as client:
        with pytest.raises(IdxAccessProtectionError, match="403"):
            client.get_json("/primary/ListedCompany/GetAnnouncement", params={})

    assert calls == 1


def test_polite_client_stops_on_429_without_retry():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"}, json={}, request=request)

    with _client(handler) as client:
        with pytest.raises(IdxAccessProtectionError, match="429"):
            client.get_json("/primary/ListedCompany/GetAnnouncement", params={})

    assert calls == 1


def test_polite_client_retries_5xx_at_most_twice():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"Replies": [], "ResultCount": 0},
            request=request,
        )

    with _client(handler, max_retries=2) as client:
        payload = client.get_json("/primary/ListedCompany/GetAnnouncement", params={})

    assert payload["Replies"] == []
    assert calls == 3


def test_polite_client_stops_on_html_access_challenge():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Cloudflare Turnstile challenge</title></html>",
            request=request,
        )

    with _client(handler) as client:
        with pytest.raises(IdxAccessProtectionError, match="challenge"):
            client.get_json("/primary/ListedCompany/GetAnnouncement", params={})


def test_polite_client_maps_404_to_resource_not_found():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(404, request=request)

    with _client(handler) as client:
        with pytest.raises(IdxResourceNotFoundError, match="404"):
            client.download(
                "https://www.idx.id/StaticData/NewsAndAnnouncement/missing.pdf",
                Path("unused.pdf"),
            )

    assert calls == 1
