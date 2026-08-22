from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from idx_digest import idx_public_snapshot_cli

runner = CliRunner()


def _env(tmp_path) -> dict[str, str]:
    return {
        "DATA_DIR": str(tmp_path / "data"),
        "AI_PROVIDER": "gemini",
        "GEMINI_API_KEY": "test-gemini-key",
        "SYNAPSE_DAILY_ENABLED": "false",
        "SYNAPSE_INTERNAL_BASE_URL": "https://synapse.example",
        "SYNAPSE_INGESTION_SECRET": "test-secret",
        "SYNAPSE_DAILY_REQUEST_DELAY_SECONDS": "0.5",
        "SYNAPSE_DAILY_REQUEST_JITTER_SECONDS": "0",
    }


def _result():
    return SimpleNamespace(
        run_id="run-id",
        status="PARTIAL",
        processing_ok=True,
        source_id="idx-public-snapshot",
        source_complete=False,
        source_diagnostics={
            "networkAccess": False,
            "authoritativeCoverageAllowed": False,
        },
        coverage_committed=False,
        publish=SimpleNamespace(
            disclosures_available=1,
            disclosures_created=1,
            attachments_staged=1,
            files_published=0,
            files_extracted=1,
            documents_analyzed=1,
            analyses_completed=1,
            partial_disclosures=0,
            errors=[],
        ),
        budget={
            "source_requests": 0,
            "attachments": 1,
            "download_bytes": 100,
            "ai_documents": 1,
        },
    )


def test_snapshot_cli_requires_explicit_publish_confirmation(tmp_path) -> None:
    result = runner.invoke(
        idx_public_snapshot_cli.app,
        [
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-07-10T22:00:00+07:00",
            "--end",
            "2026-07-10T23:00:00+07:00",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "--confirm-publish" in result.output


def test_snapshot_cli_runs_without_coverage_or_source_network(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeSnapshotSource:
        source_id = "idx-public-snapshot"

        def __init__(self, manifest):
            captured["manifest"] = manifest

    class FakeSourceRunner:
        def __init__(
            self,
            settings,
            source,
            *,
            summarizer_factory,
            allow_coverage_commit=False,
            require_external_id_prefix=None,
        ):
            captured["settings"] = settings
            captured["source"] = source
            captured["summarizer_factory"] = summarizer_factory
            captured["allow_coverage_commit"] = allow_coverage_commit
            captured["require_external_id_prefix"] = require_external_id_prefix

        def run_window(self, *, start_at, end_at):
            captured["start_at"] = start_at
            captured["end_at"] = end_at
            return _result()

    monkeypatch.setattr(idx_public_snapshot_cli, "IdxPublicSnapshotSource", FakeSnapshotSource)
    monkeypatch.setattr(idx_public_snapshot_cli, "SourceIngestionRunner", FakeSourceRunner)

    result = runner.invoke(
        idx_public_snapshot_cli.app,
        [
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-07-10T22:00:00+07:00",
            "--end",
            "2026-07-10T23:00:00+07:00",
            "--confirm-publish",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output
    assert '"status": "PARTIAL"' in result.output
    assert '"coverageCommitted": false' in result.output
    assert '"coverageAuthoritative": false' in result.output
    assert '"scheduleEnabled": false' in result.output
    assert '"sourceNetworkAccess": false' in result.output
    assert captured["allow_coverage_commit"] is False
    assert captured["require_external_id_prefix"] == "idx-public-"
    assert captured["settings"].synapse_daily_enabled is False
    assert captured["settings"].ai_provider == "gemini"


def test_snapshot_cli_rejects_cross_day_window(tmp_path) -> None:
    result = runner.invoke(
        idx_public_snapshot_cli.app,
        [
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-07-10T23:30:00+07:00",
            "--end",
            "2026-07-11T00:30:00+07:00",
            "--confirm-publish",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code != 0
    assert "must stay within one Asia/Jakarta calendar date" in result.output
