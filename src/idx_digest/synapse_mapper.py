from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

from .synapse_contract import (
    AnalysisClaim,
    AnalysisImportantDate,
    AnalysisKeyNumber,
    CommitAnalysisRequest,
    StructuredAnalysis,
)


TAXONOMY_VERSION = "synapse-taxonomy-v0.1-compat"
BRIDGE_SCHEMA_VERSION = "announcement-v3-compat-v1"
BRIDGE_PROMPT_SUFFIX = "+synapse-compat-v1"

_CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("RIGHTS_ISSUE", ("rights issue", "hak memesan efek terlebih dahulu", "hmetd")),
    ("PRIVATE_PLACEMENT", ("private placement", "penambahan modal tanpa hak memesan efek terlebih dahulu", "pmthmetd")),
    ("BUYBACK", ("buyback", "pembelian kembali saham")),
    ("STOCK_SPLIT", ("stock split", "pemecahan nilai nominal")),
    ("REVERSE_SPLIT", ("reverse stock split", "penggabungan nilai nominal")),
    ("BONUS_SHARES", ("saham bonus", "bonus shares")),
    ("CONVERSION", ("konversi", "conversion")),
    ("ACQUISITION", ("akuisisi", "acquisition")),
    ("DIVESTMENT", ("divestasi", "divestment", "pelepasan aset", "penjualan aset")),
    ("MERGER", ("merger", "penggabungan usaha")),
    ("JOINT_VENTURE", ("joint venture", "ventura bersama")),
    ("RELATED_PARTY", ("pihak berelasi", "related party", "afiliasi")),
    ("ASSET_TRANSACTION", ("transaksi aset", "asset transaction")),
    ("CONTROLLER_CHANGE", ("perubahan pengendali", "change of control", "pengendali baru")),
    ("SHAREHOLDER_CHANGE", ("perubahan pemegang saham", "shareholder change")),
    ("MANAGEMENT_CHANGE", ("perubahan pengurus", "perubahan direksi", "perubahan komisaris", "management change")),
    ("TREASURY_SHARES", ("saham treasuri", "treasury shares", "treasury stock")),
    ("FREE_FLOAT", ("free float",)),
    ("RUPSLB", ("rupslb", "rapat umum pemegang saham luar biasa")),
    ("RUPS", ("rups", "rapat umum pemegang saham")),
    ("PUBLIC_EXPOSE", ("public expose", "paparan publik", "investor presentation")),
    ("SUSPENSION", ("suspensi", "suspension")),
    ("UNSUSPENSION", ("unsuspension", "pembukaan suspensi")),
    ("DELISTING", ("delisting", "penghapusan pencatatan")),
    ("LISTING", ("listing", "pencatatan saham")),
    ("DIVIDEND", ("dividen", "dividend")),
    ("REFINANCING", ("refinancing", "refinancing", "pembiayaan kembali")),
    ("BOND", ("obligasi", "bond", "sukuk")),
    ("DEBT", ("utang", "debt", "pinjaman", "loan facility")),
    ("EARNINGS", ("laba", "earnings", "profit")),
    ("FINANCIAL_REPORT", ("laporan keuangan", "financial statement", "financial report")),
    ("NEW_SUBSIDIARY", ("anak usaha baru", "new subsidiary", "pendirian anak usaha")),
    ("NEW_CAPACITY", ("kapasitas baru", "new capacity", "commissioning")),
    ("NEW_PROJECT", ("proyek baru", "new project")),
    ("CAPEX", ("capex", "capital expenditure", "belanja modal")),
    ("EXPANSION", ("ekspansi", "expansion")),
    ("NEW_BUSINESS", ("kegiatan usaha baru", "new business", "perubahan kegiatan usaha")),
    ("CLARIFICATION", ("klarifikasi", "clarification")),
    ("IDX_INQUIRY_RESPONSE", ("tanggapan atas permintaan penjelasan", "jawaban atas permintaan penjelasan", "idx inquiry")),
    ("CORRECTION", ("koreksi", "correction", "revisi")),
    ("ANNUAL_REPORT", ("laporan tahunan", "annual report")),
    ("ROUTINE_REPORT", ("laporan bulanan", "monthly report", "laporan registrasi pemegang efek")),
    ("LEGAL", ("gugatan", "perkara", "legal", "litigation")),
    ("REGULATORY", ("regulasi", "regulatory", "otoritas", "izin")),
)

