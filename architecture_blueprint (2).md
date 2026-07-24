# End-to-End Servicing Agent — Architecture Blueprint

## 1. Overview

This document defines every Python file required to build the servicing agent prototype end to end. The system is organised into five layers with strict downward-only dependencies: API → Agent → Decision → Backend → Database. A shared kernel of schemas and config sits alongside all layers.

**Total: 17 Python files + versioned policy YAML.**

---

## 2. Design principles

- **One file, one job.** No file has two reasons to change.
- **Dependencies point down.** The agent layer imports backend; backend never imports agent. The policy engine imports nothing except schemas and YAML.
- **The LLM never decides.** It classifies intent, drives conversation, and selects tools. Policy outcomes are pure functions of account facts and versioned rules.
- **The ledger is append-only.** No UPDATE, no DELETE — enforced at the access layer, not by discipline.
- **Autonomy tiers are code, not prompts.** Whether an action needs confirmation is checked in `tools.py`, never trusted to the model.

---

## 3. Project structure

```
/project
├── shared/
│   ├── schemas.py
│   ├── config.py
│   └── exceptions.py
│
├── agent/
│   ├── state.py
│   ├── classifier.py
│   ├── llm.py
│   ├── tools.py
│   ├── escalation.py
│   ├── nodes.py
│   └── graph.py
│
├── backend/
│   ├── database.py
│   ├── accounts.py
│   ├── actions.py
│   ├── ledger.py
│   └── seed.py
│
├── api/
│   ├── main.py
│   └── routes.py
│
├── policies/
│   ├── fee.late.courtesy_waiver.v1.yaml
│   ├── credit.limit_increase.v1.yaml
│   └── card.replacement.v1.yaml
│
├── tests/
│   ├── test_policy_engine.py
│   ├── test_ledger.py
│   └── test_classifier.py
│
└── web/                  ← React frontend (not Python)
```

---

## 4. Shared kernel

These files are written first, frozen early, and imported by every layer. Neither Person A nor Person B owns them alone — they are agreed contracts.

### 4.1 `shared/schemas.py`

Every Pydantic model in the system lives here. This is `contracts/*.json` made executable.

- `Intent` — classified intent label + confidence score
- `Decision` — policy engine output: outcome (APPROVE / DECLINE / QUEUE / ESCALATE), reason code, reason text, policy ID, policy version, inputs used
- `AuditRecord` — the exact shape of one ledger row: timestamp, actor, action, inputs, decision, prev_hash, record_hash
- `Receipt` — returned to the member after a write action: reference, amount, timestamp, new balance
- `EscalationPacket` — transcript, facts read, actions attempted, decisions made, member sentiment, suggested next step
- `AgentRequest` — session ID, member ID, message text
- `AgentResponse` — reply text, actions taken, decision records, escalate flag, optional escalation packet

If a data shape is not defined here, it is not a contract and should not cross a layer boundary.

### 4.2 `shared/config.py`

All settings in one place, read from environment variables with sensible defaults.

- Database path (SQLite file location)
- LLM model name and API key reference
- Classifier confidence threshold
- Policy directory path
- API host and port

No magic numbers anywhere else in the codebase.

### 4.3 `shared/exceptions.py`

Domain-specific exceptions that routes translate into HTTP status codes.

- `PolicyNotFound` — requested policy ID does not exist in /policies
- `IdempotencyConflict` — duplicate idempotency key with different inputs
- `LedgerIntegrityError` — hash chain verification failed
- `MemberNotFound` — no account for the given member ID
- `InsufficientData` — policy cannot evaluate because required account facts are missing

Nothing in the codebase catches bare `Exception`. Every error has a name and a handler.

---

## 5. Decision layer — Person A

### 5.1 `agent/policy_engine.py`

The crown jewel. A pure function with zero upward dependencies.

```
evaluate(policy_id: str, account_facts: dict) -> Decision
```

- Loads the versioned YAML policy from `/policies`
- Runs deterministic rules against account facts (tenure, prior waivers, utilisation, income staleness)
- Returns a self-explaining `Decision` object with outcome, reason code, reason text, policy version, and every input it used
- Imports only `shared/schemas.py` and `yaml` — no LLM, no database, no network calls

This is the answer to "how do you know the model didn't hallucinate an approval?" Every branch is covered by pytest. The policy thresholds live in versioned YAML, never in a prompt.

---

