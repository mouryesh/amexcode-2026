# HANDOFF — End-to-End Servicing Agent Build

> Paste this entire file as context when starting a new agent session.
> It contains every decision, architectural choice, and open item from prior conversations.

---

## 1. What we're building

A conversational AI agent for a **hackathon** that handles card service requests — fee reversals, credit limit increases, replacement cards — in a single interaction. It maintains a hash-chained audit trail of every decision and hands off to a human agent with full context when escalation is needed.

**Team size:** 3 people
**Time constraint:** ~18–24 hours (hackathon)
**Scored on:** Full resolution · Verifiable audit trail · Complete-context handoff

---

## 2. The thesis — memorise this

> The LLM handles language. A deterministic policy engine makes every decision. The ledger proves it. Neither does the other's job.

This is the entire differentiator. The model parses intent, drives conversation, and selects tools. It **never** decides whether a fee gets waived. That decision is a pure function of account facts and a versioned policy document.

---

## 3. Problem statement (verbatim requirements)

Source: hackathon problem statement.

**Tasks from PS:**
1. Design an algorithm to classify incoming service requests and route them to the correct automated resolution flow.
2. Develop a conversational agent interface through which card members can initiate and complete requests end to end.
3. Implement a verifiable audit trail that logs every decision, action, and system call in an immutable format.
4. Integrate with backend card systems to execute resolutions such as fee waivers, limit adjustments, and card replacements.
5. Test and optimize the agent for first-contact resolution rate, audit completeness, and quality of human escalation handoffs.

**Submission format (all mandatory):**
- Project description
- Presentation
- Video and link of the project
- Any other documentation

**Key PS words that constrain the architecture:**
- "Fully resolves" → real write actions, not ticket creation
- "Verifiable" → cryptographic integrity, not just logging
- "Immutable" → append-only, hash-chained
- "Complete context" → structured escalation packet, not "transferring you now"

---

## 4. Stack decisions (locked)

| Choice | Decision | Rationale |
|--------|----------|-----------|
| Backend framework | **FastAPI** (Python) | Best fit with LangGraph, auto OpenAPI docs as free demo asset |
| Agent orchestration | **LangGraph** | First named resource in PS, state machine model maps to autonomy tiers |
| Database | **SQLite** | Zero setup for hackathon, swap to Postgres with one connection string |
| LLM | GPT-4-class API | Via tool calling / function calling |
| Frontend | React (Person C) | Not Python, not covered in this handoff |

---

## 5. Four personas — these ARE the demo

Written before any code. Four accounts, four different outcomes, one continuous demo script.

| Persona | Situation | Request | Outcome | Proves |
|---------|-----------|---------|---------|--------|
| **Priya** | 3yr tenure, zero late payments, no prior waivers | "Waive my late fee" | **APPROVE** — fee reversed, statement recalculated | Full resolution in one interaction |
| **Rahul** | Two courtesy waivers in last 8 months | "Waive my late fee" | **DECLINE** — specific reason, policy cited, appeal route offered | Graceful decline (almost nobody demos this) |
| **Ananya** | 6 months tenure, income data 14 months stale | "Raise my limit" | **QUEUE** — soft pull run, underwriting case opened | Correct refusal to over-automate |
| **Vikram** | Rising utilisation, mentions job loss mid-conversation | "I can't pay this month" | **ESCALATE** — collections suppressed, vulnerability flagged | Judgment about when NOT to act |

**Vikram wins the room.** An agent that recognises hardship and stops — rather than cheerfully offering a payment plan — demonstrates something no wrapper can fake.

---

## 6. Autonomy tiers

Declared per action, enforced in code (`tools.py`), never a runtime model judgment.

| Tier | Meaning | Example actions |
|------|---------|-----------------|
| `auto` | Agent resolves end-to-end, no human | `reverseFee`, `blockCard`, `explainCharge` |
| `auto_step_up` | Auto, but behind re-authentication | `resetPIN`, `updateAddress` |
| `propose_confirm` | Agent proposes, member confirms before commit | `issueReplacement` |
| `escalate` | Agent packages context; a human decides | `hardship`, `aprReduction` |

---

## 7. Architecture — 17 Python files

### 7.1 Shared kernel (written first, frozen early)