_HIGH_MATERIALITY = {
    "RIGHTS_ISSUE",
    "PRIVATE_PLACEMENT",
    "ACQUISITION",
    "DIVESTMENT",
    "MERGER",
    "CONTROLLER_CHANGE",
    "SUSPENSION",
    "DELISTING",
}
_MEDIUM_MATERIALITY = {
    "DIVIDEND",
    "BUYBACK",
    "STOCK_SPLIT",
    "REVERSE_SPLIT",
    "BONUS_SHARES",
    "CONVERSION",
    "JOINT_VENTURE",
    "ASSET_TRANSACTION",
    "RELATED_PARTY",
    "EXPANSION",
    "CAPEX",
    "NEW_PROJECT",
    "NEW_CAPACITY",
    "NEW_SUBSIDIARY",
    "NEW_BUSINESS",
    "MANAGEMENT_CHANGE",
    "SHAREHOLDER_CHANGE",
    "TREASURY_SHARES",
    "FREE_FLOAT",
    "DEBT",
    "REFINANCING",
    "BOND",
    "REGULATORY",
    "LEGAL",
    "UNSUSPENSION",
}


def _strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _analysis_blob(title: str, summary: dict[str, Any]) -> str:
    parts = [title, str(summary.get("category") or "")]
    for key in (
        "material_facts",
        "corporate_actions",
        "expansion_projects",
        "management_or_control_changes",
        "capital_structure_events",
        "listing_or_regulatory_events",
    ):
        parts.extend(_strings(summary.get(key)))
    return re.sub(r"\s+", " ", " ".join(parts)).lower()


def taxonomy_tags(title: str, summary: dict[str, Any]) -> list[str]:
    blob = _analysis_blob(title, summary)
    tags: list[str] = []
    for category, patterns in _CATEGORY_PATTERNS:
        if any(pattern in blob for pattern in patterns):
            tags.append(category)

    if _strings(summary.get("expansion_projects")) and not any(
        tag in tags for tag in ("EXPANSION", "CAPEX", "NEW_PROJECT", "NEW_CAPACITY", "NEW_SUBSIDIARY", "NEW_BUSINESS")
    ):
        tags.append("EXPANSION")
    if _strings(summary.get("management_or_control_changes")) and not any(
        tag in tags for tag in ("CONTROLLER_CHANGE", "MANAGEMENT_CHANGE", "BOARD_CHANGE")
    ):
        tags.append("MANAGEMENT_CHANGE")
    if _strings(summary.get("listing_or_regulatory_events")) and not any(
        tag in tags for tag in ("SUSPENSION", "UNSUSPENSION", "LISTING", "DELISTING", "REGULATORY", "LEGAL")
    ):
        tags.append("REGULATORY")

    return list(dict.fromkeys(tags or ["OTHER"]))


def materiality_for(tags: Iterable[str], analysis_mode: str | None) -> str:
    values = set(tags)
    if values & _HIGH_MATERIALITY:
        return "HIGH"
    if values & _MEDIUM_MATERIALITY:
        return "MEDIUM"
    if analysis_mode == "routine_direct" or values == {"ROUTINE_REPORT"}:
        return "ROUTINE"
    return "LOW"


def _claim_type(value: str) -> str:
    normalized = value.strip().lower()
    return {
        "explicit_fact": "EXPLICIT_FACT",
        "derived_calculation": "DERIVED_CALCULATION",
        "analyst_hypothesis": "ANALYST_HYPOTHESIS",
    }.get(normalized, "ANALYST_HYPOTHESIS")


