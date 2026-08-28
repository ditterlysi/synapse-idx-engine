from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource, _announcement_title


JAKARTA = ZoneInfo("Asia/Jakarta")


class FakePoliteClient:
    base_url = "https://www.idx.co.id"

    def __init__(self, payload):
        self.payload = payload
        self.request_count = 0
        self.downloaded_bytes = 0

    def get_json(self, path, *, params):
        assert path == "/primary/ListedCompany/GetAnnouncement"
        self.request_count += 1
        return self.payload

    def download(self, url: str, destination: Path) -> int:
        raise AssertionError("missing-title fixture should not download attachments")


def test_missing_primary_title_uses_subject_and_keeps_daily_window_alive(tmp_path):
    raw_id = "20260828211633-166/UPRI/DIR/VIII/2026_id-id"
    payload = {
        "ResultCount": 1,
        "Replies": [
            {
                "pengumuman": {
                    "Id2": raw_id,
                    "TglPengumuman": "2026-08-28T21:16:33",
                    "JudulPengumuman": "",
                    "PerihalPengumuman": "Penyampaian keterbukaan informasi UPRI",
                    "JenisPengumuman": "Keterbukaan Informasi",
                    "Kode_Emiten": "UPRI",
                },
                "attachments": [],
            }
        ],
    }
    source = IdxWebsiteSource(
        FakePoliteClient(payload),
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoint.json"),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 28, 20, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 28, 22, 0, tzinfo=JAKARTA),
    )

    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.ticker == "UPRI"
    assert disclosure.title == "Penyampaian keterbukaan informasi UPRI"
    assert disclosure.metadata["idxTitleSource"] == "PerihalPengumuman"
    assert result.diagnostics["titleFallbackRows"] == 1
    assert result.diagnostics["titleFallbackDetails"] == [
        {"rowId": raw_id, "ticker": "UPRI", "source": "PerihalPengumuman"}
    ]


def test_title_fallback_is_deterministic_when_all_idx_title_fields_are_empty():
    title, source = _announcement_title({}, "UPRI")

    assert title == "IDX disclosure UPRI"
    assert source == "synthetic"
