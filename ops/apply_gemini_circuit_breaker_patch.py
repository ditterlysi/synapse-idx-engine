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
    source = Path("src/idx_digest/source_ingestion.py")
    tests = Path("tests/test_source_ingestion.py")

    replace_once(
        source,
        '''def _truncate(value: str, limit: int) -> str:\n    return value if len(value) <= limit else value[: limit - 1] + "…"\n\n\ndef _chunks''',
        '''def _truncate(value: str, limit: int) -> str:\n    return value if len(value) <= limit else value[: limit - 1] + "…"\n\n\ndef _is_ai_rate_limit_error(error: Exception) -> bool:\n    return getattr(error, "status_code", None) == 429\n\n\ndef _chunks''',
    )
    replace_once(
        source,
        '''    analyses_completed: int = 0\n    partial_disclosures: int = 0\n    errors: list[str] = field(default_factory=list)''',
        '''    analyses_completed: int = 0\n    partial_disclosures: int = 0\n    ai_rate_limit_deferred: int = 0\n    errors: list[str] = field(default_factory=list)''',
    )
    replace_once(
        source,
        '''                if any(disclosure_statuses.get(item.external_id) != "READY" for item in local_items):\n                    summarizer = self.summarizer_factory(runtime_settings)\n\n                for disclosure in local_items:''',
        '''                if any(disclosure_statuses.get(item.external_id) != "READY" for item in local_items):\n                    summarizer = self.summarizer_factory(runtime_settings)\n\n                ai_rate_limited = False\n                for disclosure in local_items:''',
    )
    replace_once(
        source,
        '''                    if disclosure_statuses.get(disclosure.external_id) == "READY":\n                        stats.disclosures_skipped_ready += 1\n                        continue\n                    try:''',
        '''                    if disclosure_statuses.get(disclosure.external_id) == "READY":\n                        stats.disclosures_skipped_ready += 1\n                        continue\n                    if ai_rate_limited:\n                        stats.partial_disclosures += 1\n                        stats.ai_rate_limit_deferred += 1\n                        stats.errors.append(\n                            f"{disclosure.external_id}: AI_RATE_LIMITED: deferred after earlier provider 429"\n                        )\n                        try:\n                            client.update_processing_status(\n                                disclosure_id,\n                                UpdateProcessingStatusRequest(processing_status="PARTIAL"),\n                            )\n                        except Exception as status_exc:\n                            stats.errors.append(\n                                f"{disclosure.external_id}: could not mark PARTIAL: "\n                                f"{_truncate(str(status_exc), 400)}"\n                            )\n                        continue\n                    try:''',
    )
    replace_once(
        source,
        '''                    except Exception as exc:\n                        stats.partial_disclosures += 1\n                        stats.errors.append(''',
        '''                    except Exception as exc:\n                        if _is_ai_rate_limit_error(exc):\n                            ai_rate_limited = True\n                        stats.partial_disclosures += 1\n                        stats.errors.append(''',
    )
    replace_once(
        source,
        '''                    if not processing_ok:\n                        error_code = "SOURCE_PROCESSING_PARTIAL"\n                        error_message = _truncate("; ".join(stats.errors[:4]), 1000)''',
        '''                    if not processing_ok:\n                        error_code = "AI_RATE_LIMITED" if ai_rate_limited else "SOURCE_PROCESSING_PARTIAL"\n                        error_message = _truncate("; ".join(stats.errors[:4]), 1000)''',
    )

    replace_once(
        tests,
        '''def _runner(settings, source, *, allow_coverage_commit=False):\n    return SourceIngestionRunner(\n        settings,\n        source,\n        client_factory=FakeClient,\n        summarizer_factory=FakeSummarizer,''',
        '''def _runner(settings, source, *, allow_coverage_commit=False, summarizer_factory=FakeSummarizer):\n    return SourceIngestionRunner(\n        settings,\n        source,\n        client_factory=FakeClient,\n        summarizer_factory=summarizer_factory,''',
    )
    append_once(
        tests,
        "test_ai_rate_limit_trips_run_circuit_breaker",
        '''class FakeRateLimitError(ValueError):\n    status_code = 429\n\n\nclass FakeRateLimitSummarizer(FakeSummarizer):\n    announcement_calls = 0\n\n    def summarize_announcement(self, *, announcement, documents, stream=False):\n        type(self).announcement_calls += 1\n        raise FakeRateLimitError("Gemini rate limited after provider cooldown")\n\n\ndef test_ai_rate_limit_trips_run_circuit_breaker(tmp_path) -> None:\n    settings = _settings(tmp_path)\n    base = _window(tmp_path)\n    second_path = tmp_path / "disclosure-2.txt"\n    second_path.write_text("Second synthetic disclosure body", encoding="utf-8")\n    second = SourceDisclosure(\n        external_id="manual-example-2",\n        ticker="BMRI",\n        announced_at=datetime.fromisoformat("2026-08-21T10:30:00+07:00"),\n        title="Second disclosure",\n        source_url="https://example.com/disclosures/manual-example-2",\n        attachments=(\n            SourceAttachment(\n                filename="disclosure-2.txt",\n                local_path=second_path,\n                content_type="text/plain",\n            ),\n        ),\n    )\n    result_window = SourceWindowResult(\n        source_id=base.source_id,\n        requested_start=base.requested_start,\n        requested_end=base.requested_end,\n        disclosures=(base.disclosures[0], second),\n        diagnostics=base.diagnostics,\n    )\n    source = FakeSource(result_window)\n    FakeRateLimitSummarizer.announcement_calls = 0\n\n    result = _runner(\n        settings,\n        source,\n        summarizer_factory=FakeRateLimitSummarizer,\n    ).run_window(\n        start_at=source.result.requested_start,\n        end_at=source.result.requested_end,\n    )\n    client = FakeClient.latest\n    assert client is not None\n\n    assert result.processing_ok is False\n    assert result.publish.partial_disclosures == 2\n    assert result.publish.ai_rate_limit_deferred == 1\n    assert result.publish.files_extracted == 1\n    assert FakeRateLimitSummarizer.announcement_calls == 1\n    assert client.final_run_status == "PARTIAL"\n    assert client.final_error_code == "AI_RATE_LIMITED"''',
    )


if __name__ == "__main__":
    main()
