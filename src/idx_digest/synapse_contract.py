from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class SynapseModel(BaseModel):
    model_config = ConfigDict(alias_generator=_to_camel, populate_by_name=True, extra="forbid")


Priority = Literal[0, 1, 2, 3, 4]
RunMode = Literal["DAILY", "MANUAL_BACKFILL", "RETRY"]
RunStatus = Literal["RUNNING", "COMPLETE", "PARTIAL", "FAILED", "BLOCKED"]
ProcessingStatus = Literal[
    "DISCOVERED",
    "QUEUED",
    "DOWNLOADING",
    "EXTRACTING",
    "ANALYZING",
    "READY",
    "PARTIAL",
    "FAILED",
]
Materiality = Literal["HIGH", "MEDIUM", "LOW", "ROUTINE"]
Impact = Literal[
    "POSITIVE",
    "POTENTIALLY_POSITIVE",
    "NEUTRAL",
    "POTENTIALLY_NEGATIVE",
    "NEGATIVE",
    "UNCLEAR",
]
ClaimType = Literal["EXPLICIT_FACT", "DERIVED_CALCULATION", "ANALYST_HYPOTHESIS"]
DisclosureCategory = Literal[
    "FINANCIAL_REPORT", "EARNINGS", "DIVIDEND", "CASH_FLOW", "DEBT", "REFINANCING", "BOND",
    "RIGHTS_ISSUE", "PRIVATE_PLACEMENT", "BUYBACK", "STOCK_SPLIT", "REVERSE_SPLIT", "BONUS_SHARES",
    "CONVERSION", "ACQUISITION", "DIVESTMENT", "MERGER", "JOINT_VENTURE", "ASSET_TRANSACTION",
    "RELATED_PARTY", "EXPANSION", "CAPEX", "NEW_PROJECT", "NEW_CAPACITY", "NEW_SUBSIDIARY",
    "NEW_BUSINESS", "CONTROLLER_CHANGE", "SHAREHOLDER_CHANGE", "MANAGEMENT_CHANGE", "TREASURY_SHARES",
    "FREE_FLOAT", "RUPS", "RUPSLB", "PUBLIC_EXPOSE", "BOARD_CHANGE", "SUSPENSION", "UNSUSPENSION",
    "LISTING", "DELISTING", "REGULATORY", "LEGAL", "CLARIFICATION", "IDX_INQUIRY_RESPONSE", "CORRECTION",
    "ANNUAL_REPORT", "ROUTINE_REPORT", "ADMINISTRATIVE", "OTHER",
]


class TickerRelevance(SynapseModel):
    ticker: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z0-9.]+$")
    is_portfolio: bool
    is_watchlist: bool
    priority: Priority

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    def model_post_init(self, __context: object) -> None:
        if self.is_portfolio and self.priority != 0:
            raise ValueError("open portfolio positions must be P0")
        if not self.is_portfolio and self.is_watchlist and self.priority != 1:
            raise ValueError("watchlist-only positions must be P1")


class RelevanceRequest(SynapseModel):
    tickers: list[str] = Field(min_length=1, max_length=500)

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class RelevanceResponse(SynapseModel):
    items: list[TickerRelevance]


class CreateRunRequest(SynapseModel):
    mode: RunMode
    requested_from: str | None = None
    requested_to: str | None = None
    engine_version: str | None = Field(default=None, min_length=1, max_length=80)


class CreateRunResponse(SynapseModel):
    run_id: str = Field(min_length=1)


class UpdateRunRequest(SynapseModel):
    status: RunStatus | None = None
    completed_at: str | None = None
    announcements_found: int | None = Field(default=None, ge=0)
    announcements_new: int | None = Field(default=None, ge=0)
    files_downloaded: int | None = Field(default=None, ge=0)
    files_extracted: int | None = Field(default=None, ge=0)
    analyses_completed: int | None = Field(default=None, ge=0)
    source_requests: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, min_length=1, max_length=80)
    error_message: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_terminal_status(self) -> "UpdateRunRequest":
        if not any(value is not None for value in self.__dict__.values()):
            raise ValueError("at least one run update field is required")
        if self.status and self.status != "RUNNING" and not self.completed_at:
            raise ValueError("terminal run status requires completed_at")
        if self.status == "RUNNING" and self.completed_at:
            raise ValueError("RUNNING status cannot include completed_at")
        return self


class UpdateRunResponse(SynapseModel):
    run_id: str
    status: RunStatus
    completed_at: str | None = None


class DisclosureUpsertItem(SynapseModel):
    idx_announcement_id: str = Field(min_length=1, max_length=200)
    ticker: str = Field(min_length=1, max_length=10)
    announced_at: str
    title: str = Field(min_length=1, max_length=2000)
    subject: str | None = Field(default=None, max_length=4000)
    disclosure_type: str | None = Field(default=None, max_length=300)
    source_url: str | None = Field(default=None, max_length=2000)
    raw_metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()


class DisclosureUpsertRequest(SynapseModel):
    run_id: str
    items: list[DisclosureUpsertItem] = Field(min_length=1, max_length=100)


class DisclosureUpsertResult(SynapseModel):
    idx_announcement_id: str
    disclosure_id: str
    created: bool


