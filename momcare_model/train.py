"""Reproduces the locked Phase B model from momcare_model/data/.

Run manually (never imported by the running Django server):

    uv run --group ml-train python -m momcare_model.train

Verification target — do not accept a result that doesn't match this without
investigating why something drifted from the locked recipe: official test
accuracy 0.8840, confusion matrix [[439,16,13],[7,45,2],[79,5,446]]
(rows/cols = Low/Medium/High, actual/predicted).

The imputer is fit on a plain numpy array, not the pandas DataFrame directly
— fitting on a DataFrame makes scikit-learn remember column names, which then
warns on every call in predict.py (which never uses pandas, deliberately, to
keep it out of the production dependency set). Fitting on ``.to_numpy()``
avoids that mismatch entirely while keeping column order identical.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import joblib
import pandas as pd
from imblearn.over_sampling import SMOTE
from sklearn.impute import SimpleImputer
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from xgboost import XGBClassifier

from momcare_model.config import (
    FEATURE_COLS,
    RANDOM_STATE,
    REPO_ROOT,
    TARGET_COL,
    TEST_CSV,
    TRAIN_CSV,
    VALIDATION_CSV,
    artifact_dir,
)


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT), text=True,
        ).strip()
    except Exception:  # noqa: BLE001 — metadata is best-effort, never fatal
        return "unknown"


def main(version: str = "v1") -> None:
    train_df = pd.read_csv(TRAIN_CSV)
    val_df = pd.read_csv(VALIDATION_CSV)
    test_df = pd.read_csv(TEST_CSV)

    y_train = train_df[TARGET_COL].to_numpy()
    y_val = val_df[TARGET_COL].to_numpy()
    y_test = test_df[TARGET_COL].to_numpy()

    imputer = SimpleImputer(strategy="median")
    X_train_imputed = imputer.fit_transform(train_df[FEATURE_COLS].to_numpy())
    X_val_imputed = imputer.transform(val_df[FEATURE_COLS].to_numpy())
    X_test_imputed = imputer.transform(test_df[FEATURE_COLS].to_numpy())

    smote = SMOTE(random_state=RANDOM_STATE)
    X_train_resampled, y_train_resampled = smote.fit_resample(X_train_imputed, y_train)

    model = XGBClassifier(random_state=RANDOM_STATE, eval_metric="mlogloss")
    model.fit(X_train_resampled, y_train_resampled)

    val_pred = model.predict(X_val_imputed)
    val_accuracy = accuracy_score(y_val, val_pred)
    print("=== VALIDATION ===")
    print(f"Accuracy: {val_accuracy:.4f}")
    print(classification_report(y_val, val_pred, target_names=["Low", "Medium", "High"]))

    test_pred = model.predict(X_test_imputed)
    test_accuracy = accuracy_score(y_test, test_pred)
    matrix = confusion_matrix(y_test, test_pred)
    print("=== TEST (official) ===")
    print(f"Accuracy: {test_accuracy:.4f}")
    print(classification_report(y_test, test_pred, target_names=["Low", "Medium", "High"]))
    print("Confusion matrix:")
    print(matrix)

    out_dir = artifact_dir(version)
    out_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(out_dir / "xgboost_model.json"))
    # joblib.dump pickles the imputer; safe here since predict.py only ever
    # reads back a file this same script just produced, never external input.
    joblib.dump(imputer, out_dir / "imputer.joblib")

    metadata = {
        "version": version,
        "trained_at": datetime.now(UTC).isoformat(),
        "git_commit": _git_commit(),
        "feature_cols": FEATURE_COLS,
        "random_state": RANDOM_STATE,
        "validation_accuracy": round(float(val_accuracy), 4),
        "test_accuracy": round(float(test_accuracy), 4),
        "test_confusion_matrix": matrix.tolist(),
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    print(f"\nSaved artifact to {out_dir}")


if __name__ == "__main__":
    main()
