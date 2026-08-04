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
    # Commercial / premium
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GEMINI = "gemini"
    XAI = "xai"
    MOONSHOT = "moonshot"
    QWEN = "qwen"
    ZHIPU = "zhipu"

    # Free / developer tier
    OPENROUTER = "openrouter"
    GROQ = "groq"
    CEREBRAS = "cerebras"
    COHERE = "cohere"
    HUGGINGFACE = "huggingface"
    NVIDIA_NIM = "nvidia_nim"
    SAMBANOVA = "sambanova"
    TOGETHER = "together"
    FIREWORKS = "fireworks"
    DEEPINFRA = "deepinfra"
    MISTRAL = "mistral"
    REPLICATE = "replicate"
    AI21 = "ai21"


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
    # Override the request URL for Anthropic/OpenAI -- lets these two point
    # at an OpenAI/Anthropic-compatible gateway (e.g. AgentRouter, Azure,
    # a self-hosted proxy) instead of the real provider endpoint. Leave
    # blank to use the real api.anthropic.com / api.openai.com endpoints.
    anthropic_base_url: str = Field(default="https://api.anthropic.com/v1/messages")
    openai_base_url: str = Field(default="https://api.openai.com/v1/chat/completions")

    # --- LLM: additional provider API keys (AI Model Manager) ---
    xai_api_key: str = Field(default="")
    moonshot_api_key: str = Field(default="")
    qwen_api_key: str = Field(default="")
    zhipu_api_key: str = Field(default="")
    groq_api_key: str = Field(default="")
    cerebras_api_key: str = Field(default="")
    cohere_api_key: str = Field(default="")
    huggingface_api_key: str = Field(default="")
    nvidia_nim_api_key: str = Field(default="")
    sambanova_api_key: str = Field(default="")
    together_api_key: str = Field(default="")
    fireworks_api_key: str = Field(default="")
    deepinfra_api_key: str = Field(default="")
    mistral_api_key: str = Field(default="")
    replicate_api_key: str = Field(default="")
    ai21_api_key: str = Field(default="")

    # --- AI Model Manager ---
    ai_smart_routing_enabled: bool = Field(default=False, description="When true, task-type routing rules pick the provider instead of llm_provider")
    ai_fallback_provider: LLMProvider = Field(default=LLMProvider.OPENROUTER, description="Provider tried when the active provider fails (timeout/error/rate-limit/unavailable)")
    ai_provider_priority: str = Field(default="", description="Comma separated provider ids, highest priority first. Empty = enum declaration order")
    ai_disabled_providers: str = Field(default="", description="Comma separated provider ids excluded from auto-routing and fallback")

    @property
    def ai_provider_priority_list(self) -> list[str]:
        return [p.strip() for p in self.ai_provider_priority.split(",") if p.strip()]

    @property
    def ai_disabled_providers_set(self) -> set[str]:
        return {p.strip() for p in self.ai_disabled_providers.split(",") if p.strip()}

    # --- Browser ---
    browser_channel: BrowserChannel = Field(default=BrowserChannel.CHROME)
    browser_headless: bool = Field(default=True)
    browser_user_data_dir: Optional[str] = Field(default=None, description="Path to persistent Chrome profile")
    browser_slow_mo_ms: int = Field(default=0)
    browser_executable_path: Optional[str] = Field(
        default=None,
        description="Full path to a Chrome/Chromium binary for the 'Open in Chrome' manual profile "
        "session (backend/browser/manual_session.py). If unset, PATH and common per-OS install "
        "locations are checked automatically.",
    )
    browser_default_timeout_ms: int = Field(default=30_000)

    # --- Telegram ---
    telegram_bot_token: str = Field(default="")
    telegram_allowed_user_ids: str = Field(default="", description="Comma separated Telegram user IDs allowed to control the bot")

    # --- Database / Memory ---
    sqlite_path: str = Field(default=str(DATA_DIR / "nexus_agent.db"))
    chroma_persist_dir: str = Field(default=str(DATA_DIR / "chroma"))

    # --- Memory Improvements (importance, categories, expiration) ---
    memory_expiration_enabled: bool = Field(
        default=True, description="Run the periodic sweep that archives/forgets low-value, stale memories"
    )
    memory_expiration_check_interval_hours: int = Field(
        default=24, description="How often the expiration sweep runs while the backend is up"
    )
    memory_expiration_days: int = Field(
        default=90, description="Age (days) past which a low-importance memory becomes eligible for expiration"
    )
    memory_low_importance_threshold: float = Field(
        default=0.3, description="Effective importance (0-1) below which an aged memory is eligible for expiration"
    )
    memory_expire_action: str = Field(
        default="archive", description="What the expiration sweep does to eligible memories: 'archive' (reversible) or 'forget' (permanent delete)"
    )
    memory_duplicate_scan_limit: int = Field(
        default=500, description="Max number of most-recent active memories inspected per duplicate-detection scan"
    )

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

    # --- Skill Learning System ---
    skills_enabled: bool = Field(default=True, description="Enable the Skill Library: search-before-plan matching, teach mode, and post-task 'save as skill' suggestions")
    skills_match_min_score: float = Field(default=0.62, description="Minimum semantic match score (0-1) required before a skill is auto-executed instead of planning from scratch")

    # --- MCP Core ---
    mcp_enabled: bool = Field(default=True, description="Master switch for the MCP Core (tool connectors)")
    mcp_filesystem_enabled: bool = Field(default=True)
    mcp_filesystem_roots: str = Field(default="", description="Comma separated absolute paths the filesystem connector may access. Empty = project BASE_DIR only")
    mcp_terminal_enabled: bool = Field(default=True, description="Terminal connector is real code execution -- opt-in only")
    mcp_terminal_commands_allowlist: str = Field(default="", description="Comma separated executable names the terminal connector may run. Empty = connector's built-in default allow-list")
    mcp_terminal_timeout_seconds: int = Field(default=30)
    mcp_terminal_working_dir: str = Field(default=str(DATA_DIR))
    mcp_browser_enabled: bool = Field(default=True)
    mcp_browser_timeout_seconds: int = Field(default=20)
    mcp_github_enabled: bool = Field(default=True)
    mcp_github_token: str = Field(default="")
    mcp_github_default_owner: str = Field(default="")
    mcp_github_default_repo: str = Field(default="")
    # X/Discord/Gmail connectors automate the live authenticated browser
    # session (backend/mcp/connectors/social_base.py) rather than calling an
    # API with a key -- these settings are display/labeling only (the
    # "account" values never hold a credential; the Identity/Profile
    # Manager's per-profile account labels, e.g. ProfileRecord.x_account,
    # remain the source of truth for which account a given task actually
    # runs as).
    mcp_x_enabled: bool = Field(default=True)
    mcp_x_account: str = Field(default="", description="Display-only label for the dashboard, e.g. '@handle'")
    mcp_discord_enabled: bool = Field(default=True)
    mcp_discord_account: str = Field(default="", description="Display-only label for the dashboard")
    mcp_gmail_enabled: bool = Field(default=True)
    mcp_gmail_account: str = Field(default="", description="Display-only label for the dashboard, e.g. 'name@gmail.com'")
    mcp_tool_call_timeout_seconds: float = Field(default=30.0)
    mcp_router_min_score: float = Field(default=1.0)

    @property
    def mcp_filesystem_roots_list(self) -> list[str]:
        return [p.strip() for p in self.mcp_filesystem_roots.split(",") if p.strip()]

    @property
    def mcp_terminal_commands_allowlist_set(self) -> set[str]:
        return {c.strip() for c in self.mcp_terminal_commands_allowlist.split(",") if c.strip()}

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

