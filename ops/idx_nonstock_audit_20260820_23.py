from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from idx_digest.config import Settings
from idx_digest.idx_polite_http import CURRENT_IDX_BASE_URL, PoliteFetchClient
from idx_digest.sources.idx_website import (
    NON_STOCK_PRODUCT_FLAGS,
    FileCheckpointStore,
    IdxWebsiteSource,
)

START_DATE = datetime(2026, 8, 20)
DAYS = 4


def main() -> None:
    settings = Settings()
    timezone = ZoneInfo("Asia/Jakarta")
    client = PoliteFetchClient(
        base_url=CURRENT_IDX_BASE_URL,
        user_agent=settings.idx_user_agent,
        request_delay_seconds=10.0,
        request_jitter_seconds=2.0,
        max_retries=2,
        max_requests=12,
        max_download_bytes_total=1,
    )
    source = IdxWebsiteSource(
        client,
        checkpoint_store=FileCheckpointStore(Path("idx-nonstock-audit-unused.json")),
        staging_dir=Path("idx-nonstock-audit-unused-staging"),
        page_size=100,
        max_pages=10,
        timezone_name="Asia/Jakarta",
    )
    try:
        rows: list[dict[str, object]] = []
        daily: list[dict[str, object]] = []
        for offset in range(DAYS):
            day = START_DATE + timedelta(days=offset)
            start_at = day.replace(tzinfo=timezone)
            end_at = (day + timedelta(days=1) - timedelta(microseconds=1)).replace(tzinfo=timezone)
            items, pagination = source._collect_metadata(start_at=start_at, end_at=end_at)
            nonstock_count = 0
            for item in items:
                announcement = item.get("pengumuman")
                if not isinstance(announcement, dict):
                    continue
                active_flags = [flag for flag in NON_STOCK_PRODUCT_FLAGS if bool(announcement.get(flag))]
                if not active_flags:
                    continue
                nonstock_count += 1
                rows.append(
                    {
                        "rawId": str(announcement.get("Id2") or "").strip(),
                        "ticker": str(announcement.get("Kode_Emiten") or "").strip().upper() or None,
                        "title": str(announcement.get("JudulPengumuman") or "").strip() or None,
                        "announcedAt": announcement.get("TglPengumuman"),
                        "flags": active_flags,
                    }
                )
            daily.append(
                {
                    "date": day.date().isoformat(),
                    "metadataRows": len(items),
                    "nonStockRows": nonstock_count,
                    "pagination": pagination,
                }
            )
        report = {
            "metadataOnly": True,
            "idxWrites": False,
            "aiCalls": False,
            "sourceRequests": client.request_count,
            "range": ["2026-08-20", "2026-08-23"],
            "daily": daily,
            "nonStockRows": rows,
        }
        Path("ops/idx-nonstock-audit-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        raw_ids = sorted({str(row["rawId"]) for row in rows if row.get("rawId")})
        Path("ops/idx-nonstock-raw-ids.txt").write_text(
            "\n".join(raw_ids) + "\n", encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        client.close()


if __name__ == "__main__":
    main()
