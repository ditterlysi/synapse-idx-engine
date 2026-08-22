from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from dateutil.parser import isoparse

from ..source_contract import SourceAttachment, SourceDisclosure, SourceWindowResult
from .manual_manifest import ManualManifestError, ManualManifestSource

ISSUER_PUBLIC_SNAPSHOT_KIND = "issuer-public-snapshot-v1"
ISSUER_PUBLIC_EXTERNAL_ID_PREFIX = "issuer-public-"
_TICKER_RE = re.compile(r"^[A-Z0-9]{1,12}$")


class IssuerPublicSnapshotError(ManualManifestError):
    """Raised when an offline issuer-public snapshot is malformed or unsafe."""


def _aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IssuerPublicSnapshotError(f"{label} must be a non-empty ISO timestamp")
    try:
        parsed = isoparse(value.strip())
    except (TypeError, ValueError) as exc:
        raise IssuerPublicSnapshotError(f"{label} must be a valid ISO timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IssuerPublicSnapshotError(f"{label} must include an explicit timezone offset or Z")
    return parsed


def _issuer_ticker(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IssuerPublicSnapshotError("metadata.issuerTicker must be a non-empty ticker")
    ticker = value.strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise IssuerPublicSnapshotError("metadata.issuerTicker must contain only A-Z, 0-9 and be at most 12 characters")
    return ticker


def _issuer_host(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IssuerPublicSnapshotError(f"{label} must be a non-empty hostname")
    host = value.strip().lower().rstrip(".")
    if "://" in host or "/" in host or "@" in host or ":" in host:
        raise IssuerPublicSnapshotError(f"{label} must be a hostname without scheme, path, credentials, or port")
    if host == "localhost" or "." not in host:
        raise IssuerPublicSnapshotError(f"{label} must be a public issuer hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise IssuerPublicSnapshotError(f"{label} may not be an IP address")
    return host


def _issuer_hosts(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise IssuerPublicSnapshotError("metadata.issuerHosts must be a non-empty JSON array")
    hosts = tuple(dict.fromkeys(_issuer_host(item, f"metadata.issuerHosts[{index}]") for index, item in enumerate(value)))
    return hosts


def _host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    normalized = host.lower().rstrip(".")
    return any(normalized == allowed or normalized.endswith(f".{allowed}") for allowed in allowed_hosts)


def _issuer_https_url(value: object, label: str, allowed_hosts: tuple[str, ...]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IssuerPublicSnapshotError(f"{label} must be a non-empty HTTPS URL")
    raw = value.strip()
    parsed = urlparse(raw)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise IssuerPublicSnapshotError(f"{label} must be an HTTPS URL without credentials")
    if parsed.port not in (None, 443):
        raise IssuerPublicSnapshotError(f"{label} may not use a non-HTTPS port")
    if not _host_allowed(hostname, allowed_hosts):
        raise IssuerPublicSnapshotError(f"{label} host must match metadata.issuerHosts")
    return raw


class IssuerPublicSnapshotSource:
    """Offline wrapper for disclosures captured from an issuer's public website.

    The adapter performs zero network requests. Files must already be staged next
    to the manifest and are parsed by ``ManualManifestSource``. Root metadata
    binds one manifest to one issuer ticker and an explicit HTTPS host allowlist.
    Public issuer pages are useful provenance but cannot prove complete exchange
    coverage, so authoritative coverage is always disabled.
    """

    source_id = "issuer-public-snapshot"

    def __init__(self, manifest_path: Path | str):
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.delegate = ManualManifestSource(self.manifest_path, allow_complete_attestation=False)

    def _snapshot_metadata(self) -> tuple[str, datetime, str, tuple[str, ...]]:
        if not self.manifest_path.exists() or not self.manifest_path.is_file():
            raise IssuerPublicSnapshotError(f"manifest does not exist: {self.manifest_path}")
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IssuerPublicSnapshotError(f"could not read issuer snapshot manifest: {exc}") from exc
        if not isinstance(payload, dict):
            raise IssuerPublicSnapshotError("manifest must be a JSON object")
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            raise IssuerPublicSnapshotError("metadata must be a JSON object")
        if metadata.get("sourceKind") != ISSUER_PUBLIC_SNAPSHOT_KIND:
            raise IssuerPublicSnapshotError(
                f"metadata.sourceKind must equal {ISSUER_PUBLIC_SNAPSHOT_KIND!r}"
            )

        coverage = payload.get("coverage")
        if isinstance(coverage, dict) and coverage.get("complete") is True:
            raise IssuerPublicSnapshotError("issuer public snapshots may not claim authoritative coverage")

        ticker = _issuer_ticker(metadata.get("issuerTicker"))
        hosts = _issuer_hosts(metadata.get("issuerHosts"))
        source_page = _issuer_https_url(metadata.get("sourcePage"), "metadata.sourcePage", hosts)
        captured_at = _aware_timestamp(metadata.get("capturedAt"), "metadata.capturedAt")
        return source_page, captured_at, ticker, hosts

    @staticmethod
    def _normalize_attachment(
        attachment: SourceAttachment,
        *,
        allowed_hosts: tuple[str, ...],
    ) -> SourceAttachment:
        if not attachment.source_url:
            raise IssuerPublicSnapshotError(
                "issuer public snapshot attachments require sourceUrl for official-document provenance"
            )
        source_url = _issuer_https_url(
            attachment.source_url,
            "disclosure attachment sourceUrl",
            allowed_hosts,
        )
        return replace(attachment, source_url=source_url)

    @classmethod
    def _normalize_disclosure(
        cls,
        disclosure: SourceDisclosure,
        *,
        source_page: str,
        captured_at: datetime,
        issuer_ticker: str,
        allowed_hosts: tuple[str, ...],
    ) -> SourceDisclosure:
        if not disclosure.external_id.startswith(ISSUER_PUBLIC_EXTERNAL_ID_PREFIX):
            raise IssuerPublicSnapshotError(
                f"issuer public snapshot externalId must start with {ISSUER_PUBLIC_EXTERNAL_ID_PREFIX!r}"
            )
        if disclosure.ticker != issuer_ticker:
            raise IssuerPublicSnapshotError(
                f"issuer public snapshot ticker {disclosure.ticker!r} does not match metadata.issuerTicker {issuer_ticker!r}"
            )
        source_url = (
            _issuer_https_url(disclosure.source_url, "disclosure sourceUrl", allowed_hosts)
            if disclosure.source_url
            else source_page
        )
        attachments = tuple(
            cls._normalize_attachment(item, allowed_hosts=allowed_hosts)
            for item in disclosure.attachments
        )
        metadata = {
            **dict(disclosure.metadata),
            "issuerSnapshotSourcePage": source_page,
            "issuerSnapshotCapturedAt": captured_at.isoformat(),
            "issuerSnapshotAuthoritativeCoverage": False,
            "issuerSnapshotHosts": list(allowed_hosts),
        }
        return replace(
            disclosure,
            source_url=source_url,
            attachments=attachments,
            metadata=metadata,
        )

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        source_page, captured_at, ticker, hosts = self._snapshot_metadata()
        delegated = self.delegate.collect_window(start_at=start_at, end_at=end_at)
        disclosures = tuple(
            self._normalize_disclosure(
                item,
                source_page=source_page,
                captured_at=captured_at,
                issuer_ticker=ticker,
                allowed_hosts=hosts,
            )
            for item in delegated.disclosures
        )
        diagnostics = {
            **dict(delegated.diagnostics),
            "adapter": self.source_id,
            "delegateAdapter": delegated.source_id,
            "networkAccess": False,
            "officialIssuerSourcePage": source_page,
            "issuerTicker": ticker,
            "issuerHosts": list(hosts),
            "snapshotCapturedAt": captured_at.isoformat(),
            "authoritativeCoverageAllowed": False,
        }
        return SourceWindowResult(
            source_id=self.source_id,
            requested_start=delegated.requested_start,
            requested_end=delegated.requested_end,
            disclosures=disclosures,
            complete=False,
            coverage_start=None,
            coverage_end=None,
            diagnostics=diagnostics,
        )
