from idx_digest.synapse_mapper import TAXONOMY_VERSION, build_structured_analysis


def _summary(**overrides):
    payload = {
        "executive_summary": "Validated disclosure summary.",
        "material_facts": [],
        "corporate_actions": [],
        "expansion_projects": [],
        "management_or_control_changes": [],
        "capital_structure_events": [],
        "listing_or_regulatory_events": [],
        "possible_investor_relevance": [],
        "financial_figures": [],
        "analytical_scenarios": [],
        "dates_and_deadlines": [],
        "risks_or_uncertainties": [],
        "limitations": [],
    }
    payload.update(overrides)
    return payload


def test_financial_statement_headline_beats_incidental_buyback_and_divestment():
    analysis = build_structured_analysis(
        ticker="TLKM",
        title="Unaudited Consolidated Financial Statements 1Q 2026",
        summary=_summary(
            corporate_actions=[
                "The company announced a share buyback.",
                "A subsidiary divestment remains in progress.",
            ],
            financial_figures=[
                {"metric": "Revenue", "value": "37,189", "period": "1Q 2026"},
            ],
        ),
        analysis_mode="source_adapter",
    )

    assert TAXONOMY_VERSION == "synapse-taxonomy-v0.2-compat"
    assert analysis.primary_category == "FINANCIAL_REPORT"
    assert analysis.tags[0] == "FINANCIAL_REPORT"
    assert "BUYBACK" in analysis.tags
    assert "DIVESTMENT" in analysis.tags
    assert analysis.materiality == "LOW"
    assert analysis.confidence == 0.75


def test_subsidiary_management_headline_beats_rups_inside_document():
    analysis = build_structured_analysis(
        ticker="BMRI",
        title="Adjustment of Subsidiary Management of the Company",
        summary=_summary(
            material_facts=[
                "The subsidiary held an extraordinary general meeting of shareholders.",
                "The company no longer consolidates the subsidiary financial statements.",
            ],
            management_or_control_changes=[
                "Governance authority for the subsidiary was adjusted.",
            ],
        ),
        analysis_mode="source_adapter",
    )

    assert analysis.primary_category == "MANAGEMENT_CHANGE"
    assert analysis.tags[0] == "MANAGEMENT_CHANGE"
    assert "FINANCIAL_REPORT" in analysis.tags
    assert analysis.materiality == "MEDIUM"


def test_unanchored_headline_keeps_legacy_full_document_fallback():
    analysis = build_structured_analysis(
        ticker="ABCD",
        title="Material Information Update",
        summary=_summary(corporate_actions=["The company plans a share buyback."]),
        analysis_mode="source_adapter",
    )

    assert analysis.primary_category == "BUYBACK"
    assert analysis.materiality == "MEDIUM"
    assert analysis.confidence == 0.60
