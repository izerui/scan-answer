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


def _max_cell_error(snapped, reference):
    """逐格计算中心偏差，返回最大值。"""
    return max(
        ((sc[0] + sc[2] / 2 - oc[0] - oc[2] / 2) ** 2 +
         (sc[1] + sc[3] / 2 - oc[1] - oc[3] / 2) ** 2) ** 0.5
        for srow, orow in zip(snapped, reference) for sc, oc in zip(srow, orow)
    )


# ── auto_anchors ──────────────────────────────────────────────


def test_auto_anchors_199(tpl_bases, front_base, back_base):
    from scan_answer.template.auto import auto_anchors

    t, _ = tpl_bases
    result = auto_anchors(front_base, t.dpi, t.paper)
    assert result is not None and len(result) == 4
    for det, ref in zip(result, t.faces["front"].align.anchors):
        dcx, dcy = det[0] + det[2] / 2, det[1] + det[3] / 2
        rcx, rcy = ref[0] + ref[2] / 2, ref[1] + ref[3] / 2
        assert abs(dcx - rcx) < 0.5 and abs(dcy - rcy) < 0.5

    assert auto_anchors(back_base, t.dpi, t.paper) is None


# ── auto_frames ───────────────────────────────────────────────


def test_auto_frames_199_back(tpl_bases, back_base):
    from scan_answer.template.auto import auto_frames

    t, _ = tpl_bases
    result = auto_frames(back_base, t.dpi, t.paper)
    assert len(result) == 2
    result_sorted = sorted(result, key=lambda r: r[0])
    ref_sorted = sorted(t.faces["back"].align.frames, key=lambda r: r[0])
    for det, ref in zip(result_sorted, ref_sorted):
        for i, label in enumerate(("x", "y", "w", "h")):
            assert abs(det[i] - ref[i]) < 1.5, f"外框 {label} 偏差过大"


# ── auto_sync ─────────────────────────────────────────────────


def test_auto_sync_199(tpl_bases, front_base, back_base):
    from scan_answer.template.auto import auto_sync

    groups = auto_sync(front_base, 300)
    assert len(groups) == 1
    g = groups[0]
    assert g.direction == "vertical"
    assert len(g.marks) == 12
    first_cy = g.marks[0][1] + g.marks[0][3] / 2
    assert abs(first_cy - 51.3) < 0.5
    last_cy = g.marks[-1][1] + g.marks[-1][3] / 2
    assert abs(last_cy - 132.0) < 0.5
    assert auto_sync(back_base, 300) == []


# ── snap_grid ─────────────────────────────────────────────────


def test_snap_recovers_perturbed_block(tpl_bases, front_base):
    """扰动后的客观题块应吸附回原位。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = t.objective[0].grid.cells
    c0 = cells[0][0]
    ox, oy = c0[0], c0[1]
    perturbed = [
        [((c[0] - ox) * 1.02 + ox + 1.0, (c[1] - oy) * 1.02 + oy - 0.8, c[2], c[3]) for c in row]
        for row in cells
    ]
    result = snap_grid(front_base, perturbed, t.dpi)
    assert result.ok, f"吸附失败: {result.message}"
    max_err = _max_cell_error(result.cells, cells)
    assert max_err < 0.35, f"吸附后最大误差 {max_err:.3f} mm"
    # 1.02× 缩放将远端格子推至搜索半径之外（o=4 距离 ~1.5mm），
    # 约 20% 不可达，内点率 ≈75%
    assert result.inliers / result.total >= 0.7


def test_snap_rejects_blank_area(tpl_bases, front_base):
    """空白区域不应吸附成功。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = [
        [(300.0 + j * 6.0, 250.0 + i * 4.0, 4.0, 2.5) for j in range(3)]
        for i in range(3)
    ]
    result = snap_grid(front_base, cells, t.dpi)
    assert not result.ok
    for r_row, c_row in zip(result.cells, cells):
        for r, c in zip(r_row, c_row):
            assert r == c


