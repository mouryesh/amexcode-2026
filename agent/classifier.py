"""agent/classifier.py — raw member message -> {intent, confidence}.

Task 1 of the problem statement, as a named, measurable component. Maps to one
of the 12 taxonomy categories. Below the confidence threshold in config it
returns 'clarify' and never guesses.

Three layers, tried in order:
  1. LLM (llm.py), if a provider is configured — strict JSON schema.
  2. Trained ML classifier (local sentence-transformer embeddings ->
     Logistic Regression, see scripts/train_classifier.py) — the offline
     default. Needs internet ONCE to download the embedding model weights
     (~80MB, then cached locally); no LLM API key, no network per-request.
  3. Keyword heuristic — last-resort fallback if the ML artifact or
     sentence-transformers isn't available in this environment, so the graph
     never hard-fails on a missing optional dependency.

Imports: llm, shared.schemas, shared.config.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from agent.llm import LLMUnavailable, llm
from shared.config import config
from shared.schemas import Intent

# The 12-category taxonomy. Every category has a declared autonomy tier in
# tools.py; four are automated end-to-end, the rest escalate with full context.
TAXONOMY = [
    "fee_waiver",            # "waive my late fee"
    "credit_limit_increase", # "raise my limit"
    "card_replacement",      # "my card is damaged, send a new one"
    "card_block",            # "I lost my card / block it"
    "explain_charge",        # "what is this charge"
    "account_info",          # "what's my balance / limit"
    "reset_pin",             # "reset my PIN"
    "update_address",        # "change my address"
    "hardship",              # "I can't pay / lost my job"
    "dispute_transaction",   # "I didn't make this purchase" (out of scope: map)
    "payment_issue",         # "my payment failed" (out of scope: map)
    "profile_update",        # "update my email/phone" (out of scope: map)
]

# Keyword signals for the offline heuristic. Ordered by specificity: hardship
# is checked first because duty-of-care must win over a co-occurring request.
_HEURISTICS: list[tuple[str, list[str]]] = [
    ("hardship", ["can't pay", "cannot pay", "lost my job", "job loss", "laid off",
                  "struggling", "hardship", "financial difficulty", "unemployed",
                  "can't afford", "cannot afford", "behind on", "medical bills"]),
    ("fee_waiver", ["waive", "late fee", "reverse the fee", "remove the fee",
                    "fee waiver", "charged a fee", "waived"]),
    ("credit_limit_increase", ["raise my limit", "increase my limit", "higher limit",
                               "credit limit", "limit increase", "raise the limit"]),
    ("card_replacement", ["replacement", "new card", "damaged card", "replace my card",
                          "card is broken", "reissue"]),
    ("card_block", ["lost my card", "stolen", "block my card", "freeze my card",
                    "lost card", "someone stole"]),
    ("explain_charge", ["what is this charge", "explain this charge", "why was i charged",
                        "unknown charge", "what's this charge", "explain the charge"]),
    ("account_info", ["my balance", "current balance", "how much do i owe",
                      "what's my limit", "account info", "available credit"]),
    ("reset_pin", ["reset my pin", "forgot my pin", "change my pin", "new pin"]),
    ("update_address", ["change my address", "update my address", "new address",
                        "moved", "update address"]),
    ("dispute_transaction", ["didn't make this", "did not make", "dispute", "fraudulent charge",
                             "unauthorized", "i didn't buy"]),
    ("payment_issue", ["payment failed", "payment didn't go", "couldn't pay online",
                       "payment error", "autopay failed"]),
    ("profile_update", ["update my email", "change my phone", "update my number",
                        "change my email", "update profile"]),
]

_SYSTEM = (
    "You are an intent classifier for a credit-card servicing agent. "
    "Classify the member's message into exactly one category. "
    f"Categories: {', '.join(TAXONOMY)}. "
    "If the message expresses financial distress, inability to pay, or job loss, "
    "always classify it as 'hardship' regardless of any other request. "
    "If you are not confident which single category applies, return low confidence."
)
_SCHEMA_HINT = '{"label": "<one category>", "confidence": <0.0-1.0>, "rationale": "<short>"}'


def _heuristic(message: str) -> Intent:
    """Deterministic keyword classifier — last-resort fallback only."""
    text = message.lower()
    for label, keywords in _HEURISTICS:
        for kw in keywords:
            if kw in text:
                # Hardship is high-confidence by design; others solidly above
                # threshold when a distinctive phrase matched.
                conf = 0.95 if label == "hardship" else 0.82
                return Intent(label=label, confidence=conf,
                              rationale=f"matched '{kw}'")
    # Nothing distinctive matched → force a clarification.
    return Intent(label="clarify", confidence=0.30,
                  rationale="no distinctive intent signal")


_ML_ARTIFACT_PATH = Path(__file__).resolve().parent / "models" / "intent_classifier.joblib"
_ml_state: Optional[dict[str, Any]] = None
_ml_load_failed = False


def _load_ml_classifier() -> Optional[dict[str, Any]]:
    """Load the trained embedder + classifier once, lazily, and cache it.

    Returns None (permanently, for this process) if the artifact or its
    dependencies aren't available — callers fall back to the heuristic.
    """
    global _ml_state, _ml_load_failed
    if _ml_state is not None or _ml_load_failed:
        return _ml_state
    try:
        import joblib
        from sentence_transformers import SentenceTransformer

        artifact = joblib.load(_ML_ARTIFACT_PATH)
        artifact["embedder"] = SentenceTransformer(artifact["model_name"])
        _ml_state = artifact
        return _ml_state
    except Exception:
        _ml_load_failed = True
        return None


def _ml_classify(message: str) -> Intent:
    """Embed the message, classify with the trained Logistic Regression.

    Confidence is the model's real predict_proba for the top class. Rationale
    is the nearest training example by cosine similarity — a linear model on
    dense embeddings has no keyword-level "why" the way the heuristic did, so
    this substitutes a concrete, inspectable stand-in.
    """
    import numpy as np

    state = _load_ml_classifier()
    embedding = state["embedder"].encode([message])[0]
    clf = state["classifier"]
    proba = clf.predict_proba([embedding])[0]
    classes = clf.classes_
    top_idx = int(np.argmax(proba))
    label = str(classes[top_idx])
    confidence = float(proba[top_idx])

    train_emb = state["train_embeddings"]
    sims = (train_emb @ embedding) / (
        np.linalg.norm(train_emb, axis=1) * np.linalg.norm(embedding) + 1e-9
    )
    nearest = state["train_texts"][int(np.argmax(sims))]
    rationale = f"closest to training example: '{nearest}'"

    return Intent(label=label, confidence=confidence, rationale=rationale)


def _offline_classify(message: str) -> Intent:
    """ML classifier if available, else the keyword heuristic."""
    if _load_ml_classifier() is not None:
        try:
            return _ml_classify(message)
        except Exception:
            pass
    return _heuristic(message)


def classify(message: str) -> Intent:
    """Return the classified Intent, or 'clarify' when below threshold.

    Never guesses: below the configured confidence threshold the label is
    forced to 'clarify' so the graph asks a follow-up question instead of
    acting on a low-confidence guess.
    """
    if llm.available():
        try:
            data = llm.structured_json(_SYSTEM, message, _SCHEMA_HINT)
            label = str(data.get("label", "clarify"))
            confidence = float(data.get("confidence", 0.0))
            rationale = data.get("rationale")
            if label not in TAXONOMY:
                label, confidence = "clarify", min(confidence, 0.4)
            intent = Intent(label=label, confidence=confidence, rationale=rationale)
        except (LLMUnavailable, ValueError, KeyError):
            intent = _offline_classify(message)
    else:
        intent = _offline_classify(message)

    # Enforce the threshold uniformly, whatever produced the intent.
    if intent.label != "hardship" and intent.confidence < config.CONFIDENCE_THRESHOLD:
        return Intent(label="clarify", confidence=intent.confidence,
                      rationale=intent.rationale)
    return intent


def is_hardship(message: str, intent: Intent) -> bool:
    """True when the message carries distress signals (drives escalation).

    Checked independently of the top intent so a hardship phrase inside an
    otherwise routine request still triggers duty-of-care handling.
    """
    if intent.label == "hardship":
        return True
    text = message.lower()
    return any(kw in text for kw in _HEURISTICS[0][1])
