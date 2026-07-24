"""demo.py — run the four personas end-to-end through the graph, no server needed.

    python demo.py

Reseeds the database, then drives Priya / Rahul / Ananya / Vikram and prints
each reply, decision, and action. Finishes with a live ledger verify. This is
the fastest way to sanity-check the whole agent layer during the hackathon.
"""
from agent.graph import GRAPH, run
from agent.state import new_state
from backend import ledger
from backend.seed import seed

SCENARIOS = [
    ("MEM-PRIYA", "Please waive my late fee", "APPROVE — fee reversed"),
    ("MEM-RAHUL", "Can you waive my late fee?", "DECLINE — policy cited"),
    ("MEM-ANANYA", "I'd like to raise my credit limit", "QUEUE — underwriting"),
    ("MEM-VIKRAM", "I lost my job and can't pay this month", "ESCALATE — collections suppressed"),
]


def main() -> None:
    seed()
    print(f"Graph backend: {GRAPH.backend}\n" + "=" * 70)

    for i, (member, message, expected) in enumerate(SCENARIOS, 1):
        session = f"demo-{member}"
        state = run(new_state(session, member, message))
        intent = state.get("intent")
        decisions = state.get("decision_records", [])
        actions = state.get("actions_taken", [])

        print(f"\n[{i}] {member} — expected: {expected}")
        print(f"    member : {message}")
        print(f"    intent : {intent.label if intent else '?'} "
              f"(conf {intent.confidence:.2f})" if intent else "")
        for d in decisions:
            print(f"    policy : {d.outcome.value} [{d.reason_code}] "
                  f"via {d.policy_id}@{d.policy_version} rule={d.rule_id}")
        for a in actions:
            print(f"    action : {a['tool']} tier={a['tier']} status={a['status']}")
        print(f"    escalate: {state.get('escalate', False)}")
        print(f"    reply  : {state.get('reply', '').splitlines()[0] if state.get('reply') else ''}")

    print("\n" + "=" * 70)
    print("Ledger verify:", ledger.verify())


if __name__ == "__main__":
    main()
