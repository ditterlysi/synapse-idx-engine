"""Tests for the CLI daily --rules-only path (PR 3/9).

These tests verify that the CLI flags exist and that relevance lookup
behaves as documented in PHASE_13_LIGHTWEIGHT_COLLECTION.md, without
network access.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from idx_digest.idx_website_cli import app

runner = CliRunner()


def test_daily_help_contains_rules_only_flag() -> None:
    result = runner.invoke(app, ["daily", "--help"])
    assert result.exit_code == 0
    assert "rules-only" in result.output.replace("\n", " ")


def test_daily_help_contains_portfolio_flag() -> None:
    result = runner.invoke(app, ["daily", "--help"])
    assert result.exit_code == 0
    assert "in-portfolio" in result.output.replace("\n", " ")


def test_daily_help_contains_watchlist_flag() -> None:
    result = runner.invoke(app, ["daily", "--help"])
    assert result.exit_code == 0
    assert "in-watchlist" in result.output.replace("\n", " ")


def test_daily_requires_confirm_schedule() -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code != 0


def test_daily_requires_daily_enabled_env() -> None:
    result = runner.invoke(app, ["daily", "--confirm-schedule"])
    assert result.exit_code != 0


def test_collect_help_does_not_have_rules_only() -> None:
    result = runner.invoke(app, ["collect", "--help"])
    assert result.exit_code == 0
    assert "rules-only" not in result.output.replace("\n", " ")