```
shared/schemas.py      — Every Pydantic model: Decision, AuditRecord, Receipt,
                         EscalationPacket, AgentRequest/Response, Intent
shared/config.py       — All settings: DB path, LLM model, confidence threshold,
                         policy dir. Read from env with defaults.
shared/exceptions.py   — PolicyNotFound, IdempotencyConflict, LedgerIntegrityError,
                         MemberNotFound, InsufficientData
```

### 7.2 Decision layer (Person A — zero upward dependencies)

```
agent/policy_engine.py — evaluate(policy_id, account_facts) -> Decision
                         Pure function. Loads versioned YAML from /policies.
                         No LLM, no DB, no network. Every branch has a pytest.
                         Imports ONLY schemas + yaml.
```

### 7.3 Agent layer (Person A)

```
agent/state.py         — AgentState TypedDict for LangGraph: session_id, member_id,
                         intent, confidence, slots, messages, decision_records,
                         actions_taken, escalate flag, escalation_packet
agent/classifier.py    — Raw message → { intent, confidence }. 12-category taxonomy.
                         Below confidence threshold → returns "clarify", never guesses.
agent/llm.py           — SOLE file talking to LLM API. Client setup, retries,
                         structured output enforcement, token limits.
agent/tools.py         — ~15 tool definitions. Validates input, enforces autonomy tier
                         in code, delegates to backend/actions.py.
agent/escalation.py    — Builds EscalationPacket: transcript, facts read, actions
                         attempted, decisions, sentiment, suggested next action.
agent/nodes.py         — LangGraph node functions: classify_intent, check_slots,
                         run_policy, call_llm, execute_tool, build_escalation,
                         respond_to_member. Each does one thing to state.
agent/graph.py         — Assembles StateGraph, defines conditional edges, compiles.
                         Routing logic lives ONLY here.
```

**Graph flow:**
```
START → classify_intent
  → [low confidence] → call_llm (clarify) → classify_intent
  → [hardship detected] → build_escalation → END
  → check_slots
  → [slots incomplete] → call_llm (ask) → check_slots
  → run_policy
  → [ESCALATE] → build_escalation → END
  → [DECLINE] → call_llm (explain) → respond_to_member → END
  → [QUEUE] → execute_tool (open case) → respond_to_member → END
  → [APPROVE] → call_llm (select tool) → execute_tool → respond_to_member → END
```

### 7.4 Backend layer (Person B)

```
backend/database.py    — SQLite engine, session management, table DDL.
                         Tables: accounts, transactions, waiver_history,
                         audit_ledger (NO UPDATE/DELETE path), idempotency_keys.
backend/accounts.py    — Read-side: get_member, get_balance, get_waiver_history,
                         get_transactions, get_income_data. Read-only surface.
backend/actions.py     — Write-side: reverse_fee, block_card, issue_replacement,
                         adjust_credit_limit, open_underwriting_case, flag_vulnerability.
                         Every function takes idempotency_key.
                         SOLE ledger writer — only file that calls ledger.append().
backend/ledger.py      — append(): canonical JSON → sha256(prev_hash + timestamp +
                         actor + action + inputs + decision) → insert.
                         verify(): walk chain, recompute hashes, return OK or
                         { TAMPERED, broken_at_record_id }.
backend/seed.py        — Idempotent seeding of four personas + transaction history.
                         Runnable repeatedly.
```

### 7.5 API layer

```
api/main.py            — FastAPI app factory, CORS, startup hook (init DB + seed).
api/routes.py          — THREE endpoints only:
                         POST /agent/message → runs full LangGraph pipeline
                         GET  /audit/{session_id} → returns ledger records
                         GET  /audit/verify → walks chain, returns OK or TAMPERED
```

### 7.6 Tests

```
tests/test_policy_engine.py  — Every branch, parametrized. Priya/Rahul/Ananya/Vikram
                                + edge cases + boundary conditions.
tests/test_ledger.py         — Clean chain verifies, tampered chain names the row.
tests/test_classifier.py     — ~40 labelled utterances across 12 categories.
                                Reports precision/recall/F1. THIS is the metrics slide.
```

### 7.7 Policies (YAML, not Python)

