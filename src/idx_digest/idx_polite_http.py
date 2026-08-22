from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx


OFFICIAL_IDX_HOSTS = {"idx.co.id", "www.idx.co.id", "idx.id", "www.idx.id"}
_WAF_MARKERS = ("cloudflare", "turnstile", "captcha", "challenge-platform", "cf-chl-")


class IdxPoliteHttpError(RuntimeError):
    """Base error for the conservative IDX HTTP transport."""


class IdxAccessProtectionError(IdxPoliteHttpError):
    """Raised when IDX or an upstream protection layer asks the client to stop."""


class IdxUnexpectedResponseError(IdxPoliteHttpError):
    """Raised when the public endpoint no longer matches the expected response shape."""


class PoliteFetchClient:
    """Small HTTP-only client for the public IDX disclosure endpoint.

    It deliberately has no browser fallback, proxy rotation, cookie farming, CAPTCHA
    handling, or TLS impersonation. Access-protection responses fail the run closed.
    """

    def __init__(
        self,
        *,
        base_url: str = "https://www.idx.co.id",
        user_agent: str = "SynapseIDXEngine/0.16.0",
        request_delay_seconds: float = 10.0,
        request_jitter_seconds: float = 5.0,
        timeout_seconds: float = 30.0,
        max_retries: int = 2,
        max_requests: int = 50,
        max_download_bytes_total: int = 500_000_000,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        if request_delay_seconds < 0:
            raise ValueError("request_delay_seconds must be non-negative")
        if request_jitter_seconds < 0:
            raise ValueError("request_jitter_seconds must be non-negative")
        if max_retries < 0 or max_retries > 2:
            raise ValueError("max_retries must be between 0 and 2")
        if max_requests < 1:
            raise ValueError("max_requests must be at least 1")
        if max_download_bytes_total < 1:
            raise ValueError("max_download_bytes_total must be positive")

        parsed = urlparse(base_url)
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_IDX_HOSTS:
            raise ValueError("base_url must be an official HTTPS IDX host")

        self.base_url = base_url.rstrip("/")
        self.request_delay_seconds = request_delay_seconds
        self.request_jitter_seconds = request_jitter_seconds
        self.max_retries = max_retries
        self.max_requests = max_requests
        self.max_download_bytes_total = max_download_bytes_total
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._random_uniform = random_uniform
        self._last_request_at: float | None = None
        self.request_count = 0
        self.downloaded_bytes = 0

        self.client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "id-ID,id;q=0.9,en;q=0.7",
                "Referer": f"{self.base_url}/id/perusahaan-tercatat/keterbukaan-informasi",
                "User-Agent": user_agent,
            },
            timeout=httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 20.0)),
            follow_redirects=False,
            http2=True,
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "PoliteFetchClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _validate_url(url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_IDX_HOSTS:
            raise IdxPoliteHttpError(f"refusing non-IDX URL: {url!r}")

    def _pace(self) -> None:
        if self._last_request_at is None:
            return
        elapsed = max(0.0, self._monotonic() - self._last_request_at)
        wait = max(0.0, self.request_delay_seconds - elapsed)
        if self.request_jitter_seconds:
            wait += self._random_uniform(0.0, self.request_jitter_seconds)
        if wait:
            self._sleeper(wait)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self._validate_url(url if url.startswith("http") else f"{self.base_url}{url}")
        attempt = 0
        while True:
            if self.request_count >= self.max_requests:
                raise IdxPoliteHttpError("IDX source request budget exceeded")
            self._pace()
            self.request_count += 1
            try:
                response = self.client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise IdxPoliteHttpError(f"IDX transport failed after {attempt + 1} attempt(s): {exc}") from exc
                attempt += 1
                self._sleeper(min(2**attempt, 8))
                continue
            finally:
                self._last_request_at = self._monotonic()

            status = response.status_code
            if status == 403:
                raise IdxAccessProtectionError("IDX returned HTTP 403; collector stopped without bypass")
            if status == 429:
                retry_after = response.headers.get("retry-after")
                detail = f"; Retry-After={retry_after}" if retry_after else ""
                raise IdxAccessProtectionError(f"IDX returned HTTP 429; collector stopped{detail}")
            if status in {401, 407}:
                raise IdxAccessProtectionError(f"IDX access protection returned HTTP {status}; collector stopped")
            if status >= 500:
                if attempt >= self.max_retries:
                    raise IdxPoliteHttpError(f"IDX returned HTTP {status} after {attempt + 1} attempt(s)")
                attempt += 1
                self._sleeper(min(2**attempt, 8))
                continue
            if 300 <= status < 400:
                raise IdxUnexpectedResponseError(f"IDX returned redirect HTTP {status}; collector will not follow it")
            response.raise_for_status()
            return response

    @staticmethod
    def _looks_like_access_challenge(response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        if "html" not in content_type:
            return False
        preview = response.text[:4000].lower()
        return any(marker in preview for marker in _WAF_MARKERS)

    def get_json(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request("GET", path, params=params)
        if self._looks_like_access_challenge(response):
            raise IdxAccessProtectionError("IDX returned an access challenge; collector stopped without bypass")
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            raise IdxUnexpectedResponseError(
                f"IDX JSON endpoint returned unexpected content type {content_type!r}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise IdxUnexpectedResponseError("IDX JSON endpoint returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise IdxUnexpectedResponseError("IDX JSON endpoint response must be an object")
        return payload

    def download(self, url: str, destination: Path) -> int:
        self._validate_url(url)
        response = self._request("GET", url, headers={"Accept": "application/pdf,application/octet-stream,*/*"})
        if self._looks_like_access_challenge(response):
            raise IdxAccessProtectionError("IDX attachment request returned an access challenge; collector stopped")

        destination.parent.mkdir(parents=True, exist_ok=True)
        content = response.content
        projected = self.downloaded_bytes + len(content)
        if projected > self.max_download_bytes_total:
            raise IdxPoliteHttpError("IDX attachment download budget exceeded")
        destination.write_bytes(content)
        self.downloaded_bytes = projected
        return len(content)
