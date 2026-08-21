"""Source adapters for normalized Synapse disclosure ingestion."""

from .manual_manifest import MANUAL_MANIFEST_SCHEMA, ManualManifestSource

__all__ = ["MANUAL_MANIFEST_SCHEMA", "ManualManifestSource"]
