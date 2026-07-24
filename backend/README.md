# backend/database.py

The foundation of the backend layer. It is **step 5** in the build order and it
**unblocks every other backend file** (`ledger.py`, `accounts.py`, `actions.py`,
`seed.py`). Nothing in the backend can touch data until this exists.

## Why this file exists

It sits at the very bottom of the dependency stack:

```
backend/database.py  ->  imports config   (and nothing else in the project)
```

It never imports schemas, agent code, or anything upward. Keeping it this low
means every other layer can rely on it without creating a circular dependency.

## Its one job (three responsibilities)

1. **Connection setup** — open the SQLite database at the path from
   `shared/config.py`, with `row_factory = Row` (dict-like rows) and
   `PRAGMA foreign_keys = ON`.
2. **Table DDL** — define and create the five tables.
3. **A connection accessor** the other backend files call to get a cursor.

No ORM (no SQLAlchemy). Raw `sqlite3` + raw SQL, on purpose:
*"Raw SQL in database.py is honest and readable."*

## The six tables

| Table | Purpose |
|---|---|
| `accounts` | Member data: balance, credit limit, tenure, income + staleness, card status, vulnerability flag |
| `transactions` | Charge history (dates, amounts, descriptions, reversed flag) |
| `waiver_history` | Prior courtesy waivers with the policy version that granted them |
| `audit_ledger` | **Append-only, hash-chained** — the crown jewel of the demo |
| `idempotency_keys` | Tracks processed keys so a write never fires twice |
| `messages` | Durable turn-by-turn transcript: member queries + model replies |

### Why `messages` is separate from `audit_ledger`

The ledger is for **decisions and actions** — every row is meant to be an
auditable event in the hash chain. Chatty conversation turns don't belong there;
mixing them would pollute the chain and muddy the "every row is a decision"
story. So the raw transcript lives in its own `messages` table, which powers:

- the **audit-viewer replay** ("same interaction replayed as a decision log"),
- **first-contact-resolution metrics** (`test_classifier.py`), and
- **after-the-fact escalation context** for a human agent.

An index on `(session_id, turn_index)` keeps per-session transcript reads fast
and ordered.

## The rule that matters most

> The `audit_ledger` table has **NO UPDATE path and NO DELETE path**. This is
> enforced **here, at the access layer** — not by developer discipline.

This module exposes no helper that constructs an `UPDATE` or `DELETE` against
`audit_ledger`. The append-only / tamper-evident guarantee is a property of the
code surface, so nothing downstream can violate it even by accident. That is
what makes the "tamper-and-go-red" demo (`ledger.verify()`) meaningful.

## Public API

| Function | What it does | Called by |
|---|---|---|
| `get_connection()` | Returns a configured `sqlite3.Connection` | Anything needing a raw connection |
| `connection()` | Context manager: commit on success, rollback on error, always close | All backend read/write code |
| `init_db()` | Creates all tables if missing (idempotent) | `api/main.py` on startup |
| `tables_are_empty()` | `True` if `accounts` has no rows | Startup, to decide whether to seed |

## How the other files use it

- `ledger.py` — `INSERT` into `audit_ledger`, and `SELECT` in chain order for `verify()`.
- `accounts.py` — read `accounts`, `transactions`, `waiver_history`, income data.
- `actions.py` — write balance/limit/card changes, check/insert `idempotency_keys`.
- `seed.py` — create the four demo personas.
- `api/main.py` — call `init_db()` on startup, then seed if `tables_are_empty()`.

## Running it standalone

```bash
python -m backend.database
```

Initialises the schema and prints the DB location + created tables. The DB path
comes from `shared/config.py` when present, otherwise the `SERVICING_DB_PATH`
env var, otherwise `servicing.db` in the working directory.