```
policies/fee.late.courtesy_waiver.v1.yaml
policies/credit.limit_increase.v1.yaml
policies/card.replacement.v1.yaml
```

Thresholds live here, NEVER in prompts. Explicitly labelled as illustrative/synthetic. Versioned. The pitch line: "Swap in your real credit policy and nothing else changes."

---

## 8. Dependency rules

```
Imports point DOWN only. If an import points up, the architecture is broken.

shared/schemas.py       ← imported by everything
shared/config.py        ← imported by everything
shared/exceptions.py    ← imported by everything

agent/policy_engine.py  ← imports only schemas + yaml
agent/state.py          ← imports schemas
agent/classifier.py     ← imports llm, schemas, config
agent/llm.py            ← imports config
agent/tools.py          ← imports actions, policy_engine, schemas
agent/escalation.py     ← imports schemas
agent/nodes.py          ← imports classifier, llm, tools, policy_engine, escalation
agent/graph.py          ← imports nodes, state

backend/database.py     ← imports config
backend/accounts.py     ← imports database, schemas
backend/actions.py      ← imports database, ledger, schemas
backend/ledger.py       ← imports database, schemas
backend/seed.py         ← imports database, accounts

api/routes.py           ← imports graph, ledger, schemas
api/main.py             ← imports routes, database, seed
```

---

## 9. Build order (minimises blocking)

| Step | File | Who | Unblocks |
|------|------|-----|----------|
| 1 | `shared/schemas.py` | All three | Everything |
| 2 | `shared/config.py` | Anyone | Everything |
| 3 | `shared/exceptions.py` | Anyone | Error handling |
| 4 | `agent/policy_engine.py` | Person A | Tests, nodes, tools |
| 5 | `backend/database.py` | Person B | All backend |
| 6 | `backend/ledger.py` | Person B | Actions, verify |
| 7 | `backend/accounts.py` | Person B | Policy integration |
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

---

## 10. Timeline gates (non-negotiable)

| Gate | When | What must be true |
|------|------|-------------------|
| Contracts committed | Hour 0–1.5 | tools.json, audit_record.json, personas.json, demo_script.md on main |
| First integration | Hour 8 | Priya's fee waiver works end-to-end through all layers |
| Feature freeze | Hour 15 | All four personas run clean. No new features after this. |
| Demo rehearsal | Hour 15–18 | Deck, video recording, 3× clean run-throughs |

**Two rules:** Integrate at hour 8, not hour 20. Feature-freeze at hour 15 regardless of what's unfinished.

---

## 11. Demo script — 4 minutes

| Time | Beat | What happens |
|------|------|--------------|
| 0:00 | The gap | "Card members spend eleven minutes on hold for a ₹500 fee waiver that follows a rule a computer could read in a millisecond." |
| 0:20 | Priya — resolution | Request → policy applied → fee reversed → receipt card. Under 30 seconds. |
| 1:00 | The toggle | Same interaction, audit view. Every read, every decision, policy ID, reason code. "The model never decided this. This function did." |
| 1:40 | Rahul — the decline | Agent says no. Specific reason, cited policy, appeal path. |
| 2:20 | Verify | Tamper with a ledger row live. Hit /audit/verify. It goes red, names the record. |
| 2:50 | Vikram — the handoff | Agent detects hardship, stops, escalates. Human console shows full context. |
| 3:30 | Close | Architecture slide. 12 categories mapped, 4 built, tiers declared for all 12. |

---

## 12. Scope guard

**IN SCOPE — build properly:**
- Fee & charge remediation (flagship)
- Credit line management
- Card lifecycle (replacement, block, reissue)
- Account information (read layer)
- Escalation packet for all 12 categories

**OUT OF SCOPE — map, don't build:**
- Disputes, payments, card controls, profile changes (shown in matrix, not implemented)
- Real authentication, real payment rails
- Streaming infrastructure, ML models

**Cut order if behind:** Card lifecycle → Credit line → Everything except fee reversal + one escalation

**NEVER CUT:** The rules engine. The hash-chained ledger. The graceful decline.

---

## 13. Fee waiver policy context

