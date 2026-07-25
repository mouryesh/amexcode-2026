"""scripts/train_classifier.py — trains the intent classifier and saves the artifact.

    python scripts/train_classifier.py

Pipeline: agent/training_data.py -> sentence-transformer embeddings (384-dim,
local, CPU) -> Logistic Regression -> agent/models/intent_classifier.joblib.

The saved artifact bundles the fitted classifier, the label list, and the
training texts/embeddings themselves (used at inference time to report a
nearest-training-example "rationale", since a linear model on dense embeddings
has no keyword-level explanation the way the old heuristic did).

Run this whenever agent/training_data.py changes. The committed artifact means
teammates don't need to retrain to run the app — only whoever edits the
training data needs to re-run this script and commit the new artifact.
"""
from __future__ import annotations

import sys
from pathlib import Path

import joblib
import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import cross_val_score

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.training_data import TRAINING_EXAMPLES  # noqa: E402

MODEL_NAME = "all-MiniLM-L6-v2"
ARTIFACT_PATH = Path(__file__).resolve().parent.parent / "agent" / "models" / "intent_classifier.joblib"


def main() -> None:
    texts = [t for t, _ in TRAINING_EXAMPLES]
    labels = [l for _, l in TRAINING_EXAMPLES]

    print(f"Training examples: {len(texts)} across {len(set(labels))} classes")
    print(f"Loading embedder: {MODEL_NAME} (downloads once, then cached locally)")
    embedder = SentenceTransformer(MODEL_NAME)

    print("Computing embeddings...")
    embeddings = embedder.encode(texts, show_progress_bar=False)

    print("Fitting Logistic Regression...")
    # C=10 (weaker L2 regularization than the sklearn default C=1): with 13
    # classes and ~150 training examples, the default under-sharpens
    # predict_proba — correct predictions were scoring ~0.55-0.6 confidence,
    # below the classifier's own 0.65 "never guess" threshold. C=10 lands
    # confidence around 0.9-0.95 on clear cases without tipping into the
    # near-1.0 overconfidence seen at C=50+.
    clf = LogisticRegression(max_iter=3000, C=10.0)
    clf.fit(embeddings, labels)

    # 5-fold cross-validation on the TRAINING set — a quick sanity check, not
    # the real metric. The honest generalisation number comes from
    # tests/test_classifier.py's held-out set, which this script never sees.
    scores = cross_val_score(clf, embeddings, labels, cv=5)
    print(f"5-fold CV accuracy on training data: {scores.mean():.2%} (+/- {scores.std():.2%})")

    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({
        "model_name": MODEL_NAME,
        "classifier": clf,
        "labels": sorted(set(labels)),
        "train_texts": texts,
        "train_embeddings": np.asarray(embeddings),
        "train_labels": labels,
    }, ARTIFACT_PATH)
    print(f"Saved artifact to {ARTIFACT_PATH} ({ARTIFACT_PATH.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
