from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


Priority = Literal[0, 1, 2, 3, 4]


class TickerRelevance(BaseModel):
    ticker: str = Field(min_length=1, max_length=10)
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


class RelevanceRequest(BaseModel):
    tickers: list[str] = Field(min_length=1, max_length=500)

    @field_validator("tickers")
    @classmethod
    def normalize_tickers(cls, values: list[str]) -> list[str]:
        normalized = [value.strip().upper() for value in values if value.strip()]
        return list(dict.fromkeys(normalized))


class RelevanceResponse(BaseModel):
    items: list[TickerRelevance]


class CreateRunRequest(BaseModel):
    mode: Literal["DAILY", "MANUAL_BACKFILL", "RETRY"]
    requested_from: str | None = None
    requested_to: str | None = None
    engine_version: str


class CreateRunResponse(BaseModel):
    run_id: str = Field(min_length=1)
