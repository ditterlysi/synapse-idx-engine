from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from idx_digest.idx_polite_http import IdxRequestBudgetExceededError, PoliteFetchClient
from idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource


JAKARTA = ZoneInfo("Asia/Jakarta")


def _row(raw_id: str, announced_at: str, ticker: str, filename: str) -> dict[str, object]:
    return {
        "pengumuman": {
            "Id2": raw_id,
            "NoPengumuman": raw_id,
            "TglPengumuman": announced_at,
            "JudulPengumuman": f"Disclosure {raw_id}",
            "JenisPengumuman": "STOCK",
            "Kode_Emiten": ticker,
            "CreatedDate": announced_at,
            "Form_Id": "10000",
            "PerihalPengumuman": "Budget progress fixture",
        },
        "attachments": [
            {
                "PDFFilename": filename,
                "FullSavePath": f"https://www.idx.co.id/StaticData/NewsAndAnnouncement/{filename}",
                "OriginalFilename": filename,
                "IsAttachment": True,
            }
        ],
    }


class BudgetedFakeClient:
    base_url = "https://www.idx.co.id"

    def __init__(self, payload: dict[str, object], *, max_requests: int):
        self.payload = payload
        self.max_requests = max_requests
        self.request_count = 0
        self.downloaded_bytes = 0
        self.download_calls: list[str] = []

    def get_json(self, path: str, *, params: dict[str, object]) -> dict[str, object]:
        assert path == "/primary/ListedCompany/GetAnnouncement"
        assert params["emitenType"] == "*"
        if self.request_count >= self.max_requests:
            raise IdxRequestBudgetExceededError("IDX source request budget exceeded")
        self.request_count += 1
        return self.payload

    def download(self, url: str, destination: Path) -> int:
        if self.request_count >= self.max_requests:
            raise IdxRequestBudgetExceededError("IDX source request budget exceeded")
        self.request_count += 1
        self.download_calls.append(url)
        body = b"%PDF-1.4\nbudget fixture\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        self.downloaded_bytes += len(body)
        return len(body)


def test_polite_client_raises_typed_request_budget_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"Replies": [], "ResultCount": 0},
        )

    client = PoliteFetchClient(
        request_delay_seconds=0,
        request_jitter_seconds=0,
        max_retries=0,
        max_requests=1,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.get_json("/primary/ListedCompany/GetAnnouncement", params={})
        with pytest.raises(IdxRequestBudgetExceededError, match="request budget exceeded"):
            client.get_json("/primary/ListedCompany/GetAnnouncement", params={})
    finally:
        client.close()


def test_source_preserves_completed_progress_when_attachment_budget_exhausts(tmp_path: Path) -> None:
    first_id = "20260824100000-FIRST-BBRI_id-id"
    second_id = "20260824110000-SECOND-BMRI_id-id"
    payload = {
        "ResultCount": 2,
        "Replies": [
            _row(first_id, "2026-08-24T10:00:00", "BBRI", "first.pdf"),
            _row(second_id, "2026-08-24T11:00:00", "BMRI", "second.pdf"),
        ],
    }
    checkpoint_path = tmp_path / "checkpoint.json"
    cache_dir = tmp_path / "cache"
    start_at = datetime(2026, 8, 24, 9, 0, tzinfo=JAKARTA)
    end_at = datetime(2026, 8, 24, 12, 0, tzinfo=JAKARTA)

    first_client = BudgetedFakeClient(payload, max_requests=2)
    first_source = IdxWebsiteSource(
        first_client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=cache_dir,
    )
    first_result = first_source.collect_window(start_at=start_at, end_at=end_at)

    # Under a constrained source budget, the newest disclosure is completed first.
    assert [item.external_id for item in first_result.disclosures] == [f"idx-web-{second_id}"]
    assert first_result.diagnostics["sourceRequests"] == 2
    assert first_result.diagnostics["requestBudgetDeferred"] is True
    assert first_result.diagnostics["requestBudgetDeferredRowId"] == first_id
    assert checkpoint_path.exists() is False

    first_source.commit_checkpoint()
    first_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert first_checkpoint["seenIds"] == [second_id]
    # Hold the time watermark while older work is deferred so it remains eligible.
    assert first_checkpoint["latestAnnouncedAt"] is None
    assert first_id not in first_checkpoint["seenIds"]

    second_client = BudgetedFakeClient(payload, max_requests=2)
    second_source = IdxWebsiteSource(
        second_client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=cache_dir,
    )
    second_result = second_source.collect_window(start_at=start_at, end_at=end_at)

    assert [item.external_id for item in second_result.disclosures] == [f"idx-web-{first_id}"]
    assert second_result.diagnostics["alreadySeenInRequestedWindow"] == 1
    assert second_result.diagnostics["requestBudgetDeferred"] is False
    assert second_result.diagnostics["requestBudgetDeferredRowId"] is None

    second_source.commit_checkpoint()
    second_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert second_checkpoint["seenIds"] == [second_id, first_id]
    # Once backlog clears, advance to the newest completed ID observed in-window.
    assert second_checkpoint["latestAnnouncedAt"].startswith("2026-08-24T11:00:00")
