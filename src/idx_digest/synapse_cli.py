from __future__ import annotations

import json
from datetime import timedelta

import typer

from .config import Settings
from .daily_guardrails import DailyPolicy, DailyPolicyError
from .synapse_client import SynapseClient, SynapseClientConfigurationError
from .synapse_pipeline import SynapsePipelineRunner
from .timeutils import parse_boundary

app = typer.Typer(no_args_is_help=True, help="Synapse IDX engine integration commands.")

E2E_MAX_WINDOW = timedelta(hours=2)
E2E_MAX_SOURCE_REQUESTS = 12
E2E_MAX_ATTACHMENTS = 20
E2E_MAX_DOWNLOAD_BYTES = 100_000_000
E2E_MAX_AI_DOCUMENTS = 20
E2E_MAX_RUN_SECONDS = 900


def _integration_issues(settings: Settings) -> list[str]:
    issues: list[str] = []
    try:
        DailyPolicy.from_settings(settings)
    except DailyPolicyError as exc:
        issues.append(str(exc))

    if settings.synapse_daily_enabled:
        if not settings.synapse_internal_base_url.strip():
            issues.append("SYNAPSE_INTERNAL_BASE_URL is required when SYNAPSE_DAILY_ENABLED=true")
        if not settings.synapse_ingestion_secret.get_secret_value().strip():
            issues.append("SYNAPSE_INGESTION_SECRET is required when SYNAPSE_DAILY_ENABLED=true")
    return issues


def _live_e2e_issues(settings: Settings) -> list[str]:
    issues = _integration_issues(settings)
    if not settings.synapse_daily_enabled:
        issues.append("SYNAPSE_DAILY_ENABLED=true is required as the live-run kill switch")
    if not settings.synapse_internal_base_url.strip():
        issues.append("SYNAPSE_INTERNAL_BASE_URL is required")
    if not settings.synapse_ingestion_secret.get_secret_value().strip():
        issues.append("SYNAPSE_INGESTION_SECRET is required")
    if not settings.openrouter_api_key.strip():
        issues.append("OPENROUTER_API_KEY is required for the live E2E analysis path")
    return list(dict.fromkeys(issues))


def _tighten_e2e_settings(settings: Settings) -> Settings:
    """Apply caps stricter than scheduled defaults without mutating the user's config."""
    return settings.model_copy(
        update={
            "synapse_daily_max_source_requests": min(
                settings.synapse_daily_max_source_requests, E2E_MAX_SOURCE_REQUESTS
            ),
            "synapse_daily_max_attachments": min(
                settings.synapse_daily_max_attachments, E2E_MAX_ATTACHMENTS
            ),
            "synapse_daily_max_download_bytes": min(
                settings.synapse_daily_max_download_bytes, E2E_MAX_DOWNLOAD_BYTES
            ),
            "synapse_daily_max_ai_documents": min(
                settings.synapse_daily_max_ai_documents, E2E_MAX_AI_DOCUMENTS
            ),
            "synapse_daily_max_run_seconds": min(
                settings.synapse_daily_max_run_seconds, E2E_MAX_RUN_SECONDS
            ),
            "llm_concurrency": min(settings.llm_concurrency, 2),
            "llm_per_announcement_concurrency": min(settings.llm_per_announcement_concurrency, 2),
            "extraction_workers": min(settings.extraction_workers, 2),
        }
    )


@app.command()
def doctor() -> None:
    """Validate conservative-mode settings without contacting IDX or Synapse."""
    settings = Settings()
    issues = _integration_issues(settings)
    policy = DailyPolicy.from_settings(settings) if not issues else None

    report = {
        "engine": "synapse-idx-engine",
        "version": "0.16.0",
        "daily_enabled": settings.synapse_daily_enabled,
        "synapse_configured": bool(
            settings.synapse_internal_base_url.strip()
            and settings.synapse_ingestion_secret.get_secret_value().strip()
        ),
        "policy": None
        if policy is None
        else {
            "transport": policy.transport,
            "request_delay_seconds": policy.request_delay_seconds,
            "request_jitter_seconds": policy.request_jitter_seconds,
            "max_source_requests": policy.max_source_requests,
            "max_attachments": policy.max_attachments,
            "max_download_bytes": policy.max_download_bytes,
            "max_ai_documents": policy.max_ai_documents,
            "max_run_seconds": policy.max_run_seconds,
            "allow_historical_backfill": policy.allow_historical_backfill,
            "allow_ticker_fanout": policy.allow_ticker_fanout,
        },
        "issues": issues,
    }
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if issues:
        raise typer.Exit(code=1)


