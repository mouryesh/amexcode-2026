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


def _flag(name: str, default: str) -> bool:
    return _env(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Config:
    # --- Storage engine ---------------------------------------------------- #
    # PG_DSN empty (the default) selects SQLite, so the whole system runs on a
    # laptop with nothing installed. Set PG_DSN in .env to switch to Postgres.
    # backend/database.py also falls back to SQLite if psycopg is missing.
    DB_PATH: str = _env("DB_PATH", str(_ROOT / "servicing.db"))
    PG_DSN: str = _env("PG_DSN", "")
    PG_POOL_MIN: int = int(_env("PG_POOL_MIN", "1"))
    PG_POOL_MAX: int = int(_env("PG_POOL_MAX", "10"))

    # Advisory-lock key that serialises hash-chain appends. Any 64-bit constant
    # works; it only has to be the same in every process touching the ledger.
    LEDGER_LOCK_KEY: int = int(_env("LEDGER_LOCK_KEY", "834127700001"))

    # --- MongoDB: live flow state ----------------------------------------- #
    MONGO_URI: str = _env("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB: str = _env("MONGO_DB", "servicing")

    # --- Splunk: searchable audit mirror ----------------------------------- #
    # Disabled => shipping is a no-op. The Postgres chain remains authoritative,
    # so the agent keeps working; only search and dashboards degrade.
    SPLUNK_ENABLED: bool = _flag("SPLUNK_ENABLED", "false")
    SPLUNK_HEC_URL: str = _env("SPLUNK_HEC_URL", "https://localhost:8088/services/collector/event")
    SPLUNK_HEC_TOKEN: str = _env("SPLUNK_HEC_TOKEN", "")
    SPLUNK_INDEX: str = _env("SPLUNK_INDEX", "amex_audit")
    SPLUNK_SOURCETYPE: str = _env("SPLUNK_SOURCETYPE", "amex:agent:audit")
    SPLUNK_VERIFY_TLS: bool = _flag("SPLUNK_VERIFY_TLS", "false")
    SPLUNK_TIMEOUT_S: float = float(_env("SPLUNK_TIMEOUT_S", "3"))
    SPLUNK_QUEUE_MAX: int = int(_env("SPLUNK_QUEUE_MAX", "10000"))

    # --- LLM --------------------------------------------------------------- #
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

    # --- Classifier --------------------------------------------------------- #
    CONFIDENCE_THRESHOLD: float = float(_env("CONFIDENCE_THRESHOLD", "0.65"))
    CONFIDENCE_ACT: float = float(_env("CONFIDENCE_ACT", "0.82"))
    CONFIDENCE_REJECT: float = float(_env("CONFIDENCE_REJECT", "0.55"))
    CONFIDENCE_MARGIN: float = float(_env("CONFIDENCE_MARGIN", "0.12"))

    # --- Session behaviour --------------------------------------------------- #
    DECLINE_REPEAT_ESCALATE_THRESHOLD: int = int(_env("DECLINE_REPEAT_ESCALATE_THRESHOLD", "1"))
    SLOT_MAX_ATTEMPTS: int = int(_env("SLOT_MAX_ATTEMPTS", "3"))
    # A slot value the extractor read out of free text is only accepted at or
    # above this confidence. Below it the value is discarded and the member is
    # asked again — a wrong address written confidently is worse than a re-ask.
    SLOT_EXTRACT_MIN_CONFIDENCE: float = float(_env("SLOT_EXTRACT_MIN_CONFIDENCE", "0.7"))
    CLARIFY_MAX_QUESTIONS: int = int(_env("CLARIFY_MAX_QUESTIONS", "2"))
    CONFIRM_TTL_S: int = int(_env("CONFIRM_TTL_S", "120"))
    # There is no auth backend wired to this prototype, so an auto_step_up
    # action has nothing to re-authenticate against and would hang forever.
    # With this on, the secure challenge is SIMULATED: the tier is still
    # declared and still enforced, the flow still passes through it, and every
    # ledger row records step_up="simulated" so no run can be mistaken for a
    # real re-authentication. Set STEP_UP_SIMULATED=false the day an auth
    # service exists; nothing else changes.
    STEP_UP_SIMULATED: bool = _flag("STEP_UP_SIMULATED", "true")
    FLOW_IDLE_TIMEOUT_S: int = int(_env("FLOW_IDLE_TIMEOUT_S", "600"))
    RECONCILE_MAX_POLLS: int = int(_env("RECONCILE_MAX_POLLS", "3"))

    # --- Policies ------------------------------------------------------------ #
    POLICY_DIR: str = _env("POLICY_DIR", str(_ROOT / "policies"))

    # --- API ----------------------------------------------------------------- #
    API_HOST: str = _env("API_HOST", "0.0.0.0")
    API_PORT: int = int(_env("API_PORT", "8080"))

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

    @classmethod
    def splunk_configured(cls) -> bool:
        return cls.SPLUNK_ENABLED and bool(cls.SPLUNK_HEC_TOKEN)


config = Config()
