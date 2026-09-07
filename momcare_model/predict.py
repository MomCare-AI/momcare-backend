"""predict(vitals) -> dict | None — the single inference entry point.

The only function anything outside this package should ever call. The
artifact is loaded once per process and cached at module level — with two
gunicorn workers on a memory-constrained instance, re-reading the model files
from disk on every single reading would be pure waste.

Returns ``None`` when every one of FEATURE_COLS is missing. With zero real
vitals, the imputer would fill all nine inputs from training medians and the
model would still confidently return a risk level — a fabricated guess
wearing a real confidence score, built from no actual reading. Callers must
treat ``None`` as "not enough data to assess," the same way the rules-engine
seam already leaves ``score``/``confidence`` null rather than inventing
numbers — never as an error to work around.

Deliberately does NOT apply the Africa+Medium->High rule or the confidence
threshold — those are postprocessing decisions for
core.monitoring.services.reassess_risk(), which has the pregnancy's region
and hospital available and this function does not. This reports only what
the model itself produced.

No pandas here, on purpose — pandas is a training-only dependency (see the
``ml-train`` group in pyproject.toml), never installed in production. Missing
vitals are represented as ``float("nan")``, not ``None``, because that is
what the fitted SimpleImputer was configured to treat as missing.

``joblib.load`` deserializes via pickle, which is unsafe for untrusted input
— but the file it reads here (``imputer.joblib``) is produced by our own
train.py and shipped as part of this repo's own artifact, never accepted
from a request or an external source.
"""

from __future__ import annotations

import math

import joblib
from sklearn.impute import SimpleImputer
from xgboost import XGBClassifier

from momcare_model import clinical_categories
from momcare_model.config import FEATURE_COLS, LABEL_BY_CODE, artifact_dir

_model: XGBClassifier | None = None
_imputer: SimpleImputer | None = None


def _clean(value) -> float:
    return float(value) if value is not None else math.nan


def _load() -> tuple[XGBClassifier, SimpleImputer]:
    global _model, _imputer
    if _model is None or _imputer is None:
        artifact = artifact_dir()
        model = XGBClassifier()
        model.load_model(str(artifact / "xgboost_model.json"))
        _model = model
        _imputer = joblib.load(artifact / "imputer.joblib")
    return _model, _imputer


def predict(vitals: dict) -> dict | None:
    """``vitals``: any subset of FEATURE_COLS; missing entries as None or absent.

    Returns None if none of the nine were actually provided — see module
    docstring.
    """
    if all(vitals.get(col) is None for col in FEATURE_COLS):
        return None

    model, imputer = _load()

    row = [[_clean(vitals.get(col)) for col in FEATURE_COLS]]
    row_imputed = imputer.transform(row)

    probabilities = model.predict_proba(row_imputed)[0]
    code = int(probabilities.argmax())
    confidence = float(probabilities[code])

    return {
        "risk_level": LABEL_BY_CODE[code],
        "confidence": confidence,
        **clinical_categories.categorize(vitals),
    }
