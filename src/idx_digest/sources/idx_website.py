from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

from dateutil.parser import isoparse

from ..idx_polite_http import CURRENT_IDX_BASE_URL, OFFICIAL_IDX_HOSTS, PoliteFetchClient
from ..source_contract import SourceAttachment, SourceContractError, SourceDisclosure, SourceWindowResult

IDX_WEBSITE_SOURCE_ID = "idx-website"
IDX_WEBSITE_EXTERNAL_ID_PREFIX = "idx-web-"
IDX_ANNOUNCEMENT_ENDPOINT = "/primary/ListedCompany/GetAnnouncement"
IDX_DISCLOSURE_PAGE = f"{CURRENT_IDX_BASE_URL}/id/perusahaan-tercatat/keterbukaan-informasi"
CHECKPOINT_SCHEMA = "synapse-idx-website-checkpoint-v1"
MAX_WINDOW = timedelta(hours=48)


class IdxWebsiteSourceError(SourceContractError):
    """Raised when IDX website data cannot be safely normalized."""


@dataclass(frozen=True)
class IdxWebsiteCheckpoint:
    seen_ids: tuple[str, ...] = ()
    latest_announced_at: str | None = None


class FileCheckpointStore:
    """Small atomic checkpoint store for incremental website collection."""

    def __init__(self, path: Path | str, *, max_seen_ids: int = 1000):
        self.path = Path(path).expanduser().resolve()
        self.max_seen_ids = max_seen_ids

    def load(self) -> IdxWebsiteCheckpoint:
        if not self.path.exists():
            return IdxWebsiteCheckpoint()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise IdxWebsiteSourceError(f"could not read IDX website checkpoint: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") != CHECKPOINT_SCHEMA:
            raise IdxWebsiteSourceError("IDX website checkpoint schema is invalid")
        seen = payload.get("seenIds") or []
        if not isinstance(seen, list) or any(not isinstance(item, str) or not item for item in seen):
            raise IdxWebsiteSourceError("IDX website checkpoint seenIds is invalid")
        latest = payload.get("latestAnnouncedAt")
        if latest is not None and not isinstance(latest, str):
            raise IdxWebsiteSourceError("IDX website checkpoint latestAnnouncedAt is invalid")
        return IdxWebsiteCheckpoint(tuple(seen[-self.max_seen_ids :]), latest)

    def save(self, checkpoint: IdxWebsiteCheckpoint) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": CHECKPOINT_SCHEMA,
            "seenIds": list(checkpoint.seen_ids[-self.max_seen_ids :]),
            "latestAnnouncedAt": checkpoint.latest_announced_at,
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)


def _aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise IdxWebsiteSourceError(f"{label} must be timezone-aware")


