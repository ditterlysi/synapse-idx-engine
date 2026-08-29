from pathlib import Path


def test_daily_workflow_keeps_production_guardrails() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert 'cron: "7 20 * * *"' in workflow
    assert 'cron: "37 20 * * *"' in workflow
    assert "workflow_dispatch:" in workflow
    assert "vars.IDX_DAILY_ENABLED == 'true'" in workflow
    assert "needs.schedule-preflight.outputs.should-run == 'true'" in workflow
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
    assert "actions: read" in workflow
    assert "contents: read" in workflow
    assert "Annotate collector failure" in workflow
    assert "::error title=IDX Daily Collector failed::" in workflow
    assert "Synapse Telegram health watcher" in workflow
    assert "issues: write" not in workflow
    assert "gh issue create" not in workflow

    forbidden = ("playwright install", "proxy rotation", "captcha solving")
    lowered = workflow.lower()
    assert all(token not in lowered for token in forbidden)


def test_daily_workflow_has_fail_open_duplicate_guard_for_backup_schedule() -> None:
    workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")

    assert "Schedule delivery guard" in workflow
    assert "Suppress duplicate scheduled delivery" in workflow
    assert "event=schedule&per_page=20" in workflow
    assert "timedelta(hours=2)" in workflow
    assert "CURRENT_RUN_ID" in workflow
    assert "Schedule guard API unavailable; failing open" in workflow
    assert "No recent scheduled collector delivery found" in workflow
    assert "suppressing duplicate" in workflow

    # Redundant cron delivery must not create a second collector invocation in
    # the workflow definition. The preflight only decides whether the one
    # collector job is allowed to run.
    assert workflow.count("synapse-idx-website daily --confirm-schedule") == 1


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
    assert "titleFallbackRows" in workflow
    assert "disclosuresCreated" in workflow
    assert "analysesCompleted" in workflow
    assert "partialDisclosures" in workflow
    assert "errorsCount" in workflow
    assert "AI fallback activation is emitted as a warning" in workflow

    # The observability snapshot is the CLI health command, which reads Synapse
    # source state only. It must not introduce another collector invocation.
    assert workflow.count("synapse-idx-website daily --confirm-schedule") == 1