def _confidence(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().upper()
    return normalized if normalized in {"HIGH", "MEDIUM", "LOW"} else None


def _safe_iso_date(value: str) -> str | None:
    candidate = value.strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return None
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


def build_structured_analysis(
    *,
    ticker: str,
    title: str,
    summary: dict[str, Any],
    analysis_mode: str | None,
) -> StructuredAnalysis:
    tags = taxonomy_tags(title, summary)
    primary = tags[0]
    materiality = materiality_for(tags, analysis_mode)
    executive_summary = str(summary.get("executive_summary") or "").strip()
    if not executive_summary:
        raise ValueError("announcement summary is missing executive_summary")

    investor_relevance = _strings(summary.get("possible_investor_relevance"))
    why_it_matters = (
        " ".join(investor_relevance)
        if investor_relevance
        else "No additional investor-relevance statement was identified in the compatibility analysis."
    )

    claims = [
        AnalysisClaim(claim_type="EXPLICIT_FACT", text=fact)
        for fact in _strings(summary.get("material_facts"))
    ]
    for item in summary.get("analytical_scenarios") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("analysis") or "").strip()
        if not text:
            continue
        claims.append(
            AnalysisClaim(
                claim_type=_claim_type(str(item.get("classification") or "")),
                text=text,
                confidence=_confidence(str(item.get("confidence") or "")),
                basis="; ".join(_strings(item.get("basis"))) or None,
                assumptions="; ".join(_strings(item.get("assumptions"))) or None,
                caveats="; ".join(_strings(item.get("caveats"))) or None,
            )
        )

    numbers: list[AnalysisKeyNumber] = []
    for item in summary.get("financial_figures") or []:
        if not isinstance(item, dict):
            continue
        metric = str(item.get("metric") or "").strip()
        value = str(item.get("value") or "").strip()
        if not metric or not value:
            continue
        numbers.append(
            AnalysisKeyNumber(
                metric=metric,
                value_text=value,
                period=str(item.get("period") or "").strip() or None,
            )
        )

    dates: list[AnalysisImportantDate] = []
    for item in summary.get("dates_and_deadlines") or []:
        if not isinstance(item, dict):
            continue
        raw_date = str(item.get("date") or "").strip()
        event = str(item.get("event") or "").strip()
        if not raw_date or not event:
            continue
        dates.append(
            AnalysisImportantDate(
                event_type="DISCLOSURE_EVENT",
                event_date=_safe_iso_date(raw_date),
                date_text=raw_date,
                description=event,
            )
        )

    limitations = _strings(summary.get("limitations"))
    limitations.extend(
        [
            "Compatibility bridge from the legacy announcement-v3 schema; materiality is a deterministic taxonomy mapping, not a fresh AI judgment.",
            "Directional impact is intentionally UNCLEAR until a native Synapse analysis schema supplies explicit impact reasoning.",
            "Legacy announcement-v3 does not map each claim to an exact source file, so claim source_file_id is intentionally omitted.",
        ]
    )

    classification_confidence = 0.45 if primary == "OTHER" else 0.60
    if materiality == "ROUTINE":
        classification_confidence = 0.65

    return StructuredAnalysis(
        ticker=ticker,
        primary_category=primary,
        tags=tags,
        materiality=materiality,
        impact="UNCLEAR",
        confidence=classification_confidence,
        executive_summary=executive_summary,
        why_it_matters=why_it_matters,
        material_facts=claims,
        key_numbers=numbers,
        important_dates=dates,
        risks=_strings(summary.get("risks_or_uncertainties")),
        things_to_watch=[],
        limitations=list(dict.fromkeys(limitations)),
    )


def analysis_input_hash(
    *,
    announcement_id: str,
    summary: dict[str, Any],
    attachment_hashes: Iterable[str],
) -> str:
    payload = {
        "announcementId": announcement_id,
        "summary": summary,
        "attachmentHashes": sorted(value for value in attachment_hashes if value),
        "bridgeSchemaVersion": BRIDGE_SCHEMA_VERSION,
        "taxonomyVersion": TAXONOMY_VERSION,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_commit_request(
    *,
    ticker: str,
    title: str,
    announcement_id: str,
    summary: dict[str, Any],
    analysis_mode: str | None,
    model: str,
    provider: str,
    prompt_version: str,
    attachment_hashes: Iterable[str],
) -> CommitAnalysisRequest:
    return CommitAnalysisRequest(
        provider=provider or "unknown",
        model=model,
        schema_version=BRIDGE_SCHEMA_VERSION,
        prompt_version=f"{prompt_version}{BRIDGE_PROMPT_SUFFIX}",
        taxonomy_version=TAXONOMY_VERSION,
        input_hash=analysis_input_hash(
            announcement_id=announcement_id,
            summary=summary,
            attachment_hashes=attachment_hashes,
        ),
        analysis=build_structured_analysis(
            ticker=ticker,
            title=title,
            summary=summary,
            analysis_mode=analysis_mode,
        ),
    )