def _announcement_time(value: object, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IdxWebsiteSourceError("IDX announcement is missing TglPengumuman")
    try:
        parsed = isoparse(value.strip())
    except (TypeError, ValueError) as exc:
        raise IdxWebsiteSourceError(f"invalid IDX TglPengumuman: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed


def _official_attachment_url(base_url: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdxWebsiteSourceError("IDX attachment is missing FullSavePath")
    url = urljoin(base_url.rstrip("/") + "/", value.strip())
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in OFFICIAL_IDX_HOSTS:
        raise IdxWebsiteSourceError(f"IDX attachment URL is not on an official IDX HTTPS host: {url!r}")
    return parsed._replace(netloc="www.idx.id").geturl()


def _safe_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix or ""):
        return suffix
    return ".bin"


def _display_filename(attachment: dict[str, Any]) -> str:
    original = str(attachment.get("OriginalFilename") or "").strip()
    pdf_name = str(attachment.get("PDFFilename") or "").strip()
    candidate = original or pdf_name or "idx-attachment.bin"
    candidate = candidate.replace("\\", "/").split("/")[-1].strip()
    return candidate or "idx-attachment.bin"


class IdxWebsiteSource:
    """HTTP-only incremental source for IDX public disclosures.

    The adapter only reads the public GetAnnouncement JSON endpoint and official
    IDX attachment URLs. It stages new attachments locally for the existing
    source-neutral extraction/AI runner. Coverage remains non-authoritative.
    """

    source_id = IDX_WEBSITE_SOURCE_ID

    def __init__(
        self,
        client: PoliteFetchClient,
        *,
        checkpoint_store: FileCheckpointStore,
        staging_dir: Path | str,
        page_size: int = 50,
        max_pages: int = 10,
        timezone_name: str = "Asia/Jakarta",
    ) -> None:
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if max_pages < 1 or max_pages > 20:
            raise ValueError("max_pages must be between 1 and 20")
        self.client = client
        self.checkpoint_store = checkpoint_store
        self.staging_dir = Path(staging_dir).expanduser().resolve()
        self.page_size = page_size
        self.max_pages = max_pages
        self.timezone = ZoneInfo(timezone_name)
        self._pending_checkpoint: IdxWebsiteCheckpoint | None = None

    def _stage_attachment(self, raw: dict[str, Any]) -> tuple[SourceAttachment, bool]:
        url = _official_attachment_url(self.client.base_url, raw.get("FullSavePath"))
        filename = _display_filename(raw)
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        local_path = self.staging_dir / f"{cache_key}{_safe_suffix(filename)}"
        cache_hit = local_path.exists() and local_path.is_file() and local_path.stat().st_size > 0
        if not cache_hit:
            self.client.download(url, local_path)
        return (
            SourceAttachment(
                filename=filename,
                local_path=local_path,
                source_url=url,
                metadata={"idxIsAttachment": bool(raw.get("IsAttachment"))},
            ),
            cache_hit,
        )

    def collect_window(self, *, start_at: datetime, end_at: datetime) -> SourceWindowResult:
        _aware(start_at, "start_at")
        _aware(end_at, "end_at")
        if end_at <= start_at:
            raise IdxWebsiteSourceError("end_at must be later than start_at")
        if end_at - start_at > MAX_WINDOW:
            raise IdxWebsiteSourceError("IDX website collection window must be 48 hours or less")

        checkpoint = self.checkpoint_store.load()
        seen_checkpoint = set(checkpoint.seen_ids)
        ids_observed: list[str] = []
        candidates: list[tuple[str, dict[str, Any], datetime]] = []
        ids_in_run: set[str] = set()
        pages_fetched = 0
        replies_seen = 0
        reported_total: int | None = None

        offset = 0
        while pages_fetched < self.max_pages:
            payload = self.client.get_json(
                IDX_ANNOUNCEMENT_ENDPOINT,
                params={
                    "kodeEmiten": "",
                    "emitenType": "*",
                    "indexFrom": offset,
                    "pageSize": self.page_size,
                    "dateFrom": start_at.astimezone(self.timezone).strftime("%Y%m%d"),
                    "dateTo": end_at.astimezone(self.timezone).strftime("%Y%m%d"),
                    "lang": "id",
                    "keyword": "",
                },
            )
            pages_fetched += 1
            replies = payload.get("Replies")
            if not isinstance(replies, list):
                raise IdxWebsiteSourceError("IDX GetAnnouncement response does not contain Replies[]")
            try:
                reported_total = int(payload.get("ResultCount") or len(replies))
            except (TypeError, ValueError):
                reported_total = None

            replies_seen += len(replies)
            for item in replies:
                if not isinstance(item, dict):
                    continue
                announcement = item.get("pengumuman")
                if not isinstance(announcement, dict):
                    continue
                raw_id = str(announcement.get("Id2") or "").strip()
                if not raw_id or raw_id in ids_in_run:
                    continue
                ids_in_run.add(raw_id)
                ids_observed.append(raw_id)
                announced_at = _announcement_time(announcement.get("TglPengumuman"), self.timezone)
                if not (start_at <= announced_at <= end_at):
                    continue
                if raw_id in seen_checkpoint:
                    continue
                candidates.append((raw_id, item, announced_at))

            if len(replies) < self.page_size:
                break
            offset += len(replies)
            if reported_total is not None and offset >= reported_total:
                break
        else:
            if reported_total is None or replies_seen < reported_total:
                raise IdxWebsiteSourceError(
                    f"IDX website collection reached max_pages={self.max_pages} before exhausting the requested window"
                )

        disclosures: list[SourceDisclosure] = []
        attachment_downloads = 0
        attachment_cache_hits = 0
        newest_at: datetime | None = None

        for raw_id, item, announced_at in sorted(candidates, key=lambda row: row[2]):
            announcement = item.get("pengumuman") or {}
            ticker = str(announcement.get("Kode_Emiten") or "").strip().upper()
            title = str(announcement.get("JudulPengumuman") or "").strip()
            if not ticker or not title:
                raise IdxWebsiteSourceError(f"IDX announcement {raw_id!r} is missing ticker/title")

            attachments_raw = item.get("attachments") or []
            if not isinstance(attachments_raw, list):
                raise IdxWebsiteSourceError(f"IDX announcement {raw_id!r} attachments must be a list")
            attachments: list[SourceAttachment] = []
            for attachment_raw in attachments_raw:
                if not isinstance(attachment_raw, dict) or not attachment_raw.get("FullSavePath"):
                    continue
                attachment, cache_hit = self._stage_attachment(attachment_raw)
                attachments.append(attachment)
                if cache_hit:
                    attachment_cache_hits += 1
                else:
                    attachment_downloads += 1

            disclosures.append(
                SourceDisclosure(
                    external_id=f"{IDX_WEBSITE_EXTERNAL_ID_PREFIX}{raw_id}",
                    ticker=ticker,
                    announced_at=announced_at,
                    title=title,
                    attachments=tuple(attachments),
                    subject=str(announcement.get("PerihalPengumuman") or "").strip() or None,
                    disclosure_type=str(announcement.get("JenisPengumuman") or "").strip() or None,
                    source_url=IDX_DISCLOSURE_PAGE,
                    metadata={
                        "idxRawId": raw_id,
                        "idxAnnouncementNo": str(announcement.get("NoPengumuman") or "").strip() or None,
                        "idxFormId": str(announcement.get("Form_Id") or "").strip() or None,
                        "idxCreatedDate": str(announcement.get("CreatedDate") or "").strip() or None,
                    },
                )
            )
            if newest_at is None or announced_at > newest_at:
                newest_at = announced_at

        merged_seen = list(dict.fromkeys([*checkpoint.seen_ids, *ids_observed]))
        latest_value = checkpoint.latest_announced_at
        if newest_at is not None:
            latest_value = newest_at.isoformat()
        self._pending_checkpoint = IdxWebsiteCheckpoint(tuple(merged_seen), latest_value)

        diagnostics = {
            "adapter": self.source_id,
            "networkAccess": True,
            "transport": "http-only",
            "sourceRequests": self.client.request_count,
            "pagesFetched": pages_fetched,
            "repliesSeen": replies_seen,
            "reportedTotal": reported_total,
            "checkpointSeenIds": len(seen_checkpoint),
            "disclosuresNew": len(disclosures),
            "attachmentDownloads": attachment_downloads,
            "attachmentCacheHits": attachment_cache_hits,
            "downloadedBytes": self.client.downloaded_bytes,
            "authoritativeCoverageAllowed": False,
        }
        return SourceWindowResult(
            source_id=self.source_id,
            requested_start=start_at,
            requested_end=end_at,
            disclosures=tuple(disclosures),
            complete=False,
            coverage_start=None,
            coverage_end=None,
            diagnostics=diagnostics,
        )

    def commit_checkpoint(self) -> None:
        if self._pending_checkpoint is None:
            raise IdxWebsiteSourceError("no successful collection result is available to checkpoint")
        self.checkpoint_store.save(self._pending_checkpoint)