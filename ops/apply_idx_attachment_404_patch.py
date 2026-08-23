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
    polite = Path("src/idx_digest/idx_polite_http.py")
    source = Path("src/idx_digest/sources/idx_website.py")
    polite_test = Path("tests/test_idx_polite_http.py")
    source_test = Path("tests/test_idx_website_source.py")

    replace_once(
        polite,
        '''class IdxUnexpectedResponseError(IdxPoliteHttpError):\n    """Raised when the public endpoint no longer matches the expected response shape."""\n\n\nclass PoliteFetchClient:''',
        '''class IdxUnexpectedResponseError(IdxPoliteHttpError):\n    """Raised when the public endpoint no longer matches the expected response shape."""\n\n\nclass IdxResourceNotFoundError(IdxPoliteHttpError):\n    """Raised when an official IDX resource referenced by metadata returns HTTP 404."""\n\n\nclass PoliteFetchClient:''',
    )
    replace_once(
        polite,
        '''            if status in {401, 407}:\n                raise IdxAccessProtectionError(f"IDX access protection returned HTTP {status}; collector stopped")\n            if status >= 500:''',
        '''            if status in {401, 407}:\n                raise IdxAccessProtectionError(f"IDX access protection returned HTTP {status}; collector stopped")\n            if status == 404:\n                raise IdxResourceNotFoundError("IDX resource returned HTTP 404")\n            if status >= 500:''',
    )

    replace_once(
        source,
        '''from ..idx_polite_http import CURRENT_IDX_BASE_URL, OFFICIAL_IDX_HOSTS, PoliteFetchClient''',
        '''from ..idx_polite_http import (\n    CURRENT_IDX_BASE_URL,\n    OFFICIAL_IDX_HOSTS,\n    IdxResourceNotFoundError,\n    PoliteFetchClient,\n)''',
    )
    replace_once(
        source,
        '''        nonissuer_row_ids: list[str] = []\n        unsupported_ticker_row_ids: list[str] = []\n        attachment_downloads = 0''',
        '''        nonissuer_row_ids: list[str] = []\n        unsupported_ticker_row_ids: list[str] = []\n        unavailable_attachment_row_ids: list[str] = []\n        attachment_downloads = 0''',
    )
    replace_once(
        source,
        '''            attachments: list[SourceAttachment] = []\n            for attachment_raw in attachments_raw:\n                if not isinstance(attachment_raw, dict) or not attachment_raw.get("FullSavePath"):\n                    continue\n                attachment, cache_hit = self._stage_attachment(attachment_raw)\n                attachments.append(attachment)\n                if cache_hit:\n                    attachment_cache_hits += 1\n                else:\n                    attachment_downloads += 1\n\n            disclosures.append(''',
        '''            attachments: list[SourceAttachment] = []\n            attachment_unavailable = False\n            for attachment_raw in attachments_raw:\n                if not isinstance(attachment_raw, dict) or not attachment_raw.get("FullSavePath"):\n                    continue\n                try:\n                    attachment, cache_hit = self._stage_attachment(attachment_raw)\n                except IdxResourceNotFoundError:\n                    attachment_unavailable = True\n                    break\n                attachments.append(attachment)\n                if cache_hit:\n                    attachment_cache_hits += 1\n                else:\n                    attachment_downloads += 1\n\n            if attachment_unavailable:\n                if len(unavailable_attachment_row_ids) < 20:\n                    unavailable_attachment_row_ids.append(raw_id)\n                continue\n\n            disclosures.append(''',
    )
    replace_once(
        source,
        '''            "unsupportedTickerRowsSkipped": len(unsupported_ticker_row_ids),\n            "unsupportedTickerRowIds": unsupported_ticker_row_ids,\n            "issuerDisclosuresProcessed": len(disclosures),''',
        '''            "unsupportedTickerRowsSkipped": len(unsupported_ticker_row_ids),\n            "unsupportedTickerRowIds": unsupported_ticker_row_ids,\n            "unavailableAttachmentRowsSkipped": len(unavailable_attachment_row_ids),\n            "unavailableAttachmentRowIds": unavailable_attachment_row_ids,\n            "issuerDisclosuresProcessed": len(disclosures),''',
    )

    replace_once(
        polite_test,
        '''from idx_digest.idx_polite_http import (\n    IdxAccessProtectionError,\n    PoliteFetchClient,\n)''',
        '''from idx_digest.idx_polite_http import (\n    IdxAccessProtectionError,\n    IdxResourceNotFoundError,\n    PoliteFetchClient,\n)''',
    )
    append_once(
        polite_test,
        "test_polite_client_maps_404_to_resource_not_found",
        '''def test_polite_client_maps_404_to_resource_not_found():\n    calls = 0\n\n    def handler(request: httpx.Request) -> httpx.Response:\n        nonlocal calls\n        calls += 1\n        return httpx.Response(404, request=request)\n\n    with _client(handler) as client:\n        with pytest.raises(IdxResourceNotFoundError, match="404"):\n            client.download(\n                "https://www.idx.id/StaticData/NewsAndAnnouncement/missing.pdf",\n                Path("unused.pdf"),\n            )\n\n    assert calls == 1''',
    )
    replace_once(
        polite_test,
        '''import httpx\nimport pytest''',
        '''from pathlib import Path\n\nimport httpx\nimport pytest''',
    )

    replace_once(
        source_test,
        '''import pytest\n\nfrom idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource, IdxWebsiteSourceError''',
        '''import pytest\n\nfrom idx_digest.idx_polite_http import IdxResourceNotFoundError\nfrom idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource, IdxWebsiteSourceError''',
    )
    append_once(
        source_test,
        "test_source_skips_issuer_with_404_attachment_without_checkpointing_it",
        '''def test_source_skips_issuer_with_404_attachment_without_checkpointing_it(tmp_path):\n    checkpoint_path = tmp_path / "checkpoint.json"\n    payload = _payload()\n    broken_id = payload["Replies"][0]["pengumuman"]["Id2"]\n    payload["Replies"][0]["attachments"][0]["FullSavePath"] = (\n        "https://www.idx.co.id/StaticData/NewsAndAnnouncement/missing.pdf"\n    )\n\n    valid = json.loads(json.dumps(payload["Replies"][0]))\n    valid_id = "20260822202000-TEST-BMRI_id-id"\n    valid["pengumuman"]["Id2"] = valid_id\n    valid["pengumuman"]["TglPengumuman"] = "2026-08-22T20:20:00"\n    valid["pengumuman"]["Kode_Emiten"] = "BMRI"\n    valid["pengumuman"]["JudulPengumuman"] = "Second valid issuer fixture"\n    valid["attachments"][0]["FullSavePath"] = (\n        "https://www.idx.co.id/StaticData/NewsAndAnnouncement/valid.pdf"\n    )\n    payload["ResultCount"] = 2\n    payload["Replies"].append(valid)\n\n    class SelectiveMissingClient(FakePoliteClient):\n        def download(self, url: str, destination: Path) -> int:\n            if url.endswith("/missing.pdf"):\n                self.request_count += 1\n                self.download_calls.append(url)\n                raise IdxResourceNotFoundError("IDX resource returned HTTP 404")\n            return super().download(url, destination)\n\n    client = SelectiveMissingClient(payload)\n    source = IdxWebsiteSource(\n        client,\n        checkpoint_store=FileCheckpointStore(checkpoint_path),\n        staging_dir=tmp_path / "cache",\n    )\n\n    result = source.collect_window(\n        start_at=datetime(2026, 8, 22, 19, 0, tzinfo=JAKARTA),\n        end_at=datetime(2026, 8, 22, 21, 0, tzinfo=JAKARTA),\n    )\n\n    assert [item.ticker for item in result.disclosures] == ["BMRI"]\n    assert result.diagnostics["unavailableAttachmentRowsSkipped"] == 1\n    assert result.diagnostics["unavailableAttachmentRowIds"] == [broken_id]\n    assert result.diagnostics["issuerDisclosuresProcessed"] == 1\n\n    source.commit_checkpoint()\n    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))\n    assert valid_id in checkpoint["seenIds"]\n    assert broken_id not in checkpoint["seenIds"]''',
    )


if __name__ == "__main__":
    main()
