from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from idx_digest.sources.idx_website import (
    RECOVERY_LANE_NEWEST_HEAD,
    RECOVERY_LANE_OLDEST_SLOTS,
    _prioritize_candidates,
)


JAKARTA = ZoneInfo("Asia/Jakarta")


def _candidates(count: int):
    base = datetime(2026, 8, 27, 8, 0, tzinfo=JAKARTA)
    return [
        (f"row-{index}", {"pengumuman": {"Id2": f"row-{index}"}}, base + timedelta(minutes=index))
        for index in range(count)
    ]


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
