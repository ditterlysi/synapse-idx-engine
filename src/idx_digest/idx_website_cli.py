from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import typer

from .ai_provider import resolve_ai_provider
from .config import Settings
from .idx_polite_http import PoliteFetchClient
from .source_ingestion import SourceIngestionRunner
from .sources.idx_website import (
    IDX_WEBSITE_EXTERNAL_ID_PREFIX,
    FileCheckpointStore,
    IdxWebsiteSource,
)
from .synapse_cli import (
    _build_manual_import_report,
    _manual_import_issues,
    _tighten_e2e_settings,
    _validate_explicit_timestamp,
)
from .timeutils import parse_boundary

app = typer.Typer(
    add_completion=False,
    help="Run the guarded HTTP-only IDX website collector through the Synapse ingestion pipeline.",
)

COLLECT_MAX_WINDOW = timedelta(hours=48)


@app.command()
def collect(
    start: str = typer.Option(..., "--start", help="Explicit ISO timestamp, including timezone."),
    end: str = typer.Option(..., "--end", help="Explicit ISO timestamp, including timezone."),
    checkpoint: Path | None = typer.Option(
        None,
        "--checkpoint",
        help="Optional checkpoint path. Defaults to data/idx-website-checkpoint.json.",
    ),
    enable_source: bool = typer.Option(
        False,
        "--enable-source",
        help="Required explicit switch for this network source. Does not enable a scheduler.",
    ),
    confirm_publish: bool = typer.Option(
        False,
        "--confirm-publish",
        help="Required acknowledgement that the run may contact IDX, call AI, and write to Synapse.",
    ),
) -> None:
    """Collect a bounded IDX window, process new disclosures, then commit checkpoint on success."""
    if not enable_source:
        raise typer.BadParameter("--enable-source is required for IDX website collection")
    if not confirm_publish:
        raise typer.BadParameter("--confirm-publish is required for IDX website collection")

    _validate_explicit_timestamp(start, "--start")
    _validate_explicit_timestamp(end, "--end")
    settings = Settings()
    try:
        start_at = parse_boundary(start, settings.app_timezone)
        end_at = parse_boundary(end, settings.app_timezone)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"invalid IDX website collection window: {exc}") from exc

    if end_at <= start_at:
        raise typer.BadParameter("--end must be later than --start")
    if end_at - start_at > COLLECT_MAX_WINDOW:
        raise typer.BadParameter("IDX website collection window must be 48 hours or less")

    issues = _manual_import_issues(settings)
    if issues:
        raise typer.BadParameter("; ".join(issues))

    runtime_settings = _tighten_e2e_settings(settings).model_copy(
        update={
            "idx_transport": "http",
            "synapse_daily_transport": "http",
            "synapse_daily_request_delay_seconds": max(10.0, settings.synapse_daily_request_delay_seconds),
            "synapse_daily_request_jitter_seconds": max(2.0, settings.synapse_daily_request_jitter_seconds),
            "synapse_daily_allow_historical_backfill": False,
            "synapse_daily_allow_ticker_fanout": False,
        }
    )
    provider_runtime = resolve_ai_provider(runtime_settings)

    checkpoint_path = (checkpoint or (settings.data_dir / "idx-website-checkpoint.json")).expanduser().resolve()
    staging_dir = (settings.data_dir / "idx-website-cache").expanduser().resolve()

    client = PoliteFetchClient(
        base_url=settings.idx_base_url,
        user_agent=settings.idx_user_agent,
        request_delay_seconds=runtime_settings.synapse_daily_request_delay_seconds,
        request_jitter_seconds=runtime_settings.synapse_daily_request_jitter_seconds,
        max_retries=2,
        max_requests=min(runtime_settings.synapse_daily_max_source_requests, 50),
        max_download_bytes_total=runtime_settings.synapse_daily_max_download_bytes,
    )
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(checkpoint_path),
        staging_dir=staging_dir,
        page_size=min(settings.idx_page_size, 100),
        max_pages=10,
        timezone_name=settings.app_timezone,
    )

    try:
        result = SourceIngestionRunner(
            provider_runtime.settings,
            source,
            summarizer_factory=provider_runtime.summarizer_factory,
            run_mode="MANUAL_BACKFILL",
            allow_coverage_commit=False,
            require_external_id_prefix=IDX_WEBSITE_EXTERNAL_ID_PREFIX,
        ).run_window(start_at=start_at, end_at=end_at)
        checkpoint_committed = False
        if result.processing_ok:
            source.commit_checkpoint()
            checkpoint_committed = True
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "coverageCommitted": False,
                    "coverageAuthoritative": False,
                    "checkpointCommitted": False,
                    "scheduleEnabled": False,
                    "sourceNetworkAccess": True,
                    "sourceTransport": "http-only",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    finally:
        client.close()

    report = _build_manual_import_report(result)
    report["sourceNetworkAccess"] = True
    report["sourceTransport"] = "http-only"
    report["checkpointCommitted"] = checkpoint_committed
    report["checkpointPath"] = str(checkpoint_path)
    report["scheduleEnabled"] = False
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)