"""条码 / 二维码识别（zxing-cpp）。"""
from __future__ import annotations

import cv2
import numpy as np
import zxingcpp


def _area(candidate: dict) -> float:
    return abs(cv2.contourArea(np.asarray(candidate["polygon"], dtype=np.float32)))


def _same_position(a: dict, b: dict) -> bool:
    if a.get("face") != b.get("face"):
        return False
    pa = cv2.convexHull(np.asarray(a["polygon"], dtype=np.float32))
    pb = cv2.convexHull(np.asarray(b["polygon"], dtype=np.float32))
    smaller = min(abs(cv2.contourArea(pa)), abs(cv2.contourArea(pb)))
    if smaller <= 0:
        return a["polygon"] == b["polygon"]
    overlap, _ = cv2.intersectConvexConvex(pa, pb)
    # A crop can expose only part of a linear barcode's height.
    return overlap / smaller >= 0.6


def merge_barcode_candidates(candidates: list[dict]) -> list[dict]:
    """Merge overlapping observations, retaining distinct payloads as evidence."""
    groups: list[list[dict]] = []
    for candidate in sorted(candidates, key=lambda c: (
        -_area(c), str(c.get("face", "")), c["polygon"], c["value"], c["symbology"],
    )):
        group = next((g for g in groups if _same_position(g[0], candidate)), None)
        if group is None:
            groups.append([candidate])
        else:
            group.append(candidate)
    merged = []
    for group in groups:
        payloads: dict[tuple[str, str], dict] = {}
        for candidate in group:
            key = candidate["value"], candidate["symbology"]
            if key not in payloads:
                payloads[key] = {**candidate, "polygon": group[0]["polygon"]}
                if "_locations" in candidate:
                    payloads[key]["_locations"] = {}
            for field, stage in candidate.get("_locations", {}).items():
                locations = payloads[key]["_locations"]
                locations[field] = min(stage, locations.get(field, stage))
        merged.extend(payloads.values())
    return sorted(merged, key=lambda c: (
        str(c.get("face", "")), c["polygon"], c["value"], c["symbology"],
    ))


def find_barcode_candidates(gray: np.ndarray, *, upscale: bool = False) -> list[dict]:
    """Return payloads and polygons in input pixels, unioning original and Otsu."""
    if gray.size == 0:
        return []
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    candidates = []

    def collect(image, scale=1.0):
        for result in zxingcpp.read_barcodes(image):
            if not (result.valid and result.text):
                continue
            pos = result.position
            points = (pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left)
            polygon = [[float(p.x) / scale, float(p.y) / scale] for p in points]
            if not np.isfinite(polygon).all():
                continue
            contour = np.asarray(polygon, dtype=np.float32)
            if cv2.contourArea(contour) == 0:
                if not np.ptp(contour, axis=0).any():
                    continue
                # A linear decoder may return one scanline. Give the line its
                # one-pixel support instead of emitting a zero-area crop.
                center, (width, height), angle = cv2.minAreaRect(contour)
                polygon = cv2.boxPoints((
                    center, (max(width, 1.0 / scale), max(height, 1.0 / scale)), angle,
                )).astype(float).tolist()
            candidates.append({
                "value": result.text, "symbology": result.format.name, "polygon": polygon,
            })

    collect(gray)
    collect(binary)
    if not candidates and upscale:
        collect(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC), 2.0)
    return merge_barcode_candidates(candidates)


def read_barcode(gray: np.ndarray) -> tuple[str, str] | None:
    """兼容单值接口；多候选关联请使用 read_barcode_regions。"""
    candidates = find_barcode_candidates(gray, upscale=True)
    return (candidates[0]["value"], candidates[0]["symbology"]) if candidates else None


def find_barcodes(gray: np.ndarray) -> list[tuple[str, str, tuple[float, float]]]:
    """返回 [(文本, 码制, 中心像素坐标)]，保留不同位置的相同文本。"""
    return [
        (c["value"], c["symbology"], tuple(map(float, np.mean(c["polygon"], axis=0))))
        for c in find_barcode_candidates(gray)
    ]
