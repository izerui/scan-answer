"""auto.py 的测试：自动检测定位块/外框/同步头、吸附、底图导入、load_pages。"""
from pathlib import Path

import numpy as np
import pytest

from scan_answer.template import load_template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"
PDF_PATH = ROOT / "samples" / "raw" / "8k-199.pdf"


@pytest.fixture(scope="module")
def tpl_bases():
    return load_template(TEMPLATE_DIR)


@pytest.fixture(scope="module")
def front_base(tpl_bases):
    _, bases = tpl_bases
    return bases["front"]


@pytest.fixture(scope="module")
def back_base(tpl_bases):
    _, bases = tpl_bases
    return bases["back"]


# ── auto_anchors ──────────────────────────────────────────────


def test_auto_anchors_199(tpl_bases, front_base, back_base):
    from scan_answer.template.auto import auto_anchors

    t, _ = tpl_bases
    result = auto_anchors(front_base, t.dpi, t.paper)
    assert result is not None and len(result) == 4
    for det, ref in zip(result, t.faces["front"].align.anchors):
        dcx, dcy = det[0] + det[2] / 2, det[1] + det[3] / 2
        rcx, rcy = ref[0] + ref[2] / 2, ref[1] + ref[3] / 2
        assert abs(dcx - rcx) < 0.5 and abs(dcy - rcy) < 0.5, f"锚点偏差过大: det=({dcx:.2f},{dcy:.2f}) ref=({rcx:.2f},{rcy:.2f})"

    # 背面没有定位块，应返回 None
    assert auto_anchors(back_base, t.dpi, t.paper) is None


# ── auto_frames ───────────────────────────────────────────────


def test_auto_frames_199_back(tpl_bases, back_base):
    from scan_answer.template.auto import auto_frames

    t, _ = tpl_bases
    result = auto_frames(back_base, t.dpi, t.paper)
    assert len(result) == 2
    # 按 x 排序后逐个比较
    result_sorted = sorted(result, key=lambda r: r[0])
    ref_sorted = sorted(t.faces["back"].align.frames, key=lambda r: r[0])
    for det, ref in zip(result_sorted, ref_sorted):
        for i, label in enumerate(("x", "y", "w", "h")):
            assert abs(det[i] - ref[i]) < 1.5, f"外框 {label} 偏差过大: det={det[i]:.2f} ref={ref[i]:.2f}"


# ── auto_sync ─────────────────────────────────────────────────


def test_auto_sync_199(tpl_bases, front_base, back_base):
    from scan_answer.template.auto import auto_sync

    groups = auto_sync(front_base, 300)
    assert len(groups) == 1
    g = groups[0]
    assert g.direction == "vertical"
    assert len(g.marks) == 12
    # 第一个中心 y ≈ 51.3 ± 0.5
    first_cy = g.marks[0][1] + g.marks[0][3] / 2
    assert abs(first_cy - 51.3) < 0.5, f"第一个同步头 y={first_cy:.2f}"
    # 最后一个中心 y ≈ 132.0 ± 0.5
    last_cy = g.marks[-1][1] + g.marks[-1][3] / 2
    assert abs(last_cy - 132.0) < 0.5, f"最后一个同步头 y={last_cy:.2f}"

    # 背面应返回空列表
    assert auto_sync(back_base, 300) == []


# ── snap_grid ─────────────────────────────────────────────────


