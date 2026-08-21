from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from idx_digest import synapse_cli


runner = CliRunner()


def _live_env(tmp_path) -> dict[str, str]:
    return {
        "DATA_DIR": str(tmp_path),
        "SYNAPSE_DAILY_ENABLED": "true",
        "SYNAPSE_INTERNAL_BASE_URL": "https://synapse.example",
        "SYNAPSE_INGESTION_SECRET": "test-secret",
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "SYNAPSE_DAILY_MAX_SOURCE_REQUESTS": "50",
        "SYNAPSE_DAILY_MAX_ATTACHMENTS": "100",
        "SYNAPSE_DAILY_MAX_DOWNLOAD_BYTES": "500000000",
        "SYNAPSE_DAILY_MAX_AI_DOCUMENTS": "100",
        "SYNAPSE_DAILY_MAX_RUN_SECONDS": "2700",
        "LLM_CONCURRENCY": "4",
        "LLM_PER_ANNOUNCEMENT_CONCURRENCY": "4",
        "EXTRACTION_WORKERS": "4",
    }


def test_e2e_requires_explicit_live_confirmation() -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "e2e",
            "--start",
            "2026-08-21T20:00:00+07:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
        ],
    )
    assert result.exit_code == 2


def test_e2e_rejects_naive_timestamp() -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "e2e",
            "--start",
            "2026-08-21T20:00:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
            "--confirm-live-idx",
        ],
    )
    assert result.exit_code != 0
    assert "must include an explicit timezone offset or Z" in result.output


def test_e2e_rejects_window_over_two_hours(tmp_path) -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "e2e",
            "--start",
            "2026-08-21T18:00:00+07:00",
            "--end",
            "2026-08-21T21:00:01+07:00",
            "--confirm-live-idx",
        ],
        env=_live_env(tmp_path),
    )
    assert result.exit_code != 0
    assert "2 hours or less" in result.output


def test_e2e_rejects_cross_jakarta_date_window(tmp_path) -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "e2e",
            "--start",
            "2026-08-21T23:30:00+07:00",
            "--end",
            "2026-08-22T00:30:00+07:00",
            "--confirm-live-idx",
        ],
        env=_live_env(tmp_path),
    )
    assert result.exit_code != 0
    assert "one Asia/Jakarta calendar date" in result.output


def test_e2e_blocks_automated_idx_website_collection(tmp_path, monkeypatch) -> None:
    invoked = False

    class ForbiddenRunner:
        def __init__(self, settings):
            nonlocal invoked
            invoked = True

    monkeypatch.setattr(synapse_cli, "SynapsePipelineRunner", ForbiddenRunner)
    result = runner.invoke(
        synapse_cli.app,
        [
            "e2e",
            "--start",
            "2026-08-21T20:00:00+07:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
            "--confirm-live-idx",
        ],
        env=_live_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "approved/licensed source integration" in result.output
    assert invoked is False


def test_tighten_e2e_settings_preserves_bounded_caps() -> None:
    settings = synapse_cli.Settings(
        synapse_daily_max_source_requests=50,
        synapse_daily_max_attachments=100,
        synapse_daily_max_download_bytes=500_000_000,
        synapse_daily_max_ai_documents=100,
        synapse_daily_max_run_seconds=2700,
        llm_concurrency=4,
        llm_per_announcement_concurrency=4,
        extraction_workers=4,
    )

    tightened = synapse_cli._tighten_e2e_settings(settings)
    assert tightened.synapse_daily_max_source_requests == 12
    assert tightened.synapse_daily_max_attachments == 20
    assert tightened.synapse_daily_max_download_bytes == 100_000_000
    assert tightened.synapse_daily_max_ai_documents == 20
    assert tightened.synapse_daily_max_run_seconds == 900
    assert tightened.llm_concurrency == 2
    assert tightened.llm_per_announcement_concurrency == 2
    assert tightened.extraction_workers == 2


def test_e2e_report_surfaces_metadata_root_cause() -> None:
    result = SimpleNamespace(
        run_id="run-id",
        status="PARTIAL",
        coverage_committed=False,
        report={
            "status": "partial",
            "scrape_complete": False,
            "scrape_error": "IDX metadata collection remained incomplete",
            "metadata_diagnostics": {
                "complete": False,
                "strategy": "collection-error",
                "ranges": [{"reason": "IDX returned HTTP 403; conservative run stopped"}],
            },
        },
        publish=SimpleNamespace(
            announcements_available=0,
            announcements_created=0,
            files_published=0,
            files_downloaded=0,
            files_extracted=0,
            analyses_completed=0,
            partial_disclosures=0,
            errors=[],
        ),
        budget={
            "source_requests": 2,
            "attachments": 0,
            "download_bytes": 0,
            "ai_documents": 0,
        },
    )

    payload = synapse_cli._build_e2e_report(result)
    assert payload["ok"] is False
    assert payload["scrapeError"] == "IDX metadata collection remained incomplete"
    assert payload["metadataDiagnostics"]["ranges"][0]["reason"].startswith("IDX returned HTTP 403")
    assert payload["scheduleEnabled"] is False
