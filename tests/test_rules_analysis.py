from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from idx_digest.rules_analysis import (
    RULES_MODEL,
    RULES_PROVIDER,
    RULES_PROMPT_VERSION,
    classify_and_build,
)


def _announced() -> datetime:
    return datetime(2026, 11, 13, 10, 0, 0, tzinfo=ZoneInfo("Asia/Jakarta"))


def test_portfolio_financial_report_is_high() -> None:
    result = classify_and_build(
        ticker="ADES",
        title="Laporan Keuangan Tahunan 2026",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090000-test",
        in_portfolio=True,
    )
    assert result["category"] == "FINANCIAL_REPORT"
    assert result["materiality"] == "HIGH"
    assert result["summary"]["primaryCategory"] == "FINANCIAL_REPORT"
    assert result["summary"]["materiality"] == "HIGH"
    assert result["summary"]["impact"] == "UNCLEAR"
    assert result["provider"] == RULES_PROVIDER
    assert result["model"] == RULES_MODEL
    assert result["prompt_version"] == RULES_PROMPT_VERSION


def test_watchlist_dividend_is_medium() -> None:
    result = classify_and_build(
        ticker="BBRI",
        title="Pembagian Dividen Interim",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090001-test",
        in_watchlist=True,
    )
    assert result["category"] == "DIVIDEND"
    assert result["materiality"] == "MEDIUM"
    assert result["summary"]["whyItMatters"].startswith("A stock on your watchlist")


def test_routine_stays_routine() -> None:
    result = classify_and_build(
        ticker="UNVR",
        title="Pencatatan Saham",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090002-test",
    )
    assert result["category"] == "LISTING"
    assert result["materiality"] == "ROUTINE"


def test_material_facts_reference_the_title() -> None:
    result = classify_and_build(
        ticker="UNVR",
        title="Laporan Keuangan Tahunan 2026",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090003-test",
        in_portfolio=True,
    )
    facts = result["summary"]["materialFacts"]
    assert len(facts) == 1
    assert facts[0]["claimType"] == "EXPLICIT_FACT"
    assert "Laporan Keuangan Tahunan 2026" in facts[0]["text"]


def test_limitation_is_stated() -> None:
    result = classify_and_build(
        ticker="UNVR",
        title="RUPSLB",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090004-test",
    )
    assert any("was not read" in line for line in result["summary"]["limitations"])


def test_input_hash_is_stable() -> None:
    kwargs = dict(
        ticker="ADES",
        title="Laporan Keuangan Tahunan 2026",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090005-test",
        in_portfolio=True,
    )
    first = classify_and_build(**kwargs)
    second = classify_and_build(**kwargs)
    assert first["input_hash"] == second["input_hash"]
    assert len(first["input_hash"]) == 64


def test_tags_include_primary_category() -> None:
    result = classify_and_build(
        ticker="ADES",
        title="Laporan Keuangan Tahunan 2026",
        announced_at=_announced(),
        announcement_id="idx-web-20261113090006-test",
    )
    tags = result["summary"]["tags"]
    assert result["summary"]["primaryCategory"] in tags
