# Backend reconciliation — `mouryesh` ↔ `Yash_Amex`

Two backends were built in parallel against the same architecture spec and have
diverged. This document is the merge plan. It is written from the position that
**`mouryesh` is the canonical integration base** — it is the complete, running,
tested system (agent layer + backend + API + Groq + session memory, 69 passing
tests), so the safest merge keeps it working and aligns the `Yash_Amex` backend
to it, while adopting the genuinely-better ideas from `Yash_Amex` where they
don't break anything.

Nothing here silently overwrites either person's work — the code-level fix that
was already applied is called out, and the rest is a checklist to apply
deliberately.

---

## 0. Already fixed in code (on `mouryesh`)

**The two conversation tables are now one.** `mouryesh` had `session_messages`;
`Yash_Amex` had `messages`. They are reconciled into a single `messages` table
that is a **superset** of both:

```sql
CREATE TABLE messages (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id           TEXT    NOT NULL,
    member_id            TEXT,
    turn_index           INTEGER NOT NULL,    -- from Yash_Amex
    role                 TEXT    NOT NULL,     -- 'member' | 'agent' | 'system'
    content              TEXT    NOT NULL,     -- from Yash_Amex (was 'text')
    intent               TEXT,                 -- from Yash_Amex
    confidence           REAL,                 -- from Yash_Amex
    decision_outcome     TEXT,                 -- from mouryesh (repeat-decline logic)
    decision_reason_code TEXT,                 -- from mouryesh
    policy_id            TEXT,                 -- from mouryesh
    created_at           TEXT    NOT NULL      -- from Yash_Amex (was 'timestamp')
);
CREATE INDEX idx_messages_session ON messages (session_id, turn_index);
```

Adopted `Yash_Amex`'s column names so both people's code writes the same shape;
kept the three `decision_*` / `policy_id` columns because the repeat-decline
escalation (`agent/nodes.py:run_policy` → `sessions.count_prior_declines`)
depends on them.

**Action for Yash_Amex:** drop your local `messages` DDL and take the one above
(add the three `decision_*`/`policy_id` columns you didn't have). Use
`backend/sessions.py` from `mouryesh` as the single access layer for this table
(`record_turn`, `get_history`, `count_prior_declines`).

---

## 1. `accounts` table — field alignment (blocking)

The agent layer, policies, and tools read specific field names. `Yash_Amex`'s
`accounts` is missing several and renames others. Align to the canonical set:

| Canonical column (`mouryesh`) | `Yash_Amex` today | Depended on by | Fix |
|---|---|---|---|
| `late_payments_12m` | missing | `fee.late.courtesy_waiver` + `credit.limit_increase` policies | **add column** |
| `card_id` | missing | `tools.block_card`, `issue_replacement`, `nodes.check_slots` | **add column** |
| `fraud_hold` | missing | `card.replacement` policy | **add column** |
| `vulnerability_flag` | `vulnerability` | `actions.flag_vulnerability` | **rename** |
| `income_staleness_months` | `income_amount` + `income_asof` | `credit.limit_increase` policy | see note ↓ |

**Income representation:** `Yash_Amex`'s `income_asof` (a real date, staleness
computed in `get_income_data`) is genuinely better than `mouryesh`'s stored
`income_staleness_months` int. Recommended: **keep `income_asof`** and have
`get_account_facts` (see §3) derive `income_staleness_months` from it. This is
the one place to adopt `Yash_Amex`'s design over `mouryesh`'s. Not blocking —
can ship with the int and switch later — but it's the right end state.

---

## 2. `transactions` / `waiver_history` — field alignment (blocking)

| Table | Canonical (`mouryesh`) | `Yash_Amex` | Depended on by | Fix |
|---|---|---|---|---|
| transactions | `date` | `posted_at` | `accounts.get_transactions`, `get_account_facts` | rename |
| transactions | `kind` (`'fee'`, `'replacement'`) | `category` | `nodes.check_slots` (finds the fee), `get_account_facts` (counts `replacements_30d`) | rename + use the same category values |
| waiver_history | `date` | `waived_at` | `accounts.get_waiver_history`, `get_account_facts` | rename |
| waiver_history | `fee_amount` | `amount` | seed / waiver reads | rename |