## 6. Agent layer — Person A

### 6.1 `agent/state.py`

Defines the `AgentState` TypedDict that LangGraph threads through every node. This is the single mutable object in a conversation.

Contents: session ID, member ID, classified intent, confidence score, collected slots (fee amount, transaction ref, card ID), full message history, decision records list, actions taken list, escalate flag, and the escalation packet.

Everything reads from and writes to this object. No globals, no side channels.

### 6.2 `agent/classifier.py`

Takes the member's raw message and returns an intent label plus a confidence score.

- Calls the LLM via `llm.py` with a strict JSON schema
- Maps to one of 12 categories defined in the intent taxonomy
- If confidence falls below the threshold defined in `config.py`, returns `intent: "clarify"` and asks a follow-up question
- Never guesses — below threshold it always seeks clarification

This is Task 1 of the problem statement. It exists as a named, measurable component because judges will look for it.

### 6.3 `agent/llm.py`

The only file that talks to the LLM API. Every other file that needs the model goes through this one.

- Client setup and API key management
- Retry logic with exponential backoff
- Structured output enforcement (JSON schema validation on responses)
- Token limit management
- Model name read from `config.py`

Architect's reason for isolation: when you swap models or the API flakes at hour 14, you touch one file and nothing else breaks.

### 6.4 `agent/tools.py`

The ~15 tool definitions that the LLM is allowed to call. Each tool is a thin wrapper that does three things:

1. Validates input against the expected schema
2. Enforces the autonomy tier (auto / auto_step_up / propose_confirm / escalate) — this is checked in code here, never trusted to the model
3. Delegates to the appropriate function in `backend/actions.py`

Tool list:

- `reverse_fee()` — auto tier, calls backend, returns receipt
- `block_card()` — auto tier, calls backend, returns confirmation
- `issue_replacement()` — propose_confirm tier, proposes to member first, then calls backend on confirmation
- `explain_charge()` — auto tier, read-only, returns charge detail
- `get_account_info()` — auto tier, read-only, returns member facts
- `request_limit_increase()` — calls policy engine first, then backend based on decision
- `reset_pin()` — auto_step_up tier, requires re-authentication before execution
- `update_address()` — auto_step_up tier, requires re-authentication
- `escalate_to_human()` — escalate tier, builds escalation packet, sets flag
- Plus ~6 more covering the 12-category taxonomy

### 6.5 `agent/escalation.py`

Builds the `EscalationPacket` — the structured handoff document a human agent receives.

Contents assembled: full conversation transcript, account facts that were read during the session, actions attempted and their outcomes, all policy decisions with reason codes, detected member sentiment, and a suggested next action for the human agent.

This is a separate file because "complete-context handoff" is a scored deliverable. It is not a side effect of ending a conversation — it is a first-class artifact that a stranger should be able to act on in ten seconds.

### 6.6 `agent/nodes.py`

LangGraph node functions. Each takes state, does exactly one thing, and returns updated state.

- `classify_intent(state)` — runs `classifier.py`, updates `state.intent` and `state.confidence`
- `check_slots(state)` — determines if all required parameters are collected for the classified intent
- `run_policy(state)` — runs `policy_engine.py`, appends result to `state.decision_records`
- `call_llm(state)` — sends messages and tool definitions to LLM via `llm.py`, gets reply or tool call
- `execute_tool(state)` — executes the tool the LLM selected via `tools.py`, appends to `state.actions_taken`
- `build_escalation(state)` — packages everything into `state.escalation_packet` via `escalation.py`
- `respond_to_member(state)` — formats the final reply as a receipt card or prose

No routing logic lives here. Each node does its job and returns state. The graph decides what runs next.

### 6.7 `agent/graph.py`

Assembles nodes into a LangGraph `StateGraph`, defines all conditional edges, and compiles the runnable graph.

Flow:

```
START
  → classify_intent
  → [if confidence < threshold] → call_llm (ask clarifying question) → classify_intent
  → [if intent is hardship/distress] → build_escalation → END
  → check_slots
  → [if slots incomplete] → call_llm (ask for missing info) → check_slots
  → run_policy
  → [if ESCALATE] → build_escalation → END
  → [if DECLINE] → call_llm (explain decline with reason) → respond_to_member → END
  → [if QUEUE] → execute_tool (open case) → respond_to_member → END
  → [if APPROVE] → call_llm (select tool) → execute_tool → respond_to_member → END
```

