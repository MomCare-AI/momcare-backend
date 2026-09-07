"""Constants shared by train.py, evaluate.py, and predict.py — single source.

Nothing here may be redefined anywhere else. The category-threshold drift
this project already hit once (risk_rules.py silently disagreeing with the
CSVs that actually trained the model) was exactly this kind of "same fact,
defined twice" bug — this file exists so it cannot happen again for the
feature list, the encoding, or the artifact path.
"""

from __future__ import annotations

from pathlib import Path

# ── Reproducibility ──────────────────────────────────────────────────────────
RANDOM_STATE = 42

# ── Features ──────────────────────────────────────────────────────────────────
# Order matters: this is the exact column order the locked model was trained
# on. Reordering, adding, or removing an entry here without retraining feeds
# the model different data than it learned from, silently.
FEATURE_COLS = [
    "age",
    "systolic_bp",
    "diastolic_bp",
    "body_temp_f",
    "heart_rate",
    "hemoglobin",
    "blood_glucose",
    "stress_score",
    "phys_activity_score",
]

TARGET_COL = "risk_level"

# ── Risk level encoding ──────────────────────────────────────────────────────
# Canonical everywhere, including sort keys (MEMORY.md). The model's raw
# output is one of these three integers; predict() translates to the string
# labels RiskAssessment.LEVEL_CHOICES actually stores.
LEVEL_LOW = 0
LEVEL_MEDIUM = 1
LEVEL_HIGH = 2

LABEL_BY_CODE = {
    LEVEL_LOW: "low",
    LEVEL_MEDIUM: "medium",
    LEVEL_HIGH: "high",
}

# ── Paths ─────────────────────────────────────────────────────────────────────
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DATA_DIR = PACKAGE_DIR / "data"
ARTIFACTS_DIR = PACKAGE_DIR / "models" / "artifacts"

TRAIN_CSV = DATA_DIR / "momcare_train_final.csv"
VALIDATION_CSV = DATA_DIR / "momcare_validation_final.csv"
TEST_CSV = DATA_DIR / "momcare_test_final.csv"

# ── Active model version ─────────────────────────────────────────────────────
# predict.py loads from ARTIFACTS_DIR / ACTIVE_MODEL_VERSION. Promoting a
# newly-trained model to production is changing this one string and
# redeploying — the previous version's folder is never deleted or overwritten,
# so a rollback is also just changing this string back.
ACTIVE_MODEL_VERSION = "v1"


def artifact_dir(version: str | None = None) -> Path:
    """The directory holding one trained artifact's three files."""
    return ARTIFACTS_DIR / (version or ACTIVE_MODEL_VERSION)
