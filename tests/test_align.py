from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

from scan_answer.engine.align import align_features, align_primary, effective_dpi
from scan_answer.template.model import AlignSpec

PDF = Path(__file__).resolve().parents[1] / "samples" / "raw" / "8k-199.pdf"
FRONT = AlignSpec(method="anchors", anchors=[(17.45, 11.4, 4.7, 3.5), (372.6, 11.4, 4.7, 3.5),
                                             (372.6, 255.65, 4.7, 3.5), (17.45, 255.65, 4.7, 3.5)])
BACK = AlignSpec(method="frames", frames=[(19.5, 25.7, 166.0, 225.9), (199.0, 25.7, 166.4, 225.9)])


@pytest.fixture(scope="module")
def raw():
    doc = pymupdf.open(PDF)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
        pages.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
    return pages


def _rotate(img, deg, shift_px=(0, 0), pad=200):
    """旋转 + 平移，四周加 pad 像素白边（模拟扫描仪背板），返回新图和 3x3 变换矩阵。"""
    h, w = img.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    M[:, 2] += (shift_px[0] + pad, shift_px[1] + pad)
    return cv2.warpAffine(img, M, (w + 2 * pad, h + 2 * pad), borderValue=250), np.vstack([M, [0, 0, 1]])


def test_raw_front_anchors_and_back_frames(raw):
    f = align_primary(raw[0], FRONT, 300)
    assert f.ok and f.method == "anchors" and abs(f.skew_deg - (-0.36)) < 0.2
    b = align_primary(raw[1], BACK, 300)
    assert b.ok and b.method == "frames" and abs(b.skew_deg - 1.32) < 0.2 and b.residual_mm < 0.5
    assert abs(effective_dpi(f.H) - 300) < 3


@pytest.mark.parametrize("deg,shift", [(2.5, (60, -40)), (-3.0, (-80, 50))])
def test_consistent_after_rotation(raw, deg, shift):
    base = align_primary(raw[0], FRONT, 300)
    img, M = _rotate(raw[0], deg, shift)
    res = align_primary(img, FRONT, 300)
    assert res.ok
    # 同一个原图点：经旋转后再用新 H 映射，应与原 H 映射到同一 mm 位置
    pts = np.array([[[1000.0, 800.0]], [[3500.0, 2600.0]], [[2300.0, 1500.0]]])
    a = cv2.perspectiveTransform(pts, base.H)
    b = cv2.perspectiveTransform(cv2.perspectiveTransform(pts, M), res.H)
    assert np.abs(a - b).max() < 0.5


def test_features_fallback_when_anchor_covered(raw):
    img, _ = _rotate(raw[0], 1.5, (30, 20))
    img[-450:, -650:] = 250
    assert not align_primary(img, FRONT, 300).ok
    res = align_features(img, raw[0], 300, 300)
    assert res.ok and res.method == "features"


def test_blank_has_no_frames():
    blank = np.full((3189, 4606), 250, np.uint8)
    assert not align_primary(blank, BACK, 300).ok
