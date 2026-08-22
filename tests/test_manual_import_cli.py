from __future__ import annotations

from types import SimpleNamespace

from typer.testing import CliRunner

from idx_digest import synapse_cli


runner = CliRunner()


def _manual_env(tmp_path) -> dict[str, str]:
    return {
        "DATA_DIR": str(tmp_path / "data"),
        "SYNAPSE_DAILY_ENABLED": "false",
        "SYNAPSE_INTERNAL_BASE_URL": "https://synapse.example",
        "SYNAPSE_INGESTION_SECRET": "test-secret",
        "OPENROUTER_API_KEY": "test-openrouter-key",
        "SYNAPSE_DAILY_REQUEST_DELAY_SECONDS": "0.5",
        "SYNAPSE_DAILY_REQUEST_JITTER_SECONDS": "0",
    }


def _result():
    return SimpleNamespace(
        run_id="run-id",
        status="PARTIAL",
        processing_ok=True,
        source_id="manual-manifest",
        source_complete=False,
        source_diagnostics={"networkAccess": False, "completeAttestationAllowed": False},
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


def test_manual_import_requires_explicit_publish_confirmation(tmp_path) -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "manual-import",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-08-21T20:00:00+07:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
        ],
        env=_manual_env(tmp_path),
    )
    assert result.exit_code == 2


def test_manual_import_runs_without_enabling_daily_or_website_automation(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManualManifestSource:
        source_id = "manual-manifest"

        def __init__(self, manifest, *, allow_complete_attestation=False):
            captured["manifest"] = manifest
            captured["allow_complete_attestation"] = allow_complete_attestation

    class FakeSourceRunner:
        def __init__(
            self,
            settings,
            source,
            *,
            summarizer_factory=None,
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

    monkeypatch.setattr(synapse_cli, "ManualManifestSource", FakeManualManifestSource)
    monkeypatch.setattr(synapse_cli, "SourceIngestionRunner", FakeSourceRunner)

    result = runner.invoke(
        synapse_cli.app,
        [
            "manual-import",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-08-21T20:00:00+07:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
            "--confirm-publish",
        ],
        env=_manual_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert '"ok": true' in result.output
    assert '"status": "PARTIAL"' in result.output
    assert '"coverageCommitted": false' in result.output
    assert captured["allow_complete_attestation"] is False
    assert captured["allow_coverage_commit"] is False
    assert captured["require_external_id_prefix"] == "manual-"
    assert captured["summarizer_factory"] is not None
    assert captured["settings"].synapse_daily_enabled is False
    assert synapse_cli.SOURCE_AUTOMATION_ENABLED is False


def test_manual_import_accepts_gemini_without_openrouter_key(tmp_path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeManualManifestSource:
        def __init__(self, manifest, *, allow_complete_attestation=False):
            captured["allow_complete_attestation"] = allow_complete_attestation

    class FakeSourceRunner:
        def __init__(self, settings, source, **kwargs):
            captured["settings"] = settings
            captured["summarizer_factory"] = kwargs.get("summarizer_factory")

        def run_window(self, *, start_at, end_at):
            return _result()

    monkeypatch.setattr(synapse_cli, "ManualManifestSource", FakeManualManifestSource)
    monkeypatch.setattr(synapse_cli, "SourceIngestionRunner", FakeSourceRunner)

    env = _manual_env(tmp_path)
    env.update(
        {
            "AI_PROVIDER": "gemini",
            "GEMINI_API_KEY": "test-gemini-key",
            "OPENROUTER_API_KEY": "",
        }
    )
    result = runner.invoke(
        synapse_cli.app,
        [
            "manual-import",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-08-21T20:00:00+07:00",
            "--end",
            "2026-08-21T21:00:00+07:00",
            "--confirm-publish",
        ],
        env=env,
    )

    assert result.exit_code == 0, result.output
    assert captured["settings"].openrouter_provider == "google-gemini"
    assert captured["settings"].openrouter_model == "gemini-3.5-flash-lite"
    assert captured["summarizer_factory"].__name__ == "GeminiSummarizer"


def test_manual_import_rejects_window_over_two_hours(tmp_path) -> None:
    result = runner.invoke(
        synapse_cli.app,
        [
            "manual-import",
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--start",
            "2026-08-21T18:00:00+07:00",
            "--end",
            "2026-08-21T20:00:01+07:00",
            "--confirm-publish",
        ],
        env=_manual_env(tmp_path),
    )
    assert result.exit_code != 0
    assert "2 hours or less" in result.output


def test_manual_import_report_treats_expected_partial_coverage_as_processing_success() -> None:
    report = synapse_cli._build_manual_import_report(_result())
    assert report["ok"] is True
    assert report["status"] == "PARTIAL"
    assert report["coverageCommitted"] is False
    assert report["coverageAuthoritative"] is False
    assert report["scheduleEnabled"] is False
