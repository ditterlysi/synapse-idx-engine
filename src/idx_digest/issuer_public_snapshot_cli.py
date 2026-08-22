from __future__ import annotations

import json
from pathlib import Path

import typer

from .ai_provider import resolve_ai_provider
from .config import Settings
from .snapshot_ingestion import SnapshotSourceIngestionRunner
from .sources.issuer_public_snapshot import (
    ISSUER_PUBLIC_EXTERNAL_ID_PREFIX,
    IssuerPublicSnapshotSource,
)
from .synapse_cli import (
    E2E_MAX_WINDOW,
    _build_manual_import_report,
    _manual_import_issues,
    _tighten_e2e_settings,
    _validate_explicit_timestamp,
)
from .timeutils import parse_boundary

app = typer.Typer(
    add_completion=False,
    help="Import an offline snapshot captured from an issuer's official public website.",
)


@app.command()
def import_snapshot(
    manifest: Path = typer.Option(..., "--manifest", help="Path to an offline issuer-public snapshot manifest."),
    start: str = typer.Option(..., "--start", help="Explicit ISO timestamp, including timezone."),
    end: str = typer.Option(..., "--end", help="Explicit ISO timestamp, including timezone."),
    confirm_publish: bool = typer.Option(
        False,
        "--confirm-publish",
        help="Required acknowledgement that the import may call the configured AI provider and write to Synapse.",
    ),
) -> None:
    """Process staged issuer documents without contacting the issuer website."""
    if not confirm_publish:
        raise typer.BadParameter("--confirm-publish is required for an issuer public snapshot import")

    _validate_explicit_timestamp(start, "--start")
    _validate_explicit_timestamp(end, "--end")
    settings = Settings()
    try:
        start_at = parse_boundary(start, settings.app_timezone)
        end_at = parse_boundary(end, settings.app_timezone)
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(f"invalid snapshot import window: {exc}") from exc

    if end_at <= start_at:
        raise typer.BadParameter("--end must be later than --start")
    if end_at - start_at > E2E_MAX_WINDOW:
        raise typer.BadParameter("snapshot import window must be 2 hours or less")
    if start_at.date() != end_at.date():
        raise typer.BadParameter("snapshot import must stay within one Asia/Jakarta calendar date")

    issues = _manual_import_issues(settings)
    if issues:
        raise typer.BadParameter("; ".join(issues))

    provider_runtime = resolve_ai_provider(_tighten_e2e_settings(settings))
    source = IssuerPublicSnapshotSource(manifest)
    try:
        result = SnapshotSourceIngestionRunner(
            provider_runtime.settings,
            source,
            summarizer_factory=provider_runtime.summarizer_factory,
            allow_coverage_commit=False,
            require_external_id_prefix=ISSUER_PUBLIC_EXTERNAL_ID_PREFIX,
        ).run_window(start_at=start_at, end_at=end_at)
    except Exception as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                    "coverageCommitted": False,
                    "coverageAuthoritative": False,
                    "scheduleEnabled": False,
                    "sourceNetworkAccess": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        raise typer.Exit(code=1) from exc

    report = _build_manual_import_report(result)
    report["sourceNetworkAccess"] = False
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["ok"]:
        raise typer.Exit(code=1)
