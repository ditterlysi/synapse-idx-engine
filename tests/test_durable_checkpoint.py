from __future__ import annotations

from idx_digest.durable_checkpoint import SelectiveMemoryCheckpointStore
from idx_digest.sources.idx_website import IdxWebsiteCheckpoint


def test_selective_checkpoint_keeps_full_watermark_when_all_new_ids_are_ready() -> None:
    initial = IdxWebsiteCheckpoint(("A",), "2026-08-26T10:00:00+07:00")
    eligible = {"idx-web-B", "idx-web-C"}
    store = SelectiveMemoryCheckpointStore(initial, eligible_external_ids=eligible)

    store.save(IdxWebsiteCheckpoint(("A", "B", "C"), "2026-08-27T09:00:00+07:00"))

    assert store.saved == IdxWebsiteCheckpoint(
        ("A", "B", "C"),
        "2026-08-27T09:00:00+07:00",
    )


def test_selective_checkpoint_preserves_ready_ids_but_holds_watermark_on_partial_failure() -> None:
    initial = IdxWebsiteCheckpoint(("A",), "2026-08-26T10:00:00+07:00")
    eligible = {"idx-web-B"}
    store = SelectiveMemoryCheckpointStore(initial, eligible_external_ids=eligible)

    store.save(IdxWebsiteCheckpoint(("A", "B", "C"), "2026-08-27T09:00:00+07:00"))

    assert store.saved == IdxWebsiteCheckpoint(
        ("A", "B"),
        "2026-08-26T10:00:00+07:00",
    )


def test_selective_checkpoint_ignores_unrelated_external_id_namespaces() -> None:
    initial = IdxWebsiteCheckpoint((), None)
    eligible = {"manual-B", "idx-web-C"}
    store = SelectiveMemoryCheckpointStore(initial, eligible_external_ids=eligible)

    store.save(IdxWebsiteCheckpoint(("B", "C"), "2026-08-27T09:00:00+07:00"))

    assert store.saved == IdxWebsiteCheckpoint(("C",), None)
