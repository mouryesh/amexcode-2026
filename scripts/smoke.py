"""One runnable check for the ported backend.

    python scripts/init_stores.py && python scripts/smoke.py

Proves the four things that would silently rot: the chain verifies, an action
is atomic and idempotent, a decline still produces an audit row, and tampering
is caught at the exact record.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import accounts, actions, docstore, ledger, policy_registry, seed, sessions  # noqa: E402
from backend import database
from backend.database import db_session  # noqa: E402
from shared.exceptions import IdempotencyConflict, PolicyNotBound  # noqa: E402


def main() -> int:
    seed.seed()
    sid = f"smoke-{uuid.uuid4().hex[:8]}"

    # --- policy dispatch is exact, and a miss fails closed ------------------ #
    assert policy_registry.resolve("fees_interest.request_fee_waiver") == \
        "fee.late.courtesy_waiver"
    try:
        policy_registry.resolve("membership_rewards.transfer_to_another_person")
        raise AssertionError("unbound intent should have failed closed")
    except PolicyNotBound:
        pass

    # --- facts are floats, not Decimals ------------------------------------- #
    facts = accounts.get_account_facts("MEM-PRIYA")
    assert isinstance(facts["utilisation"], float), type(facts["utilisation"])
    assert facts["prior_waivers_8m"] == 0
    assert accounts.get_account_facts("MEM-RAHUL")["prior_waivers_8m"] == 2

    # --- the four facts that used to be missing ------------------------------ #
    assert 1000 < facts["account_age_days"] < 1200, facts["account_age_days"]
    assert facts["consecutive_missed_periods"] == 0
    assert accounts.get_account_facts("MEM-VIKRAM")["consecutive_missed_periods"] == 3
    assert facts["months_since_last_cli"] is None, "never asked is not zero"
    assert accounts.get_account_facts("MEM-ANANYA")["months_since_last_cli"] == 2
    # is_primary_holder only appears once a card is known — it is card-scoped.
    assert "is_primary_holder" not in facts
    assert accounts.get_account_facts("MEM-PRIYA", card_id="CARD-P1")["is_primary_holder"]

    # --- multi-card: two candidates must never resolve silently -------------- #
    # Deepa carries the ambiguous case; Priya deliberately stays single-card so
    # the headline demo does not open with a clarifying question.
    cards = accounts.get_cards("MEM-DEEPA")
    assert len(cards) == 2, cards
    assert {c["last4"] for c in cards} == {"5510", "8823"}
    assert len(accounts.get_unreversed_fees("MEM-DEEPA")) == 2, "should be ambiguous"
    scoped = accounts.get_unreversed_fees("MEM-DEEPA", card_id="CARD-D1")
    assert len(scoped) == 1 and scoped[0]["last4"] == "5510", scoped
    assert len(accounts.get_cards("MEM-PRIYA")) == 1, "single card auto-resolves"

    # --- sensitivity fails safe on unknown slots ----------------------------- #
    assert sessions.sensitivity_of("card_selection") == "internal"
    assert sessions.sensitivity_of("otp") == "restricted"
    assert sessions.sensitivity_of("some_field_nobody_classified") == "restricted"

    # --- a DECLINE writes an audit row even though nothing was mutated ------ #
    decline = {
        "outcome": "DECLINE", "reason_code": "PRIOR_WAIVER_WITHIN_8M",
        "policy_id": "fee.late.courtesy_waiver", "policy_version": "v1",
    }
    ledger.emit_policy(sid, "MEM-RAHUL", decline, accounts.get_account_facts("MEM-RAHUL"))
    rows = ledger.records_for_session(sid)
    assert len(rows) == 1 and rows[0]["event_type"] == "policy", rows
    assert rows[0]["decision"]["reason_code"] == "PRIOR_WAIVER_WITHIN_8M"

    # --- a write action: atomic, verified, idempotent ------------------------ #
    before = accounts.get_balance("MEM-PRIYA")["balance"]
    fee = accounts.get_unreversed_fees("MEM-PRIYA", card_id="CARD-P1")[0]
    key = f"{sid}:reverse_fee"
    approve = {"outcome": "APPROVE", "reason_code": "CLEAN_HISTORY",
               "policy_id": "fee.late.courtesy_waiver", "policy_version": "v1"}

    r1 = actions.reverse_fee("MEM-PRIYA", fee["amount"], fee["txn_ref"], approve, key, sid)
    assert r1.new_balance == before - fee["amount"], (r1.new_balance, before)

    r2 = actions.reverse_fee("MEM-PRIYA", fee["amount"], fee["txn_ref"], approve, key, sid)
    assert r2.reference == r1.reference, "idempotent replay returned a new receipt"
    assert accounts.get_balance("MEM-PRIYA")["balance"] == r1.new_balance, "double charged"

    try:
        actions.reverse_fee("MEM-PRIYA", 999.0, fee["txn_ref"], approve, key, sid)
        raise AssertionError("key reuse with different inputs should conflict")
    except IdempotencyConflict:
        pass

    # --- session state round-trips ------------------------------------------ #
    sessions.record_turn(sid, "MEM-RAHUL", "member", "waive my late fee",
                         intent="fees_interest.request_fee_waiver", confidence=0.94,
                         decision_outcome="DECLINE",
                         decision_reason_code="PRIOR_WAIVER_WITHIN_8M",
                         policy_id="fee.late.courtesy_waiver")
    assert len(sessions.get_history(sid)) == 1
    assert sessions.count_prior_declines(
        sid, "fee.late.courtesy_waiver", "PRIOR_WAIVER_WITHIN_8M") == 1

    # --- terminalize purges secrets, keeps context --------------------------- #
    sessions.start_flow(sid, "f-1", "fees_interest", "request_fee_waiver")
    sessions.set_slot(sid, "card_selection", "CARD-P1", source="member_message")
    sessions.set_slot(sid, "otp", "123456")
    sessions.set_slot(sid, "mystery_field", "whatever")
    slots = sessions.get_slots(sid)
    assert slots["card_selection"]["source"] == "member_message"
    assert slots["otp"]["sensitivity"] == "restricted"

    sessions.terminalize(sid, "APPROVE")
    kept = sessions.get_session(sid)["completed_flows"][-1]["slots"]
    assert "card_selection" in kept
    assert "otp" not in kept, "secret survived terminalize"
    assert "mystery_field" not in kept, "unclassified slot survived — fail-open"

    # --- the chain verifies -------------------------------------------------- #
    v = ledger.verify()
    assert v["status"] == "OK", v
    print(f"chain OK across {v['records']} records, head {v['chain_head'][:12]}...")

    anchor = ledger.anchor_head(sid)
    assert anchor["anchored"] and anchor["chain_head"] == v["chain_head"]

    # --- tampering is caught at the exact record ----------------------------- #
    # The trigger blocks UPDATE for everyone, including the owner. Having to
    # disable it to tamper at all is the point; the chain then names the row.
    target = ledger.records_for_session(sid)[0]["record_id"]

    # The guard blocks UPDATE for everyone, so tampering at all requires
    # lifting it first. Prove that, then prove the chain catches it anyway.
    try:
        with db_session() as conn:
            conn.execute(
                "UPDATE audit_ledger SET action = 'tampered' WHERE record_id = %s", (target,)
            )
        raise AssertionError("append-only guard did not block the UPDATE")
    except AssertionError:
        raise
    except Exception:
        pass  # blocked, as designed

    with database.ledger_guard_disabled():
        with db_session() as conn:
            conn.execute(
                "UPDATE audit_ledger SET action = 'tampered' WHERE record_id = %s", (target,)
            )

    v2 = ledger.verify()
    assert v2["status"] == "TAMPERED", v2
    assert v2["broken_at_record_id"] == target, (v2, target)
    print(f"tamper caught at record {v2['broken_at_record_id']}: {v2['reason']}")

    # Leave the chain verifying again. Without this the tampered row persists
    # and every later /audit/verify reports TAMPERED — which looks like a
    # broken system rather than a finished test.
    with database.ledger_guard_disabled():
        with db_session() as conn:
            conn.execute("DELETE FROM audit_ledger")
    assert ledger.verify()["status"] == "OK"
    print("ledger reset — chain verifies clean again")

    print(f"\nsplunk: {__import__('shared.splunk', fromlist=['x']).stats()}")
    print(f"mongo:  {docstore.stats()}")
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


