from pathlib import Path


def test_daily_workflow_keeps_production_guardrails() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert 'cron: "0 20 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "vars.IDX_DAILY_ENABLED == 'true'" in workflow
    assert "SYNAPSE_DAILY_ENABLED: \"true\"" in workflow
    assert "SYNAPSE_DAILY_TRANSPORT: http" in workflow
    assert "SYNAPSE_DAILY_ALLOW_HISTORICAL_BACKFILL: \"false\"" in workflow
    assert "SYNAPSE_DAILY_ALLOW_TICKER_FANOUT: \"false\"" in workflow
    assert "set -o pipefail" in workflow
    assert "synapse-idx-website daily --confirm-schedule | tee idx-daily-report.json" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "issues: write" in workflow
    assert "gh issue create" in workflow

    forbidden = ("playwright install", "proxy rotation", "captcha solving")
    lowered = workflow.lower()
    assert all(token not in lowered for token in forbidden)
