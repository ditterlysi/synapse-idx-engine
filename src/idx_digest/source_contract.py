from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


class SourceContractError(ValueError):
    """Raised when a source violates the normalized disclosure contract."""


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SourceContractError(f"{label} must be timezone-aware")


@dataclass(frozen=True)
class SourceAttachment:
    """A source-neutral attachment reference.

    `local_path` is used by offline/manual adapters. `source_url` is reserved for
    authorized network sources. At least one locator must be present.
    """

    filename: str
    local_path: Path | None = None
    source_url: str | None = None
    content_type: str | None = None
    sha256: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        filename = self.filename.strip()
        if not filename:
            raise SourceContractError("attachment filename must not be empty")
        object.__setattr__(self, "filename", filename)

        if self.local_path is None and not (self.source_url or "").strip():
            raise SourceContractError("attachment requires local_path or source_url")

        if self.sha256 is not None:
            digest = self.sha256.strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                raise SourceContractError("attachment sha256 must be a 64-character lowercase hex digest")
            object.__setattr__(self, "sha256", digest)


@dataclass(frozen=True)
class SourceDisclosure:
    """Normalized disclosure metadata independent of the upstream provider."""

    external_id: str
    ticker: str
    announced_at: datetime
    title: str
    attachments: tuple[SourceAttachment, ...] = ()
    subject: str | None = None
    disclosure_type: str | None = None
    source_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        external_id = self.external_id.strip()
        ticker = self.ticker.strip().upper()
        title = self.title.strip()
        if not external_id:
            raise SourceContractError("disclosure external_id must not be empty")
        if not ticker:
            raise SourceContractError("disclosure ticker must not be empty")
        if not title:
            raise SourceContractError("disclosure title must not be empty")
        _require_aware(self.announced_at, "disclosure announced_at")
        object.__setattr__(self, "external_id", external_id)
        object.__setattr__(self, "ticker", ticker)
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "attachments", tuple(self.attachments))


@dataclass(frozen=True)
class SourceWindowResult:
    """One normalized collection result plus explicit coverage evidence."""

    source_id: str
    requested_start: datetime
    requested_end: datetime
    disclosures: tuple[SourceDisclosure, ...] = ()
    complete: bool = False
    coverage_start: datetime | None = None
    coverage_end: datetime | None = None
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_aware(self.requested_start, "requested_start")
        _require_aware(self.requested_end, "requested_end")
        if self.requested_end <= self.requested_start:
            raise SourceContractError("requested_end must be greater than requested_start")

        source_id = self.source_id.strip()
        if not source_id:
            raise SourceContractError("source_id must not be empty")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "disclosures", tuple(self.disclosures))

        if (self.coverage_start is None) != (self.coverage_end is None):
            raise SourceContractError("coverage_start and coverage_end must be supplied together")
        if self.coverage_start is not None and self.coverage_end is not None:
            _require_aware(self.coverage_start, "coverage_start")
            _require_aware(self.coverage_end, "coverage_end")
            if self.coverage_end <= self.coverage_start:
                raise SourceContractError("coverage_end must be greater than coverage_start")
        if self.complete and not self.proves_requested_window():
            raise SourceContractError("complete source result must prove the entire requested window")

        ids = [item.external_id for item in self.disclosures]
        if len(ids) != len(set(ids)):
            raise SourceContractError("source result contains duplicate disclosure external_id values")
        outside = [
            item.external_id
            for item in self.disclosures
            if not (self.requested_start <= item.announced_at <= self.requested_end)
        ]
        if outside:
            raise SourceContractError(
                f"source result contains disclosures outside the requested window: {sorted(outside)!r}"
            )

    def proves_requested_window(self) -> bool:
        if self.coverage_start is None or self.coverage_end is None:
            return False
        return self.coverage_start <= self.requested_start and self.requested_end <= self.coverage_end


@runtime_checkable
class DisclosureSource(Protocol):
    """Boundary implemented by manual, licensed, or future approved sources."""

    source_id: str

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        """Return normalized disclosures and explicit completeness evidence."""
        ...
