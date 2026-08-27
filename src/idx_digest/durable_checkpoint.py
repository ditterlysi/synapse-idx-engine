from __future__ import annotations

from dataclasses import dataclass, field

from .sources.idx_website import IDX_WEBSITE_EXTERNAL_ID_PREFIX, IdxWebsiteCheckpoint


@dataclass
class MemoryCheckpointStore:
    """Checkpoint store used to bridge an ephemeral runner to Synapse state.

    The source can keep its existing load/save contract. `save()` only captures
    the post-processing checkpoint in memory; the CLI persists it through the
    authenticated Synapse source-state API after processing succeeds.
    """

    initial: IdxWebsiteCheckpoint
    saved: IdxWebsiteCheckpoint | None = None

    def load(self) -> IdxWebsiteCheckpoint:
        return self.initial

    def save(self, checkpoint: IdxWebsiteCheckpoint) -> None:
        self.saved = checkpoint


@dataclass
class SelectiveMemoryCheckpointStore(MemoryCheckpointStore):
    """Keep only source IDs whose Synapse processing reached READY.

    The IDX source stages attachments before the source-neutral runner performs
    extraction and AI analysis. On a partially failed run the source therefore
    knows more staged IDs than are safe to checkpoint. This store receives a
    live set populated by the Synapse client only for disclosures already READY
    or whose analysis commit succeeded. If any newly staged source ID is not in
    that set, the time watermark stays at the previous durable value so the
    unresolved disclosure remains discoverable on the next run.
    """

    eligible_external_ids: set[str] = field(default_factory=set)
    external_id_prefix: str = IDX_WEBSITE_EXTERNAL_ID_PREFIX

    def save(self, checkpoint: IdxWebsiteCheckpoint) -> None:
        initial_ids = list(self.initial.seen_ids)
        initial_seen = set(initial_ids)
        new_raw_ids = [item for item in checkpoint.seen_ids if item not in initial_seen]
        eligible_raw_ids = {
            item[len(self.external_id_prefix) :]
            for item in self.eligible_external_ids
            if item.startswith(self.external_id_prefix) and len(item) > len(self.external_id_prefix)
        }
        completed_new_ids = [item for item in new_raw_ids if item in eligible_raw_ids]
        merged_seen = list(dict.fromkeys([*initial_ids, *completed_new_ids]))
        all_new_completed = all(item in eligible_raw_ids for item in new_raw_ids)
        latest_announced_at = (
            checkpoint.latest_announced_at if all_new_completed else self.initial.latest_announced_at
        )
        self.saved = IdxWebsiteCheckpoint(tuple(merged_seen[-1000:]), latest_announced_at)
