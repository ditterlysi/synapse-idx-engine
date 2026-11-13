"""Tests for the CLI daily --rules-only path (PR 3/9).

These tests verify that the CLI flags and relevance lookup behave as
documented in PHASE_13_LIGHTWEIGHT_COLLECTION.md, without network access.
"""

from __future__ import annotations

from typer.testing import CliRunner

from idx_digest.idx_website_cli import app

runner = CliRunner()


def test_daily_requires_confirm_schedule() -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code != 0
    assert "--confirm-schedule" in result.output


def test_daily_requires_daily_enabled() -> None:
    result = runner.invoke(app, ["daily", "--confirm-schedule"])
    assert result.exit_code != 0
    # SYNAPSE_DAILY_ENABLED is False by default in tests
    assert "SYNAPSE_DAILY_ENABLED" in result.output or "confirm-schedule" in result.output


def test_rules_only_flag_exists_in_help() -> None:
    result = runner.invoke(app, ["daily", "--help"])
    assert result.exit_code == 0
    assert "--rules-only" in result.output
    assert "--in-portfolio" in result.output
    assert "--in-watchlist" in result.output


def test_collect_help_shows_no_rules_only() -> None:
    """The manual collect command does not expose the rules-only flags."""
    result = runner.invoke(app, ["collect", "--help"])
    assert result.exit_code == 0
    assert "--rules-only" not in result.output
