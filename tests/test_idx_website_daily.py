from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click import unstyle
from typer.testing import CliRunner

from idx_digest.config import Settings
from idx_digest.idx_website_cli import (
    COLLECT_MAX_WINDOW,
    DAILY_FALLBACK_LOOKBACK,
    _collector_runtime_settings,
    _daily_window,
    app,
)
from idx_digest.sources.idx_website import IdxWebsiteCheckpoint


runner = CliRunner()


def _now() -> datetime:
    return datetime(2026, 8, 22, 3, 0, tzinfo=ZoneInfo("Asia/Jakarta"))


def test_daily_window_uses_30_hour_fallback_without_checkpoint() -> None:
    settings = Settings(_env_file=None)
    start_at, end_at = _daily_window(settings, IdxWebsiteCheckpoint(), now=_now())
    assert end_at == _now()
    assert end_at - start_at == DAILY_FALLBACK_LOOKBACK


def test_daily_window_expands_for_stale_checkpoint_but_caps_at_48_hours() -> None:
    settings = Settings(_env_file=None, idx_incremental_overlap_days=1.0)
    checkpoint = IdxWebsiteCheckpoint(latest_announced_at=(_now() - timedelta(hours=40)).isoformat())
    start_at, end_at = _daily_window(settings, checkpoint, now=_now())
    assert end_at - start_at == COLLECT_MAX_WINDOW


def test_daily_window_keeps_conservative_fallback_for_recent_checkpoint() -> None:
    settings = Settings(_env_file=None, idx_incremental_overlap_days=1.0)
    checkpoint = IdxWebsiteCheckpoint(latest_announced_at=(_now() - timedelta(hours=1)).isoformat())
    start_at, end_at = _daily_window(settings, checkpoint, now=_now())
    assert end_at - start_at == DAILY_FALLBACK_LOOKBACK


def test_daily_window_rejects_checkpoint_far_in_the_future() -> None:
    settings = Settings(_env_file=None)
    checkpoint = IdxWebsiteCheckpoint(latest_announced_at=(_now() + timedelta(minutes=10)).isoformat())
    with pytest.raises(ValueError, match="ahead of the current time"):
        _daily_window(settings, checkpoint, now=_now())


def test_daily_runtime_uses_production_budgets_but_manual_keeps_e2e_caps() -> None:
    settings = Settings(
        _env_file=None,
        synapse_daily_max_source_requests=50,
        synapse_daily_max_attachments=100,
        synapse_daily_max_ai_documents=100,
    )

    daily = _collector_runtime_settings(settings, run_mode="DAILY")
    manual = _collector_runtime_settings(settings, run_mode="MANUAL_BACKFILL")

    assert daily.synapse_daily_max_source_requests == 50
    assert daily.synapse_daily_max_attachments == 100
    assert daily.synapse_daily_max_ai_documents == 100
    assert manual.synapse_daily_max_source_requests == 12
    assert manual.synapse_daily_max_attachments == 20
    assert manual.synapse_daily_max_ai_documents == 20
    assert daily.idx_transport == "http"
    assert daily.synapse_daily_transport == "http"
    assert daily.synapse_daily_request_delay_seconds >= 10.0
    assert daily.synapse_daily_request_jitter_seconds >= 2.0
    assert daily.synapse_daily_allow_historical_backfill is False
    assert daily.synapse_daily_allow_ticker_fanout is False


def test_daily_command_requires_explicit_schedule_confirmation() -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code != 0
    assert "--confirm-schedule is required" in unstyle(result.output)


def test_daily_command_refuses_when_kill_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPSE_DAILY_ENABLED", "false")
    result = runner.invoke(app, ["daily", "--confirm-schedule"])
    assert result.exit_code != 0
    assert "SYNAPSE_DAILY_ENABLED=true is required" in unstyle(result.output)


def test_recovery_command_keeps_read_only_default_and_rejects_snapshot_in_live_mode(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "runType": "RETRY",
                "records": [
                    {
                        "disclosureId": "11111111-1111-4111-8111-111111111111",
                        "expectedStatus": "PARTIAL",
                        "expectedUpdatedAt": "2026-09-10T12:00:00Z",
                        "expectedExternalId": "idx-web-test",
                        "ticker": "TEST",
                        "bucket": "C",
                        "declaredAttachmentCount": 0,
                        "expectedAttachmentHashes": [],
                        "intendedRecoveryAction": "resume",
                        "recoveryAllowed": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("{}", encoding="utf-8")

    default = runner.invoke(app, ["recover-pending", "--manifest", str(manifest)])
    assert default.exit_code != 0

    live_with_snapshot = runner.invoke(
        app,
        [
            "recover-pending",
            "--manifest",
            str(manifest),
            "--snapshot",
            str(snapshot),
            "--execute-live",
        ],
    )
    assert live_with_snapshot.exit_code != 0

    live_without_audit_phase = runner.invoke(
        app,
        [
            "recover-pending",
            "--manifest",
            str(manifest),
            "--execute-live",
            "--max-records",
            "1",
            "--max-source-requests",
            "12",
            "--max-attachments",
            "20",
            "--max-ai-documents",
            "20",
        ],
    )
    assert live_without_audit_phase.exit_code != 0
    assert "--execute-live requires an explicit --audit-phase" in live_without_audit_phase.output
