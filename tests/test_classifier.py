"""tests/test_classifier.py — the ~40-utterance labelled set. THIS is the metrics slide.

Held out from agent/training_data.py — the classifier never saw these exact
phrasings during training, so this is a genuine generalisation measurement,
not a memorisation check. Runs against the trained ML classifier (local
sentence-transformer embeddings + Logistic Regression, see
scripts/train_classifier.py); falls back to the keyword heuristic only if the
ML artifact isn't available in this environment, so results stay reproducible
in CI either way.
"""
from collections import defaultdict

import pytest

from agent.classifier import TAXONOMY, classify
from agent.graph import run
from agent.state import new_state
from backend.seed import seed

# (utterance, gold_label). 'clarify' is the fallback for genuinely ambiguous
# input; hardship phrases are labelled hardship even when a request co-occurs.
LABELLED = [
    ("Please waive my late fee", "fee_waiver"),
    ("Can you reverse the fee I was charged?", "fee_waiver"),
    ("I'd like the late fee waived", "fee_waiver"),
    ("remove the fee from my statement", "fee_waiver"),
    ("Raise my credit limit please", "credit_limit_increase"),
    ("I want to increase my limit", "credit_limit_increase"),
    ("Can I get a higher limit?", "credit_limit_increase"),
    ("My card is damaged, can I get a replacement?", "card_replacement"),
    ("Send me a new card", "card_replacement"),
    ("I need to reissue my card", "card_replacement"),
    ("I lost my card, please block it", "card_block"),
    ("My card was stolen", "card_block"),
    ("Freeze my card immediately", "card_block"),
    ("What is this charge on my account?", "explain_charge"),
    ("Why was I charged this amount?", "explain_charge"),
    ("Explain this charge for me", "explain_charge"),
    ("What's my current balance?", "account_info"),
    ("How much available credit do I have?", "account_info"),
    ("Tell me my account info", "account_info"),
    ("I forgot my PIN, reset it", "reset_pin"),
    ("Change my PIN please", "reset_pin"),
    ("I need a new pin", "reset_pin"),
    ("Update my address, I moved", "update_address"),
    ("Change my address on file", "update_address"),
    ("I can't pay this month", "hardship"),
    ("I lost my job and can't afford the bill", "hardship"),
    ("I'm struggling financially", "hardship"),
    ("I was laid off and behind on payments", "hardship"),
    ("I didn't make this purchase", "dispute_transaction"),
    ("There's an unauthorized charge", "dispute_transaction"),
    ("I want to dispute a transaction", "dispute_transaction"),
    ("My payment failed online", "payment_issue"),
    ("Autopay failed this month", "payment_issue"),
    ("Update my email address", "profile_update"),
    ("Change my phone number", "profile_update"),
    # Ambiguous / out-of-scope → clarify.
    ("hello", "clarify"),
    ("I have a question", "clarify"),
    ("can you help me", "clarify"),
    ("thanks", "clarify"),
]


def _predict(utterance: str) -> str:
    return classify(utterance).label


def test_metrics_report(capsys):
    """Compute and print per-category precision/recall/F1; assert overall accuracy."""
    tp = defaultdict(int)
    fp = defaultdict(int)
    fn = defaultdict(int)
    correct = 0

    for utterance, gold in LABELLED:
        pred = _predict(utterance)
        if pred == gold:
            correct += 1
            tp[gold] += 1
        else:
            fp[pred] += 1
            fn[gold] += 1

    labels = sorted(set(TAXONOMY) | {"clarify"})
    print("\n\n=== Classifier metrics (held-out set) ===")
    print(f"{'category':<24}{'P':>6}{'R':>6}{'F1':>6}")
    for label in labels:
        p = tp[label] / (tp[label] + fp[label]) if (tp[label] + fp[label]) else 0.0
        r = tp[label] / (tp[label] + fn[label]) if (tp[label] + fn[label]) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        if tp[label] + fp[label] + fn[label] > 0:
            print(f"{label:<24}{p:>6.2f}{r:>6.2f}{f1:>6.2f}")

    accuracy = correct / len(LABELLED)
    print(f"\noverall accuracy: {accuracy:.2%} ({correct}/{len(LABELLED)})")

    with capsys.disabled():
        pass
    assert accuracy >= 0.90, f"classifier accuracy {accuracy:.2%} below 90%"


@pytest.mark.parametrize(
    "utterance, gold",
    [(u, g) for u, g in LABELLED if g != "clarify"],
)
def test_each_labelled_utterance(utterance, gold):
    # "clarify" is an acceptable outcome here, distinct from a confidently
    # WRONG label: the classifier's threshold means "not sure enough" is a
    # safe deferral (asks a follow-up question), never a silent wrong guess.
    # A genuinely wrong non-clarify label still fails this test.
    pred = _predict(utterance)
    assert pred in (gold, "clarify"), f"{utterance!r} -> {pred!r}, expected {gold!r}"


def test_hardship_beats_cooccurring_request():
    # A hardship phrase alongside a fee request must classify as hardship.
    assert _predict("I can't pay and I want my late fee waived") == "hardship"


# Persona, message, and whether we EXPECT first-contact resolution. Vikram is
# deliberately expected to NOT resolve on first contact — escalating hardship
# instead of answering it alone is correct behaviour, not a shortfall. A 100%
# FCR across all four would actually indicate a bug (duty-of-care bypassed),
# not a win.
FCR_SCENARIOS = [
    ("MEM-PRIYA", "please waive my late fee", True),
    ("MEM-RAHUL", "please waive my late fee", True),
    ("MEM-ANANYA", "I'd like to raise my credit limit", True),
    ("MEM-VIKRAM", "I lost my job and can't pay this month", False),
]


def _resolved_first_contact(member_id: str, message: str) -> bool:
    """True if the agent produced a complete answer in one turn — no follow-up
    question needed (awaiting_member) and no human handoff (escalate). A
    DECLINE or QUEUE outcome still counts as resolved: the agent gave the
    member a final, actionable answer without leaving the conversation open."""
    result = run(new_state(f"fcr-{member_id}", member_id, message))
    return not result.get("awaiting_member", False) and not result.get("escalate", False)


def test_first_contact_resolution_rate(capsys):
    """Task 5 of the problem statement: 'test and optimize... for first-contact
    resolution rate'. Named as a required metric in the original architecture
    doc's test_classifier.py spec but not previously computed — this is that
    metric, across the four demo personas."""
    seed()
    resolved = 0
    print("\n\n=== First-contact resolution ===")
    for member_id, message, expected in FCR_SCENARIOS:
        actual = _resolved_first_contact(member_id, message)
        resolved += actual
        mark = "resolved" if actual else "escalated/awaiting"
        print(f"  {member_id:12} -> {mark:20} (expected: "
              f"{'resolved' if expected else 'escalated/awaiting'})")
        assert actual == expected, (
            f"{member_id}: expected first-contact-resolved={expected}, got {actual}"
        )

    rate = resolved / len(FCR_SCENARIOS)
    print(f"\nFCR: {rate:.0%} ({resolved}/{len(FCR_SCENARIOS)}) — "
          f"1 of 4 (Vikram/hardship) is EXPECTED to escalate, not a miss.")
