"""Spatial barcode discovery and order-independent field association."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping

import cv2
import numpy as np

from ..template.model import BarcodeSpec
from .align import crop_region
from .barcode import find_barcode_candidates, merge_barcode_candidates


def _expand(rect):
    x, y, w, h = rect
    return x - 10.0, y - 10.0, w + 20.0, h + 20.0


def _contains(rect, point):
    x, y, w, h = rect
    return x <= point[0] <= x + w and y <= point[1] <= y + h


def _clipped(candidates, shape):
    height, width = shape[:2]
    for candidate in candidates:
        polygon = np.asarray(candidate["polygon"])
        if (polygon.min(axis=0) <= 2).any() or (polygon.max(axis=0) >= [width - 3, height - 3]).any():
            return True
    return False


def _transform(candidates, face, matrix, locations):
    transformed = []
    for candidate in candidates:
        polygon = cv2.perspectiveTransform(
            np.asarray([candidate["polygon"]], dtype=np.float64), matrix,
        )[0]
        if np.isfinite(polygon).all() and (np.ptp(polygon, axis=0) > 0).all():
            transformed.append({
                **candidate, "face": face, "polygon": polygon.tolist(),
                "_locations": dict(locations),
            })
    return transformed


def _records(observations):
    records = []
    for candidate in merge_barcode_candidates(observations):
        polygon = np.asarray(candidate["polygon"])
        low, high = polygon.min(axis=0), polygon.max(axis=0)
        position = [candidate["face"], np.round(polygon, 3).tolist()]
        physical_identity = json.dumps(position, separators=(",", ":")).encode("ascii")
        identity = json.dumps(
            [*position, candidate["value"], candidate["symbology"]], separators=(",", ":"),
        ).encode("ascii")
        records.append({
            **candidate,
            "id": candidate["face"] + "-" + hashlib.sha256(identity).hexdigest()[:20],
            "_physical_id": hashlib.sha256(physical_identity).hexdigest()[:20],
            "rect": [*map(float, low), *map(float, high - low)],
        })
    return records


def _field_candidates(spec, records):
    candidates = []
    cx, cy = spec.rect[0] + spec.rect[2] / 2, spec.rect[1] + spec.rect[3] / 2
    for record in records:
        if record["face"] != spec.face:
            continue
        center = np.mean(record["polygon"], axis=0)
        stage = record["_locations"].get(spec.key, 2)
        # Location reports the decoding stage, while association also considers
        # candidates learned from other crops or the full page.
        rank = min(stage, 0 if _contains(spec.rect, center) else
                   1 if _contains(_expand(spec.rect), center) else 2)
        candidates.append({
            **{k: v for k, v in record.items() if k != "_locations"},
            "located": ("region", "expanded", "page")[stage],
            "distance_mm": float(np.hypot(center[0] - cx, center[1] - cy)),
            "_rank": rank,
        })
    return sorted(candidates, key=lambda c: (c["_rank"], c["distance_mm"], c["id"], c["value"]))


def read_barcode_regions(
    specs: Iterable[BarcodeSpec],
    sources: Mapping[str, np.ndarray],
    homographies: Mapping[str, np.ndarray],
    dpis: Mapping[str, float],
) -> tuple[dict[str, tuple[str, str, str] | None], dict[str, dict]]:
    """Return legacy hits and review evidence with polygons/rects in template mm.

    Sources are oriented images; homographies map source pixels to template mm.
    Local crops are tried first, then 10 mm expansion, then at most one page
    search per unresolved face. All assignments are resolved together.
    """
    specs = sorted(specs, key=lambda spec: spec.key)
    hits = dict.fromkeys(spec.key for spec in specs)
    evidence = {}
    observations = []
    available = {
        spec.face for spec in specs
        if spec.face in sources and sources[spec.face].size
        and spec.face in homographies and spec.face in dpis
        and np.isfinite(dpis[spec.face]) and dpis[spec.face] > 0
        and np.asarray(homographies[spec.face]).shape == (3, 3)
        and np.isfinite(homographies[spec.face]).all()
        and np.linalg.det(homographies[spec.face]) != 0
    }
    found = set()
    clipped = set()
    for stage in (0, 1):
        for spec in specs:
            if spec.face not in available or (spec.key in found and spec.key not in clipped):
                continue
            rect = spec.rect if stage == 0 else _expand(spec.rect)
            dpi = dpis[spec.face]
            crop = crop_region(sources[spec.face], homographies[spec.face], rect, dpi)
            matrix = np.array([
                [25.4 / dpi, 0, rect[0]],
                [0, 25.4 / dpi, rect[1]],
                [0, 0, 1],
            ], dtype=np.float64)
            decoded = find_barcode_candidates(crop, upscale=True)
            if _clipped(decoded, crop.shape):
                clipped.add(spec.key)
            elif decoded:
                clipped.discard(spec.key)
            candidates = _transform(decoded, spec.face, matrix, {spec.key: stage})
            observations.extend(candidates)
            if candidates:
                found.add(spec.key)
    missing_faces = {
        spec.face for spec in specs if spec.key not in found or spec.key in clipped
    } & available
    for face in sorted(missing_faces):
        observations.extend(_transform(
            find_barcode_candidates(sources[face]), face, homographies[face], {},
        ))

    records = _records(observations)
    by_field = {}
    limits = {}
    for spec in specs:
        candidates = _field_candidates(spec, records)
        by_field[spec.key] = candidates
        limits[spec.key] = max(spec.rect[2:]) + 30.0
        evidence[spec.key] = {
            "status": "failed",
            "candidates": [
                {k: v for k, v in candidate.items() if not k.startswith("_")}
                for candidate in candidates
            ],
            "selected_id": None,
            "reason": ("source_unavailable" if spec.face not in available else
                       "out_of_range" if candidates else "not_found"),
        }

    # Direct region evidence outranks a missing field's distant fallback. Within
    # each tier resolve all fields together, so template order never owns a code.
    unresolved = set(by_field)
    reserved = set()
    for rank in range(3):
        proposals = {}
        claims = defaultdict(set)
        for key in sorted(unresolved):
            pool = [c for c in by_field[key] if c["_rank"] == rank and c["_physical_id"] not in reserved]
            choices = pool
            if rank == 2:
                eligible = [c for c in pool if c["distance_mm"] <= limits[key]]
                nearest = min((c["distance_mm"] for c in eligible), default=None)
                # A near-tie just outside the cutoff still challenges certainty.
                choices = [] if nearest is None else [c for c in pool if c["distance_mm"] <= nearest + 2.0]
            if not choices:
                continue
            proposals[key] = choices
            for candidate in choices:
                claims[candidate["_physical_id"]].add(key)
        for key, choices in proposals.items():
            contended = any(len(claims[c["_physical_id"]]) > 1 for c in choices)
            if contended or len(choices) > 1:
                evidence[key].update(status="ambiguous", reason="candidate_contended" if contended
                                     else "multiple_candidates")
            else:
                selected = choices[0]
                evidence[key].update(status="ok", selected_id=selected["id"], reason=None)
                hits[key] = selected["value"], selected["symbology"], selected["located"]
            reserved.update(c["_physical_id"] for c in choices)
            unresolved.remove(key)
    for key in unresolved:
        if any(c["_physical_id"] in reserved and (c["_rank"] < 2 or c["distance_mm"] <= limits[key])
               for c in by_field[key]):
            evidence[key]["reason"] = "candidate_reserved"
    return hits, evidence
