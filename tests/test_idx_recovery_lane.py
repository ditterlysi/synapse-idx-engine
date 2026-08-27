from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from idx_digest.sources.idx_website import (
    RECOVERY_LANE_NEWEST_HEAD,
    RECOVERY_LANE_OLDEST_SLOTS,
    FileCheckpointStore,
    IdxWebsiteSource,
    IdxWebsiteSourceError,
    _prioritize_candidates,
)


JAKARTA = ZoneInfo("Asia/Jakarta")


def _candidates(count: int):
    base = datetime(2026, 8, 27, 8, 0, tzinfo=JAKARTA)
    return [
        (f"row-{index}", {"pengumuman": {"Id2": f"row-{index}"}}, base + timedelta(minutes=index))
        for index in range(count)
    ]


def _metadata_row(row_id: str):
    return {"pengumuman": {"Id2": row_id}}


class DuplicateWidePageClient:
    base_url = "https://www.idx.co.id"

    def __init__(self, *, short_raw_page: bool = False):
        self.short_raw_page = short_raw_page
        self.request_count = 0
        self.downloaded_bytes = 0

    def get_json(self, path, *, params):
        self.request_count += 1
        assert path == "/primary/ListedCompany/GetAnnouncement"
        if params["pageSize"] == 2:
            return {
                "ResultCount": 3,
                "Replies": [_metadata_row("row-a"), _metadata_row("row-b")],
            }
        replies = [_metadata_row("row-a"), _metadata_row("row-b")]
        if not self.short_raw_page:
            replies.append(_metadata_row("row-b"))
        return {"ResultCount": 3, "Replies": replies}


def test_recovery_lane_keeps_newest_head_then_promotes_oldest_backlog():
    candidates = _candidates(12)

    ordered, recovery_ids = _prioritize_candidates(candidates)

    ordered_ids = [row[0] for row in ordered]
    assert ordered_ids[:RECOVERY_LANE_NEWEST_HEAD] == [
        "row-11",
        "row-10",
        "row-9",
        "row-8",
        "row-7",
        "row-6",
    ]
    assert recovery_ids == ["row-0", "row-1", "row-2"]
    assert ordered_ids[
        RECOVERY_LANE_NEWEST_HEAD : RECOVERY_LANE_NEWEST_HEAD + RECOVERY_LANE_OLDEST_SLOTS
    ] == recovery_ids
    assert ordered_ids[-3:] == ["row-5", "row-4", "row-3"]
    assert sorted(ordered_ids) == sorted(row[0] for row in candidates)


def test_small_candidate_set_stays_pure_newest_first():
    candidates = _candidates(RECOVERY_LANE_NEWEST_HEAD)

    ordered, recovery_ids = _prioritize_candidates(candidates)

    assert recovery_ids == []
    assert [row[0] for row in ordered] == ["row-5", "row-4", "row-3", "row-2", "row-1", "row-0"]


def test_wide_probe_accepts_reported_rows_when_duplicate_ids_are_collapsed(tmp_path):
    client = DuplicateWidePageClient()
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoint.json"),
        staging_dir=tmp_path / "cache",
        page_size=2,
    )

    items, diagnostics = source._collect_metadata(
        start_at=datetime(2026, 8, 27, 8, 0, tzinfo=JAKARTA),
        end_at=datetime(2026, 8, 27, 9, 0, tzinfo=JAKARTA),
    )

    assert [item["pengumuman"]["Id2"] for item in items] == ["row-a", "row-b"]
    assert diagnostics["reportedTotal"] == 3
    assert diagnostics["metadataRowsRaw"] == 3
    assert diagnostics["metadataRowsCollected"] == 2
    assert diagnostics["metadataDuplicateRowsCollapsed"] == 1


def test_wide_probe_still_fails_closed_when_raw_rows_are_actually_missing(tmp_path):
    client = DuplicateWidePageClient(short_raw_page=True)
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(tmp_path / "checkpoint.json"),
        staging_dir=tmp_path / "cache",
        page_size=2,
    )

    with pytest.raises(IdxWebsiteSourceError, match="received 2 raw rows .* of 3 reported rows"):
        source._collect_metadata(
            start_at=datetime(2026, 8, 27, 8, 0, tzinfo=JAKARTA),
            end_at=datetime(2026, 8, 27, 9, 0, tzinfo=JAKARTA),
        )