def test_snap_exam_grid_not_misled(tpl_bases, front_base):
    """199 考号网格间距过小，snap_grid 应安全拒绝而非误吸附。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    result = snap_grid(front_base, t.exam_no.grid.cells, t.dpi)
    if result.ok:
        for g, (snap_row, orig_row) in enumerate(zip(result.cells, t.exam_no.grid.cells)):
            for o, (sc, oc) in enumerate(zip(snap_row, orig_row)):
                dx = (sc[0] + sc[2] / 2) - (oc[0] + oc[2] / 2)
                dy = (sc[1] + sc[3] / 2) - (oc[1] + oc[3] / 2)
                dist = (dx ** 2 + dy ** 2) ** 0.5
                assert dist < 0.3, f"考号 g={g} o={o} 偏移 {dist:.3f} mm"


@pytest.mark.parametrize("dx", [-1.75, -1.5, -1.25, -1.0, -0.75, -0.5, -0.25, 0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75])
@pytest.mark.parametrize("dy", [-0.4, 0, 0.4])
@pytest.mark.parametrize("radius", [1.0, 1.5, 2.0])
def test_snap_exam_grid_perturbed_never_wrong(tpl_bases, front_base, dx, dy, radius):
    """考号网格平移后吸附：要么 ok=False，要么每格中心 < 0.3mm。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    cells = t.exam_no.grid.cells
    shifted = [[(c[0] + dx, c[1] + dy, c[2], c[3]) for c in row] for row in cells]
    result = snap_grid(front_base, shifted, t.dpi, radius_mm=radius)
    if result.ok:
        max_err = _max_cell_error(result.cells, cells)
        assert max_err < 0.3, f"dx={dx} dy={dy} r={radius}: ok=True 但偏差 {max_err:.2f} mm"


