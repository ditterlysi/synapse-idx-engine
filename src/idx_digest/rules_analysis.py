"""Rule-based analysis for the lightweight collection pipeline.

This module builds a Synapse analysis payload without reading any document
and without calling any language model. It is the Phase 13 replacement for
the download-and-analyze path: the classification comes from the disclosure
title (disclosure_classifier) and the payload states its own limits.

The resulting analysis is committed through the same internal analysis
endpoint as an AI analysis, so the disclosure can reach READY and appear in
the reader. The reader distinguishes it by provider ("rule-based").
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any

from .disclosure_classifier import (
    CLASSIFICATION_FALLBACK,
    classify_disclosure,
    materiality_for_disclosure,
)
from .synapse_mapper import BRIDGE_SCHEMA_VERSION, TAXONOMY_VERSION

RULES_PROVIDER = "rule-based"
RULES_MODEL = "idx-rules-v1"
RULES_PROMPT_VERSION = "rules-v1"
RULES_TAXONOMY_VERSION = TAXONOMY_VERSION
RULES_LIMITATION = (
    "This summary was produced by rule-based classification from IDX "
    "metadata only. The source document was not read."
)


def rules_input_hash(
    *, announcement_id: str, title: str, category: str, materiality: str
) -> str:
    """Stable hash of the classified metadata.

    A repeated classification of unchanged metadata therefore maps to the
    same committed analysis instead of creating duplicates.
    """
    payload = json.dumps(
        {
            "announcementId": announcement_id,
            "title": title,
            "primaryCategory": category,
            "materiality": materiality,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_rules_summary(
    *,
    ticker: str,
    title: str,
    announced_at: datetime,
    category: str,
    materiality: str,
    in_portfolio: bool,
    in_watchlist: bool,
) -> dict[str, Any]:
    """Build the analysis payload for one disclosure from metadata only."""
    announced_date = announced_at.date() if isinstance(announced_at, datetime) else date.min
    relevance = (
        "a stock you hold"
        if in_portfolio
        else "a stock on your watchlist"
        if in_watchlist
        else "a listed issuer"
    )
    why = (
        f"{relevance.capitalize()} published a disclosure classified as {category}."
        if materiality != "ROUTINE"
        else f"{relevance.capitalize()} published a disclosure of category {category}."
    )
    return {
        "ticker": ticker,
        "primaryCategory": category,
        "tags": [category if category != CLASSIFICATION_FALLBACK else "ROUTINE_REPORT"],
        "materiality": materiality,
        "impact": "UNCLEAR",
        "confidence": 1.0,
        "executiveSummary": (
            f"{ticker} — {category.replace('_', ' ').title()}. "
            f"Announced {announced_date.isoformat()}. Open the official document for details."
        ),
        "whyItMatters": why,
        "materialFacts": [
            {
                "claimType": "EXPLICIT_FACT",
                "text": (
                    f"{ticker} published an announcement titled "
                    f"\"{title}\" on {announced_date.isoformat()}."
                ),
                "confidence": "HIGH",
                "basis": None,
                "assumptions": None,
                "caveats": None,
                "sourceFileId": None,
                "sourcePage": None,
                "evidenceExcerpt": None,
            }
        ],
        "keyNumbers": [],
        "importantDates": [],
        "risks": [],
        "thingsToWatch": [],
        "limitations": [RULES_LIMITATION],
    }


def classify_and_build(
    *,
    ticker: str,
    title: str,
    announced_at: datetime,
    announcement_id: str,
    in_portfolio: bool = False,
    in_watchlist: bool = False,
) -> dict[str, Any]:
    """Classify one disclosure and build its full analysis payload."""
    category = classify_disclosure(title)
    materiality = materiality_for_disclosure(
        category, in_portfolio=in_portfolio, in_watchlist=in_watchlist
    )
    summary = build_rules_summary(
        ticker=ticker,
        title=title,
        announced_at=announced_at,
        category=category,
        materiality=materiality,
        in_portfolio=in_portfolio,
        in_watchlist=in_watchlist,
    )
    return {
        "category": category,
        "materiality": materiality,
        "summary": summary,
        "input_hash": rules_input_hash(
            announcement_id=announcement_id,
            title=title,
            category=category,
            materiality=materiality,
        ),
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "taxonomy_version": RULES_TAXONOMY_VERSION,
        "provider": RULES_PROVIDER,
        "model": RULES_MODEL,
        "prompt_version": RULES_PROMPT_VERSION,
    }