Amex does NOT publish its courtesy-waiver rules. What's public:
- US late fees: ~$27 first offence, up to $38 for repeat within 6 billing cycles, capped at minimum payment due (CARD Act)
- Factors considered: tenure, payment history, prior waivers, hardship
- Hardship goes to a formal financial relief programme, not the fee-waiver counter

**For our build:** Write synthetic policy YAML, explicitly labelled as illustrative, with each threshold carrying a `rationale` field. Pitch line: "These thresholds are illustrative. The point is that they live in a versioned document, not a prompt — swap in the real credit policy and nothing else changes."

---

## 14. Q&A prep — rehearse these

| Judge asks | Our answer |
|-----------|------------|
| "How do you stop it hallucinating an approval?" | "It can't. The decision is a pure function; the model never sees the threshold." |
| "What makes the trail verifiable?" | "It's hash-chained. Here — watch it go red." (tamper live) |
| "What if an action fires twice?" | "Every write is idempotency-keyed. Same key, one reversal." |
| "Why only four categories?" | "We mapped twelve and declared a tier for each. We built four where automation is defensible, and built the handoff for the eight where it isn't." |
| "What happens on a hardship call?" | "It stops. Duty of care isn't an automation target." |
| "Why not Splunk/Elastic?" | "Those are the query layer — they make a trail searchable. Verifiable is a cryptographic property. Our chain is the integrity guarantee; Elastic would be a consumer of the trail, not the trail itself." |
| "Why not LangChain/RAG?" | "Our policies are structured YAML, not a document corpus. Retrieval is probabilistic; policy lookup shouldn't be." |
| "How do you prove the chain wasn't recomputed after tampering?" | "Detection, not prevention at this layer. Production: HMAC with key held outside the DB, or anchor chain head to append-only external store." |

---

## 15. Key concepts the builder should know

**Tier 1 — cannot ship without:**
1. LLM tool calling / function calling
2. Structured output & JSON schema (Pydantic)
3. Intent classification (taxonomy, confidence, fallback)
4. Conversation state management (slot filling, turn history)
5. REST API design (FastAPI)
6. Cryptographic hashing & hash chains (SHA-256, canonical serialisation)
7. Relational data modelling & append-only design
8. Idempotency

**Tier 2 — what makes you win:**
9. LangGraph (nodes, edges, conditional routing, state, interrupts)
10. Rules engines & decision tables
11. Deterministic vs probabilistic system design
12. Testing (pytest, parametrized, coverage)

---

## 16. Frontend surfaces (Person C, React)

Three surfaces:
1. **Member chat** — actions render as receipt cards, not paragraphs. "Late fee reversed · ₹500 · ref TXN-8842 · statement updated"
2. **Audit trail viewer** — same interaction replayed as decision log. Toggle between chat and audit on one screen.
3. **Agent console** — escalation packet lands here. Transcript, what the agent read, what it tried, why it stopped, sentiment, suggested next action.

---

## 17. Repo layout

```
/contracts     tools.json, audit_record.json, personas.json, demo_script.md
/shared        schemas.py, config.py, exceptions.py
/agent         state.py, classifier.py, llm.py, tools.py, escalation.py, nodes.py, graph.py
/policies      versioned YAML — thresholds live here, NEVER in prompts
/tests         test_policy_engine.py, test_ledger.py, test_classifier.py
/backend       database.py, accounts.py, actions.py, ledger.py, seed.py
/api           main.py, routes.py
/web           React frontend (Person C)
/deck          slides + demo notes (Person C)
```

---

## 18. Open items / not yet built

- [ ] No code has been written yet — architecture only
- [ ] `tools.json` contract not yet drafted (15 actions with params, return shapes, tiers)
- [ ] `audit_record.json` not yet drafted
- [ ] `personas.json` not yet drafted with full account data
- [ ] `demo_script.md` not yet word-for-word scripted
- [ ] Policy YAML files not yet written
- [ ] Frontend not started
- [ ] Presentation deck not started
- [ ] Video recording not planned
- [ ] Project description (mandatory submission) — assign to Person B after hour 13

---

## 19. Instructions for the next agent

You are continuing a hackathon build. Everything above is decided and agreed — do not re-debate architecture. When asked to write a file, follow these rules:

