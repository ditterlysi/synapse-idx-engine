from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from idx_digest.idx_polite_http import IdxResourceNotFoundError
from idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource


JAKARTA = ZoneInfo("Asia/Jakarta")


class FakePoliteClient:
    base_url = "https://www.idx.co.id"

    def __init__(self, payload):
        self.payload = payload
        self.request_count = 0
        self.downloaded_bytes = 0
        self.download_calls: list[str] = []

    def get_json(self, path, *, params):
        assert path == "/primary/ListedCompany/GetAnnouncement"
        assert params["emitenType"] == "*"
        self.request_count += 1
        return self.payload

    def download(self, url: str, destination: Path) -> int:
        self.request_count += 1
        self.download_calls.append(url)
        body = b"%PDF-1.4\ncontrolled fixture\n"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        self.downloaded_bytes += len(body)
        return len(body)


def _payload():
    return {
        "ResultCount": 1,
        "Replies": [
            {
                "pengumuman": {
                    "Id2": "20260822201500-TEST-BBRI_id-id",
                    "NoPengumuman": "TEST/BBRI/VIII/2026",
                    "TglPengumuman": "2026-08-22T20:15:00",
                    "JudulPengumuman": "Controlled IDX website source fixture",
                    "JenisPengumuman": "STOCK",
                    "Kode_Emiten": "BBRI   ",
                    "CreatedDate": "2026-08-22T20:16:00",
                    "Form_Id": "10000",
                    "PerihalPengumuman": "Controlled fixture",
                },
                "attachments": [
                    {
                        "PDFFilename": "fixture.pdf",
                        "FullSavePath": "https://www.idx.co.id/StaticData/NewsAndAnnouncement/fixture.pdf",
                        "OriginalFilename": "20260822_BBRI_fixture.pdf",
                        "IsAttachment": True,
                    }
                ],
            }
        ],
    }


