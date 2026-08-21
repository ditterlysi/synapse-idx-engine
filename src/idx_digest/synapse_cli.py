from __future__ import annotations

import json

import typer

from .config import Settings
from .daily_guardrails import DailyPolicy, DailyPolicyError
from .synapse_client import SynapseClientConfigurationError

app = typer.Typer(no_args_is_help=True, help="Synapse IDX engine integration commands.")


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


@app.command()
def daily() -> None:
    """Reserved entry point for the scheduled Synapse pipeline.

    Live collection remains intentionally disabled until the Synapse internal API
    and manual narrow-range E2E are implemented and verified.
    """
    settings = Settings()
    issues = _integration_issues(settings)
    if issues:
        raise typer.BadParameter("; ".join(issues))
    if not settings.synapse_daily_enabled:
        typer.echo("Synapse daily collection is disabled. Set SYNAPSE_DAILY_ENABLED=true only after E2E approval.")
        raise typer.Exit(code=2)
    raise SynapseClientConfigurationError(
        "Daily live collection is not wired yet; implement the Synapse API boundary and manual E2E first"
    )
