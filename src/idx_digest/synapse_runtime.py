from __future__ import annotations

import json
import random
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Iterator

import httpx

from .daily_guardrails import DailyPolicy, DailyRunBudget
from .downloader import AttachmentDownloader
from .idx_client import IDXClient, IDXResponseError


class DailyRateLimitStop(RuntimeError):
    """Raised immediately when scheduled-mode source access is rate limited."""


class DailyAccessProtectionStop(RuntimeError):
    """Raised when scheduled-mode source access encounters access protection."""


@dataclass
class ConservativeRuntime:
    policy: DailyPolicy
    budget: DailyRunBudget
    sleeper: Callable[[float], None] = time.sleep
    jitter: Callable[[float, float], float] = random.uniform
    clock: Callable[[], float] = time.monotonic
    _last_source_request_at: float = field(default=0.0, init=False, repr=False)
    _pace_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def before_source_request(self) -> None:
        """Apply one global source-request budget and conservative pacing."""
        self.budget.consume_source_request()
        with self._pace_lock:
            now = self.clock()
            wait = 0.0
            if self._last_source_request_at:
                wait = max(
                    0.0,
                    self.policy.request_delay_seconds - max(0.0, now - self._last_source_request_at),
                )
            if self.policy.request_jitter_seconds > 0:
                wait += self.jitter(0.0, self.policy.request_jitter_seconds)
            if wait > 0:
                self.sleeper(wait)
            self._last_source_request_at = self.clock()


_RUNTIME = threading.local()


def current_runtime() -> ConservativeRuntime:
    runtime = getattr(_RUNTIME, "value", None)
    if runtime is None:
        raise RuntimeError("conservative Synapse runtime is not bound")
    return runtime


@contextmanager
def bind_runtime(runtime: ConservativeRuntime) -> Iterator[None]:
    previous = getattr(_RUNTIME, "value", None)
    _RUNTIME.value = runtime
    try:
        yield
    finally:
        if previous is None:
            try:
                delattr(_RUNTIME, "value")
            except AttributeError:
                pass
        else:
            _RUNTIME.value = previous


class ConservativeIDXClient(IDXClient):
    """HTTP-only IDX client used exclusively by the gated Synapse runner.

    Generic/manual research keeps the original IDXClient behavior. This subclass
    intentionally has no browser fallback, no wide-page rescue and no automatic
    retry after HTTP 429.
    """

    def browser_transport(self):  # type: ignore[override]
        raise DailyAccessProtectionStop(
            "Synapse conservative mode does not use browser fallback or access-protection bypass"
        )

    def _get_json_http(self, params: dict[str, object]) -> dict[str, object]:  # type: ignore[override]
        runtime = current_runtime()
        runtime.before_source_request()
        response = self.client.get(self.ENDPOINT, params=params)
        if response.status_code == 429:
            raise DailyRateLimitStop("IDX returned HTTP 429; conservative run stopped")
        if response.status_code == 403:
            raise DailyAccessProtectionStop("IDX returned HTTP 403; conservative run stopped")
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            preview = response.text[:200].replace("\n", " ")
            raise IDXResponseError(
                f"Expected JSON from IDX but received {content_type!r}: {preview!r}"
            )
        payload = response.json()
        if not isinstance(payload, dict) or "Replies" not in payload:
            raise IDXResponseError("IDX response does not contain the expected Replies field")
        return payload

    def _get_json_endpoint(
        self,
        endpoint: str,
        params: dict[str, object],
        *,
        require_replies: bool = False,
    ) -> dict[str, object]:  # type: ignore[override]
        runtime = current_runtime()
        runtime.before_source_request()
        response = self.client.get(endpoint, params=params)
        if response.status_code == 429:
            raise DailyRateLimitStop("IDX returned HTTP 429; conservative run stopped")
        if response.status_code == 403:
            raise DailyAccessProtectionStop("IDX returned HTTP 403; conservative run stopped")
        response.raise_for_status()
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise IDXResponseError("IDX endpoint response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise IDXResponseError("IDX endpoint response is not a JSON object")
        if require_replies and "Replies" not in payload:
            raise IDXResponseError("IDX endpoint response does not contain Replies")
        return payload

    def _wide_page_probe(self, *args, **kwargs):  # type: ignore[override]
        if self.observer:
            self.observer.event(
                "idx",
                "wide-page rescue disabled in Synapse conservative mode",
                level="WARNING",
                always=True,
            )
        return None


class ConservativeAttachmentDownloader(AttachmentDownloader):
    """HTTP-only attachment downloader sharing the scheduled run budget."""

    def _download_bytes_http(
        self,
        url: str,
        task_id: int | None = None,
    ) -> tuple[bytes, str]:  # type: ignore[override]
        runtime = current_runtime()
        runtime.before_source_request()
        chunks: list[bytes] = []
        received = 0
        with self.client.stream("GET", url) as response:
            if response.status_code == 429:
                raise DailyRateLimitStop("attachment source returned HTTP 429; conservative run stopped")
            if response.status_code == 403:
                raise DailyAccessProtectionStop("attachment source returned HTTP 403; conservative run stopped")
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            total = int(content_length) if content_length and content_length.isdigit() else None
            if self.observer and total is not None:
                self.observer.update_task(task_id, total=total)
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if not chunk:
                    continue
                runtime.budget.consume_download_bytes(len(chunk))
                chunks.append(chunk)
                received += len(chunk)
                if self.observer:
                    self.observer.update_task(task_id, completed=received)
            content_type = response.headers.get("content-type", "application/octet-stream")
        return b"".join(chunks), content_type

    def download(self, **kwargs):  # type: ignore[override]
        current_runtime().budget.consume_attachment()
        return super().download(**kwargs)


class BudgetedSummarizerProxy:
    """Count source documents entering AI while delegating the existing summarizer."""

    def __init__(self, wrapped: object, budget: DailyRunBudget):
        self._wrapped = wrapped
        self._budget = budget

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)

    def summarize_document(self, *args, **kwargs):
        self._budget.consume_ai_document()
        return getattr(self._wrapped, "summarize_document")(*args, **kwargs)

    def summarize_routine_announcement(self, *args, **kwargs):
        raw_documents = kwargs.get("raw_documents")
        if raw_documents is None and len(args) >= 2:
            raw_documents = args[1]
        count = len(raw_documents) if isinstance(raw_documents, list) else 1
        self._budget.consume_ai_document(max(1, count))
        return getattr(self._wrapped, "summarize_routine_announcement")(*args, **kwargs)
