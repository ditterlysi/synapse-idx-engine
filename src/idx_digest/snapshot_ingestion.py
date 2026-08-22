from __future__ import annotations

from typing import Any, Mapping

from .source_contract import SourceDisclosure
from .source_ingestion import SourceIngestionRunner
from .synapse_contract import DisclosureUpsertItem


def _nonempty_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _false_only(value: object) -> bool | None:
    return False if value is False else None


def _host_list(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple)):
        return None
    hosts: list[str] = []
    for item in value:
        normalized = _nonempty_string(item)
        if normalized:
            hosts.append(normalized.lower())
        if len(hosts) >= 20:
            break
    return list(dict.fromkeys(hosts)) or None


def snapshot_source_provenance(
    source_id: str,
    metadata: Mapping[str, Any],
) -> dict[str, object]:
    """Return only audited provenance fields for approved offline snapshot sources.

    Snapshot manifests can carry arbitrary user/source metadata. Persisting the
    whole mapping would risk leaking local paths, credentials, or unrelated
    annotations. This function intentionally copies a small fixed allowlist and
    drops every other key.
    """

    if source_id == "idx-public-snapshot":
        source_page = _nonempty_string(metadata.get("snapshotSourcePage"))
        captured_at = _nonempty_string(metadata.get("snapshotCapturedAt"))
        authoritative = _false_only(metadata.get("snapshotAuthoritativeCoverage"))
        result: dict[str, object] = {}
        if source_page:
            result["sourcePage"] = source_page
        if captured_at:
            result["capturedAt"] = captured_at
        if authoritative is not None:
            result["authoritativeCoverage"] = authoritative
        return result

    if source_id == "issuer-public-snapshot":
        source_page = _nonempty_string(metadata.get("issuerSnapshotSourcePage"))
        captured_at = _nonempty_string(metadata.get("issuerSnapshotCapturedAt"))
        authoritative = _false_only(metadata.get("issuerSnapshotAuthoritativeCoverage"))
        hosts = _host_list(metadata.get("issuerSnapshotHosts"))
        result = {}
        if source_page:
            result["sourcePage"] = source_page
        if captured_at:
            result["capturedAt"] = captured_at
        if authoritative is not None:
            result["authoritativeCoverage"] = authoritative
        if hosts:
            result["issuerHosts"] = hosts
        return result

    return {}


class SnapshotSourceIngestionRunner(SourceIngestionRunner):
    """Source runner that safely persists provenance for offline snapshot adapters."""

    @staticmethod
    def _disclosure_item(source_id: str, disclosure: SourceDisclosure) -> DisclosureUpsertItem:
        item = SourceIngestionRunner._disclosure_item(source_id, disclosure)
        provenance = snapshot_source_provenance(source_id, disclosure.metadata)
        if not provenance:
            return item
        raw_metadata = dict(item.raw_metadata)
        raw_metadata["sourceProvenance"] = provenance
        return item.model_copy(update={"raw_metadata": raw_metadata})
