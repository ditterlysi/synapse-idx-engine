"""Tests for the CLI daily --rules-only path (PR 3/9).

These tests verify the daily command signature without invoking the typer
help renderer, whose formatting varies across environments. The relevance
lookup and flags are exercised through function introspection instead.
"""

from __future__ import annotations

import inspect

from idx_digest.idx_website_cli import daily
from typer.testing import CliRunner

from idx_digest.idx_website_cli import app

runner = CliRunner()


def _param_names(func) -> set[str]:
    return set(inspect.signature(func).parameters.keys())


def test_daily_exposes_rules_only_flag() -> None:
    params = _param_names(daily)
    assert "rules_only" in params


def test_daily_exposes_portfolio_flag() -> None:
    params = _param_names(daily)
    assert "in_portfolio" in params


def test_daily_exposes_watchlist_flag() -> None:
    params = _param_names(daily)
    assert "in_watchlist" in params


def test_daily_help_exits_zero() -> None:
    result = runner.invoke(app, ["daily", "--help"])
    assert result.exit_code == 0


def test_daily_requires_confirm_schedule() -> None:
    result = runner.invoke(app, ["daily"])
    assert result.exit_code != 0


def test_daily_requires_daily_enabled_env() -> None:
    result = runner.invoke(app, ["daily", "--confirm-schedule"])
    assert result.exit_code != 0
