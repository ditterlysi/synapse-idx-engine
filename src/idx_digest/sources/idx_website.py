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

from ..attachment_selector import classify_attachments
from ..idx_polite_http import (
    CURRENT_IDX_BASE_URL,
    OFFICIAL_IDX_HOSTS,
    IdxRequestBudgetExceededError,
    IdxResourceNotFoundError,
    PoliteFetchClient,
)
from ..source_contract import SourceAttachment, SourceContractError, SourceDisclosure, SourceWindowResult

IDX_WEBSITE_SOURCE_ID = "idx-website"
IDX_WEBSITE_EXTERNAL_ID_PREFIX = "idx-web-"
IDX_ANNOUNCEMENT_ENDPOINT = "/primary/ListedCompany/GetAnnouncement"
IDX_DISCLOSURE_PAGE = f"{CURRENT_IDX_BASE_URL}/id/perusahaan-tercatat/keterbukaan-informasi"
CHECKPOINT_SCHEMA = "synapse-idx-website-checkpoint-v1"
MAX_WINDOW = timedelta(hours=48)
RECOVERY_LANE_NEWEST_HEAD = 6
RECOVERY_LANE_OLDEST_SLOTS = 3
NON_STOCK_PRODUCT_FLAGS = (
    "EfekEmiten_ETF",
    "EfekEmiten_DIRE",
    "EfekEmiten_DINFRA",
    "EfekEmiten_EBA",
    "EfekEmiten_SPEI",
)


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


def _raw_id(item: dict[str, Any]) -> str:
    announcement = item.get("pengumuman")
    if not isinstance(announcement, dict):
        return ""
    return str(announcement.get("Id2") or "").strip()


def _dedupe_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = _raw_id(item)
        if item_id and item_id not in unique:
            unique[item_id] = item
    return list(unique.values())