1. Check which file is being requested against the 17-file list in section 7.
2. Import only from the allowed dependencies in section 8. If you're about to import upward, stop.
3. Use Pydantic models from `shared/schemas.py` for all cross-boundary data.
4. Policy thresholds go in `/policies/*.yaml`, NEVER in prompts or Python code.
5. The policy engine is a pure function — no LLM, no DB, no side effects.
6. Every write action takes an `idempotency_key`.
7. Only `backend/actions.py` calls `ledger.append()`.
8. The audit ledger has no UPDATE or DELETE path.
9. Canonical JSON serialisation (`sort_keys=True`) before hashing.
10. When writing tests, every branch of the policy engine must be covered.

---

## 20. Shared kernel — COMPLETE BUILD REFERENCE (as built)

> **This section documents the actual implemented `shared/` code, not the plan.**
> These three files are the frozen contracts every layer imports. Imports point
> DOWN into `shared/` only — nothing in `shared/` imports from `agent/`,
> `backend/`, or `api/`. If a shape or setting must change, change it HERE and
> tell the whole team; do not redefine any of it locally.

### 20.0 Why the shared kernel exists

The system has five layers (API → Agent → Decision → Backend → DB). They must
agree on (a) what data looks like, (b) shared settings, and (c) what errors mean
— WITHOUT importing each other's code. If the shape of a `Decision` lived inside
the policy engine, the backend would have to import agent code just to persist
it: an upward dependency, which the architecture forbids. Putting these in a
neutral `shared/` lets everyone import *down* and nobody import *sideways*, so
Person A and Person B build in parallel against the same contracts.

Three files, three jobs:

| File | Job | Depends on |
|---|---|---|
| `shared/schemas.py` | the **nouns** — Pydantic models that cross boundaries | `pydantic`, stdlib |
| `shared/config.py` | the **knobs** — settings from env with defaults | stdlib only |
| `shared/exceptions.py` | the **failures** — named errors mapped to HTTP codes | stdlib only |

Design principles enforced here: *one file one job* · *no magic numbers outside
config* · *every error has a name* · *if a shape isn't here, it doesn't cross a
layer boundary*.

---

### 20.1 `shared/schemas.py`

All models are **Pydantic v2** `BaseModel`. Validation happens on construction:
build a model with a bad value and it raises immediately, instead of letting bad
data flow deep into another layer. Enums inherit `str`, so they validate as a
closed set but serialise to plain JSON strings.

**Module helper**

```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

Single source of truth for timestamps: UTC, ISO-8601. Used as the default factory
for every `timestamp`/`created_at` field. Consistency matters because timestamps
are part of what the ledger hashes — inconsistent formats would break hash
reproducibility. Leading underscore = internal, not for import elsewhere.

**Enums (closed vocabularies)**

```python
class Outcome(str, Enum):
    APPROVE  = "APPROVE"
    DECLINE  = "DECLINE"
    QUEUE    = "QUEUE"
    ESCALATE = "ESCALATE"

class AutonomyTier(str, Enum):
    AUTO            = "auto"
    AUTO_STEP_UP    = "auto_step_up"
    PROPOSE_CONFIRM = "propose_confirm"
    ESCALATE        = "escalate"