The graph is the only place routing logic lives. One file to read to understand the entire control flow.

---

## 7. Backend layer — Person B

### 7.1 `backend/database.py`

SQLite engine setup, session management, and table DDL.

Tables:

- `accounts` — member data, balance, credit limit, tenure, contact info
- `transactions` — charge history with dates, amounts, descriptions
- `waiver_history` — prior courtesy waivers with dates and policy versions
- `audit_ledger` — append-only, hash-chained (no UPDATE path, no DELETE path exists in this file)
- `idempotency_keys` — tracks processed keys to prevent duplicate writes

The audit table's append-only constraint is enforced here at the access layer. No function in the codebase constructs an UPDATE or DELETE statement against this table.

### 7.2 `backend/accounts.py`

Read-side of the fake core banking system.

- `get_member(member_id)` — returns full account profile
- `get_balance(member_id)` — current balance and credit limit
- `get_waiver_history(member_id, months)` — courtesy waivers within a time window
- `get_transactions(member_id, limit)` — recent charge history
- `get_income_data(member_id)` — income records and staleness date

Split from writes so the policy engine's inputs come from a read-only surface. The policy engine never touches a function that could mutate state.

### 7.3 `backend/actions.py`

Write-side of the fake core banking system. Every function follows the same pattern:

1. Check idempotency key — if already processed, return the original result
2. Mutate the relevant table (reverse fee, adjust limit, block card)
3. Call `ledger.append()` to record the action with its decision
4. Return a structured `Receipt`

Functions:

- `reverse_fee(member_id, fee_amount, txn_ref, decision, idempotency_key)` — reverses the charge, recalculates statement
- `block_card(member_id, card_id, reason, idempotency_key)` — sets card status to blocked
- `issue_replacement(member_id, card_id, idempotency_key)` — creates replacement card record
- `adjust_credit_limit(member_id, new_limit, decision, idempotency_key)` — updates credit limit
- `open_underwriting_case(member_id, request_type, decision, idempotency_key)` — queues for human review
- `flag_vulnerability(member_id, signals, idempotency_key)` — marks account for sensitive handling, suppresses collections

This is the sole ledger writer in the codebase. No other file calls `ledger.append()`.

### 7.4 `backend/ledger.py`

The entire audit trail logic — two public functions.

**`append(actor, action, inputs, decision)`**