@pytest.mark.parametrize("blk_idx", [0, 1, 2, 3])
@pytest.mark.parametrize("dx", [-1.0, -0.5, 0, 0.5, 1.0])
@pytest.mark.parametrize("dy", [-1.0, -0.5, 0, 0.5, 1.0])
def test_snap_objective_perturbed_never_wrong(tpl_bases, front_base, blk_idx, dx, dy):
    """客观题块平移后吸附：要么 ok=False，要么每格中心 < 0.35mm。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    blk = t.objective[blk_idx]
    shifted = [[(c[0] + dx, c[1] + dy, c[2], c[3]) for c in row] for row in blk.grid.cells]
    result = snap_grid(front_base, shifted, t.dpi)
    if result.ok:
        max_err = _max_cell_error(result.cells, blk.grid.cells)
        assert max_err < 0.35, f"{blk.name} dx={dx} dy={dy}: ok=True 但偏差 {max_err:.2f} mm"


def test_snap_objective_probes_mostly_ok(tpl_bases, front_base):
    """客观题块小幅平移后吸附应有 >= 80% 返回 ok=True。"""
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    ok_count = 0
    total_count = 0
    for blk in t.objective:
        for dx in [-1.0, -0.5, 0, 0.5, 1.0]:
            for dy in [-1.0, -0.5, 0, 0.5, 1.0]:
                shifted = [[(c[0] + dx, c[1] + dy, c[2], c[3]) for c in row] for row in blk.grid.cells]
                result = snap_grid(front_base, shifted, t.dpi)
                total_count += 1
                if result.ok:
                    ok_count += 1
    ratio = ok_count / total_count
    assert ratio >= 0.8, f"客观题块 ok 比率 {ratio:.0%}（{ok_count}/{total_count}），期望 >= 80%"


def test_snap_objective_blocks_stable(tpl_bases, front_base):
    """199 每个客观题块在正确位置直接吸附：ok 且最大偏移 < 0.35 mm。

    31-45 块因印刷特征差异偏移量 0.30-0.31mm，0.35mm 容差覆盖此情况。
    """
    from scan_answer.template.auto import snap_grid

    t, _ = tpl_bases
    for blk in t.objective:
        result = snap_grid(front_base, blk.grid.cells, t.dpi)
        assert result.ok, f"客观题块 {blk.name} 吸附失败: {result.message}"
        max_shift = _max_cell_error(result.cells, [blk.grid.cells[g] for g in range(len(blk.grid.cells))])
        assert max_shift < 0.35, f"客观题块 {blk.name} 最大偏移 {max_shift:.3f} mm"


# ── grid_in_rect ──────────────────────────────────────────────


def test_grid_in_rect_shape_and_bounds():
    from scan_answer.template.auto import grid_in_rect

    cells = grid_in_rect((10, 20, 30, 60), 15, 5, True, block_every=5)
    assert len(cells) == 15
    assert all(len(row) == 5 for row in cells)
    for row in cells:
        for x, y, w, h in row:
            assert x >= 10 - 0.01 and y >= 20 - 0.01
            assert x + w <= 40 + 0.01 and y + h <= 80 + 0.01
    cy1 = cells[0][0][1] + cells[0][0][3] / 2
    cy2 = cells[1][0][1] + cells[1][0][3] / 2
    cy5 = cells[4][0][1] + cells[4][0][3] / 2
    cy6 = cells[5][0][1] + cells[5][0][3] / 2
    normal_gap = cy2 - cy1
    cross_gap = cy6 - cy5
    ratio = cross_gap / normal_gap
    assert abs(ratio - 1.5) < 0.015


# ── load_pages ────────────────────────────────────────────────


def test_load_pages_pdf_and_image(tmp_path):
    from PIL import Image

    from scan_answer.template.auto import load_pages

    pages = load_pages(PDF_PATH)
    assert len(pages) == 2
    for gray, dpi in pages:
        assert dpi == 300
        assert abs(gray.shape[0] - 3189) <= 2 and abs(gray.shape[1] - 4606) <= 2

    img = Image.fromarray(np.zeros((100, 200), dtype=np.uint8))
    p_dpi = tmp_path / "with_dpi.png"
    img.save(str(p_dpi), dpi=(200, 200))
    result = load_pages(p_dpi)
    assert len(result) == 1 and result[0][1] == 200

    p_no_dpi = tmp_path / "no_dpi.png"
    img.save(str(p_no_dpi))
    result = load_pages(p_no_dpi)
    assert len(result) == 1 and result[0][1] == 300

    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"garbage data not a real image")
    with pytest.raises(ValueError, match="bad.jpg"):
        load_pages(bad)


# ── import_base ───────────────────────────────────────────────


def test_import_base_deskews_back():
    from scan_answer.template.auto import import_base, load_pages

    pages = load_pages(PDF_PATH)
    gray, dpi = pages[1]
    result = import_base(gray, dpi)
    assert result.method == "frames"
    assert abs(result.skew_deg - 1.45) < 0.2
    result2 = import_base(result.image, 300)
    assert abs(result2.skew_deg) < 0.1
    assert abs(result.paper[0] - 390.0) < 0.5
    assert abs(result.paper[1] - 269.9) < 0.5


# ── 重建后的 199 模板检查 ─────────────────────────────────────


def test_rebuilt_199_template(tpl_bases):
    from scan_answer.template import check_alignment

    t, bases = tpl_bases
    assert t.version == 3  # 加入标识区（混卡检测）后升版
    assert len(t.sync) == 1 and len(t.sync[0].marks) == 12
    last_col = t.exam_no.grid.cells[14]
    zero_cell = last_col[0]
    cx = zero_cell[0] + zero_cell[2] / 2
    assert abs(cx - 187.0) < 0.3, f"考号第15位0格中心 x={cx:.2f}"
    for blk in t.objective:
        assert blk.group == 5
    issues = check_alignment(t, bases)
    assert issues == [], f"对齐检查失败: {issues}"