def test_source_stages_new_disclosure_and_commits_checkpoint_explicitly(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    client = FakePoliteClient(_payload())
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
        page_size=50,
        max_pages=2,
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert result.complete is False
    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.external_id == "idx-web-20260822201500-TEST-BBRI_id-id"
    assert disclosure.ticker == "BBRI"
    assert disclosure.title == "Controlled IDX website source fixture"
    assert len(disclosure.attachments) == 1
    assert disclosure.attachments[0].local_path is not None
    assert disclosure.attachments[0].local_path.exists()
    assert result.diagnostics["sourceRequests"] == 2
    assert result.diagnostics["attachmentDownloads"] == 1
    assert result.diagnostics["metadataRowsCollected"] == 1
    assert result.diagnostics["metadataRowsInRequestedWindow"] == 1
    assert result.diagnostics["alreadySeenInRequestedWindow"] == 0
    assert result.diagnostics["newCandidates"] == 1
    assert result.diagnostics["nonIssuerRowsSkipped"] == 0
    assert result.diagnostics["unsupportedTickerRowsSkipped"] == 0
    assert result.diagnostics["issuerDisclosuresProcessed"] == 1
    assert checkpoint_path.exists() is False

    source.commit_checkpoint()
    assert checkpoint_path.exists() is True
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["seenIds"] == ["20260822201500-TEST-BBRI_id-id"]


def test_checkpoint_skips_already_seen_disclosure_and_avoids_redownload(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    first_client = FakePoliteClient(_payload())
    first_source = IdxWebsiteSource(
        first_client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )
    first_source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )
    first_source.commit_checkpoint()

    second_client = FakePoliteClient(_payload())
    second_source = IdxWebsiteSource(
        second_client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )
    result = second_source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert result.disclosures == ()
    assert second_client.download_calls == []
    assert result.diagnostics["sourceRequests"] == 1
    assert result.diagnostics["metadataRowsInRequestedWindow"] == 1
    assert result.diagnostics["alreadySeenInRequestedWindow"] == 1
    assert result.diagnostics["newCandidates"] == 0
    assert result.diagnostics["nonIssuerRowsSkipped"] == 0
    assert result.diagnostics["unsupportedTickerRowsSkipped"] == 0
    assert result.diagnostics["issuerDisclosuresProcessed"] == 0
    assert result.diagnostics["disclosuresNew"] == 0


def test_source_filters_records_outside_exact_requested_time_without_checkpointing_them(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    client = FakePoliteClient(_payload())
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 20, 30, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert result.disclosures == ()
    assert client.download_calls == []
    assert result.diagnostics["metadataRowsCollected"] == 1
    assert result.diagnostics["metadataRowsInRequestedWindow"] == 0
    assert result.diagnostics["alreadySeenInRequestedWindow"] == 0
    assert result.diagnostics["newCandidates"] == 0

    source.commit_checkpoint()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["seenIds"] == []
    assert checkpoint["latestAnnouncedAt"] is None


def test_source_skips_nonissuer_rows_without_checkpointing_them(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    payload = _payload()
    nonissuer_id = "20260821163757-Peng-PK-00059/BEI.PLP/08-2026_id-id"
    payload["ResultCount"] = 2
    payload["Replies"] = [
        {
            "pengumuman": {
                "Id2": nonissuer_id,
                "NoPengumuman": "Peng-PK-00059/BEI.PLP/08-2026",
                "TglPengumuman": "2026-08-22T20:10:00",
                "JudulPengumuman": "Pengumuman Bursa",
                "Kode_Emiten": "",
            },
            "attachments": [],
        },
        *payload["Replies"],
    ]
    client = FakePoliteClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert len(result.disclosures) == 1
    assert result.disclosures[0].ticker == "BBRI"
    assert result.diagnostics["metadataRowsInRequestedWindow"] == 2
    assert result.diagnostics["newCandidates"] == 2
    assert result.diagnostics["nonIssuerRowsSkipped"] == 1
    assert result.diagnostics["nonIssuerRowIds"] == [nonissuer_id]
    assert result.diagnostics["unsupportedTickerRowsSkipped"] == 0
    assert result.diagnostics["issuerDisclosuresProcessed"] == 1

    source.commit_checkpoint()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["seenIds"] == ["20260822201500-TEST-BBRI_id-id"]
    assert nonissuer_id not in checkpoint["seenIds"]


def test_source_skips_unsupported_ticker_rows_without_checkpointing_them(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    payload = _payload()
    unsupported_id = "20260821173008-21A/IIM-XILV/VIII/2026_id-id"
    payload["ResultCount"] = 2
    payload["Replies"] = [
        {
            "pengumuman": {
                "Id2": unsupported_id,
                "NoPengumuman": "21A/IIM-XILV/VIII/2026",
                "TglPengumuman": "2026-08-22T20:10:00",
                "JudulPengumuman": "Non-stock security announcement",
                "Kode_Emiten": "XILV-W",
            },
            "attachments": [],
        },
        *payload["Replies"],
    ]
    client = FakePoliteClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert len(result.disclosures) == 1
    assert result.disclosures[0].ticker == "BBRI"
    assert result.diagnostics["unsupportedTickerRowsSkipped"] == 1
    assert result.diagnostics["unsupportedTickerRowIds"] == [unsupported_id]
    assert result.diagnostics["issuerDisclosuresProcessed"] == 1

    source.commit_checkpoint()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["seenIds"] == ["20260822201500-TEST-BBRI_id-id"]
    assert unsupported_id not in checkpoint["seenIds"]


def test_source_uses_official_fallback_when_issuer_title_is_missing(tmp_path):
    payload = _payload()
    payload["Replies"][0]["pengumuman"]["JudulPengumuman"] = ""
    client = FakePoliteClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoint.json"),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.title == "Controlled fixture"
    assert disclosure.metadata["idxTitleSource"] == "PerihalPengumuman"
    assert result.diagnostics["titleFallbackRows"] == 1


def test_source_skips_issuer_with_404_attachment_without_checkpointing_it(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    payload = _payload()
    broken_id = payload["Replies"][0]["pengumuman"]["Id2"]
    payload["Replies"][0]["attachments"][0]["FullSavePath"] = (
        "https://www.idx.co.id/StaticData/NewsAndAnnouncement/missing.pdf"
    )

    valid = json.loads(json.dumps(payload["Replies"][0]))
    valid_id = "20260822202000-TEST-BMRI_id-id"
    valid["pengumuman"]["Id2"] = valid_id
    valid["pengumuman"]["TglPengumuman"] = "2026-08-22T20:20:00"
    valid["pengumuman"]["Kode_Emiten"] = "BMRI"
    valid["pengumuman"]["JudulPengumuman"] = "Second valid issuer fixture"
    valid["attachments"][0]["FullSavePath"] = (
        "https://www.idx.co.id/StaticData/NewsAndAnnouncement/valid.pdf"
    )
    payload["ResultCount"] = 2
    payload["Replies"].append(valid)

    class SelectiveMissingClient(FakePoliteClient):
        def download(self, url: str, destination: Path) -> int:
            if url.endswith("/missing.pdf"):
                self.request_count += 1
                self.download_calls.append(url)
                raise IdxResourceNotFoundError("IDX resource returned HTTP 404")
            return super().download(url, destination)

    client = SelectiveMissingClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert [item.ticker for item in result.disclosures] == ["BMRI"]
    assert result.diagnostics["unavailableAttachmentRowsSkipped"] == 1
    assert result.diagnostics["unavailableAttachmentRowIds"] == [broken_id]
    assert result.diagnostics["issuerDisclosuresProcessed"] == 1

    source.commit_checkpoint()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert valid_id in checkpoint["seenIds"]
    assert broken_id not in checkpoint["seenIds"]


def test_source_skips_valid_format_etf_ticker_without_checkpointing_it(tmp_path):
    checkpoint_path = tmp_path / "checkpoint.json"
    payload = _payload()
    etf_id = "20260821175507-1684/MAJORIS/VIII/2026_id-id"
    payload["ResultCount"] = 2
    payload["Replies"] = [
        {
            "pengumuman": {
                "Id2": etf_id,
                "NoPengumuman": "1684/MAJORIS/VIII/2026",
                "TglPengumuman": "2026-08-22T20:10:00",
                "JudulPengumuman": "Laporan Harian atas Nilai Aktiva Bersih dan Komposisi Portofolio",
                "Kode_Emiten": "XMIG",
                "EfekEmiten_ETF": True,
            },
            "attachments": [],
        },
        *payload["Replies"],
    ]
    client = FakePoliteClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),
    )

    assert [item.ticker for item in result.disclosures] == ["BBRI"]
    assert result.diagnostics["nonStockProductRowsSkipped"] == 1
    assert result.diagnostics["nonStockProductRowIds"] == [etf_id]

    source.commit_checkpoint()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["seenIds"] == ["20260822201500-TEST-BBRI_id-id"]
    assert etf_id not in checkpoint["seenIds"]
