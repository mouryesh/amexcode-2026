# PROJECT HANDOFF — current state (as built)

> Paste this file as context when starting a new session, or hand it to a
> teammate joining the project. It describes what **actually exists across all
> branches right now**, not the original plan.
>
> Companion docs: `handoff.md` (original design rationale + frozen contracts,
> still valid), `BACKEND_RECONCILIATION.md` (how to merge the two backends),
> `open_questions.txt` (tracked design gaps), `README.md` (quick start).

Last updated: 2026-07-25.

---

## 1. What this is

A conversational credit-card **servicing agent** for a hackathon. A member sends
a message ("waive my late fee"); the agent classifies intent, gathers the facts,
runs a **deterministic policy** to decide, executes a real write action, and logs
every step to a **hash-chained audit trail** — escalating to a human with full
context when it shouldn't act alone.

**The thesis (memorise):** the LLM handles language; a deterministic policy
engine makes every decision; the ledger proves it. Neither does the other's job.

---

## 2. Branch map — where everything lives

| Branch | State | Contents |
|---|---|---|
| **`mouryesh`** | **CANONICAL — the complete, working system** | Full agent layer + backend + API + tests + trained ML classifier + Groq. 72/72 tests passing. Latest commit `e006c15`. |
| `main` | Pristine submission branch | Just a README (problem statement). Keep clean; merge final work here at the end. |
| `Yash_Amex` | Teammate's backend (parallel build) | `backend/` (accounts, actions, database, ledger) + `shared/exceptions.py`. Well-written, self-checks in each file, but **diverged from the agent layer** — see §7. Not yet merged. |

**Canonical decision:** `mouryesh` is the integration base. It is the only
branch that runs end-to-end (agent + backend + API together). `Yash_Amex`'s
backend is a parallel reimplementation of a slice `mouryesh` already has working;
the merge aligns it to `mouryesh` (see `BACKEND_RECONCILIATION.md`), keeping the
genuinely-better `Yash_Amex` ideas.

---

## 3. Architecture as built (5 layers, strict downward deps)

```
API      →  api/main.py, api/routes.py
Agent    →  agent/classifier.py, llm.py, tools.py, escalation.py,
            nodes.py, graph.py, state.py, training_data.py
Decision →  agent/policy_engine.py  (pure function) + policies/*.yaml
Backend  →  backend/database.py, accounts.py, actions.py, ledger.py,
            sessions.py, seed.py
Shared   →  shared/schemas.py, config.py, exceptions.py  (frozen contracts)
```

Imports point down only. The policy engine imports nothing but schemas + yaml.
Full file-by-file walkthrough is in `README.md` and `handoff.md` §20.

### Request lifecycle (what runs, in order)

1. `api/routes.py` receives `POST /agent/message` (validated by `AgentRequest`).
2. `backend/sessions.py:get_history` loads prior turns.
3. `agent/state.py:new_state` builds `AgentState` with history threaded in.
4. `agent/graph.py:run` drives the graph (LangGraph, or a pure-Python fallback).
5. `classify_intent` → `classifier.py` (LLM or ML) + `accounts.get_account_facts`.
6. `check_slots` → auto-resolves parameters from `accounts.py`.
7. `run_policy` → `policy_engine.evaluate(policy_id, facts)` on `policies/*.yaml`;
   repeated-decline override checks `sessions.count_prior_declines`.
8. `execute_tool` → `tools.execute` (autonomy-tier gate) → `actions.py`
   (atomic `write_transaction`: mutate + `ledger.append_on_conn` + idempotency).
9. `respond_to_member` formats the reply (receipt card / prose).
10. `api/routes.py` persists both turns via `sessions.record_turn`, returns
    `AgentResponse`.

Branches: low-confidence → clarify; hardship/out-of-scope → `escalation.py`;
DECLINE → LLM explanation; QUEUE → underwriting case.

---

## 4. What's built and working

| Capability | Where | Status |
|---|---|---|
| Intent classification (12 categories + clarify) | `classifier.py`, `training_data.py`, `agent/models/intent_classifier.joblib` | ✅ ML: embeddings (all-MiniLM-L6-v2) + LogReg, **94.87% held-out** |
| Deterministic policy engine | `policy_engine.py`, `policies/*.yaml` | ✅ Pure function, every branch pytested |
| Autonomy tiers (auto / step-up / confirm / escalate) | `tools.py:_check_tier` | ✅ Enforced in code (step-up OTP is a stub — see §6) |
| Hash-chained audit ledger | `ledger.py` | ✅ Append-only, tamper-evident, **atomic + concurrency-safe** |
| Escalation packet (complete-context handoff) | `escalation.py` | ✅ Transcript, facts, decisions, sentiment, next action |
| Session memory | `sessions.py`, `messages` table | ✅ History loaded/saved each turn |
| Repeated-decline auto-escalation | `nodes.py:run_policy` | ✅ 2nd identical decline → `REPEATED_DECLINE_ESCALATED` |
| Idempotency (no double-writes) | `actions.py` | ✅ Keyed `session:tool`, atomic |
| LLM provider (Groq) | `config.py`, `llm.py`, `.env` | ✅ OpenAI-compatible; offline ML fallback |
| API (3 endpoints + health) | `api/routes.py` | ✅ |
| Tests | `tests/` | ✅ **72/72 passing** |

### The four demo personas (seed in `backend/seed.py`)

| Persona | Message | Outcome |
|---|---|---|
| Priya (`MEM-PRIYA`) | "waive my late fee" | **APPROVE** — fee reversed, receipt |
| Rahul (`MEM-RAHUL`) | "waive my late fee" | **DECLINE** — policy cited, appeal offered; **2nd ask → ESCALATE** |
| Ananya (`MEM-ANANYA`) | "raise my limit" | **QUEUE** — underwriting case opened |
| Vikram (`MEM-VIKRAM`) | "I lost my job and can't pay" | **ESCALATE** — collections suppressed |

