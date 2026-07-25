"""shared/config.py — all settings in one place.

Read from environment variables with sensible defaults. No magic numbers
anywhere else in the codebase.

Imported by: everything.
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env")
except ImportError:
    pass  # python-dotenv is optional; env vars set another way still work


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


class Config:
    # --- Database -------------------------------------------------------- #
    DB_PATH: str = _env("DB_PATH", str(_ROOT / "servicing.db"))

    # --- LLM ------------------------------------------------------------- #
    # provider: "anthropic" | "openai" | "groq" | "offline"
    # "offline" uses the deterministic heuristic fallbacks so the whole graph
    # runs without any API key during demo prep and CI.
    LLM_PROVIDER: str = _env("LLM_PROVIDER", "offline")
    _MODEL_DEFAULTS = {
        "anthropic": "claude-sonnet-5",
        "openai": "gpt-4o-mini",
        "groq": "llama-3.3-70b-versatile",
    }
    LLM_MODEL: str = _env("LLM_MODEL", _MODEL_DEFAULTS.get(LLM_PROVIDER, "claude-sonnet-5"))
    ANTHROPIC_API_KEY: str = _env("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = _env("OPENAI_API_KEY", "")
    GROQ_API_KEY: str = _env("GROQ_API_KEY", "")
    GROQ_BASE_URL: str = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
    LLM_MAX_TOKENS: int = int(_env("LLM_MAX_TOKENS", "1024"))
    LLM_MAX_RETRIES: int = int(_env("LLM_MAX_RETRIES", "3"))

    # --- Classifier ------------------------------------------------------ #
    CONFIDENCE_THRESHOLD: float = float(_env("CONFIDENCE_THRESHOLD", "0.65"))

    # --- Session behaviour ------------------------------------------------ #
    # How many prior DECLINEs for the same (policy_id, reason_code) in one
    # session before the next identical request auto-escalates instead of
    # declining again. 1 = the first ask declines normally; the second ask
    # (repeat) escalates.
    DECLINE_REPEAT_ESCALATE_THRESHOLD: int = int(_env("DECLINE_REPEAT_ESCALATE_THRESHOLD", "1"))

    # --- Policies -------------------------------------------------------- #
    POLICY_DIR: str = _env("POLICY_DIR", str(_ROOT / "policies"))

    # --- API ------------------------------------------------------------- #
    API_HOST: str = _env("API_HOST", "0.0.0.0")
    API_PORT: int = int(_env("API_PORT", "8000"))

    @classmethod
    def llm_configured(cls) -> bool:
        """True when a real LLM provider is usable; else the graph runs offline."""
        if cls.LLM_PROVIDER == "anthropic":
            return bool(cls.ANTHROPIC_API_KEY)
        if cls.LLM_PROVIDER == "openai":
            return bool(cls.OPENAI_API_KEY)
        if cls.LLM_PROVIDER == "groq":
            return bool(cls.GROQ_API_KEY)
        return False


config = Config()
