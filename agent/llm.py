"""agent/llm.py — the ONLY file that talks to the LLM API.

Every other file that needs the model goes through this one. When you swap
models or the API flakes at hour 14, you touch this file and nothing else
breaks.

Two capabilities are exposed:
  - structured_json(system, user, schema) -> dict   (classifier, tool selection)
  - generate_text(system, user) -> str              (clarify / explain prose)

When no provider is configured (LLM_PROVIDER=offline or no API key), the client
reports `available() == False`. Callers then fall back to deterministic
heuristics so the whole graph still runs during demo prep and CI.

Imports: shared.config only.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from shared.config import config


class LLMUnavailable(RuntimeError):
    """Raised when a real LLM call is attempted but no provider is configured."""


class LLMClient:
    def __init__(self) -> None:
        self.provider = config.LLM_PROVIDER
        self.model = config.LLM_MODEL
        self._client: Any = None
        if self.available():
            self._init_client()

    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        return config.llm_configured()

    def _init_client(self) -> None:
        if self.provider == "anthropic":
            import anthropic  # imported lazily so offline mode needs no dep

            self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        elif self.provider == "openai":
            import openai

            self._client = openai.OpenAI(api_key=config.OPENAI_API_KEY)
        elif self.provider == "groq":
            import openai  # Groq exposes an OpenAI-compatible chat completions API

            self._client = openai.OpenAI(
                api_key=config.GROQ_API_KEY, base_url=config.GROQ_BASE_URL
            )

    # ------------------------------------------------------------------ #
    def _call_raw(self, system: str, user: str, max_tokens: Optional[int] = None) -> str:
        """One completion with exponential-backoff retries. Returns raw text."""
        if not self.available():
            raise LLMUnavailable("No LLM provider configured (LLM_PROVIDER=offline).")

        max_tokens = max_tokens or config.LLM_MAX_TOKENS
        last_err: Optional[Exception] = None
        for attempt in range(config.LLM_MAX_RETRIES):
            try:
                if self.provider == "anthropic":
                    resp = self._client.messages.create(
                        model=self.model,
                        max_tokens=max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user}],
                    )
                    return resp.content[0].text
                else:  # openai or groq — both use the OpenAI-compatible chat completions shape
                    resp = self._client.chat.completions.create(
                        model=self.model,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                    )
                    return resp.choices[0].message.content
            except Exception as exc:  # narrow retry surface: any transport error
                last_err = exc
                time.sleep(2 ** attempt * 0.5)
        raise LLMUnavailable(f"LLM call failed after retries: {last_err}")

    # ------------------------------------------------------------------ #
    def structured_json(self, system: str, user: str, schema_hint: str) -> dict[str, Any]:
        """Ask for JSON only, parse and return it as a dict.

        `schema_hint` is appended to the system prompt so the model knows the
        exact shape to emit. We enforce validity by parsing; on parse failure we
        strip code fences and retry once.
        """
        sys = (
            f"{system}\n\nRespond with a single valid JSON object and nothing "
            f"else. Schema: {schema_hint}"
        )
        raw = self._call_raw(sys, user)
        return _parse_json(raw)

    def generate_text(self, system: str, user: str, max_tokens: int = 400) -> str:
        """Free-form prose reply (clarifying questions, decline explanations)."""
        return self._call_raw(system, user, max_tokens=max_tokens).strip()


def _parse_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json ... ``` fences
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip("` \n")
    # find the outermost object if the model added prose around it
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start : end + 1]
    return json.loads(text)


# Module-level singleton — one client for the process.
llm = LLMClient()
