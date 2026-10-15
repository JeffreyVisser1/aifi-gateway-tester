"""Metrics zonder externe afhankelijkheden (tegen scikit-learn geverifieerd in tests/test_core.py)."""
from __future__ import annotations

import numpy as np


def confusion_matrix(y_true, y_pred, n_classes: int) -> np.ndarray:
    cm = np.zeros((n_classes, n_classes), dtype=np.int64)
    np.add.at(cm, (np.asarray(y_true, int), np.asarray(y_pred, int)), 1)
    return cm  # rijen = waarheid, kolommen = voorspelling


def _safe_div(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return np.divide(a, b, out=np.zeros_like(a), where=b > 0)


def per_class_prf(cm: np.ndarray) -> dict:
    tp = np.diag(cm).astype(float)
    fp = cm.sum(axis=0) - tp
    fn = cm.sum(axis=1) - tp
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1, "support": cm.sum(axis=1)}


def modality_report(y_true, y_pred, classes: list[str]) -> dict:
    cm = confusion_matrix(y_true, y_pred, len(classes))
    prf = per_class_prf(cm)
    present = prf["support"] > 0
    return {
        "accuracy": float(np.trace(cm) / max(1, cm.sum())),
        "macro_f1": float(prf["f1"][present].mean()) if present.any() else 0.0,
        "per_class": {c: {"precision": float(prf["precision"][i]), "recall": float(prf["recall"][i]),
                          "f1": float(prf["f1"][i]), "support": int(prf["support"][i])}
                      for i, c in enumerate(classes)},
        "confusion_matrix": cm.tolist(),
    }


def region_report(y_true: np.ndarray, probs: np.ndarray, regions: list[str], threshold: float = 0.5) -> dict:
    """Multi-label. Rijen met y_true == -1 (onbekend) tellen niet mee."""
    y_true, probs = np.asarray(y_true), np.asarray(probs)
    labeled = (y_true >= 0).all(axis=1)
    if not labeled.any():
        return {"n_labeled": 0, "macro_f1": 0.0, "exact_match": 0.0, "per_region": {}}
    t = y_true[labeled].astype(bool)
    p = probs[labeled] >= threshold
    tp = (t & p).sum(axis=0)
    fp = (~t & p).sum(axis=0)
    fn = (t & ~p).sum(axis=0)
    precision = _safe_div(tp, tp + fp)
    recall = _safe_div(tp, tp + fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    support = t.sum(axis=0)
    present = support > 0
    return {
        "n_labeled": int(labeled.sum()),
        "macro_f1": float(f1[present].mean()) if present.any() else 0.0,
        "exact_match": float((t == p).all(axis=1).mean()),
        "per_region": {r: {"precision": float(precision[i]), "recall": float(recall[i]),
                           "f1": float(f1[i]), "support": int(support[i])} for i, r in enumerate(regions)},
    }
