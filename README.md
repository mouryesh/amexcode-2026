# Servicing Agent — Agent Layer (Person A)

Conversational card-servicing agent. **The LLM handles language; a deterministic
policy engine makes every decision; the hash-chained ledger proves it.**

## Layout

```
shared/    schemas.py · config.py · exceptions.py      (frozen contracts)
agent/     policy_engine.py · state.py · llm.py · classifier.py
           tools.py · escalation.py · nodes.py · graph.py
backend/   database.py · accounts.py · actions.py · ledger.py · seed.py   (Person B)
api/       main.py · routes.py
policies/  *.v1.yaml   (thresholds live here, NEVER in prompts)
tests/     test_policy_engine.py · test_ledger.py · test_classifier.py · test_graph.py
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python backend/seed.py     # seed the four personas
python demo.py             # run all four personas end-to-end, then verify the ledger
pytest -q                  # 60+ tests: policy branches, ledger tamper, classifier metrics, e2e
uvicorn api.main:app --reload   # serve the API (OpenAPI docs at /docs)
```

## LLM modes

`LLM_PROVIDER` (env) selects the model backend:

- `offline` (default) — a trained ML intent classifier (see below) + canned
  prose. No LLM API key needed, so demo prep and CI are reproducible.
- `anthropic` — set `ANTHROPIC_API_KEY` (+ optional `LLM_MODEL`).
- `openai` — set `OPENAI_API_KEY`.

`agent/llm.py` is the *only* file that talks to a model; swapping providers
touches nothing else.

## Intent classifier

`agent/classifier.py` tries three layers in order: LLM (if configured) →
trained ML classifier → keyword heuristic (last-resort fallback).

The ML classifier is local sentence-transformer embeddings
(`all-MiniLM-L6-v2`, 384-dim, CPU-only) feeding a Logistic Regression trained
on `agent/training_data.py`. **First run needs internet once** to download the
~80MB embedding model (cached afterward in `~/.cache/huggingface` — no network
needed on subsequent runs, and no LLM API key ever needed for this path).

```bash
python scripts/train_classifier.py    # re-run after editing training_data.py
```

Saves `agent/models/intent_classifier.joblib` (committed to git — teammates
don't need to retrain to run the app, only to change the training data).
`tests/test_classifier.py` measures real held-out accuracy: its ~40 utterances
are deliberately disjoint from the training set, so the reported
precision/recall/F1 is genuine generalisation, not memorisation.

## The three endpoints

| Endpoint | Purpose |
|---|---|
| `POST /agent/message` | Run the full pipeline for one member message |
| `GET  /audit/{session_id}` | Ledger records for a session, in chain order |
| `GET  /audit/verify` | Walk the chain → `OK` or `TAMPERED` + broken record id |

### Confirmation contract

The graph is stateless per turn. When a response has `awaiting_member=true`
(a `propose_confirm` action needs confirming, or an `auto_step_up` action needs
re-authentication), the client **resubmits the same `message`** with
`confirm=true` (or `reauthenticated=true`). Every turn is independently
replayable, and every write is idempotency-keyed on `session_id:tool`.

## The four personas

| Persona | Message | Outcome |
|---|---|---|
| Priya (`MEM-PRIYA`) | "waive my late fee" | **APPROVE** — fee reversed |
| Rahul (`MEM-RAHUL`) | "waive my late fee" | **DECLINE** — policy cited, appeal offered |
| Ananya (`MEM-ANANYA`) | "raise my limit" | **QUEUE** — underwriting case opened |
| Vikram (`MEM-VIKRAM`) | "I lost my job and can't pay" | **ESCALATE** — collections suppressed |

## Why it's safe

- **The model never sees a threshold.** `agent/policy_engine.py` is a pure
  function of account facts + versioned YAML. Every branch has a pytest.
- **Autonomy tiers are enforced in code** (`agent/tools.py`), never trusted to
  the model.
- **The ledger is append-only and hash-chained.** Tamper with a row →
  `/audit/verify` names the exact broken record.
- **Writes are atomic and the chain is concurrency-safe.** Each write action
  (mutation + ledger row + idempotency record) runs in a single
  `BEGIN IMMEDIATE` transaction (`backend/database.py:write_transaction`), so a
  crash can't leave a mutation without its audit row, and concurrent appends
  can't fork the chain. Proven by `tests/test_ledger.py` — 8 threads × 15
  appends verify clean; the same load forks an unserialised chain.
