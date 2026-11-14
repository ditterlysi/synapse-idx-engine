from __future__ import annotations

from idx_digest.disclosure_classifier import (
    CLASSIFICATION_FALLBACK,
    classify_disclosure,
    disclosure_class,
    materiality_for_disclosure,
)


def test_disclosure_class_is_preserved() -> None:
    assert disclosure_class("Paparan Publik Bulanan") == "public_expose"
    assert disclosure_class("Laporan Keuangan Tahunan") == "financial_report"
    assert disclosure_class("Laporan Bulanan Registrasi") == "general"


def test_classify_disclosure_public_expose() -> None:
    assert classify_disclosure("Paparan Publik Bulanan Oktober 2026") == "PUBLIC_EXPOSE"
    assert classify_disclosure("Public Expose PT XYZ Tbk") == "PUBLIC_EXPOSE"


def test_classify_disclosure_financial_report() -> None:
    assert classify_disclosure("Laporan Keuangan Tahunan 2026") == "FINANCIAL_REPORT"
    assert classify_disclosure("Laporan Keuangan Triwulan III 2026") == "FINANCIAL_REPORT"


def test_classify_disclosure_dividend() -> None:
    assert classify_disclosure("Pembagian Dividen Interim") == "DIVIDEND"
    assert classify_disclosure("Dividen Per Saham") == "DIVIDEND"


def test_classify_disclosure_rights_issue() -> None:
    assert classify_disclosure("Hak Tagih Saham") == "RIGHTS_ISSUE"
    assert classify_disclosure("Metode Hak Tagih") == "RIGHTS_ISSUE"


def test_classify_disclosure_buyback() -> None:
    assert classify_disclosure("Pembelian Kembali Saham") == "BUYBACK"
    assert classify_disclosure("Buy Back Saham") == "BUYBACK"
    assert classify_disclosure("Pembelian Kembali Saham oleh Perusahaan") == "BUYBACK"


def test_classify_disclosure_bond() -> None:
    assert classify_disclosure("Terbitan Obligasi Syariah") == "BOND"
    assert classify_disclosure("Surat Berharga Sukuk") == "BOND"


def test_classify_disclosure_management_change() -> None:
    assert classify_disclosure("Perubahan Anggota Direksi") == "MANAGEMENT_CHANGE"
    assert classify_disclosure("Perubahan Anggota Komisaris") == "MANAGEMENT_CHANGE"


def test_classify_disclosure_rups() -> None:
    assert classify_disclosure("Rapat Umum Pemegang Saham") == "RUPS"
    assert classify_disclosure("RUPSLB Tahun 2026") == "RUPS"


def test_classify_disclosure_correction() -> None:
    assert classify_disclosure("Koreksi Laporan Keuangan") == "CORRECTION"
    assert classify_disclosure("Perbaikan atas Laporan Tahunan") == "CORRECTION"


def test_classify_disclosure_routine() -> None:
    assert classify_disclosure("Pemberitahuan Umum") == "ROUTINE_REPORT"
    assert classify_disclosure("Kegiatan Registrasi Pemegang Efek") == "ROUTINE_REPORT"


def test_classify_disclosure_fallback() -> None:
    assert classify_disclosure("Peristiwa Biasa") == CLASSIFICATION_FALLBACK


def test_materiality_portfolio_financial_report() -> None:
    assert materiality_for_disclosure("FINANCIAL_REPORT", in_portfolio=True, in_watchlist=False) == "HIGH"


def test_materiality_watchlist_dividend() -> None:
    assert materiality_for_disclosure("DIVIDEND", in_portfolio=False, in_watchlist=True) == "MEDIUM"


def test_materiality_routine_ignores_portfolio() -> None:
    assert materiality_for_disclosure("PUBLIC_EXPOSE", in_portfolio=True, in_watchlist=False) == "ROUTINE"


def test_materiality_unknown_is_routine() -> None:
    assert materiality_for_disclosure("ROUTINE_REPORT", in_portfolio=False, in_watchlist=False) == "ROUTINE"