class DisclosureUpsertResponse(SynapseModel):
    items: list[DisclosureUpsertResult]


class DisclosureFileUpsertItem(SynapseModel):
    source_url: str = Field(min_length=1, max_length=2000)
    original_filename: str | None = Field(default=None, max_length=500)
    normalized_filename: str | None = Field(default=None, max_length=500)
    content_type: str | None = Field(default=None, max_length=200)
    file_extension: str | None = Field(default=None, max_length=30)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    size_bytes: int | None = Field(default=None, ge=0, le=2_000_000_000)
    selected_for_analysis: bool = False
    selection_category: str | None = Field(default=None, max_length=100)
    selection_reason: str | None = Field(default=None, max_length=1000)
    download_status: Literal["PENDING", "DOWNLOADED", "SKIPPED", "FAILED"] = "PENDING"
    extraction_status: Literal["PENDING", "EXTRACTED", "SKIPPED", "FAILED"] = "PENDING"
    extraction_method: str | None = Field(default=None, max_length=100)
    extracted_text_hash: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    extracted_text_ref: str | None = Field(default=None, max_length=2000)
    extraction_error: str | None = Field(default=None, max_length=1000)
    downloaded_at: str | None = None
    extracted_at: str | None = None


class DisclosureFilesUpsertRequest(SynapseModel):
    files: list[DisclosureFileUpsertItem] = Field(min_length=1, max_length=100)


class DisclosureFileResult(SynapseModel):
    file_id: str
    source_url: str


class DisclosureFilesUpsertResponse(SynapseModel):
    files: list[DisclosureFileResult]


class AnalysisClaim(SynapseModel):
    claim_type: ClaimType
    text: str = Field(min_length=1, max_length=10_000)
    confidence: Literal["HIGH", "MEDIUM", "LOW"] | None = None
    basis: str | None = Field(default=None, max_length=5000)
    assumptions: str | None = Field(default=None, max_length=5000)
    caveats: str | None = Field(default=None, max_length=5000)
    source_file_id: str | None = None
    source_page: int | None = Field(default=None, ge=1, le=100_000)
    evidence_excerpt: str | None = Field(default=None, max_length=3000)


class AnalysisKeyNumber(SynapseModel):
    metric: str = Field(min_length=1, max_length=300)
    value_numeric: float | None = None
    value_text: str | None = Field(default=None, max_length=1000)
    unit: str | None = Field(default=None, max_length=100)
    currency: str | None = Field(default=None, max_length=20)
    period: str | None = Field(default=None, max_length=200)
    source_file_id: str | None = None
    source_page: int | None = Field(default=None, ge=1, le=100_000)

    @model_validator(mode="after")
    def require_value(self) -> "AnalysisKeyNumber":
        if self.value_numeric is None and not self.value_text:
            raise ValueError("key number requires value_numeric or value_text")
        return self


class AnalysisImportantDate(SynapseModel):
    event_type: str | None = Field(default=None, max_length=200)
    event_date: str | None = None
    date_text: str | None = Field(default=None, max_length=500)
    description: str = Field(min_length=1, max_length=3000)
    source_file_id: str | None = None
    source_page: int | None = Field(default=None, ge=1, le=100_000)


class StructuredAnalysis(SynapseModel):
    ticker: str = Field(min_length=1, max_length=10)
    primary_category: DisclosureCategory
    tags: list[DisclosureCategory] = Field(min_length=1, max_length=25)
    materiality: Materiality
    impact: Impact
    confidence: float = Field(ge=0, le=1)
    executive_summary: str = Field(min_length=1, max_length=10_000)
    why_it_matters: str = Field(min_length=1, max_length=10_000)
    material_facts: list[AnalysisClaim] = Field(default_factory=list, max_length=100)
    key_numbers: list[AnalysisKeyNumber] = Field(default_factory=list, max_length=100)
    important_dates: list[AnalysisImportantDate] = Field(default_factory=list, max_length=100)
    risks: list[str] = Field(default_factory=list, max_length=50)
    things_to_watch: list[str] = Field(default_factory=list, max_length=50)
    limitations: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def primary_category_is_tagged(self) -> "StructuredAnalysis":
        if self.primary_category not in self.tags:
            raise ValueError("tags must include primary_category")
        self.tags = list(dict.fromkeys(self.tags))
        return self


class CommitAnalysisRequest(SynapseModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(min_length=1, max_length=80)
    prompt_version: str = Field(min_length=1, max_length=80)
    taxonomy_version: str = Field(min_length=1, max_length=80)
    input_hash: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_ms: int | None = Field(default=None, ge=0)
    analysis: StructuredAnalysis


class CommitAnalysisResponse(SynapseModel):
    analysis_id: str
    promoted: bool


class UpdateProcessingStatusRequest(SynapseModel):
    processing_status: ProcessingStatus


class UpdateProcessingStatusResponse(SynapseModel):
    disclosure_id: str
    processing_status: ProcessingStatus
    ready_at: str | None = None


class CoverageCommitRequest(SynapseModel):
    run_id: str
    scope: str = Field(default="ALL", min_length=1, max_length=100)
    covered_from: str
    covered_to: str


class CoverageCommitResponse(SynapseModel):
    coverage_id: str
    created: bool