The `kind` values matter, not just the column name: `check_slots` looks for
`kind == 'fee'` to auto-resolve which charge to waive, and `get_account_facts`
counts `kind == 'replacement'` in the last 30 days. Use those literal values.

---

## 3. `accounts.get_account_facts()` — missing on `Yash_Amex` (blocking)

The policy engine consumes a single flat dict. `mouryesh/backend/accounts.py`
has `get_account_facts(member_id, distress_signals=False)` that assembles it;
`Yash_Amex/backend/accounts.py` has only per-field getters, so the policy
pipeline would have nothing to call.

**Action:** take `get_account_facts` from `mouryesh`. It must return exactly the
keys the policy YAMLs reference by name: `member_id`, `tenure_months`,
`late_payments_12m`, `prior_waivers_8m`, `income_staleness_months`,
`utilisation`, `fraud_hold`, `replacements_30d`, `distress_signals`. (If keeping
`income_asof` per §1, derive `income_staleness_months` here.)

---

## 4. `actions.py` — thread `session_id` into the ledger (blocking for the demo)

`Yash_Amex/backend/actions.py` calls `append("agent", action, inputs, decision)`
with **no `session_id`**. Result: every `audit_ledger` row has a NULL session,
so `GET /audit/{session_id}` returns nothing and the audit-viewer replay — a
headline demo beat — is empty.

Also, `agent/tools.py` calls each write action with a `session_id=` kwarg, so the
`Yash_Amex` signatures (which don't accept it) are incompatible with the tool
layer.

**Action:** for every write in `actions.py`, add a `session_id=None` parameter
and pass it through to `append(..., session_id=session_id)`. `mouryesh`'s
`actions.py` already does this — use it as the reference. (`ledger.py` itself is
effectively identical on both branches and already accepts `session_id`.)

---

## 5. `idempotency_keys` — minor, non-blocking

`Yash_Amex`'s table has an extra `action` column (nice for audit); `mouryesh`'s
does not. Either works. If unifying, adopt `Yash_Amex`'s richer shape and update
`mouryesh/actions.py`'s insert accordingly. Low priority.

---

## 6. Housekeeping

- **`seed.py`** — exists on `mouryesh` (four demo personas), missing on
  `Yash_Amex`. Use `mouryesh`'s. Once §1–§2 field names are aligned, confirm the
  seed data still matches each persona's expected outcome
  (Priya→APPROVE, Rahul→DECLINE, Ananya→QUEUE, Vikram→ESCALATE).
- **`hi.py`** — stray `print("hi")` file on `Yash_Amex`. Delete.
- **`shared/exceptions.py`** — identical on both branches. No action.
- **`ledger.py`** — logic identical. No action beyond §4's `session_id` flow.

---

## Suggested merge order

1. §0 conversation table — **done in code** on `mouryesh`.
2. §1–§2 field alignment on the `Yash_Amex` backend (or just adopt `mouryesh`'s
   `database.py` wholesale — it already has every column the agent needs).
3. §3 `get_account_facts` — adopt from `mouryesh`.
4. §4 `session_id` threading — adopt `mouryesh`'s `actions.py`.
5. §6 housekeeping.
6. Run `pytest -q` (must stay 69/69) and `python demo.py` (all four personas +
   `Ledger verify: OK`).

The blunt shortcut: **`mouryesh/backend/` already satisfies §1–§4 and §6.** The
fastest safe merge is to make `mouryesh/backend/` the shared backend and port
across only the specific `Yash_Amex` improvements worth keeping (the richer
`messages` columns — already done — and optionally `income_asof` and the
idempotency `action` column).
