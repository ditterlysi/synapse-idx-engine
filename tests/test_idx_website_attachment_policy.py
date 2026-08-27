from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource


JAKARTA = ZoneInfo("Asia/Jakarta")


def _attachment(filename: str, *, is_attachment: bool = True) -> dict[str, object]:
    return {
        "PDFFilename": filename,
        "OriginalFilename": filename,
        "FullSavePath": f"https://www.idx.co.id/StaticData/NewsAndAnnouncement/{filename}",
        "IsAttachment": is_attachment,
    }


class FakeClient:
    base_url = "https://www.idx.co.id"

    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.request_count = 0
        self.downloaded_bytes = 0
        self.download_calls: list[str] = []

    def get_json(self, path: str, *, params: dict[str, object]) -> dict[str, object]:
        assert path == "/primary/ListedCompany/GetAnnouncement"
        self.request_count += 1
        return self.payload

    def download(self, url: str, destination: Path) -> int:
        self.request_count += 1
        self.download_calls.append(url)
        body = b"controlled attachment fixture"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(body)
        self.downloaded_bytes += len(body)
        return len(body)


def test_financial_disclosure_stages_only_primary_statement_sources(tmp_path: Path) -> None:
    raw_id = "20260827094727-003/AV/VIII/2026-CSC_id-id"
    payload = {
        "ResultCount": 1,
        "Replies": [
            {
                "pengumuman": {
                    "Id2": raw_id,
                    "NoPengumuman": "003/AV/VIII/2026-CSC",
                    "TglPengumuman": "2026-08-27T09:47:27",
                    "JudulPengumuman": "Penyampaian Laporan Keuangan Interim Yang Ditelaah Secara Terbatas",
                    "JenisPengumuman": "STOCK",
                    "Kode_Emiten": "ARTA",
                    "CreatedDate": "2026-08-27T09:48:00",
                    "Form_Id": "10000",
                },
                "attachments": [
                    _attachment("FinancialStatement-2026.xlsx"),
                    _attachment("LK ARTA 30 Juni 2026.pdf", is_attachment=False),
                    _attachment("FinancialStatement-2026.pdf"),
                    _attachment("Checklist LK ARTA.pdf"),
                    _attachment("Pengungkapan LK ARTA.pdf"),
                    _attachment("ARTA-XBRL.zip"),
                    _attachment("supporting-note.pdf"),
                ],
            }
        ],
    }
    client = FakeClient(payload)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoint.json"),
        staging_dir=tmp_path / "cache",
    )

    result = source.collect_window(
        start_at=datetime(2026, 8, 27, 9, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 27, 10, 0, tzinfo=JAKARTA),
    )

    assert len(result.disclosures) == 1
    disclosure = result.disclosures[0]
    assert disclosure.ticker == "ARTA"
    assert [item.filename for item in disclosure.attachments] == [
        "FinancialStatement-2026.xlsx",
        "LK ARTA 30 Juni 2026.pdf",
    ]
    assert len(client.download_calls) == 2
    assert not any(url.endswith(".zip") for url in client.download_calls)
    assert result.diagnostics["attachmentsConsidered"] == 7
    assert result.diagnostics["attachmentsSelected"] == 2
    assert result.diagnostics["attachmentsSkippedByPolicy"] == 5
    assert result.diagnostics["attachmentDownloads"] == 2
    assert disclosure.metadata["idxAttachmentCountOriginal"] == 7
    assert disclosure.metadata["idxAttachmentCountSelected"] == 2