- Canonical-JSON serialises the record (sort_keys=True — field order matters or hashes won't reproduce)
- Computes: `record_hash = sha256(prev_hash + timestamp + actor + action + inputs + decision)`
- Inserts the row — append only, no updates ever

**`verify()`**

- Walks every record in chain order
- Recomputes each hash from its predecessor
- Returns `{ status: "OK" }` or `{ status: "TAMPERED", broken_at_record_id: 42 }`

The `verify()` function is the tamper-and-go-red demo beat. Tamper with a row in the DB live on stage, hit the endpoint, watch it turn red and name the exact record. Rehearse until it is muscle memory.

### 7.5 `backend/seed.py`

Idempotent seeding of the four personas plus enough transaction and waiver history for policies to evaluate correctly.

| Persona | Situation | Expected Outcome |
|---------|-----------|-------------------|
| Priya | 3yr tenure, zero late payments, no prior waivers | APPROVE — fee reversed |
| Rahul | Two courtesy waivers in last 8 months | DECLINE — policy cited, appeal offered |
| Ananya | 6 months tenure, income data 14 months stale | QUEUE — underwriting case opened |
| Vikram | Rising utilisation, distress signals | ESCALATE — collections suppressed |

Runnable repeatedly — you will nuke the database during demo prep and need to reseed in seconds.

---

## 8. API layer

### 8.1 `api/main.py`

FastAPI application factory.

- Creates the app instance
- Configures CORS (you will hit this at the hour-8 integration — configure it now)
- Startup hook: initialise database, run seed if tables are empty
- Mounts the route router

### 8.2 `api/routes.py`

Three endpoints. No business logic — pure translation between HTTP and schemas.

**`POST /agent/message`**

- Input: `{ session_id, member_id, message }`
- Runs the full LangGraph pipeline via `graph.py`
- Output: `{ reply, actions_taken[], decision_records[], escalate, escalation_packet? }`

**`GET /audit/{session_id}`**

- Returns every ledger record for this session, in chain order

**`GET /audit/verify`**

- Runs `ledger.verify()`
- Returns `{ status: "OK" }` or `{ status: "TAMPERED", broken_at_record_id }`

Three endpoints covers the full demo. Do not build more.

---

## 9. Tests

### 9.1 `tests/test_policy_engine.py`

One test per branch of every policy, parametrized so adding a new case is one line.

- `test_approve_clean_payer` — Priya's case
- `test_decline_repeat_waiver` — Rahul's case
- `test_queue_stale_income` — Ananya's case
- `test_escalate_hardship_signal` — Vikram's case
- `test_decline_below_minimum_tenure` — edge case
- `test_approve_at_exact_boundary` — boundary condition
- `test_unknown_policy_id_raises` — error handling

### 9.2 `tests/test_ledger.py`

- `test_clean_chain_verifies` — append three records, verify returns OK
- `test_tampered_chain_identifies_record` — append three, mutate the second, verify names record 2
- `test_append_is_idempotent` — same content, different hash because of timestamp and chaining

### 9.3 `tests/test_classifier.py`

The ~40-utterance labelled test set. This file IS the metrics slide.

- Covers all 12 intent categories plus out-of-scope inputs
- Reports precision, recall, and F1 per category
- Tracks first-contact resolution rate across the four persona scenarios

---

## 10. What is deliberately not here

| Absent file | Why it does not exist |
|---|---|
| `utils.py` | A utils file is where architecture goes to die. If a helper has no home, its home is wrong. |
| `repository.py` | Repository pattern is overhead for a hackathon. Raw SQL in `database.py` and `accounts.py` is honest and readable. |
| `services.py` | Service classes add a layer that buys nothing when you have 17 files. The nodes ARE the service layer. |
| `middleware.py` | No auth middleware, no rate limiting. Out of scope per the build plan. |
| `models.py` (ORM) | SQLAlchemy is overkill for four tables in SQLite. Table DDL lives in `database.py`. |

---

## 11. Dependency rules

```
shared/schemas.py      ← imported by everything
shared/config.py       ← imported by everything
shared/exceptions.py   ← imported by everything

agent/policy_engine.py ← imports only schemas + yaml (ZERO upward deps)
agent/state.py         ← imports schemas
agent/classifier.py    ← imports llm, schemas, config
agent/llm.py           ← imports config (sole LLM API caller)
agent/tools.py         ← imports actions, policy_engine, schemas
agent/escalation.py    ← imports schemas
agent/nodes.py         ← imports classifier, llm, tools, policy_engine, escalation
agent/graph.py         ← imports nodes, state

backend/database.py    ← imports config
backend/accounts.py    ← imports database, schemas
backend/actions.py     ← imports database, ledger, schemas
backend/ledger.py      ← imports database, schemas
backend/seed.py        ← imports database, accounts

api/routes.py          ← imports graph, ledger, schemas
api/main.py            ← imports routes, database, seed
```

If an import would point upward (backend importing agent, policy engine importing actions), the architecture is broken. Fix the dependency, not the linter.

---

## 12. Build order

This is the order that minimises blocking between three people.

| Step | File | Who | Unblocks |
|------|------|-----|----------|
| 1 | `shared/schemas.py` | All three together | Everything |
| 2 | `shared/config.py` | Anyone | Everything |
| 3 | `shared/exceptions.py` | Anyone | Error handling everywhere |
| 4 | `agent/policy_engine.py` | Person A | Tests, nodes, tools |
| 5 | `backend/database.py` | Person B | All backend files |
| 6 | `backend/ledger.py` | Person B | Actions, verify endpoint |
| 7 | `backend/accounts.py` | Person B | Policy engine integration |
| 8 | `backend/actions.py` | Person B | Tools |
| 9 | `backend/seed.py` | Person B | Demo data |
| 10 | `agent/state.py` | Person A | Graph, nodes |
| 11 | `agent/llm.py` | Person A | Classifier, nodes |
| 12 | `agent/classifier.py` | Person A | Nodes |
| 13 | `agent/tools.py` | Person A | Nodes |
| 14 | `agent/escalation.py` | Person A | Nodes |
| 15 | `agent/nodes.py` | Person A | Graph |
| 16 | `agent/graph.py` | Person A | Routes |
| 17 | `api/routes.py` | A + B | Frontend |
| 18 | `api/main.py` | A + B | Demo |
