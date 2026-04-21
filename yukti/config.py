"""
yukti/config.py
All runtime configuration via pydantic-settings.
Reads from .env file or Doppler-injected environment.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Broker ────────────────────────────────────────
    dhan_client_id: str = ""
    dhan_access_token: str = ""

    # ── AI provider ───────────────────────────────────
    # "claude"  → Anthropic Claude Sonnet 4.6  ($3/$15 per MTok)
    # "gemini"  → Google Gemini 2.0 Flash      (free ≤15 rpm, then $0.075/MTok)
    # "ab_test" → run both per call, log comparison, execute the primary
    ai_provider: Literal["claude", "gemini", "ab_test"] = "gemini"

    # Claude
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 1000

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # Shared AI settings
    ai_max_retries: int = 2
    ai_temperature: float = 0.1   # low = deterministic decisions

    # A/B test — when ai_provider="ab_test"
    # ab_primary is executed for real; ab_secondary is called in background and logged only
    ab_primary: Literal["claude", "gemini"] = "gemini"
    ab_secondary: Literal["claude", "gemini"] = "claude"

    # Voyage AI (journal embeddings)
    voyage_api_key: str = ""

    # ── Telegram ──────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Data stores ───────────────────────────────────
    postgres_url: str = "postgresql+psycopg://yukti:password@localhost:5432/yukti"
    redis_url: str = "redis://localhost:6379/0"

    # ── Operating mode ────────────────────────────────
    mode: Literal["live", "paper", "shadow", "backtest"] = "paper"

    # ── Account ───────────────────────────────────────
    account_value: float = Field(default=500_000.0, gt=0)
    risk_pct: float = Field(default=0.01, gt=0, le=0.05)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    max_single_stock_pct: float = Field(default=0.25, gt=0, le=1.0)
    max_sector_pct: float = Field(default=0.40, gt=0, le=1.0)

    # ── Risk gates ────────────────────────────────────
    daily_loss_limit_pct: float = Field(default=0.02, gt=0)
    min_rr: float = Field(default=1.8, gt=0)
    min_conviction: int = Field(default=5, ge=1, le=10)
    max_loss_cap_pct: float = Field(default=0.015, gt=0)
    max_per_trade_risk_pct: float = Field(default=0.05, gt=0, le=0.1)
    atr_multiplier: float = Field(default=1.5, gt=0)
    max_atr_multiplier: float = Field(default=2.5, gt=0)

    # ── Cooldown ──────────────────────────────────────
    cooldown_cycles: int = Field(default=3, ge=1)

    # ── Candle config ─────────────────────────────────
    candle_interval: str = "5"
    candle_history: int = 100

    # ── API settings ───────────────────────────────────
    cors_allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://localhost:5173"])

    # ── Universe scanner ─────────────────────────────
    scanner_pick_count: int = Field(default=15, ge=5, le=50)
    min_turnover_cr: float = Field(default=10, gt=0)
    volume_surge_threshold: float = Field(default=2.0, gt=0)
    price_move_threshold: float = Field(default=1.5, gt=0)
    intraday_refresh_times: list[str] = Field(default_factory=lambda: ["10:00", "12:00"])

    # ── Daily candle (multi-timeframe) ────────────────
    daily_candle_history: int = Field(default=60, ge=20, le=200)
    daily_cache_ttl: int = Field(default=3600 * 8, ge=3600)

    # ── Scheduler times (IST) ─────────────────────────
    market_open:    str = "09:15"
    morning_prep:   str = "09:00"
    eod_squareoff:  str = "15:10"
    daily_journal:  str = "16:00"
    position_recon: str = "09:05"

    # ── DhanHQ constants ──────────────────────────────
    exchange_nse:     str = "NSE_EQ"
    exchange_bse:     str = "BSE_EQ"
    product_intraday: str = "INTRADAY"
    product_delivery: str = "DELIVERY"

    @field_validator("watchlist", mode="before")
    @classmethod
    def split_watchlist(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [s.strip().upper() for s in v.split(",") if s.strip()]
        return [s.upper() for s in v]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
