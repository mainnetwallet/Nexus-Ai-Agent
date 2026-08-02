"""
Central configuration for Nexus-Agent.

All runtime configuration is loaded from environment variables (see .env.example).
Nothing sensitive is ever hardcoded here.
"""
from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "logs"
SCREENSHOT_DIR = BASE_DIR / "screenshots"
DATA_DIR = BASE_DIR / "data"

for d in (LOG_DIR, SCREENSHOT_DIR, DATA_DIR):
    d.mkdir(parents=True, exist_ok=True)


class LLMProvider(str, Enum):
    OPENAI = "openai"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


class BrowserChannel(str, Enum):
    CHROME = "chrome"
    EDGE = "edge"
    CHROMIUM = "chromium"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---
    app_name: str = "Nexus-Agent"
    environment: str = Field(default="development")
    debug: bool = Field(default=False)
    api_host: str = Field(default="127.0.0.1")
    api_port: int = Field(default=8000)
    api_auth_token: str = Field(default="", description="Bearer token required on all REST/WS calls")
    cors_allowed_origins: str = Field(
        default="", description="Comma separated origins allowed for CORS (e.g. dashboard URL). Empty + debug=true means allow all; empty + debug=false means allow none."
    )

    # --- LLM ---
    llm_provider: LLMProvider = Field(default=LLMProvider.ANTHROPIC)
    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    gemini_api_key: str = Field(default="")
    openrouter_api_key: str = Field(default="")
    llm_model_override: str = Field(default="")

    # --- Browser ---
    browser_channel: BrowserChannel = Field(default=BrowserChannel.CHROME)
    browser_headless: bool = Field(default=False)
    browser_user_data_dir: Optional[str] = Field(default=None, description="Path to persistent Chrome profile")
    browser_slow_mo_ms: int = Field(default=0)
    browser_default_timeout_ms: int = Field(default=30_000)

    # --- Telegram ---
    telegram_bot_token: str = Field(default="")
    telegram_allowed_user_ids: str = Field(default="", description="Comma separated Telegram user IDs allowed to control the bot")

    # --- Database / Memory ---
    sqlite_path: str = Field(default=str(DATA_DIR / "nexus_agent.db"))
    chroma_persist_dir: str = Field(default=str(DATA_DIR / "chroma"))

    # --- Wallet safety policy ---
    wallet_require_manual_approval: bool = Field(default=True)
    wallet_max_auto_approve_value_usd: float = Field(default=0.0, description="0 = always require manual approval")
    wallet_allowlisted_contracts: str = Field(default="", description="Comma separated contract addresses the agent may interact with automatically")

    # --- Wallet manager: read-only RPC endpoints per network, used only for
    # address-balance lookups and chain-id checks. No key material involved. ---
    rpc_ethereum: str = Field(default="https://eth.llamarpc.com")
    rpc_polygon: str = Field(default="https://polygon-rpc.com")
    rpc_arbitrum: str = Field(default="https://arb1.arbitrum.io/rpc")
    rpc_optimism: str = Field(default="https://mainnet.optimism.io")
    rpc_base: str = Field(default="https://mainnet.base.org")
    rpc_bsc: str = Field(default="https://bsc-dataseed.binance.org")

    @property
    def rpc_endpoints(self) -> dict[str, str]:
        return {
            "ethereum": self.rpc_ethereum,
            "polygon": self.rpc_polygon,
            "arbitrum": self.rpc_arbitrum,
            "optimism": self.rpc_optimism,
            "base": self.rpc_base,
            "bsc": self.rpc_bsc,
        }

    # --- Vision / OCR perception fallback ---
    vision_enabled: bool = Field(default=True, description="Allow the planner to fall back to a vision-LLM read of the screenshot")
    vision_min_elements_threshold: int = Field(default=3, description="If fewer than this many interactive elements are found in the DOM, trigger the vision/OCR fallback")
    vision_model_override: str = Field(default="", description="Optional vision-capable model id override (defaults to the active provider's vision model)")
    ocr_enabled: bool = Field(default=True, description="Allow Tesseract OCR extraction of on-canvas / image-only text as part of the perception fallback")
    ocr_lang: str = Field(default="eng")
    ocr_max_chars: int = Field(default=4000)

    # --- Live browser session (real-time screenshot streaming) ---
    live_session_enabled: bool = Field(default=True, description="Enable the live browser session stream (screenshot polling + WebSocket broadcast)")
    live_session_interval_ms: int = Field(default=1000, description="How often the live session captures a screenshot of the active task's page")
    live_session_jpeg_quality: int = Field(default=60, description="JPEG quality (1-100) used for live session frames -- lower is faster/smaller")

    # --- Plugin framework ---
    plugins_enabled: bool = Field(default=True, description="Discover and auto-enable plugins under plugins_dir at startup")
    plugins_dir: str = Field(default=str(BASE_DIR / "backend" / "plugins" / "installed"), description="Directory scanned for plugin .py files. Never populated over the network/API -- files must already be on disk")

    @field_validator("telegram_allowed_user_ids", "wallet_allowlisted_contracts", "cors_allowed_origins")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @property
    def cors_origins(self) -> list[str]:
        explicit = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        if explicit:
            return explicit
        return ["*"] if self.debug else []

    @property
    def allowed_telegram_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_allowed_user_ids.split(",") if x.strip().isdigit()}

    @property
    def allowlisted_contracts(self) -> set[str]:
        return {x.strip().lower() for x in self.wallet_allowlisted_contracts.split(",") if x.strip()}


settings = Settings()