```

`Outcome` is the only set of things a policy can decide (matches the four
personas). `AutonomyTier` is the per-action tier enforced in `tools.py`. Being
enums, no code can smuggle in a value like `"maybe"`.

**`Intent`** — classifier output
| field | type | default | notes |
|---|---|---|---|
| `label` | str | required | one of the 12 taxonomy categories, or `"clarify"` |
| `confidence` | float | required | constrained `ge=0.0, le=1.0` |
| `rationale` | str \| None | `None` | short model explanation, debug only |

Produced by `agent/classifier.py`. The graph reads `confidence` against
`config.CONFIDENCE_THRESHOLD` to decide act-vs-clarify.

**`Decision`** — policy engine output; the audit-critical, self-explaining shape
| field | type | default | notes |
|---|---|---|---|
| `outcome` | `Outcome` | required | APPROVE/DECLINE/QUEUE/ESCALATE |
| `reason_code` | str | required | machine code, e.g. `WAIVER_FREQUENCY_EXCEEDED` |
| `reason_text` | str | required | member-readable explanation |
| `policy_id` | str | required | e.g. `fee.late.courtesy_waiver` |
| `policy_version` | str | required | e.g. `v1` |
| `inputs_used` | dict | `{}` | **every account fact the winning rule read** |
| `rule_id` | str \| None | `None` | which rule branch fired |

Carries not just *what* was decided but *why*, *by which policy version*, and
*from which exact facts*. `inputs_used` is the auditable proof the decision was
grounded in real data (e.g. Rahul's `{"prior_waivers_8m": 2}`). Constructed only
by `agent/policy_engine.py`; everyone else consumes it. `reason_code` = for
logs/machines, `reason_text` = for the member.

**`AuditRecord`** — the exact shape of ONE hash-chained ledger row
| field | type | default | notes |
|---|---|---|---|
| `record_id` | int \| None | `None` | assigned by DB autoincrement |
| `session_id` | str \| None | `None` | groups a conversation |
| `timestamp` | str | `_now_iso()` | UTC ISO-8601 |
| `actor` | str | required | e.g. `"system"` |
| `action` | str | required | e.g. `"reverse_fee"` |
| `inputs` | dict | `{}` | action inputs |
| `decision` | dict \| None | `None` | the `Decision` dict when present |
| `prev_hash` | str | required | predecessor's `record_hash` (genesis = 64 zeros) |
| `record_hash` | str | required | `sha256(prev_hash+timestamp+actor+action+inputs+decision)` |

The agreed *shape* of a ledger link. `prev_hash`+`record_hash` make it
tamper-evident. The write/verify logic lives in `backend/ledger.py`; this file
only defines the shape.

**`Receipt`** — returned to the member after a successful WRITE
| field | type | default | notes |
|---|---|---|---|
| `reference` | str | required | tracking id, e.g. `TXN-A1B2C3D4` |
| `action` | str | required | which action produced it |
| `amount` | float \| None | `None` | e.g. reversed fee amount |
| `currency` | str | `"INR"` | |
| `timestamp` | str | `_now_iso()` | UTC ISO-8601 |
| `new_balance` | float \| None | `None` | for the receipt card |
| `detail` | str \| None | `None` | human summary line |

Every write function in `backend/actions.py` returns one of these; the frontend
renders it as a receipt card ("Late fee reversed · ₹500 · ref TXN-8842").

**`EscalationPacket`** — the complete-context human handoff
| field | type | default | notes |
|---|---|---|---|
| `session_id` | str | required | |
| `member_id` | str | required | |
| `reason` | str | required | why the agent stopped and escalated |
| `transcript` | list[dict[str,str]] | `[]` | full conversation |
| `facts_read` | dict | `{}` | account facts read this session |
| `actions_attempted` | list[dict] | `[]` | what it tried + outcomes |
| `decisions` | list[`Decision`] | `[]` | all policy decisions (nested models) |
| `member_sentiment` | str | `"neutral"` | e.g. `distressed` |
| `suggested_next_action` | str | `""` | what the human should do |
| `created_at` | str | `_now_iso()` | |

Built by `agent/escalation.py`. Note `decisions` nests `Decision` models — the
packet carries structured decision history, not flattened text.

**`AgentRequest`** — `POST /agent/message` input
| field | type | default | notes |
|---|---|---|---|
| `session_id` | str | required | |
| `member_id` | str | required | |
| `message` | str | required | raw member text |
| `confirm` | bool | `False` | member confirmed a propose_confirm action |
| `reauthenticated` | bool | `False` | member cleared an auto_step_up challenge |

Confirmation contract (graph is stateless per turn): when a prior response has
`awaiting_member=true`, the client resubmits the **same `message`** with
`confirm=true` (propose_confirm) or `reauthenticated=true` (auto_step_up) to
complete the action. Every turn is independently replayable.

**`AgentResponse`** — `POST /agent/message` output
| field | type | default | notes |
|---|---|---|---|
| `reply` | str | required | member-facing text |
| `actions_taken` | list[dict] | `[]` | tools run this turn + status |
| `decision_records` | list[`Decision`] | `[]` | decisions made this turn |
| `escalate` | bool | `False` | true when handed to a human |
| `escalation_packet` | `EscalationPacket` \| None | `None` | present when escalated |
| `intent` | `Intent` \| None | `None` | classified intent |
| `awaiting_member` | bool | `False` | true when waiting on the member (clarify/confirm/step-up) |

Everything the three demo surfaces need comes back in this one object.

---

### 20.2 `shared/config.py`

Single shared instance: `from shared.config import config`. Every value is read
from an environment variable with a sensible default, via:

```python
def _env(name, default):        # use env var if set, else default
    return os.environ.get(name, default)