def test_snap_recovers_perturbed_block(tpl_bases, front_base):
    """扰动后的客观题块应吸附回原位。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = t.objective[0].grid.cells
    # 平移 (+1.0, -0.8) mm，缩放 1.02（以第一格为原点）
    c0 = cells[0][0]
    ox, oy = c0[0], c0[1]
    perturbed = [
        [((c[0] - ox) * 1.02 + ox + 1.0, (c[1] - oy) * 1.02 + oy - 0.8, c[2], c[3]) for c in row]
        for row in cells
    ]
    result = snap_grid(front_base, perturbed, t.dpi)
    assert result.ok, f"吸附失败: {result.message}"
    # 逐格比较中心误差
    max_err = 0.0
    for snapped_row, orig_row in zip(result.cells, cells):
        for sc, oc in zip(snapped_row, orig_row):
            err = ((sc[0] + sc[2] / 2 - oc[0] - oc[2] / 2) ** 2 +
                   (sc[1] + sc[3] / 2 - oc[1] - oc[3] / 2) ** 2) ** 0.5
            max_err = max(max_err, err)
    assert max_err < 0.35, f"吸附后最大误差 {max_err:.3f} mm"
    # 扰动后部分格可能因边界/双峰检测被跳过，但内点仍须满足算法阈值 60%
    assert result.inliers / result.total >= 0.6


def test_snap_rejects_blank_area(tpl_bases, front_base):
    """空白区域不应吸附成功。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = [
        [(300.0 + j * 6.0, 250.0 + i * 4.0, 4.0, 2.5) for j in range(3)]
        for i in range(3)
    ]
    result = snap_grid(front_base, cells, t.dpi)
    assert not result.ok, "空白区域不应吸附成功"
    # cells 应与输入相同
    for r_row, c_row in zip(result.cells, cells):
        for r, c in zip(r_row, c_row):
            assert r == c


def test_snap_exam_grid_not_misled(tpl_bases, front_base):
    """199 考号网格：紧密排列的方括号型格子不应被误吸附到分隔线上。

    snap_grid 应返回 ok=False（不可靠），或者每个格中心与原位置偏差 < 0.3 mm。
    """
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = t.exam_no.grid.cells
    result = snap_grid(front_base, cells, t.dpi)
    if result.ok:
        # 如果声称成功，每个格中心应当在原位 ±0.3 mm 内
        for g, (snap_row, orig_row) in enumerate(zip(result.cells, cells)):
            for o, (sc, oc) in enumerate(zip(snap_row, orig_row)):
                dx = (sc[0] + sc[2] / 2) - (oc[0] + oc[2] / 2)
                dy = (sc[1] + sc[3] / 2) - (oc[1] + oc[3] / 2)
                dist = (dx ** 2 + dy ** 2) ** 0.5
                assert dist < 0.3, f"考号 g={g} o={o} 偏移 {dist:.3f} mm > 0.3（ok=True 但位置错误）"
    # ok=False 也可接受（安全拒绝）


