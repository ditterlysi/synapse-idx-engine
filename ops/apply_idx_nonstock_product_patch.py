from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected patch context not found in {path}: {old[:120]!r}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(path: Path, marker: str, block: str) -> None:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n\n" + block.strip() + "\n", encoding="utf-8")


def main() -> None:
    source = Path("src/idx_digest/sources/idx_website.py")
    source_test = Path("tests/test_idx_website_source.py")

    replace_once(
        source,
        '''CHECKPOINT_SCHEMA = "synapse-idx-website-checkpoint-v1"\nMAX_WINDOW = timedelta(hours=48)''',
        '''CHECKPOINT_SCHEMA = "synapse-idx-website-checkpoint-v1"\nMAX_WINDOW = timedelta(hours=48)\nNON_STOCK_PRODUCT_FLAGS = (\n    "EfekEmiten_ETF",\n    "EfekEmiten_DIRE",\n    "EfekEmiten_DINFRA",\n    "EfekEmiten_EBA",\n    "EfekEmiten_SPEI",\n)''',
    )
    replace_once(
        source,
        '''        unsupported_ticker_row_ids: list[str] = []\n        unavailable_attachment_row_ids: list[str] = []''',
        '''        unsupported_ticker_row_ids: list[str] = []\n        nonstock_product_row_ids: list[str] = []\n        unavailable_attachment_row_ids: list[str] = []''',
    )
    replace_once(
        source,
        '''            if re.fullmatch(r"[A-Z0-9.]{1,10}", ticker) is None:\n                if len(unsupported_ticker_row_ids) < 20:\n                    unsupported_ticker_row_ids.append(raw_id)\n                continue\n\n            title = str(announcement.get("JudulPengumuman") or "").strip()''',
        '''            if re.fullmatch(r"[A-Z0-9.]{1,10}", ticker) is None:\n                if len(unsupported_ticker_row_ids) < 20:\n                    unsupported_ticker_row_ids.append(raw_id)\n                continue\n            if any(bool(announcement.get(flag)) for flag in NON_STOCK_PRODUCT_FLAGS):\n                if len(nonstock_product_row_ids) < 20:\n                    nonstock_product_row_ids.append(raw_id)\n                continue\n\n            title = str(announcement.get("JudulPengumuman") or "").strip()''',
    )
    replace_once(
        source,
        '''            "unsupportedTickerRowsSkipped": len(unsupported_ticker_row_ids),\n            "unsupportedTickerRowIds": unsupported_ticker_row_ids,\n            "unavailableAttachmentRowsSkipped": len(unavailable_attachment_row_ids),''',
        '''            "unsupportedTickerRowsSkipped": len(unsupported_ticker_row_ids),\n            "unsupportedTickerRowIds": unsupported_ticker_row_ids,\n            "nonStockProductRowsSkipped": len(nonstock_product_row_ids),\n            "nonStockProductRowIds": nonstock_product_row_ids,\n            "unavailableAttachmentRowsSkipped": len(unavailable_attachment_row_ids),''',
    )
    append_once(
        source_test,
        "test_source_skips_valid_format_etf_ticker_without_checkpointing_it",
        '''def test_source_skips_valid_format_etf_ticker_without_checkpointing_it(tmp_path):\n    checkpoint_path = tmp_path / "checkpoint.json"\n    payload = _payload()\n    etf_id = "20260821175507-1684/MAJORIS/VIII/2026_id-id"\n    payload["ResultCount"] = 2\n    payload["Replies"] = [\n        {\n            "pengumuman": {\n                "Id2": etf_id,\n                "NoPengumuman": "1684/MAJORIS/VIII/2026",\n                "TglPengumuman": "2026-08-22T20:10:00",\n                "JudulPengumuman": "Laporan Harian atas Nilai Aktiva Bersih dan Komposisi Portofolio",\n                "Kode_Emiten": "XMIG",\n                "EfekEmiten_ETF": True,\n            },\n            "attachments": [],\n        },\n        *payload["Replies"],\n    ]\n    client = FakePoliteClient(payload)\n    source = IdxWebsiteSource(\n        client,\n        checkpoint_store=FileCheckpointStore(checkpoint_path),\n        staging_dir=tmp_path / "cache",\n    )\n\n    result = source.collect_window(\n        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),\n        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),\n    )\n\n    assert [item.ticker for item in result.disclosures] == ["BBRI"]\n    assert result.diagnostics["nonStockProductRowsSkipped"] == 1\n    assert result.diagnostics["nonStockProductRowIds"] == [etf_id]\n\n    source.commit_checkpoint()\n    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))\n    assert checkpoint["seenIds"] == ["20260822201500-TEST-BBRI_id-id"]\n    assert etf_id not in checkpoint["seenIds"]''',
    )


if __name__ == "__main__":
    main()
