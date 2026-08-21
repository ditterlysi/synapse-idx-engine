from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from idx_digest.config import Settings
from idx_digest.daily_guardrails import DailyPolicy, DailyRunBudget
from idx_digest.db import Database
from idx_digest.synapse_mapper import build_structured_analysis, taxonomy_tags
from idx_digest.synapse_pipeline import SynapsePipelineRunner
from idx_digest.synapse_runtime import (
    ConservativeIDXClient,
    ConservativeRuntime,
    DailyAccessProtectionStop,
    DailyRateLimitStop,
    bind_runtime,
)
from idx_digest.timeutils import parse_idx_datetime


RUN_ID = "986b5105-f894-4a69-a733-a4e1bcf2cc62"
DISCLOSURE_ID = "70f28dd7-09f2-4936-92c8-01c22d1a1e95"
FILE_ID = "a15b1438-61b0-4bd9-9cf0-5a831d3f531f"


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
        synapse_daily_request_delay_seconds=0.5,
        synapse_daily_request_jitter_seconds=0.0,
    )


def _summary() -> dict[str, object]:
    return {
        "ticker": "BBRI",
        "announcement_id": "IDX-1",
        "announced_at": "2026-08-21T08:00:00+07:00",
        "title": "Rencana belanja modal dan ekspansi",
        "executive_summary": "Emiten menjelaskan rencana ekspansi dan belanja modal.",
        "category": "expansion",
        "material_facts": ["Perusahaan menyampaikan rencana ekspansi."],
        "financial_figures": [{"metric": "Capex", "value": "Rp1 triliun", "period": "2026"}],
        "corporate_actions": [],
        "expansion_projects": ["Belanja modal untuk proyek baru."],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "analytical_scenarios": [
            {
                "classification": "analyst_hypothesis",
                "topic": "Execution",
                "analysis": "Timing remains subject to execution.",
                "basis": ["Management plan"],
                "assumptions": ["Project proceeds"],
                "confidence": "medium",
                "caveats": ["No completion date confirmed"],
            }
        ],
        "dates_and_deadlines": [{"date": "2026-12-31", "event": "Target period"}],
        "risks_or_uncertainties": ["Execution risk."],
        "possible_investor_relevance": ["The plan may affect future capacity."],
        "source_files": [{"filename": "disclosure.pdf", "url": "https://example.com/disclosure.pdf"}],
        "limitations": [],
    }


def _seed_database(settings: Settings, *, with_summary: bool = True) -> Database:
    settings.ensure_directories()
    db = Database(settings.database_path)
    raw = {
        "pengumuman": {
            "Id2": "IDX-1",
            "Kode_Emiten": "BBRI",
            "TglPengumuman": "21-08-2026 08:00:00",
            "JudulPengumuman": "Rencana belanja modal dan ekspansi",
            "NoPengumuman": "001/TEST/2026",
            "JenisPengumuman": "Keterbukaan Informasi",
            "PerihalPengumuman": "Ekspansi",
        }
    }
    announced = parse_idx_datetime("21-08-2026 08:00:00", settings.app_timezone)
    db.upsert_announcement(raw, announced.isoformat())
    db.upsert_attachment(
        "IDX-1",
        {
            "FullSavePath": "https://example.com/disclosure.pdf",
            "OriginalFilename": "disclosure.pdf",
            "IsAttachment": True,
        },
        selected_for_analysis=True,
        selection_reason="primary disclosure",
        selection_category="primary",
    )
    raw_path = settings.data_dir / "raw" / "cached.pdf"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"synthetic pdf bytes")
    text_path = settings.data_dir / "text" / "cached.txt"
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("synthetic extracted text", encoding="utf-8")
    db.update_attachment_file(
        "https://example.com/disclosure.pdf",
        local_path=str(raw_path),
        sha256="a" * 64,
        content_type="application/pdf",
    )
    db.update_extraction(
        "https://example.com/disclosure.pdf",
        text_path=str(text_path),
        method="native",
        error=None,
    )
    if with_summary:
        db.save_announcement_summary(
            "IDX-1",
            "BBRI",
            _summary(),
            "deepseek/deepseek-v4-flash-0731",
            "announcement-v3",
            analysis_mode="full",
        )
    return db


class FakeClient:
    latest: "FakeClient | None" = None

    def __init__(self, settings: Settings):
        self.settings = settings
        self.calls: list[tuple[str, object]] = []
        self.final_run_status: str | None = None
        self.coverage_committed = False
        self.analysis = None
        FakeClient.latest = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_run(self, request):
        self.calls.append(("create_run", request))
        return SimpleNamespace(run_id=RUN_ID)

    def upsert_disclosures(self, request):
        self.calls.append(("upsert_disclosures", request))
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    idx_announcement_id=item.idx_announcement_id,
                    disclosure_id=DISCLOSURE_ID,
                    created=True,
                )
                for item in request.items
            ]
        )

    def upsert_files(self, disclosure_id, request):
        self.calls.append(("upsert_files", request))
        return SimpleNamespace(
            files=[SimpleNamespace(file_id=FILE_ID, source_url=item.source_url) for item in request.files]
        )

    def update_processing_status(self, disclosure_id, request):
        self.calls.append(("status", request.processing_status))
        return SimpleNamespace(disclosure_id=disclosure_id, processing_status=request.processing_status)

    def commit_analysis(self, disclosure_id, request):
        self.calls.append(("analysis", request))
        self.analysis = request.analysis
        return SimpleNamespace(analysis_id="analysis-id", promoted=True)

    def update_run(self, run_id, request):
        self.calls.append(("update_run", request))
        if request.status:
            self.final_run_status = request.status
        return SimpleNamespace(run_id=run_id, status=request.status, completed_at=request.completed_at)

    def commit_coverage(self, request):
        self.calls.append(("coverage", request))
        self.coverage_committed = True
        return SimpleNamespace(coverage_id="coverage-id", created=True)


