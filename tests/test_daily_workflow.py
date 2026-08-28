from pathlib import Path


def test_daily_workflow_keeps_production_guardrails() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert 'cron: "7 20 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "vars.IDX_DAILY_ENABLED == 'true'" in workflow
    assert "SYNAPSE_DAILY_ENABLED: \"true\"" in workflow
    assert "SYNAPSE_DAILY_TRANSPORT: http" in workflow
    assert "SYNAPSE_DAILY_ALLOW_HISTORICAL_BACKFILL: \"false\"" in workflow
    assert "SYNAPSE_DAILY_ALLOW_TICKER_FANOUT: \"false\"" in workflow
    assert "tesseract-ocr" in workflow
    assert "tesseract-ocr-eng" in workflow
    assert "tesseract-ocr-ind" in workflow
    assert "set -o pipefail" in workflow
    assert "synapse-idx-website daily --confirm-schedule | tee idx-daily-report.json" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "contents: read" in workflow
    assert "Annotate collector failure" in workflow
    assert "::error title=IDX Daily Collector failed::" in workflow
    assert "Synapse Telegram health watcher" in workflow
    assert "issues: write" not in workflow
    assert "gh issue create" not in workflow

    forbidden = ("playwright install", "proxy rotation", "captcha solving")
    lowered = workflow.lower()
    assert all(token not in lowered for token in forbidden)


def test_daily_workflow_exposes_lightweight_observability_without_extra_idx_or_ai_calls() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert "Capture durable source state" in workflow
    assert "synapse-idx-website health > idx-daily-health.json" in workflow
    assert "idx-daily-health.json" in workflow
    assert "continue-on-error: true" in workflow
    assert "processingOk" in workflow
    assert "requestBudgetDeferred" in workflow
    assert "requestBudgetDeferredRowId" in workflow
    assert "checkpointSeenIdsBefore" in workflow
    assert "checkpointSeenIdsAfter" in workflow
    assert "latestAnnouncedAt" in workflow
    assert "issuerDisclosuresProcessed" in workflow
    assert "disclosuresCreated" in workflow
    assert "analysesCompleted" in workflow
    assert "partialDisclosures" in workflow
    assert "errorsCount" in workflow
    assert "AI fallback activation is emitted as a warning" in workflow

    # The observability snapshot is the CLI health command, which reads Synapse
    # source state only. It must not introduce another collector invocation.
    assert workflow.count("synapse-idx-website daily --confirm-schedule") == 1
