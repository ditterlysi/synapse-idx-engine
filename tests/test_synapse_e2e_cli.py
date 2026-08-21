from __future__ import annotations

import json
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
    assert result.exit_code != 0
    assert "--confirm-live-idx is required" in result.output


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


def test_e2e_runs_with_tightened_caps_and_no_schedule(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeRunner:
        def __init__(self, settings):
            captured["settings"] = settings

        def run_window(self, *, start_at, end_at):
            captured["start_at"] = start_at
            captured["end_at"] = end_at
            return SimpleNamespace(
                run_id="run-id",
                status="COMPLETE",
                coverage_committed=True,
                report={"status": "completed", "scrape_complete": True},
                publish=SimpleNamespace(
                    announcements_available=1,
                    announcements_created=1,
                    files_published=1,
                    files_downloaded=1,
                    files_extracted=1,
                    analyses_completed=1,
                    partial_disclosures=0,
                    errors=[],
                ),
                budget={
                    "source_requests": 3,
                    "attachments": 1,
                    "download_bytes": 1024,
                    "ai_documents": 1,
                },
            )

    monkeypatch.setattr(synapse_cli, "SynapsePipelineRunner", FakeRunner)
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

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["scheduleEnabled"] is False
    assert payload["coverageCommitted"] is True

    settings = captured["settings"]
    assert settings.synapse_daily_max_source_requests == 12
    assert settings.synapse_daily_max_attachments == 20
    assert settings.synapse_daily_max_download_bytes == 100_000_000
    assert settings.synapse_daily_max_ai_documents == 20
    assert settings.synapse_daily_max_run_seconds == 900
    assert settings.llm_concurrency == 2
    assert settings.llm_per_announcement_concurrency == 2
    assert settings.extraction_workers == 2