```

This means the code runs with zero configuration, but anything can be overridden
at deploy/test time without touching code.

| setting | env var | default | consumed by |
|---|---|---|---|
| `DB_PATH` | `DB_PATH` | `<repo>/servicing.db` | backend/database.py |
| `LLM_PROVIDER` | `LLM_PROVIDER` | `offline` | agent/llm.py (`offline`\|`anthropic`\|`openai`) |
| `LLM_MODEL` | `LLM_MODEL` | `claude-sonnet-5` | agent/llm.py |
| `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` | `""` | agent/llm.py |
| `OPENAI_API_KEY` | `OPENAI_API_KEY` | `""` | agent/llm.py |
| `LLM_MAX_TOKENS` | `LLM_MAX_TOKENS` | `1024` | agent/llm.py |
| `LLM_MAX_RETRIES` | `LLM_MAX_RETRIES` | `3` | agent/llm.py (backoff) |
| `CONFIDENCE_THRESHOLD` | `CONFIDENCE_THRESHOLD` | `0.65` | agent/classifier.py |
| `POLICY_DIR` | `POLICY_DIR` | `<repo>/policies` | agent/policy_engine.py |
| `API_HOST` | `API_HOST` | `0.0.0.0` | api |
| `API_PORT` | `API_PORT` | `8000` | api |

The one bit of logic:

```python
@classmethod
def llm_configured(cls) -> bool:
    if cls.LLM_PROVIDER == "anthropic": return bool(cls.ANTHROPIC_API_KEY)
    if cls.LLM_PROVIDER == "openai":    return bool(cls.OPENAI_API_KEY)
    return False
```

Answers "can we call a real LLM right now?" When it returns False (offline, or no
key), the classifier and nodes use deterministic fallbacks — **this is what lets
the whole graph run with no API key** for CI and demo prep.

`DB_PATH` is read, never hard-coded, precisely so `tests/conftest.py` can point
the whole suite at a temp DB (it sets `DB_PATH` before any module imports config).

---

### 20.3 `shared/exceptions.py`

Every domain error inherits a base that carries the HTTP status it should become:

```python
class ServicingError(Exception):
    http_status: int = 400
```

`api/routes.py` catches the whole family in one clause and maps it:

```python
except ServicingError as exc:
    raise HTTPException(status_code=exc.http_status, detail=str(exc))
```

So you `raise` the specific error and the correct HTTP status is handled for you.
Nothing in the codebase catches bare `Exception`.

| exception | http_status | raised when | raised in |
|---|---|---|---|
| `PolicyNotFound` | 404 | policy id missing / malformed YAML / unknown operator | agent/policy_engine.py |
| `IdempotencyConflict` | 409 | same idempotency key reused with different inputs | backend/actions.py |
| `LedgerIntegrityError` | 500 | hash-chain verification failed | backend/ledger.py |
| `MemberNotFound` | 404 | no account for the given member id | backend/accounts.py |
| `InsufficientData` | 422 | a policy needs a fact that wasn't provided | agent/policy_engine.py |

Result: instead of a generic 500, a bad member id returns a clean `404
MemberNotFound`, a duplicate write returns `409 IdempotencyConflict`, etc. Each
failure is a named thing you can catch and test.

---

### 20.4 Mental model + change control

```
schemas.py    →  the NOUNS      (Decision, Receipt, Intent, EscalationPacket, ...)
config.py     →  the KNOBS      (thresholds, paths, keys — all env-overridable)
exceptions.py →  the FAILURES   (named, each mapped to an HTTP status)
```

Everything above imports *down* into these three; they import almost nothing.
That's why they're frozen first: once agreed, both people build in parallel
without stepping on each other. **To change a contract, edit `shared/` and
announce it — never shadow these shapes in another layer.**
