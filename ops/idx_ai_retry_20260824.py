from __future__ import annotations

import json
from pathlib import Path

from idx_digest.ai_provider import resolve_ai_provider
from idx_digest.config import Settings
from idx_digest.idx_polite_http import CURRENT_IDX_BASE_URL, PoliteFetchClient
from idx_digest.idx_website_cli import _collector_runtime_settings
from idx_digest.source_ingestion import SourceIngestionRunner
from idx_digest.source_state_client import SourceStateSynapseClient, checkpoint_from_payload
from idx_digest.sources.idx_website import (
    IDX_WEBSITE_EXTERNAL_ID_PREFIX,
    IDX_WEBSITE_SOURCE_ID,
    FileCheckpointStore,
    IdxWebsiteSource,
)
from idx_digest.timeutils import parse_boundary

START = "2026-08-21T21:20:00+07:00"
END = "2026-08-23T09:30:00+07:00"


def main() -> None:
    settings = Settings()
    runtime_settings = _collector_runtime_settings(settings, run_mode="DAILY")
    provider_runtime = resolve_ai_provider(runtime_settings)

    with SourceStateSynapseClient(
        provider_runtime.settings,
        source_id=IDX_WEBSITE_SOURCE_ID,
    ) as state_client:
        state = state_client.get_source_state()
    checkpoint = checkpoint_from_payload(state.get("checkpoint"))
    checkpoint_path = Path("idx-ai-retry-checkpoint.json")
    checkpoint_store = FileCheckpointStore(checkpoint_path)
    checkpoint_store.save(checkpoint)

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
        staging_dir=settings.data_dir / "idx-website-cache",
        page_size=min(settings.idx_page_size, 100),
        max_pages=10,
        timezone_name=settings.app_timezone,
    )

    def client_factory(client_settings: Settings):
        return SourceStateSynapseClient(
            client_settings,
            source_id=IDX_WEBSITE_SOURCE_ID,
            source_request_counter=lambda: idx_client.request_count,
        )

    start_at = parse_boundary(START, settings.app_timezone)
    end_at = parse_boundary(END, settings.app_timezone)
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

        report = {
            "ok": result.processing_ok,
            "runId": result.run_id,
            "status": result.status,
            "window": {"start": START, "end": END},
            "checkpointSeedCount": len(checkpoint.seen_ids),
            "sourceDiagnostics": result.source_diagnostics,
            "publish": {
                "disclosuresAvailable": result.publish.disclosures_available,
                "disclosuresCreated": result.publish.disclosures_created,
                "disclosuresSkippedReady": result.publish.disclosures_skipped_ready,
                "attachmentsStaged": result.publish.attachments_staged,
                "filesPublished": result.publish.files_published,
                "filesExtracted": result.publish.files_extracted,
                "documentsAnalyzed": result.publish.documents_analyzed,
                "analysesCompleted": result.publish.analyses_completed,
                "partialDisclosures": result.publish.partial_disclosures,
                "aiRateLimitDeferred": getattr(result.publish, "ai_rate_limit_deferred", 0),
                "errors": result.publish.errors,
            },
            "durableCheckpointWritten": False,
            "note": "Durable source checkpoint must be rebuilt from READY stock-scope canonical IDs plus aliases after a successful retry.",
        }
        Path("ops/idx-ai-retry-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not result.processing_ok:
            raise SystemExit(1)
    finally:
        idx_client.close()


if __name__ == "__main__":
    main()
