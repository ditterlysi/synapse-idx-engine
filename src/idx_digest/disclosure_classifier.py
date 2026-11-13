from __future__ import annotations

import re


def disclosure_class(title: str) -> str:
    value = re.sub(r"\s+", " ", str(title or "").strip().lower())
    public_patterns = (
        "paparan publik",
        "public expose",
        "public exposure",
        "investor meeting",
        "investor presentation",
        "pemaparan kinerja",
        "presentasi investor",
        "materi investor",
    )
    if any(pattern in value for pattern in public_patterns):
        return "public_expose"
    if "laporan keuangan" in value or "financial statement" in value:
        return "financial_report"
    return "general"


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", str(title or "").strip().lower())


# Ordered rules: first match wins. Each rule maps literal title patterns to a
# Synapse taxonomy category (src/lib/idx-internal-contract.ts in the Synapse
# repo). CLASSIFICATION_FALLBACK must stay last.
_CLASSIFICATION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("paparan publik", "public expose", "public exposure", "investor meeting",
      "investor presentation", "pemaparan kinerja", "presentasi investor",
      "materi investor"), "PUBLIC_EXPOSE"),
    (("rups", "rapat umum para pemegang saham", "rapat pemegang saham",
      "rapat umum pemegang saham"), "RUPS"),
    (("koreksi", "perbaikan atas"), "CORRECTION"),
    (("laporan keuangan", "financial statement"), "FINANCIAL_REPORT"),
    (("dividen", "dividend"), "DIVIDEND"),
    (("beli back", "beli-back", "buyback", "buy back"), "BUYBACK"),
    (("hak tagih", "rights issue", "metode hak tagih"), "RIGHTS_ISSUE"),
    (("obligasi", "sukuk", "bond"), "BOND"),
    (("pemeringkatan", "pemeringkat efek", "pefindo", "moody's", "s&p",
      "rating agen"), "REGULATORY"),
    (("kepemilikan saham", "perubahan kepemilikan", "ownership"), "SHAREHOLDER_CHANGE"),
    (("pencatatan saham", "pencatatan tambahan"), "LISTING"),
    (("delisting", "penghapusan dari daftar", "go private"), "DELISTING"),
    (("direksi", "komisaris", "manajemen", "komite nominasi"), "MANAGEMENT_CHANGE"),
    (("akuisisi", "acquisition"), "ACQUISITION"),
    (("penjualan aset", "pengalihan aset"), "ASSET_TRANSACTION"),
    (("perkara hukum", "tuntutan hukum", "gugatan", "legal", "litigasi"), "LEGAL"),
    (("surat bebas tekanan", "regulasi", "surat pernyataan"), "REGULATORY"),
    (("klarifikasi atas", "tanggapan atas berita"), "CLARIFICATION"),
    (("bulan", "kegiatan registrasi", "registrasi", "pemberitahuan", "iklan"), "ROUTINE_REPORT"),
)

CLASSIFICATION_FALLBACK = "ROUTINE_REPORT"


def _rule_matches(patterns: tuple[str, ...], value: str) -> bool:
    return any(pattern in value for pattern in patterns)


def classify_disclosure(title: str) -> str:
    """Map an IDX disclosure title to a Synapse taxonomy category.

    The mapping is deliberately literal pattern matching on the normalised
    title. It never reads the document body and never calls a language model,
    so the result is a classification, not an analysis. Titles that match no
    rule fall back to CLASSIFICATION_FALLBACK rather than being dropped.
    """
    value = _normalize_title(title)
    for patterns, category in _CLASSIFICATION_RULES:
        if _rule_matches(patterns, value):
            return category
    return CLASSIFICATION_FALLBACK


def materiality_for_disclosure(
    category: str, *, in_portfolio: bool, in_watchlist: bool
) -> str:
    """Derive materiality from the classification and owner relevance.

    Direction (impact) cannot be known from a title, so callers must not
    present the result as an judgement about the disclosure's effect.
    """
    significant = {
        "FINANCIAL_REPORT",
        "DIVIDEND",
        "RUPS",
        "RIGHTS_ISSUE",
        "BUYBACK",
        "BOND",
        "DEBT",
        "REVERSE_SPLIT",
        "STOCK_SPLIT",
        "ACQUISITION",
        "MERGER",
        "DELISTING",
        "SHAREHOLDER_CHANGE",
        "LISTING",
        "DELISTING",
    }
    if category not in significant:
        return "ROUTINE"
    if in_portfolio:
        return "HIGH"
    if in_watchlist:
        return "MEDIUM"
    return "ROUTINE"
