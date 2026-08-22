from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import typer

from .ai_provider import resolve_ai_provider
from .config import Settings
from .durable_checkpoint import MemoryCheckpointStore
from .idx_polite_http import CURRENT_IDX_BASE_URL, PoliteFetchClient
from .source_ingestion import SourceIngestionRunner
from .source_state_client import SourceStateSynapseClient, checkpoint_from_payload
from .sources.idx_website import (
    IDX_WEBSITE_EXTERNAL_ID_PREFIX,
    IDX_WEBSITE_SOURCE_ID,
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


@app.callback()
def main() -> None:
    """Guarded IDX website collection commands."""


def _source_state_client(settings: Settings, counter=None) -> SourceStateSynapseClient:
    return SourceStateSynapseClient(
        settings,
        source_id=IDX_WEBSITE_SOURCE_ID,
        source_request_counter=counter,
    )


@app.command()
def health() -> None:
    """Read collector health/checkpoint from Synapse without contacting IDX or AI."""
    settings = Settings()
    if not settings.synapse_internal_base_url.strip():
        raise typer.BadParameter("SYNAPSE_INTERNAL_BASE_URL is required")
    if not settings.synapse_ingestion_secret.get_secret_value().strip():
        raise typer.BadParameter("SYNAPSE_INGESTION_SECRET is required")

    try:
        with _source_state_client(settings) as state_client:
            state = state_client.get_source_state()
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        raise typer.Exit(code=1) from exc

    latest = state.get("latestAttempt")
    latest_status = latest.get("status") if isinstance(latest, dict) else None
    metadata = latest.get("metadata") if isinstance(latest, dict) else None
    source_state = metadata.get("sourceState") if isinstance(metadata, dict) else None
    processing_ok = source_state.get("processingOk") if isinstance(source_state, dict) else None
    checkpoint = state.get("checkpoint")
    healthy = latest_status not in {"FAILED", "BLOCKED"} and processing_ok is not False and checkpoint is not None
    typer.echo(
        json.dumps(
            {
                "ok": True,
                "healthy": healthy,
                "sourceId": IDX_WEBSITE_SOURCE_ID,
                "latestAttempt": latest,
                "checkpoint": checkpoint,
                "idxNetworkAccess": False,
                "aiNetworkAccess": False,
                "scheduleEnabled": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def collect(
    start: str = typer.Option(..., "--start", help="Explicit ISO timestamp, including timezone."),
    end: str = typer.Option(..., "--end", help="Explicit ISO timestamp, including timezone."),
    checkpoint: Path | None = typer.Option(
        None,
        "--checkpoint",
        help="Optional local checkpoint path. Omit it to use durable Synapse source state.",
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
    """Collect a bounded IDX window and persist checkpoint only after processing success."""
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

    checkpoint_backend = "local" if checkpoint is not None else "synapse"
    checkpoint_path: Path | None = None
    if checkpoint is not None:
        checkpoint_path = checkpoint.expanduser().resolve()
        checkpoint_store = FileCheckpointStore(checkpoint_path)
    else:
        try:
            with _source_state_client(provider_runtime.settings) as state_client:
                remote_state = state_client.get_source_state()
            checkpoint_store = MemoryCheckpointStore(checkpoint_from_payload(remote_state.get("checkpoint")))
        except Exception as exc:
            typer.echo(
                json.dumps(
                    {
                        "ok": False,
                        "errorType": type(exc).__name__,
                        "error": f"could not restore durable IDX checkpoint: {exc}",
                        "checkpointBackend": "synapse",
                        "checkpointCommitted": False,
                        "scheduleEnabled": False,
                        "sourceNetworkAccess": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise typer.Exit(code=1) from exc

    staging_dir = (settings.data_dir / "idx-website-cache").expanduser().resolve()
    idx_client = PoliteFetchClient(
        base_url=CURRENT_IDX_BASE_URL,
        user_agent=settings.idx_user_agent,
        request_delay_seconds=runtime_settings.synapse_daily_request_delay_seconds,
        request_jitter_seconds=runtime_settings.synapse_daily_request_jitter_seconds,
        max_retries=2,
        max_requests=min(runtime_settings.synapse_daily_max_source_requests, 50),
        max_download_bytes_total=runtime_settings.synapse_daily_max_download_bytes,
    )
    source = IdxWebsiteSource(
        idx_client,
        checkpoint_store=checkpoint_store,
        staging_dir=staging_dir,
        page_size=min(settings.idx_page_size, 100),
        max_pages=10,
        timezone_name=settings.app_timezone,
    )

    def client_factory(client_settings: Settings) -> SourceStateSynapseClient:
        return _source_state_client(client_settings, counter=lambda: idx_client.request_count)

    checkpoint_committed = False
    try:
        result = SourceIngestionRunner(
            provider_runtime.settings,
            source,
            client_factory=client_factory,
            summarizer_factory=provider_runtime.summarizer_factory,
            run_mode="MANUAL_BACKFILL",
            allow_coverage_commit=False,
            require_external_id_prefix=IDX_WEBSITE_EXTERNAL_ID_PREFIX,
        ).run_window(start_at=start_at, end_at=end_at)

        if result.processing_ok:
            source.commit_checkpoint()
            if checkpoint_backend == "synapse":
                saved = checkpoint_store.saved
                if saved is None:
                    raise RuntimeError("IDX source did not produce a checkpoint after successful processing")
                with _source_state_client(provider_runtime.settings) as state_client:
                    state_client.commit_source_state(
                        run_id=result.run_id,
                        processing_ok=True,
                        source_transport="http-only",
                        source_complete=result.source_complete,
                        coverage_committed=result.coverage_committed,
                        checkpoint=saved,
                    )
            checkpoint_committed = True
        elif checkpoint_backend == "synapse":
            with _source_state_client(provider_runtime.settings) as state_client:
                state_client.commit_source_state(
                    run_id=result.run_id,
                    processing_ok=False,
                    source_transport="http-only",
                    source_complete=result.source_complete,
                    coverage_committed=False,
                    checkpoint=None,
                )
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "coverageCommitted": False,
                    "coverageAuthoritative": False,
                    "checkpointBackend": checkpoint_backend,
                    "checkpointCommitted": False,
                    "scheduleEnabled": False,
                    "sourceNetworkAccess": idx_client.request_count > 0,
                    "sourceTransport": "http-only",
                    "sourceRequests": idx_client.request_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc
    finally:
        idx_client.close()

    report = _build_manual_import_report(result)
    report["sourceNetworkAccess"] = True
    report["sourceTransport"] = "http-only"
    report["sourceRequests"] = idx_client.request_count
    report["checkpointBackend"] = checkpoint_backend
    report["checkpointCommitted"] = checkpoint_committed
    report["checkpointPath"] = str(checkpoint_path) if checkpoint_path is not None else None
    report["scheduleEnabled"] = False
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)
