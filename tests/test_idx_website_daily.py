from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from idx_digest.config import Settings
from idx_digest.idx_website_cli import COLLECT_MAX_WINDOW, DAILY_FALLBACK_LOOKBACK, _daily_window, app
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


def test_daily_command_requires_explicit_schedule_confirmation() -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code != 0
    assert "--confirm-schedule is required" in result.output


def test_daily_command_refuses_when_kill_switch_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNAPSE_DAILY_ENABLED", "false")
    result = runner.invoke(app, ["daily", "--confirm-schedule"])
    assert result.exit_code != 0
    assert "SYNAPSE_DAILY_ENABLED=true is required" in result.output