def test_snap_objective_blocks_stable(tpl_bases, front_base):
    """199 每个客观题块在正确位置直接吸附：ok 且最大偏移 < 0.3 mm。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    for blk in t.objective:
        result = snap_grid(front_base, blk.grid.cells, t.dpi)
        assert result.ok, f"客观题块 {blk.name} 吸附失败: {result.message}"
        max_shift = 0.0
        for snap_row, orig_row in zip(result.cells, blk.grid.cells):
            for sc, oc in zip(snap_row, orig_row):
                dx = (sc[0] + sc[2] / 2) - (oc[0] + oc[2] / 2)
                dy = (sc[1] + sc[3] / 2) - (oc[1] + oc[3] / 2)
                max_shift = max(max_shift, (dx ** 2 + dy ** 2) ** 0.5)
        assert max_shift < 0.35, f"客观题块 {blk.name} 最大偏移 {max_shift:.3f} mm"


# ── grid_in_rect ──────────────────────────────────────────────


def test_grid_in_rect_shape_and_bounds():
    from scan_answer.template.auto import grid_in_rect

    cells = grid_in_rect((10, 20, 30, 60), 15, 5, True, block_every=5)
    assert len(cells) == 15
    assert all(len(row) == 5 for row in cells)
    # 所有格都在矩形内
    for row in cells:
        for x, y, w, h in row:
            assert x >= 10 - 0.01 and y >= 20 - 0.01, f"格 ({x},{y}) 超出矩形左上"
            assert x + w <= 40 + 0.01 and y + h <= 80 + 0.01, f"格 ({x+w},{y+h}) 超出矩形右下"
    # 第 5 题与第 6 题的间距比第 1 题与第 2 题大 50%（±1%）
    cy1 = cells[0][0][1] + cells[0][0][3] / 2
    cy2 = cells[1][0][1] + cells[1][0][3] / 2
    cy5 = cells[4][0][1] + cells[4][0][3] / 2
    cy6 = cells[5][0][1] + cells[5][0][3] / 2
    normal_gap = cy2 - cy1
    cross_gap = cy6 - cy5
    ratio = cross_gap / normal_gap
    assert abs(ratio - 1.5) < 0.015, f"组间间距比 {ratio:.3f}，期望 1.5"


# ── load_pages ────────────────────────────────────────────────


def test_load_pages_pdf_and_image(tmp_path):
    from PIL import Image

    from scan_answer.template.auto import load_pages

    # PDF：2 页，dpi=300，形状约 (3188, 4606)
    pages = load_pages(PDF_PATH)
    assert len(pages) == 2
    for gray, dpi in pages:
        assert dpi == 300
        assert abs(gray.shape[0] - 3189) <= 2 and abs(gray.shape[1] - 4606) <= 2

    # 有 dpi 信息的 PNG
    img = Image.fromarray(np.zeros((100, 200), dtype=np.uint8))
    p_dpi = tmp_path / "with_dpi.png"
    img.save(str(p_dpi), dpi=(200, 200))
    result = load_pages(p_dpi)
    assert len(result) == 1 and result[0][1] == 200

    # 没有 dpi 信息的 PNG
    p_no_dpi = tmp_path / "no_dpi.png"
    img.save(str(p_no_dpi))
    result = load_pages(p_no_dpi)
    assert len(result) == 1 and result[0][1] == 300

    # 损坏的文件
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"garbage data not a real image")
    with pytest.raises(ValueError, match="bad.jpg"):
        load_pages(bad)


# ── import_base ───────────────────────────────────────────────


def test_import_base_deskews_back():
    from scan_answer.template.auto import import_base, load_pages

    pages = load_pages(PDF_PATH)
    gray, dpi = pages[1]  # 背面
    result = import_base(gray, dpi)
    assert result.method == "frames"
    assert abs(result.skew_deg - 1.45) < 0.2, f"纠偏角 {result.skew_deg:.2f}，期望 ≈1.45"
    # 纠偏后再算一次角度，应接近 0
    result2 = import_base(result.image, 300)
    assert abs(result2.skew_deg) < 0.1, f"残余角度 {result2.skew_deg:.3f}"
    # paper 约 (390.0, 269.9) ± 0.5
    assert abs(result.paper[0] - 390.0) < 0.5, f"paper_w={result.paper[0]:.1f}"
    assert abs(result.paper[1] - 269.9) < 0.5, f"paper_h={result.paper[1]:.1f}"


# ── 重建后的 199 模板检查 ─────────────────────────────────────


def test_rebuilt_199_template(tpl_bases):
    from scan_answer.template import check_alignment

    t, bases = tpl_bases
    assert t.version == 2
    assert len(t.sync) == 1 and len(t.sync[0].marks) == 12
    # 考号最后一列（第 15 位）"0" 格中心 x 在 187.0 ± 0.3（原始 grid_cells 位置，未吸附）
    last_col = t.exam_no.grid.cells[14]  # 第 15 位（0-indexed 14）
    zero_cell = last_col[0]  # "0" 选项
    cx = zero_cell[0] + zero_cell[2] / 2
    assert abs(cx - 187.0) < 0.3, f"考号第15位0格中心 x={cx:.2f}，期望 187.0±0.3"
    # 客观题块都有 group=5
    for blk in t.objective:
        assert blk.group == 5, f"客观题块 {blk.name} group={blk.group}"
    # check_alignment 通过
    issues = check_alignment(t, bases)
    assert issues == [], f"对齐检查失败: {issues}"