@app.command("api-check")
def api_check(
    ticker: list[str] = typer.Option(..., "--ticker", "-t", help="Ticker to resolve through Synapse. Repeatable."),
) -> None:
    """Check the authenticated Synapse boundary without contacting IDX."""
    settings = Settings()
    try:
        with SynapseClient(settings) as client:
            response = client.resolve_relevance(ticker)
    except (SynapseClientConfigurationError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    report = {
        "ok": True,
        "items": [
            {
                "ticker": item.ticker,
                "isPortfolio": item.is_portfolio,
                "isWatchlist": item.is_watchlist,
                "priority": item.priority,
            }
            for item in response.items
        ],
    }
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))


@app.command("e2e")
def e2e(
    start: str = typer.Option(..., "--start", help="Explicit ISO timestamp, including timezone."),
    end: str = typer.Option(..., "--end", help="Explicit ISO timestamp, including timezone."),
    confirm_live_idx: bool = typer.Option(
        False,
        "--confirm-live-idx",
        help="Required acknowledgement that this command performs live IDX reads and Synapse writes.",
    ),
) -> None:
    """Run one tightly bounded live integration window; never schedules itself."""
    if not confirm_live_idx:
        raise typer.BadParameter("--confirm-live-idx is required for a live E2E run")
    if len(start.strip()) == 10 or len(end.strip()) == 10:
        raise typer.BadParameter("E2E requires explicit timestamps; date-only windows are not allowed")

    settings = Settings()
    issues = _live_e2e_issues(settings)
    if issues:
        raise typer.BadParameter("; ".join(issues))

    try:
        start_at = parse_boundary(start, settings.app_timezone)
        end_at = parse_boundary(end, settings.app_timezone)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"invalid E2E window: {exc}") from exc

    if end_at <= start_at:
        raise typer.BadParameter("--end must be later than --start")
    if end_at - start_at > E2E_MAX_WINDOW:
        raise typer.BadParameter("live E2E window must be 2 hours or less")

    e2e_settings = _tighten_e2e_settings(settings)
    try:
        result = SynapsePipelineRunner(e2e_settings).run_window(start_at=start_at, end_at=end_at)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "scheduleEnabled": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc

    report = {
        "ok": result.status == "COMPLETE" and result.coverage_committed,
        "runId": result.run_id,
        "status": result.status,
        "coverageCommitted": result.coverage_committed,
        "pipelineStatus": result.report.get("status"),
        "scrapeComplete": result.report.get("scrape_complete"),
        "publish": {
            "announcementsAvailable": result.publish.announcements_available,
            "announcementsCreated": result.publish.announcements_created,
            "filesPublished": result.publish.files_published,
            "filesDownloaded": result.publish.files_downloaded,
            "filesExtracted": result.publish.files_extracted,
            "analysesCompleted": result.publish.analyses_completed,
            "partialDisclosures": result.publish.partial_disclosures,
            "errors": result.publish.errors,
        },
        "budget": result.budget,
        "scheduleEnabled": False,
    }
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)


@app.command()
def daily() -> None:
    """Reserved entry point for future scheduled execution."""
    settings = Settings()
    issues = _integration_issues(settings)
    if issues:
        raise typer.BadParameter("; ".join(issues))
    if not settings.synapse_daily_enabled:
        typer.echo("Synapse daily collection is disabled. Keep it disabled until repeated live E2E approval.")
        raise typer.Exit(code=2)
    raise SynapseClientConfigurationError(
        "Scheduled daily execution is intentionally not activated yet; use the bounded e2e command for manual validation"
    )
