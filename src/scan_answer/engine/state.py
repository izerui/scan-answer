"""SheetAnalysis 与 JSON 互转：结束批次时据此重新判定，不必重新对齐和计算涂黑度。"""
from __future__ import annotations

import numpy as np

from .align import AlignResult
from .recognize import FacePlacement, SheetAnalysis


def _values(values) -> list:
    array = np.asarray(values, dtype=np.float64)
    return np.where(np.isfinite(array), array, None).tolist()


def analysis_to_dict(a: SheetAnalysis) -> dict:
    return {
        "swapped": a.swapped,
        "placements": {f: {"source": p.source, "rotated": p.rotated, "dpi": p.dpi,
                           "method": p.align.method, "H": np.asarray(p.align.H).tolist(),
                           "skew_deg": p.align.skew_deg, "residual_mm": p.align.residual_mm,
                           "missing": list(p.align.missing)}
                       for f, p in a.placements.items()},
        "exam_fills": None if a.exam_fills is None else _values(a.exam_fills),
        "absent_fill": a.absent_fill if a.absent_fill is not None and np.isfinite(a.absent_fill) else None,
        "absent_invalid": a.absent_fill is not None and not bool(np.isfinite(a.absent_fill)),
        "objective_fills": [_values(f) for f in a.objective_fills],
        "barcodes": {k: (list(v) if v else None) for k, v in a.barcodes.items()},
        "omr_field_fills": {k: _values(v) for k, v in a.omr_field_fills.items()},
        "choose_fills": {k: _values(v) for k, v in a.choose_fills.items()},
        "sync": dict(a.sync),
        "sampling": a.sampling,
        "barcode_evidence": a.barcode_evidence,
        "evidence_version": a.evidence_version,
    }


def analysis_from_dict(d: dict) -> SheetAnalysis:
    placements = {
        f: FacePlacement(source=int(p["source"]), rotated=int(p["rotated"]), dpi=float(p["dpi"]),
                         align=AlignResult(True, p["method"], H=np.array(p["H"], dtype=np.float64),
                                           skew_deg=float(p["skew_deg"]), residual_mm=float(p["residual_mm"]),
                                           missing=tuple(int(k) for k in p.get("missing", ()))))
        for f, p in d["placements"].items()
    }
    return SheetAnalysis(
        placements=placements,
        swapped=bool(d["swapped"]),
        exam_fills=None if d["exam_fills"] is None else np.array(d["exam_fills"], dtype=np.float32),
        absent_fill=float("nan") if d.get("absent_invalid") else d["absent_fill"],
        objective_fills=[np.array(f, dtype=np.float32) for f in d["objective_fills"]],
        barcodes={k: (tuple(v) if v else None) for k, v in d["barcodes"].items()},
        omr_field_fills={k: np.array(v, dtype=np.float32) for k, v in d.get("omr_field_fills", {}).items()},
        choose_fills={k: np.array(v, dtype=np.float32) for k, v in d.get("choose_fills", {}).items()},
        sync=d.get("sync", {}),
        sampling=d.get("sampling", {}),
        barcode_evidence=d.get("barcode_evidence", {}),
        evidence_version=d.get("evidence_version", 1),
    )


def analysis_fills(a: SheetAnalysis) -> np.ndarray:
    """拟合批次阈值用的全部涂黑度（客观题 + 考号 + 自定义填涂字段 + 选做组）。"""
    parts = [np.asarray(f).ravel() for f in a.objective_fills]
    if a.exam_fills is not None:
        parts.append(np.asarray(a.exam_fills).ravel())
    for v in a.omr_field_fills.values():
        parts.append(np.asarray(v).ravel())
    for v in a.choose_fills.values():
        parts.append(np.asarray(v).ravel())
    values = np.concatenate(parts) if parts else np.zeros(0, np.float32)
    return values[np.isfinite(values)]
