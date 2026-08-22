from __future__ import annotations

from dataclasses import dataclass

from .sources.idx_website import IdxWebsiteCheckpoint


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
