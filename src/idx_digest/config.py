from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # AI backend selection for source-neutral Synapse ingestion. Keep OpenRouter
    # as the compatibility default; direct Gemini can be selected explicitly.
    ai_provider: str = "openrouter"

    # Direct Gemini Developer API.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-3.5-flash-lite"

    # OpenRouter is OpenAI-compatible. Provider routing is pinned separately so
    # the selected model cannot silently run on a different host.
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "deepseek/deepseek-v4-flash-0731"
    openrouter_provider: str = "deepinfra"
    openrouter_allow_fallbacks: bool = False
    openrouter_require_parameters: bool = True
    openrouter_http_referer: str = ""
    openrouter_app_title: str = "Synapse IDX Engine"

    idx_base_url: str = "https://www.idx.co.id"
    idx_page_size: int = Field(default=50, ge=1, le=200)
    idx_request_delay_seconds: float = Field(default=0.35, ge=0)
    idx_user_agent: str = "SynapseIDXEngine/0.16.0"
    idx_cookie: str = ""
    idx_transport: str = "auto"
    idx_browser_profile_dir: Path = Path("./data/browser-profile")
    idx_browser_headless: bool = False
    idx_browser_navigation_timeout_ms: int = Field(default=60_000, ge=1_000)
    idx_browser_verification_timeout_seconds: int = Field(default=180, ge=5)
    # IDX/Cloudflare metadata throttling guardrails. These affect only IDX reads,
    # never AI generation limits.
    idx_429_cooldown_initial_seconds: float = Field(default=12.0, ge=1.0, le=120.0)
    idx_429_cooldown_max_seconds: float = Field(default=90.0, ge=5.0, le=300.0)
    idx_429_jitter_seconds: float = Field(default=4.0, ge=0.0, le=30.0)
    # When IDX reports more rows than offset pagination actually exposes, retry
    # the shard from indexFrom=0 with a larger pageSize before falling back to
    # expensive per-ticker reconstruction. This remains available for explicit
    # manual audit mode, but Synapse daily mode disables ticker fan-out.
    idx_wide_page_probe_size: int = Field(default=200, ge=50, le=1000)
    idx_wide_page_probe_max_size: int = Field(default=1000, ge=200, le=2000)
    idx_ticker_shard_delay_seconds: float = Field(default=1.0, ge=0.0, le=30.0)
    idx_ticker_shard_jitter_seconds: float = Field(default=0.20, ge=0.0, le=5.0)
    idx_ticker_shard_burst_size: int = Field(default=25, ge=1, le=200)
    idx_ticker_shard_burst_cooldown_seconds: float = Field(default=12.0, ge=0.0, le=300.0)
    # Normal runs subtract proven coverage from the requested interval and read
    # only missing gaps. A small calendar overlap can surround a gap boundary;
    # covered timestamps are filtered before downstream work.
    idx_incremental_overlap_days: float = Field(default=1.0, ge=0.0, le=7.0)

    # Synapse integration. The engine never receives a Supabase service-role key;
    # it writes through a narrow authenticated Synapse internal API instead.
    synapse_internal_base_url: str = ""
    synapse_ingestion_secret: SecretStr = SecretStr("")

    # Conservative scheduled mode. Disabled until the Synapse API boundary and
    # narrow-range E2E are verified. Daily mode is intentionally HTTP-only and
    # must stop on access protection rather than switching to browser bypasses.
    synapse_daily_enabled: bool = False
    synapse_daily_transport: str = "http"
    synapse_daily_request_delay_seconds: float = Field(default=1.5, ge=0.5, le=30.0)
    synapse_daily_request_jitter_seconds: float = Field(default=0.35, ge=0.0, le=5.0)
    synapse_daily_max_source_requests: int = Field(default=50, ge=1, le=500)
    synapse_daily_max_attachments: int = Field(default=100, ge=1, le=1000)
    synapse_daily_max_download_bytes: int = Field(default=500_000_000, ge=1_000_000)
    synapse_daily_max_ai_documents: int = Field(default=100, ge=1, le=1000)
    synapse_daily_max_run_seconds: int = Field(default=2700, ge=60, le=7200)
    synapse_daily_allow_historical_backfill: bool = False
    synapse_daily_allow_ticker_fanout: bool = False

    app_timezone: str = "Asia/Jakarta"
    data_dir: Path = Path("./data")
    ocr_lang: str = "ind+eng"
    ocr_min_chars_per_page: int = Field(default=80, ge=0)
    max_xlsx_cells: int = Field(default=250_000, ge=1)
    llm_chunk_chars: int = Field(default=45_000, ge=5_000)
    # Document summaries are independent map tasks. Keep this bounded because
    # the selected upstream provider may enforce RPM/TPM limits.
    llm_concurrency: int = Field(default=2, ge=1, le=8)
    llm_per_announcement_concurrency: int = Field(default=2, ge=1, le=8)
    llm_document_chunk_concurrency: int = Field(default=2, ge=1, le=4)
    extraction_workers: int = Field(default=3, ge=1, le=8)
    extraction_queue_size: int = Field(default=8, ge=1, le=32)
    llm_adaptive_concurrency: bool = True
    routine_triage_enabled: bool = True
    routine_triage_max_chars: int = Field(default=70_000, ge=10_000, le=200_000)
    attachment_dedup_enabled: bool = True
    attachment_near_duplicate_threshold: float = Field(default=0.985, ge=0.95, le=1.0)
    company_incremental_cache_enabled: bool = True
    company_single_announcement_promotion: bool = True
    stock_master_enabled: bool = True
    stock_master_max_age_hours: float = Field(default=24.0, ge=1.0, le=720.0)
    stock_master_min_tickers: int = Field(default=500, ge=100, le=2000)
    network_watchdog_enabled: bool = True
    network_probe_interval_seconds: float = Field(default=3.0, ge=0.5, le=30.0)
    network_probe_timeout_seconds: float = Field(default=2.0, ge=0.2, le=10.0)
    routine_ownership_delta_threshold_pct: float = Field(default=0.10, ge=0.0, le=25.0)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "idx_digest.sqlite3"

    @property
    def prompt_config_path(self) -> Path:
        return self.data_dir / "prompts.json"

    @property
    def openrouter_provider_preferences(self) -> dict[str, object]:
        """Strict OpenRouter routing preferences for every LLM request."""
        provider = self.openrouter_provider.strip()
        if not provider:
            raise ValueError("OPENROUTER_PROVIDER must not be empty")
        return {
            "only": [provider],
            "allow_fallbacks": self.openrouter_allow_fallbacks,
            "require_parameters": self.openrouter_require_parameters,
        }

    def ensure_directories(self) -> None:
        for child in ("raw", "text", "companies", "logs", "traces", "runs", "recovery"):
            (self.data_dir / child).mkdir(parents=True, exist_ok=True)