def _prioritize_candidates(
    candidates: list[tuple[str, dict[str, Any], datetime]],
) -> tuple[list[tuple[str, dict[str, Any], datetime]], list[str]]:
    """Keep newest-first service while reserving deterministic backlog retries.

    The first few newest disclosures remain the real-time priority. When there is
    a deeper backlog, a small set of the oldest uncheckpointed candidates is then
    promoted ahead of the remaining newest-first queue so a full request budget
    cannot starve the same old disclosures forever.
    """

    newest_first = sorted(candidates, key=lambda row: row[2], reverse=True)
    if len(newest_first) <= RECOVERY_LANE_NEWEST_HEAD:
        return newest_first, []

    recovery_count = min(
        RECOVERY_LANE_OLDEST_SLOTS,
        len(newest_first) - RECOVERY_LANE_NEWEST_HEAD,
    )
    if recovery_count <= 0:
        return newest_first, []

    head = newest_first[:RECOVERY_LANE_NEWEST_HEAD]
    recovery = list(reversed(newest_first[-recovery_count:]))
    remaining = newest_first[RECOVERY_LANE_NEWEST_HEAD:-recovery_count]
    return [*head, *recovery, *remaining], [row[0] for row in recovery]


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
        wide_page_size: int = 200,
        max_wide_page_size: int = 1000,
        timezone_name: str = "Asia/Jakarta",
    ) -> None:
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be between 1 and 100")
        if max_pages < 1 or max_pages > 20:
            raise ValueError("max_pages must be between 1 and 20")
        if wide_page_size < page_size:
            raise ValueError("wide_page_size must be at least page_size")
        if max_wide_page_size < wide_page_size:
            raise ValueError("max_wide_page_size must be at least wide_page_size")
        if max_wide_page_size > 2000:
            raise ValueError("max_wide_page_size must not exceed 2000")
        self.client = client
        self.checkpoint_store = checkpoint_store
        self.staging_dir = Path(staging_dir).expanduser().resolve()
        self.page_size = page_size
        self.max_pages = max_pages
        self.wide_page_size = wide_page_size
        self.max_wide_page_size = max_wide_page_size
        self.timezone = ZoneInfo(timezone_name)
        self._pending_checkpoint: IdxWebsiteCheckpoint | None = None

    def _metadata_params(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        index_from: int,
        page_size: int,
    ) -> dict[str, Any]:
        return {
            "kodeEmiten": "",
            "emitenType": "*",
            "indexFrom": index_from,
            "pageSize": page_size,
            "dateFrom": start_at.astimezone(self.timezone).strftime("%Y%m%d"),
            "dateTo": end_at.astimezone(self.timezone).strftime("%Y%m%d"),
            "lang": "id",
            "keyword": "",
        }

    def _fetch_metadata_page(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
        index_from: int,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], int | None]:
        payload = self.client.get_json(
            IDX_ANNOUNCEMENT_ENDPOINT,
            params=self._metadata_params(
                start_at=start_at,
                end_at=end_at,
                index_from=index_from,
                page_size=page_size,
            ),
        )
        replies = payload.get("Replies")
        if not isinstance(replies, list):
            raise IdxWebsiteSourceError("IDX GetAnnouncement response does not contain Replies[]")
        normalized = [item for item in replies if isinstance(item, dict)]
        try:
            reported_total = int(payload.get("ResultCount")) if payload.get("ResultCount") is not None else None
        except (TypeError, ValueError):
            reported_total = None
        return normalized, reported_total

    def _collect_metadata(
        self,
        *,
        start_at: datetime,
        end_at: datetime,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        pages_fetched = 0
        first_page, reported_total = self._fetch_metadata_page(
            start_at=start_at,
            end_at=end_at,
            index_from=0,
            page_size=self.page_size,
        )
        pages_fetched += 1
        collected = _dedupe_items(first_page)

        if reported_total is not None and reported_total <= len(collected):
            return collected, {
                "pagesFetched": pages_fetched,
                "reportedTotal": reported_total,
                "paginationStrategy": "single-page",
                "metadataRowsCollected": len(collected),
            }
        if len(first_page) < self.page_size and (reported_total is None or reported_total <= len(collected)):
            return collected, {
                "pagesFetched": pages_fetched,
                "reportedTotal": reported_total,
                "paginationStrategy": "single-page",
                "metadataRowsCollected": len(collected),
            }

        if reported_total is not None and len(collected) < reported_total <= self.max_wide_page_size:
            probe_size = min(self.max_wide_page_size, max(self.wide_page_size, reported_total))
            probe_page, probe_total = self._fetch_metadata_page(
                start_at=start_at,
                end_at=end_at,
                index_from=0,
                page_size=probe_size,
            )
            pages_fetched += 1
            probe_items = _dedupe_items(probe_page)
            effective_total = probe_total if probe_total is not None else reported_total
            if effective_total is not None and len(probe_items) < effective_total:
                raise IdxWebsiteSourceError(
                    "IDX metadata wide-page probe remained incomplete: "
                    f"collected {len(probe_items)} of {effective_total} reported rows; collector stopped"
                )
            return probe_items, {
                "pagesFetched": pages_fetched,
                "reportedTotal": effective_total,
                "paginationStrategy": "wide-page-probe",
                "widePageProbeSize": probe_size,
                "metadataRowsCollected": len(probe_items),
            }

        offset = len(first_page)
        all_items = list(first_page)
        complete = False
        while pages_fetched < self.max_pages:
            page, page_total = self._fetch_metadata_page(
                start_at=start_at,
                end_at=end_at,
                index_from=offset,
                page_size=self.page_size,
            )
            pages_fetched += 1
            if reported_total is None and page_total is not None:
                reported_total = page_total
            if not page:
                break
            all_items.extend(page)
            unique_items = _dedupe_items(all_items)
            if reported_total is not None and len(unique_items) >= reported_total:
                all_items = unique_items
                complete = True
                break
            if len(page) < self.page_size:
                all_items = unique_items
                complete = reported_total is None or len(unique_items) >= reported_total
                break
            offset += len(page)

        unique_items = _dedupe_items(all_items)
        if reported_total is None:
            complete = complete or (pages_fetched < self.max_pages)
        elif len(unique_items) >= reported_total:
            complete = True

        if not complete:
            expected = reported_total if reported_total is not None else "unknown"
            raise IdxWebsiteSourceError(
                "IDX metadata pagination is incomplete: "
                f"collected {len(unique_items)} of {expected} reported rows; collector stopped without fan-out"
            )

        return unique_items, {
            "pagesFetched": pages_fetched,
            "reportedTotal": reported_total,
            "paginationStrategy": "offset-pagination",
            "metadataRowsCollected": len(unique_items),
        }

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
        metadata_items, pagination = self._collect_metadata(start_at=start_at, end_at=end_at)

        ids_in_window: list[str] = []
        already_seen_in_window = 0
        newest_seen_at: datetime | None = None
        candidates: list[tuple[str, dict[str, Any], datetime]] = []
        for item in metadata_items:
            announcement = item.get("pengumuman")
            if not isinstance(announcement, dict):
                continue
            raw_id = str(announcement.get("Id2") or "").strip()
            if not raw_id:
                continue
            announced_at = _announcement_time(announcement.get("TglPengumuman"), self.timezone)
            if not (start_at <= announced_at <= end_at):
                continue
            ids_in_window.append(raw_id)
            if raw_id in seen_checkpoint:
                already_seen_in_window += 1
                if newest_seen_at is None or announced_at > newest_seen_at:
                    newest_seen_at = announced_at
                continue
            candidates.append((raw_id, item, announced_at))

        ordered_candidates, recovery_lane_ids = _prioritize_candidates(candidates)
        disclosures: list[SourceDisclosure] = []
        processed_ids: list[str] = []
        nonissuer_row_ids: list[str] = []
        unsupported_ticker_row_ids: list[str] = []
        nonstock_product_row_ids: list[str] = []
        unavailable_attachment_row_ids: list[str] = []
        no_selected_attachment_row_ids: list[str] = []
        attachment_downloads = 0
        attachment_cache_hits = 0
        attachments_considered = 0
        attachments_selected = 0
        attachments_skipped_by_policy = 0
        attachment_policy_skips: list[dict[str, str]] = []
        request_budget_deferred = False
        request_budget_deferred_row_id: str | None = None
        newest_at: datetime | None = None

        for raw_id, item, announced_at in ordered_candidates:
            announcement = item.get("pengumuman") or {}
            ticker = str(announcement.get("Kode_Emiten") or "").strip().upper()
            if not ticker:
                if len(nonissuer_row_ids) < 20:
                    nonissuer_row_ids.append(raw_id)
                continue
            if re.fullmatch(r"[A-Z0-9.]{1,10}", ticker) is None:
                if len(unsupported_ticker_row_ids) < 20:
                    unsupported_ticker_row_ids.append(raw_id)
                continue
            if any(bool(announcement.get(flag)) for flag in NON_STOCK_PRODUCT_FLAGS):
                if len(nonstock_product_row_ids) < 20:
                    nonstock_product_row_ids.append(raw_id)
                continue

            title = str(announcement.get("JudulPengumuman") or "").strip()
            if not title:
                raise IdxWebsiteSourceError(f"IDX issuer announcement {raw_id!r} is missing title")

            attachments_raw = item.get("attachments") or []
            if not isinstance(attachments_raw, list):
                raise IdxWebsiteSourceError(f"IDX announcement {raw_id!r} attachments must be a list")
            valid_attachment_rows = [
                attachment
                for attachment in attachments_raw
                if isinstance(attachment, dict) and attachment.get("FullSavePath")
            ]
            decisions = classify_attachments(title, valid_attachment_rows, policy="smart")
            selected_attachment_rows = [decision.attachment for decision in decisions if decision.selected]
            attachments_considered += len(decisions)
            attachments_selected += len(selected_attachment_rows)
            attachments_skipped_by_policy += sum(not decision.selected for decision in decisions)
            for decision in decisions:
                if decision.selected or len(attachment_policy_skips) >= 20:
                    continue
                attachment_policy_skips.append(
                    {
                        "rowId": raw_id,
                        "filename": decision.filename,
                        "category": decision.category,
                        "reason": decision.reason,
                    }
                )

            if valid_attachment_rows and not selected_attachment_rows:
                if len(no_selected_attachment_row_ids) < 20:
                    no_selected_attachment_row_ids.append(raw_id)
                continue

            attachments: list[SourceAttachment] = []
            attachment_unavailable = False
            for attachment_raw in selected_attachment_rows:
                try:
                    attachment, cache_hit = self._stage_attachment(attachment_raw)
                except IdxRequestBudgetExceededError:
                    request_budget_deferred = True
                    request_budget_deferred_row_id = raw_id
                    break
                except IdxResourceNotFoundError:
                    attachment_unavailable = True
                    break
                attachments.append(attachment)
                if cache_hit:
                    attachment_cache_hits += 1
                else:
                    attachment_downloads += 1

            if request_budget_deferred:
                break
            if attachment_unavailable:
                if len(unavailable_attachment_row_ids) < 20:
                    unavailable_attachment_row_ids.append(raw_id)
                continue

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
                        "idxAttachmentCountOriginal": len(valid_attachment_rows),
                        "idxAttachmentCountSelected": len(selected_attachment_rows),
                    },
                )
            )
            processed_ids.append(raw_id)
            if newest_at is None or announced_at > newest_at:
                newest_at = announced_at

        # The endpoint also returns exchange/market/product rows that do not fit
        # Synapse's issuer ticker contract. Those rows are outside this product's
        # issuer-disclosure scope. They are reported in diagnostics, skipped
        # without failing the whole window, and never added to checkpoint state.
        merged_seen = list(dict.fromkeys([*checkpoint.seen_ids, *processed_ids]))
        latest_value = checkpoint.latest_announced_at
        # Newest-first processing must not move the time watermark past older
        # candidates when a request budget is exhausted. Processed IDs are still
        # remembered individually, while the older window remains eligible for
        # the next run. Once the backlog clears, advance to the newest completed
        # source ID observed in this window (including IDs completed earlier).
        if not request_budget_deferred:
            completed_latest = newest_at
            if newest_seen_at is not None and (completed_latest is None or newest_seen_at > completed_latest):
                completed_latest = newest_seen_at
            if completed_latest is not None:
                if latest_value:
                    previous_latest = isoparse(latest_value)
                    if previous_latest.tzinfo is None or previous_latest.utcoffset() is None:
                        previous_latest = previous_latest.replace(tzinfo=self.timezone)
                    if previous_latest > completed_latest:
                        completed_latest = previous_latest
                latest_value = completed_latest.isoformat()
        self._pending_checkpoint = IdxWebsiteCheckpoint(tuple(merged_seen), latest_value)

        diagnostics = {
            "adapter": self.source_id,
            "networkAccess": True,
            "transport": "http-only",
            "sourceRequests": self.client.request_count,
            "pagesFetched": pagination["pagesFetched"],
            "repliesSeen": pagination["metadataRowsCollected"],
            "reportedTotal": pagination["reportedTotal"],
            "paginationStrategy": pagination["paginationStrategy"],
            "metadataRowsCollected": pagination["metadataRowsCollected"],
            "metadataRowsInRequestedWindow": len(ids_in_window),
            "alreadySeenInRequestedWindow": already_seen_in_window,
            "newCandidates": len(candidates),
            "candidateOrdering": "newest-head-oldest-recovery-newest",
            "recoveryLaneNewestHead": min(len(candidates), RECOVERY_LANE_NEWEST_HEAD),
            "recoveryLaneCandidates": len(recovery_lane_ids),
            "recoveryLaneRowIds": recovery_lane_ids,
            "nonIssuerRowsSkipped": len(nonissuer_row_ids),
            "nonIssuerRowIds": nonissuer_row_ids,
            "unsupportedTickerRowsSkipped": len(unsupported_ticker_row_ids),
            "unsupportedTickerRowIds": unsupported_ticker_row_ids,
            "nonStockProductRowsSkipped": len(nonstock_product_row_ids),
            "nonStockProductRowIds": nonstock_product_row_ids,
            "unavailableAttachmentRowsSkipped": len(unavailable_attachment_row_ids),
            "unavailableAttachmentRowIds": unavailable_attachment_row_ids,
            "noSelectedAttachmentRowsSkipped": len(no_selected_attachment_row_ids),
            "noSelectedAttachmentRowIds": no_selected_attachment_row_ids,
            "attachmentsConsidered": attachments_considered,
            "attachmentsSelected": attachments_selected,
            "attachmentsSkippedByPolicy": attachments_skipped_by_policy,
            "attachmentPolicySkips": attachment_policy_skips,
            "requestBudgetDeferred": request_budget_deferred,
            "requestBudgetDeferredRowId": request_budget_deferred_row_id,
            "issuerDisclosuresProcessed": len(disclosures),
            "checkpointSeenIds": len(seen_checkpoint),
            "disclosuresNew": len(disclosures),
            "attachmentDownloads": attachment_downloads,
            "attachmentCacheHits": attachment_cache_hits,
            "downloadedBytes": self.client.downloaded_bytes,
            "authoritativeCoverageAllowed": False,
        }
        if "widePageProbeSize" in pagination:
            diagnostics["widePageProbeSize"] = pagination["widePageProbeSize"]

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