class FakePipeline:
    def __init__(self, settings: Settings, *, with_summary: bool = True):
        self.settings = settings
        self.db = _seed_database(settings, with_summary=with_summary)
        self.summarizer = None
        self.closed = False

    def run(self, **kwargs):
        assert kwargs["metadata_mode"] == "incremental"
        assert kwargs["ticker"] is None
        assert kwargs["keyword"] == ""
        assert kwargs["max_announcements"] is None
        start = kwargs["start_at"]
        end = kwargs["end_at"]
        return {
            "status": "completed",
            "scrape_complete": True,
            "errors": [],
            "metadata_announcements_collected": 1,
            "metadata_deferred_ranges": [],
            "metadata_coverage_after": [{"start_at": start.isoformat(), "end_at": end.isoformat()}],
        }

    def close(self):
        self.closed = True


def test_legacy_mapper_is_conservative_about_impact() -> None:
    tags = taxonomy_tags("Rencana belanja modal dan ekspansi", _summary())
    analysis = build_structured_analysis(
        ticker="BBRI",
        title="Rencana belanja modal dan ekspansi",
        summary=_summary(),
        analysis_mode="full",
    )
    assert "CAPEX" in tags or "EXPANSION" in tags
    assert analysis.impact == "UNCLEAR"
    assert analysis.materiality in {"MEDIUM", "LOW"}
    assert analysis.material_facts[0].claim_type == "EXPLICIT_FACT"
    assert any(claim.claim_type == "ANALYST_HYPOTHESIS" for claim in analysis.material_facts)
    assert analysis.key_numbers[0].value_text == "Rp1 triliun"


def test_runner_publishes_cached_pipeline_and_commits_coverage(tmp_path) -> None:
    settings = _settings(tmp_path)
    start = parse_idx_datetime("21-08-2026 00:00:00", settings.app_timezone)
    end = parse_idx_datetime("21-08-2026 23:00:00", settings.app_timezone)
    runner = SynapsePipelineRunner(
        settings,
        client_factory=FakeClient,
        pipeline_factory=lambda runtime_settings: FakePipeline(runtime_settings),
    )
    result = runner.run_window(start_at=start, end_at=end)
    client = FakeClient.latest
    assert client is not None
    assert result.status == "COMPLETE"
    assert result.coverage_committed is True
    assert result.publish.announcements_created == 1
    assert result.publish.files_published == 1
    assert result.publish.analyses_completed == 1
    assert client.final_run_status == "COMPLETE"
    assert client.coverage_committed is True
    assert client.analysis.impact == "UNCLEAR"


def test_runner_keeps_coverage_uncommitted_when_analysis_missing(tmp_path) -> None:
    settings = _settings(tmp_path)
    start = parse_idx_datetime("21-08-2026 00:00:00", settings.app_timezone)
    end = parse_idx_datetime("21-08-2026 23:00:00", settings.app_timezone)
    runner = SynapsePipelineRunner(
        settings,
        client_factory=FakeClient,
        pipeline_factory=lambda runtime_settings: FakePipeline(runtime_settings, with_summary=False),
    )
    result = runner.run_window(start_at=start, end_at=end)
    client = FakeClient.latest
    assert client is not None
    assert result.status == "PARTIAL"
    assert result.coverage_committed is False
    assert result.publish.partial_disclosures == 1
    assert client.final_run_status == "PARTIAL"
    assert client.coverage_committed is False


def test_conservative_client_stops_on_429_without_browser_fallback(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path).model_copy(update={"idx_transport": "http"})
    policy = DailyPolicy.from_settings(settings)
    budget = DailyRunBudget(policy)
    runtime = ConservativeRuntime(
        policy,
        budget,
        sleeper=lambda _seconds: None,
        jitter=lambda _start, _end: 0.0,
        clock=lambda: 1.0,
    )
    client = ConservativeIDXClient(settings)
    response = httpx.Response(
        429,
        request=httpx.Request("GET", "https://www.idx.co.id/primary/ListedCompany/GetAnnouncement"),
    )
    monkeypatch.setattr(client.client, "get", lambda *args, **kwargs: response)
    try:
        with bind_runtime(runtime):
            with pytest.raises(DailyRateLimitStop):
                client._get_json_http({})
            with pytest.raises(DailyAccessProtectionStop):
                client.browser_transport()
            assert client._wide_page_probe() is None
    finally:
        client.close()
    assert budget.source_requests == 1