---

## 5. How to run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python backend/seed.py            # seed the four personas
python demo.py                    # all four personas end-to-end + ledger verify
pytest -q                         # 72 tests
uvicorn api.main:app --reload     # serve API (OpenAPI docs at /docs)
```

**First run needs internet once** to download the ~80MB embedding model
(`all-MiniLM-L6-v2`), cached afterward in `~/.cache/huggingface`.

### LLM configuration

`LLM_PROVIDER` selects the backend (read from `.env`, auto-loaded):
- `groq` (currently set) — `GROQ_API_KEY` in `.env`, model `llama-3.3-70b-versatile`.
- `anthropic` / `openai` — set the matching key.
- `offline` — no LLM; uses the trained ML classifier + canned prose. Tests always
  force this, so CI is deterministic and needs no key.

`.env` is gitignored — the Groq key is NOT in the repo. Retrain the classifier
only after editing `training_data.py`: `python scripts/train_classifier.py`.

### Confirmation contract (stateless per turn)

When a response has `awaiting_member=true` (a `propose_confirm` action needs a
yes, or `auto_step_up` needs re-auth), the client **resubmits the same message**
with `confirm=true` (or `reauthenticated=true`). Every turn is independently
replayable; every write is idempotency-keyed.

---

## 6. Known limitations (honest — for Q&A and future work)

These are the gaps between a hackathon prototype and a real Amex deployment.
The architecture (deterministic decisions + audit) scales; these surrounding
concerns are what a launch would need to close:

- **Step-up / OTP is a boolean stub.** `reauthenticated=true` is asserted by the
  caller with nothing verifying it. Real auth = server-issued, single-use,
  expiring token. (Tiers are still enforced in code, so the model can't
  authorize — only the verification of identity is stubbed.)
- **PII crosses to a third party.** `llm.py` sends the member message (and, if you
  ground prose, facts) to Groq. A bank needs redaction/tokenisation or a
  self-hosted/BAA model. Biggest "not launchable as-is" item.
- **Regulated outputs.** Credit declines legally require templated adverse-action
  notices; an LLM must not author them. Template off `reason_code`.
- **Classifier realism.** 94.87% is on clean, self-written English. Real members
  bring typos, code-switching, multi-intent, OOD. Needs harder training data +
  drift monitoring.
- **Ledger is detection, not prevention** (by design at this layer). Production:
  HMAC key outside the DB, or anchor the chain head externally.

Already fixed this cycle: the ledger's **concurrency race and non-atomic writes**
(mutation + ledger row + idempotency now commit in one `BEGIN IMMEDIATE`
transaction — proven in `tests/test_ledger.py`).

---

## 7. Integration status (mouryesh ↔ Yash_Amex)

The two backends diverged. **`BACKEND_RECONCILIATION.md` is the full merge plan.**
Already fixed in code: the two conversation tables are unified into one
`messages` table (superset, adopts Yash_Amex column names). Still to align on the
`Yash_Amex` side (all documented, blocking for a joint run):

- `accounts` missing `card_id`, `fraud_hold`, `late_payments_12m`; rename
  `vulnerability` → `vulnerability_flag`.
- `transactions`/`waiver_history` field renames (`posted_at`→`date`,
  `category`→`kind`, `waived_at`→`date`, `amount`→`fee_amount`).
- `accounts.get_account_facts()` — missing; the policy engine needs it.
- `actions.py` must thread `session_id` into `append()` or `/audit/{session_id}`
  returns empty (kills the audit-viewer demo).
- Delete `hi.py`; adopt `mouryesh`'s `seed.py`.

Shortcut: `mouryesh/backend/` already satisfies all of the above — the fastest
safe merge is to make it the shared backend and port only the specific
`Yash_Amex` improvements worth keeping.

---

## 8. Open questions (see `open_questions.txt` for detail)

1. ~~Persistent/repeated request handling~~ — **RESOLVED** (session memory +
   repeat-decline escalation).
2. **Full per-member cross-session history/analysis** — data exists (`messages` +
   `audit_ledger` carry `member_id`); no query/reporting layer yet.
3. **Repeat-threshold scope for QUEUE** — only DECLINE auto-escalates today.
4. **Ambiguous slot resolution** — if a member has two unreversed fees,
   `check_slots` silently picks the newest. Agreed approach: surface options and
   ask which one. Not yet built.

---

## 9. What's NOT done / next steps

- Merge `Yash_Amex` per `BACKEND_RECONCILIATION.md` (Priority 0).
- Frontend (Person C, React) — three surfaces: member chat, audit viewer, agent
  console. Not started here.
- The four production hardening items in §6 (PII boundary, real step-up,
  templated regulated outputs, classifier robustness).
- Open questions #2–#4.
- Final: PR `mouryesh` → `main` for submission (compare URL in the PR flow).

---

## 10. Rules for the next agent

1. `mouryesh` is canonical. Don't reintroduce the divergent `Yash_Amex` schema.
2. The policy engine stays a pure function — no DB, no LLM, no session awareness.
   Session-pattern logic lives in `nodes.py`, not `policy_engine.py`.
3. Autonomy tiers are code in `tools.py`, never a model judgment.
4. Only `backend/actions.py` writes the ledger; the audit table has no
   UPDATE/DELETE path; writes go through `write_transaction` (atomic + serialised).
5. Policy thresholds live in `policies/*.yaml`, never in prompts or Python.
6. Keep tests green (72/72) and keep `.env` out of git.
7. Classifier test set (`tests/test_classifier.py`) must stay disjoint from
   `training_data.py`, or the reported accuracy is meaningless.
```
