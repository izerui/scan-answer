"""全局对齐：把扫描图映射到模板坐标（mm）。

定位方式：定位块（anchors）/ 外框（frames）；都失败时用特征匹配（features）兜底。
所有函数返回的单应矩阵 H 都把"图像像素坐标"映射到"模板 mm 坐标"。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..template.model import AlignSpec, Rect

MM_PER_INCH = 25.4


def px_per_mm(dpi: float) -> float:
    return dpi / MM_PER_INCH


def scale_matrix(dpi: float) -> np.ndarray:
    s = px_per_mm(dpi)
    return np.array([[s, 0, 0], [0, s, 0], [0, 0, 1]], dtype=np.float64)


@dataclass
class AlignResult:
    ok: bool
    method: str
    H: np.ndarray | None = None  # 图像像素 -> 模板 mm
    skew_deg: float = 0.0
    residual_mm: float = 0.0
    reason: str = ""


def _center(r: Rect) -> tuple[float, float]:
    return (r[0] + r[2] / 2, r[1] + r[3] / 2)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """按 左上、右上、右下、左下 排序。"""
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def _anchor_candidates(gray: np.ndarray, size_mm: tuple[float, float], dpi: float) -> np.ndarray:
    """全图找与定位块尺寸相近的实心深色块，返回中心坐标数组 (N, 2)（像素）。"""
    s = px_per_mm(dpi)
    rw, rh = size_mm[0] * s, size_mm[1] * s
    n, _, st, cen = cv2.connectedComponentsWithStats((gray < 140).astype(np.uint8))
    keep = []
    for k in range(1, n):
        x, y, w, h, a = st[k]
        # 允许旋转后外接框变大：宽高在 0.6~1.6 倍之间，实心度 > 0.5
        if 0.6 * min(rw, rh) < min(w, h) < 1.6 * max(rw, rh) and 0.6 * max(rw, rh) < max(w, h) < 1.8 * max(rw, rh) \
                and a > 0.5 * w * h:
            keep.append(cen[k])
    return np.array(keep, dtype=np.float64).reshape(-1, 2)


def detect_anchors(gray: np.ndarray, expected: list[Rect], dpi: float, tol_mm: float = 3.0) -> list[tuple[float, float]] | None:
    """找出与模板定位块几何关系一致的一组黑块，按 expected 的顺序返回中心（像素）；找不到返回 None。

    做法：任取两个候选块，假设它们是第 0、1 个定位块，求相似变换，预测其余定位块的位置，
    在 tol_mm 内都能找到候选块才算成立；取总误差最小的假设。与纸张在图中的位置、旋转角度无关。
    """
    s = px_per_mm(dpi)
    cand = _anchor_candidates(gray, (expected[0][2], expected[0][3]), dpi)
    if len(cand) < len(expected):
        return None
    exp = np.array([_center(r) for r in expected], dtype=np.float64) * s
    e0, e1 = exp[0], exp[1]
    ve = complex(*(e1 - e0))
    best, best_err = None, None
    for i in range(len(cand)):
        for j in range(len(cand)):
            if i == j:
                continue
            vc = complex(*(cand[j] - cand[i]))
            z = vc / ve  # 旋转 + 缩放
            # 容许 DPI 估计误差；只接受 ±45° 内的旋转——定位块近似中心对称，180° 由调用方旋转图像后再试
            if not 0.85 < abs(z) < 1.15 or abs(np.degrees(np.angle(z))) > 45:
                continue
            pred = [cand[i] + np.array([(z * complex(*(p - e0))).real, (z * complex(*(p - e0))).imag]) for p in exp]
            picked, err = [], 0.0
            for p in pred:
                d = np.linalg.norm(cand - p, axis=1)
                k = int(d.argmin())
                if d[k] > tol_mm * s:
                    break
                picked.append((float(cand[k][0]), float(cand[k][1])))
                err += float(d[k])
            else:
                if best_err is None or err < best_err:
                    best, best_err = picked, err
    return best


def detect_frames(gray: np.ndarray, expected: list[Rect], dpi: float) -> list[np.ndarray] | None:
    """找与期望外框面积、位置相近的四边形，返回每个外框的 4 个角点（像素，已排序）。"""
    s = px_per_mm(dpi)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 51, 15)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = min(r[2] * r[3] for r in expected) * s * s * 0.6
    quads = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        approx = cv2.approxPolyDP(c, 0.01 * cv2.arcLength(c, True), True)
        if len(approx) == 4:
            quads.append((area, _order_quad(approx.reshape(4, 2).astype(np.float32))))
    result = []
    for r in expected:
        exp_area = r[2] * r[3] * s * s
        ec = np.array(_center(r)) * s
        best, best_d = None, None
        for area, q in quads:
            if not (0.7 * exp_area < area < 1.3 * exp_area):
                continue
            d = float(np.linalg.norm(q.mean(0) - ec))
            if d > 60 * s:
                continue
            # 同一框有内外两条轮廓：距离相近时取面积更大的（外侧）
            if best_d is None or d < best_d - 2 * s or (abs(d - best_d) <= 2 * s and area > best[0]):
                best, best_d = (area, q), d
        if best is None:
            return None
        result.append(best[1])
    return result


def _rect_corners(r: Rect) -> np.ndarray:
    x, y, w, h = r
    return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float32)


def _finish(method: str, src_px: np.ndarray, dst_mm: np.ndarray) -> AlignResult:
    if len(src_px) == 3:
        # 只有 3 个定位块（如某个角没有印定位块）：用仿射变换（平移、旋转、缩放、剪切）
        A = cv2.getAffineTransform(src_px.astype(np.float32), dst_mm.astype(np.float32))
        H = np.vstack([A, [0.0, 0.0, 1.0]])
    else:
        H, _ = cv2.findHomography(src_px.astype(np.float32), dst_mm.astype(np.float32), 0)
    if H is None:
        return AlignResult(False, method, reason="单应矩阵求解失败")
    proj = cv2.perspectiveTransform(src_px.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
    residual = float(np.linalg.norm(proj - dst_mm, axis=1).max())
    return AlignResult(True, method, H=H, skew_deg=_skew(src_px, dst_mm), residual_mm=residual)


def _skew(src_px: np.ndarray, dst_mm: np.ndarray) -> float:
    """倾斜角：取在模板上水平跨度最大、竖向差最小的一对点，比较图像上与模板上连线的角度差。

    定位块缺一个角时，前两个点可能在对角线上，不能直接用前两个点。"""
    best, pair = None, (0, 1)
    for i in range(len(dst_mm)):
        for j in range(len(dst_mm)):
            dx, dy = dst_mm[j][0] - dst_mm[i][0], dst_mm[j][1] - dst_mm[i][1]
            if dx <= 0:
                continue
            score = dx - 10 * abs(dy)
            if best is None or score > best:
                best, pair = score, (i, j)
    i, j = pair
    a_img = np.arctan2(src_px[j][1] - src_px[i][1], src_px[j][0] - src_px[i][0])
    a_tpl = np.arctan2(dst_mm[j][1] - dst_mm[i][1], dst_mm[j][0] - dst_mm[i][0])
    d = float(np.degrees(a_img - a_tpl))
    return (d + 180) % 360 - 180


def align_primary(gray: np.ndarray, spec: AlignSpec, dpi: float) -> AlignResult:
    if spec.method == "anchors":
        centers = detect_anchors(gray, spec.anchors, dpi)
        if centers is None:
            return AlignResult(False, "anchors", reason="定位块缺失")
        return _finish("anchors", np.array(centers), np.array([_center(r) for r in spec.anchors]))
    if spec.method == "frames":
        quads = detect_frames(gray, spec.frames, dpi)
        if quads is None:
            return AlignResult(False, "frames", reason="定位外框缺失")
        return _finish("frames", np.vstack(quads), np.vstack([_rect_corners(r) for r in spec.frames]))
    raise ValueError(spec.method)


def align_features(gray: np.ndarray, base: np.ndarray, base_dpi: float, dpi: float, work_dpi: float = 100) -> AlignResult:
    """兜底：ORB 特征点 + RANSAC，与空白底图匹配。"""
    a = cv2.resize(gray, None, fx=work_dpi / dpi, fy=work_dpi / dpi, interpolation=cv2.INTER_AREA)
    b = cv2.resize(base, None, fx=work_dpi / base_dpi, fy=work_dpi / base_dpi, interpolation=cv2.INTER_AREA)
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None:
        return AlignResult(False, "features", reason="特征点不足")
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 40:
        return AlignResult(False, "features", reason="特征匹配不足")
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    Hw, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 3.0)
    if Hw is None or int(mask.sum()) < 40:
        return AlignResult(False, "features", reason="特征匹配内点不足")
    # a 像素 = 原图像素 * work/dpi；b 像素 -> mm = /px_per_mm(work)
    down = np.diag([work_dpi / dpi, work_dpi / dpi, 1.0])
    to_mm = np.diag([1 / px_per_mm(work_dpi), 1 / px_per_mm(work_dpi), 1.0])
    H = to_mm @ Hw @ down
    skew = float(np.degrees(np.arctan2(Hw[1, 0], Hw[0, 0])))
    inl = mask.ravel().astype(bool)
    proj = cv2.perspectiveTransform(pa[inl].reshape(-1, 1, 2), Hw).reshape(-1, 2)
    residual = float(np.median(np.linalg.norm(proj - pb[inl], axis=1))) / px_per_mm(work_dpi)
    return AlignResult(True, "features", H=H, skew_deg=-skew, residual_mm=residual)


def effective_dpi(H: np.ndarray) -> float:
    """由 H（像素 -> mm）反推图像实际分辨率。"""
    return MM_PER_INCH / float(np.sqrt(abs(np.linalg.det(H[:2, :2]))))


def warp_to_template(gray: np.ndarray, H: np.ndarray, paper: tuple[float, float], dpi: float) -> np.ndarray:
    """把原图按 H 重采样成模板坐标系下、指定 dpi 的整页图。"""
    s = px_per_mm(dpi)
    size = (int(round(paper[0] * s)), int(round(paper[1] * s)))
    return cv2.warpPerspective(gray, scale_matrix(dpi) @ H, size, flags=cv2.INTER_LINEAR, borderValue=255)


def crop_region(gray: np.ndarray, H: np.ndarray, rect: Rect, dpi: float) -> np.ndarray:
    """从原图上直接裁切模板 rect（mm）区域，输出分辨率为 dpi。"""
    x, y, w, h = rect
    T = np.array([[1, 0, -x], [0, 1, -y], [0, 0, 1]], dtype=np.float64)
    s = px_per_mm(dpi)
    size = (max(1, int(round(w * s))), max(1, int(round(h * s))))
    return cv2.warpPerspective(gray, scale_matrix(dpi) @ T @ H, size, flags=cv2.INTER_LINEAR, borderValue=255)
