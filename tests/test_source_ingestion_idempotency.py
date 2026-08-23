from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from idx_digest.config import Settings
from idx_digest.source_contract import SourceAttachment, SourceDisclosure, SourceWindowResult
from idx_digest.source_ingestion import SourceIngestionRunner


class ReadySource:
    source_id = "manual-manifest"

    def __init__(self, result: SourceWindowResult) -> None:
        self.result = result

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        assert start_at == self.result.requested_start
        assert end_at == self.result.requested_end
        return self.result


class ReadyClient:
    latest: "ReadyClient | None" = None

    def __init__(self, _settings: Settings) -> None:
        self.calls: list[tuple[str, object]] = []
        ReadyClient.latest = self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def create_run(self, request):
        self.calls.append(("create_run", request))
        return SimpleNamespace(run_id="986b5105-f894-4a69-a733-a4e1bcf2cc62")

    def upsert_disclosures(self, request):
        self.calls.append(("upsert_disclosures", request))
        item = request.items[0]
        return SimpleNamespace(
            items=[
                SimpleNamespace(
                    idx_announcement_id=item.idx_announcement_id,
                    disclosure_id="70f28dd7-09f2-4936-92c8-01c22d1a1e95",
                    created=False,
                    processing_status="READY",
                )
            ]
        )

    def update_processing_status(self, _disclosure_id, request):
        raise AssertionError(f"READY disclosure must not be mutated to {request.processing_status}")

    def upsert_files(self, _disclosure_id, _request):
        raise AssertionError("READY disclosure files must not be republished")

    def commit_analysis(self, _disclosure_id, _request):
        raise AssertionError("READY disclosure analysis must not be replaced")

    def update_run(self, _run_id, request):
        self.calls.append(("update_run", request))
        return SimpleNamespace(status=request.status, completed_at=request.completed_at)

    def commit_coverage(self, _request):
        raise AssertionError("non-authoritative test source must not commit coverage")


def test_existing_ready_disclosure_is_not_reprocessed(tmp_path) -> None:
    start = datetime.fromisoformat("2026-08-21T09:00:00+07:00")
    end = datetime.fromisoformat("2026-08-21T10:00:00+07:00")
    path = tmp_path / "already-ready.txt"
    path.write_text("already processed", encoding="utf-8")
    disclosure = SourceDisclosure(
        external_id="manual-ready-1",
        ticker="BBRI",
        announced_at=datetime.fromisoformat("2026-08-21T09:30:00+07:00"),
        title="Already processed disclosure",
        source_url="https://example.com/disclosures/manual-ready-1",
        attachments=(
            SourceAttachment(
                filename="already-ready.txt",
                local_path=path,
                source_url="https://example.com/files/already-ready.txt",
                content_type="text/plain",
            ),
        ),
    )
    source = ReadySource(
        SourceWindowResult(
            source_id="manual-manifest",
            requested_start=start,
            requested_end=end,
            disclosures=(disclosure,),
            complete=False,
            diagnostics={"networkAccess": False},
        )
    )
    settings = Settings(
        data_dir=tmp_path / "data",
        synapse_internal_base_url="https://synapse.example",
        synapse_ingestion_secret="test-secret",
        synapse_daily_request_delay_seconds=0.5,
        synapse_daily_request_jitter_seconds=0.0,
    )

    def summarizer_factory(_settings):
        raise AssertionError("READY disclosure must not instantiate the AI provider")

    def extractor(*_args, **_kwargs):
        raise AssertionError("READY disclosure must not be extracted again")

    result = SourceIngestionRunner(
        settings,
        source,
        client_factory=ReadyClient,
        summarizer_factory=summarizer_factory,
        extractor=extractor,
        require_external_id_prefix="manual-",
    ).run_window(start_at=start, end_at=end)

    assert result.processing_ok is True
    assert result.publish.disclosures_created == 0
    assert result.publish.disclosures_skipped_ready == 1
    assert result.publish.attachments_staged == 0
    assert result.publish.files_extracted == 0
    assert result.publish.documents_analyzed == 0
    assert result.publish.analyses_completed == 0
    client = ReadyClient.latest
    assert client is not None
    assert [name for name, _payload in client.calls].count("upsert_disclosures") == 1
