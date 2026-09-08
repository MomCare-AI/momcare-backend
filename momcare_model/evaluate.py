"""Confidence-vs-error analysis for the confidence-threshold decision.

Answers the question raised earlier: where do the errors that actually
matter — a true High-risk patient predicted as Low — sit on the model's own
confidence scale? That is real evidence for whether 70% is the right
confidence-threshold default, rather than a guess. Run after train.py has
produced an artifact:

    uv run --group ml-train python -m momcare_model.evaluate
"""

from __future__ import annotations

import joblib
import pandas as pd
from xgboost import XGBClassifier

from momcare_model.config import FEATURE_COLS, LEVEL_HIGH, LEVEL_LOW, TARGET_COL, TEST_CSV, artifact_dir


def main(version: str | None = None) -> None:
    artifact = artifact_dir(version)
    model = XGBClassifier()
    model.load_model(str(artifact / "xgboost_model.json"))
    imputer = joblib.load(artifact / "imputer.joblib")

    test_df = pd.read_csv(TEST_CSV)
    X_test = imputer.transform(test_df[FEATURE_COLS].to_numpy())
    y_test = test_df[TARGET_COL].to_numpy()

    probabilities = model.predict_proba(X_test)
    predictions = probabilities.argmax(axis=1)
    confidences = probabilities.max(axis=1)

    correct = predictions == y_test
    print(f"Overall test accuracy: {correct.mean():.4f}")
    print(f"Mean confidence, correct predictions:   {confidences[correct].mean():.4f}")
    print(f"Mean confidence, incorrect predictions: {confidences[~correct].mean():.4f}")

    # The safety-critical error: a true High-risk patient predicted as Low.
    dangerous = (y_test == LEVEL_HIGH) & (predictions == LEVEL_LOW)
    total_high = (y_test == LEVEL_HIGH).sum()
    print(f"\nTrue High predicted as Low: {dangerous.sum()} of {total_high} true High cases")

    if dangerous.sum():
        dangerous_confidences = confidences[dangerous]
        print(
            f"Their confidence — mean: {dangerous_confidences.mean():.4f}, "
            f"min: {dangerous_confidences.min():.4f}, max: {dangerous_confidences.max():.4f}",
        )
        print("\nWhat each candidate threshold would actually do:")
        for threshold in (0.50, 0.60, 0.70, 0.80, 0.90):
            caught = int((dangerous_confidences < threshold).sum())
            flagged_overall = float((confidences < threshold).mean())
            print(
                f"  {threshold:.2f}: catches {caught}/{dangerous.sum()} of the dangerous misses "
                f"for review, flags {flagged_overall:.1%} of all test predictions overall",
            )


if __name__ == "__main__":
    main()
