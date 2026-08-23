from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from idx_digest.config import Settings
from idx_digest.idx_polite_http import CURRENT_IDX_BASE_URL, PoliteFetchClient
from idx_digest.sources.idx_website import FileCheckpointStore, IdxWebsiteSource

TARGET_IDS = {
    "20260821115009-451/SIMASFIN-DIR/VIII/2026_id-id",
    "20260821115044-452/SIMASFIN-DIR/VIII/2026_id-id",
    "20260821121255-081/CS-OJK/PTI/VIII/26_id-id",
    "20260821163757-Peng-PK-00059/BEI.PLP/08-2026_id-id",
    "20260821163949-02121/FMS/OPS-BTIM/VIII/26_id-id",
    "20260821173335-949/IPIM-RD/VIII/2026_id-id",
    "20260821175507-1684/MAJORIS/VIII/2026_id-id",
    "20260821182050-022/XMES/08/2026_id-id",
    "20260821195331-Peng-P-00949/BEI.PP2/08-2026_id-id",
    "20260821200235-Peng-P-00953/BEI.PP2/08-2026_id-id",
    "20260821215318-BXS/0/021/003/2026_id-id",
    "20260821220913-02132/FMS/OPS-BTIM/VIII/26_id-id",
    "20260821224527-BXS/08/021/007/2026_id-id",
}


def _scalar_metadata(value: dict[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in value.items()
        if item is None or isinstance(item, (str, int, float, bool))
    }


def main() -> None:
    settings = Settings()
    timezone = ZoneInfo("Asia/Jakarta")
    start_at = datetime(2026, 8, 21, 0, 0, 0, tzinfo=timezone)
    end_at = datetime(2026, 8, 21, 23, 59, 59, tzinfo=timezone)
    client = PoliteFetchClient(
        base_url=CURRENT_IDX_BASE_URL,
        user_agent=settings.idx_user_agent,
        request_delay_seconds=10.0,
        request_jitter_seconds=2.0,
        max_retries=2,
        max_requests=4,
        max_download_bytes_total=1,
    )
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(Path("idx-reconcile-unused-checkpoint.json")),
        staging_dir=Path("idx-reconcile-unused-staging"),
        page_size=min(settings.idx_page_size, 100),
        max_pages=10,
        timezone_name="Asia/Jakarta",
    )
    try:
        items, pagination = source._collect_metadata(start_at=start_at, end_at=end_at)
        found: dict[str, dict[str, object]] = {}
        for item in items:
            announcement = item.get("pengumuman")
            if not isinstance(announcement, dict):
                continue
            raw_id = str(announcement.get("Id2") or "").strip()
            if raw_id not in TARGET_IDS:
                continue
            ticker = str(announcement.get("Kode_Emiten") or "").strip().upper()
            title = str(announcement.get("JudulPengumuman") or "").strip()
            top_level = {
                key: value
                for key, value in item.items()
                if key not in {"pengumuman", "attachments"}
                and (value is None or isinstance(value, (str, int, float, bool)))
            }
            found[raw_id] = {
                "ticker": ticker or None,
                "title": title or None,
                "announcedAt": announcement.get("TglPengumuman"),
                "attachmentCount": len(item.get("attachments") or []),
                "classification": (
                    "NON_ISSUER_MISSING_TICKER"
                    if not ticker
                    else "UNSUPPORTED_TICKER"
                    if not (1 <= len(ticker) <= 10 and all(ch.isalnum() or ch == "." for ch in ticker))
                    else "VALID_ISSUER_TICKER"
                ),
                "announcementMetadata": _scalar_metadata(announcement),
                "topLevelMetadata": top_level,
            }
        report = {
            "metadataOnly": True,
            "idxWrites": False,
            "aiCalls": False,
            "sourceRequests": client.request_count,
            "pagination": pagination,
            "targetCount": len(TARGET_IDS),
            "foundCount": len(found),
            "missingFromMetadata": sorted(TARGET_IDS - set(found)),
            "items": {key: found[key] for key in sorted(found)},
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
