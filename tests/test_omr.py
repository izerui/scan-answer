import numpy as np
import pytest

from scan_answer.engine import omr
from scan_answer.engine.omr import Thresholds, cell_fill, decide, fit_thresholds

TH = Thresholds(mark=0.45, uncertain=0.2)
L = list("ABCDE")


def f(*v):
    return np.array(v, dtype=np.float32)


def test_decide_single():
    assert decide(f(0.02, 0.9, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "ok")
    assert decide(f(0.02, 0.01, 0.03, 0.01, 0.0), L, "single", TH) == ("", "blank")
    assert decide(f(0.8, 0.02, 0.85, 0.01, 0.0), L, "single", TH) == ("AC", "multiple")
    assert decide(f(0.02, 0.3, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "uncertain")  # 浅涂
    assert decide(f(0.12, 0.85, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "ok")  # 擦除残留
    assert decide(f(0.35, 0.85, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "uncertain")  # 另一个也不浅


def test_decide_multiple_choice():
    assert decide(f(0.8, 0.02, 0.85, 0.01, 0.0), L, "multiple", TH) == ("AC", "ok")
    assert decide(f(0.8, 0.3, 0.85, 0.01, 0.0), L, "multiple", TH) == ("AC", "uncertain")


def test_fit_thresholds_bimodal_and_default():
    rng = np.random.default_rng(0)
    v = np.concatenate([rng.uniform(0, 0.08, 1000), rng.uniform(0.75, 0.95, 200)])
    th = fit_thresholds(v)
    assert 0.3 <= th.mark <= 0.6 and abs(th.uncertain - th.mark * 0.45) < 1e-6
    assert fit_thresholds(np.zeros(10)) == Thresholds()


def test_cell_fill_subtracts_base():
    base = np.full((100, 100), 250, np.uint8)
    base[40:60, 40:60] = 200  # 印刷的浅色字
    work = base.copy()
    assert cell_fill(work, base, (0, 0, 25.4, 25.4), dpi=100) == 0.0  # 与底图相同 -> 0
    work[:, :] = 50
    assert cell_fill(work, base, (0, 0, 25.4, 25.4), dpi=100) > 0.9


def test_cell_fill_negative_shift_is_invalid():
    base = np.full((100, 100), 250, np.uint8)
    work = base.copy()
    # 底部行设为黑色，若负索引 wrap-around 会读到这里并返回非零值
    work[90:, :] = 0
    result = cell_fill(work, base, (0, 0, 25.4, 25.4), dpi=100, shift=(-20, -20))
    assert np.isnan(result)


@pytest.mark.parametrize("rect", [
    (-5, 20, 20, 20), (20, -5, 20, 20),
    (90, 20, 20, 20), (20, 90, 20, 20),
])
def test_sample_cell_rejects_jointly_clipped_bounds(rect):
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(base, base, rect, 25.4)
    assert np.isnan(value)
    assert reason == "base_out_of_bounds"
    assert np.isnan(cell_fill(base, base, rect, 25.4))


@pytest.mark.parametrize("shift", [(-30, 0), (0, -30), (80, 0), (0, 80)])
def test_sample_cell_rejects_shifted_work_bounds(shift):
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(base, base, (20, 20, 20, 20), 25.4, shift)
    assert np.isnan(value)
    assert reason == "work_out_of_bounds"


def test_sample_cell_checks_base_even_when_shift_makes_work_valid():
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(base, base, (-5, 20, 20, 20), 25.4, (20, 0))
    assert np.isnan(value)
    assert reason == "base_out_of_bounds"


@pytest.mark.parametrize("small", ["base", "work"])
def test_sample_cell_checks_each_image_extent(small):
    images = {name: np.full((100, 100), 250, np.uint8) for name in ("base", "work")}
    images[small] = images[small][:30, :30]
    value, reason = omr.sample_cell(images["work"], images["base"], (20, 20, 20, 20), 25.4)
    assert np.isnan(value)
    assert reason == f"{small}_out_of_bounds"


@pytest.mark.parametrize("rect", [(20, 20, 0, 20), (20, 20, 20, 0), (20, 20, 0.1, 0.1)])
def test_sample_cell_rejects_empty_sample(rect):
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(base, base, rect, 25.4)
    assert np.isnan(value)
    assert reason == "empty_sample"


@pytest.mark.parametrize("rect,dpi", [
    ((20, 20, -20, 20), 25.4),
    ((np.nan, 20, 20, 20), 25.4),
    ((20, 20, np.inf, 20), 25.4),
    ((20, 20, 20, 20), 0),
    ((20, 20, 20, 20), np.nan),
])
def test_sample_cell_rejects_invalid_geometry(rect, dpi):
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(base, base, rect, dpi)
    assert np.isnan(value)
    assert reason == "invalid_geometry"


@pytest.mark.parametrize("mask_value", [True, 1, 255])
def test_sample_cell_samples_only_inset_and_accepts_exact_image_edge(mask_value):
    base = np.full((37, 37), 250, np.uint8)
    work = base.copy()
    work[23:37, 23:37] = 150
    coverage = np.zeros_like(base)
    coverage[23:37, 23:37] = mask_value
    value, reason = omr.sample_cell(work, base, (20, 20, 20, 20), 25.4, coverage=coverage)
    assert value == pytest.approx(0.5)
    assert reason is None


@pytest.mark.parametrize("hole", [(23, 23), (36, 36), (30, 30)])
def test_sample_cell_rejects_even_one_uncovered_pixel(hole):
    base = np.full((100, 100), 250, np.uint8)
    coverage = np.ones_like(base, dtype=bool)
    coverage[hole] = False
    value, reason = omr.sample_cell(base, base, (20, 20, 20, 20), 25.4, coverage=coverage)
    assert np.isnan(value)
    assert reason == "source_uncovered"
    assert np.isnan(cell_fill(base, base, (20, 20, 20, 20), 25.4, coverage=coverage))


def test_sample_cell_checks_coverage_in_shifted_work_coordinates():
    base = np.full((100, 100), 250, np.uint8)
    coverage = np.zeros_like(base, dtype=bool)
    coverage[43:57, 53:67] = True
    value, reason = omr.sample_cell(base, base, (20, 20, 20, 20), 25.4, (30, 20), coverage)
    assert value == 0
    assert reason is None
    coverage[56, 66] = False
    value, reason = omr.sample_cell(base, base, (20, 20, 20, 20), 25.4, (30, 20), coverage)
    assert np.isnan(value)
    assert reason == "source_uncovered"


def test_sample_cell_rejects_truncated_coverage():
    base = np.full((100, 100), 250, np.uint8)
    value, reason = omr.sample_cell(
        base, base, (20, 20, 20, 20), 25.4, coverage=np.ones((30, 30), dtype=bool),
    )
    assert np.isnan(value)
    assert reason == "coverage_out_of_bounds"


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_sample_cell_rejects_nonfinite_coverage(bad):
    base = np.full((100, 100), 250, np.uint8)
    coverage = np.ones_like(base, dtype=float)
    coverage[30, 30] = bad
    value, reason = omr.sample_cell(base, base, (20, 20, 20, 20), 25.4, coverage=coverage)
    assert np.isnan(value)
    assert reason == "source_uncovered"


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
@pytest.mark.parametrize("image", ["base", "work"])
def test_sample_cell_does_not_clip_nonfinite_pixels_into_valid_fill(bad, image):
    images = {name: np.full((100, 100), 250, np.float32) for name in ("base", "work")}
    images[image][30, 30] = bad
    value, reason = omr.sample_cell(images["work"], images["base"], (20, 20, 20, 20), 25.4)
    assert np.isnan(value)
    assert reason == "nonfinite_sample"


@pytest.mark.parametrize("image", ["base", "work"])
def test_sample_cell_rejects_incompatible_image_shapes(image):
    images = {name: np.full((100, 100), 250, np.uint8) for name in ("base", "work")}
    images[image] = np.full((100, 100, 3), 250, np.uint8)
    value, reason = omr.sample_cell(images["work"], images["base"], (20, 20, 20, 20), 25.4)
    assert np.isnan(value)
    assert reason == "sample_shape_mismatch"


def test_sample_grid_records_actual_shift_and_per_cell_validity():
    base = np.full((120, 120), 250, np.uint8)
    yy, xx = np.indices((20, 60))
    base[20:40, 20:80] = ((xx * 17 + yy * 31 + xx * yy * 7) % 256).astype(np.uint8)
    work = np.roll(base, (-2, 2), axis=(0, 1))
    cells = [[(10, 10, 10, 10), (30, 10, 10, 10)]]
    coverage = np.ones_like(base, dtype=bool)
    coverage[21, 65] = False

    fills, metadata = omr.sample_grid(work, base, cells, 50.8, coverage)

    assert fills.dtype == np.float32
    assert fills.shape == (1, 2)
    assert fills[0, 0] == 0
    assert np.isnan(fills[0, 1])
    assert metadata == {"shift_mm": [1.0, -1.0], "reasons": [[None, "source_uncovered"]]}
    wrapped = omr.grid_fills(work, base, cells, 50.8, coverage=coverage)
    assert isinstance(wrapped, np.ndarray)
    np.testing.assert_array_equal(wrapped, fills)
    np.testing.assert_array_equal(omr.grid_fills(work, base, cells, 50.8), [[0, 0]])


def test_sample_grid_keeps_valid_blank_and_invalid_cell_distinct():
    base = np.full((100, 100), 250, np.uint8)
    cells = [[(20, 20, 20, 20)], [(90, 20, 20, 20)]]
    fills, metadata = omr.sample_grid(base, base, cells, 25.4)
    assert fills[0, 0] == 0
    assert np.isnan(fills[1, 0])
    assert metadata == {"shift_mm": [0.0, 0.0], "reasons": [[None], ["base_out_of_bounds"]]}


def test_sample_grid_does_not_align_using_wrapped_negative_base_coordinates():
    base = np.full((100, 100), 250, np.uint8)
    yy, xx = np.indices((20, 30))
    base[20:40, 10:40] = ((xx * 17 + yy * 31 + xx * yy * 7) % 256).astype(np.uint8)
    cells = [[(-90, 20, 20, 20)], [(20, 20, 20, 20)]]

    fills, metadata = omr.sample_grid(base, base, cells, 25.4)

    assert fills[1, 0] == 0
    assert np.isnan(fills[0, 0])
    assert metadata == {"shift_mm": [0.0, 0.0], "reasons": [["base_out_of_bounds"], [None]]}


@pytest.mark.parametrize("cells,reasons,shape", [([], [], (0,)), ([[], []], [[], []], (2, 0))])
def test_sample_grid_empty_input(cells, reasons, shape):
    base = np.full((100, 100), 250, np.uint8)
    fills, metadata = omr.sample_grid(base, base, cells, 25.4)
    assert fills.shape == shape
    assert metadata == {"shift_mm": [0.0, 0.0], "reasons": reasons}


@pytest.mark.parametrize("qtype", ["single", "judge", "multiple"])
@pytest.mark.parametrize("values", [[], [np.nan], [0.9, np.nan], [np.inf, 0], [0, -np.inf],
                                   [-0.1, 0.9], [1.1, 0]])
def test_decisions_fail_entire_invalid_row(values, qtype):
    fills = np.array(values, dtype=np.float32)
    assert omr.decide_idx(fills, qtype, TH) == ([], "failed")
    assert decide(fills, L, qtype, TH) == ("", "failed")


@pytest.mark.parametrize("fills", [np.array(0.8), np.array([[0.8, 0.0]])])
def test_decide_idx_rejects_non_row_input(fills):
    assert omr.decide_idx(fills, "single", TH) == ([], "failed")


def test_fit_thresholds_excludes_invalid_values():
    clean = np.array([0] * 90 + [0.8] * 10, dtype=np.float32)
    noisy = np.concatenate([clean, [np.nan, np.inf, -np.inf, -0.1, 1.1]])
    assert fit_thresholds(noisy) == fit_thresholds(clean)
    assert fit_thresholds(noisy).mark == pytest.approx(0.3)


@pytest.mark.parametrize("values", [
    [np.nan, np.inf, -np.inf] * 20,
    [0] * 44 + [0.8] * 5 + [np.nan, np.inf, -np.inf],
    [0] * 50 + [0.8] * 4 + [np.inf] * 10,
    [0] * 90 + [0.4] * 10,
])
def test_fit_thresholds_retains_finite_count_and_dark_mark_gates(values):
    assert fit_thresholds(np.array(values)) == Thresholds()


def test_fit_thresholds_includes_exact_half_marks():
    assert fit_thresholds(np.array([0] * 45 + [0.5] * 5)).mark == pytest.approx(0.3)


def test_threshold_issues_detects_shallow_batch_without_dark_marks():
    values = np.array([0] * 90 + [0.4] * 10, dtype=np.float32)
    assert "LIGHT_MARKS" in {issue["code"] for issue in omr.threshold_issues(values)}
    assert fit_thresholds(values) == Thresholds()


@pytest.mark.parametrize("values", [
    [0] * 100,
    [0.01, 0.05, 0.1, 0.15] * 25,
    [0] * 99 + [0.4],
    [0] * 30 + [0.4] * 5,
    [np.nan, np.inf, -np.inf] * 30,
    [0] * 44 + [0.4] * 5 + [np.nan] * 50,
    [0] * 90 + [0.8] * 10,
])
def test_threshold_issues_does_not_mislabel_blanks_noise_or_insufficient_data(values):
    assert omr.threshold_issues(np.array(values)) == []


def test_threshold_issues_filters_invalid_values_and_reports_low_separation():
    clean = np.array([0.15] * 90 + [0.4] * 10, dtype=np.float32)
    noisy = np.concatenate([clean, [np.nan, np.inf, -np.inf, -0.1, 1.1]])
    issues = omr.threshold_issues(noisy)
    assert issues == omr.threshold_issues(clean)
    assert {issue["code"] for issue in issues} == {"LIGHT_MARKS", "LOW_SEPARATION"}


def _textured(h: int, w: int) -> np.ndarray:
    yy, xx = np.indices((h, w))
    return (((xx * 17 + yy * 31 + xx * yy * 7) % 200) + 30).astype(np.uint8)


def test_local_shift_stays_within_fraction_of_pitch():
    """一行行重复的网格（如考号）错开半行左右也能“匹配上”：搜索范围限制在格距的 40% 内。"""
    base = np.full((200, 200), 250, np.uint8)
    base[40:160, 40:160] = _textured(120, 120)
    work = np.roll(base, 12, axis=0)  # 50.8 DPI 下 12 px = 6 mm
    bbox = (20.0, 20.0, 60.0, 60.0)
    assert omr.local_shift(work, base, bbox, 50.8, max_shift_mm=8.0) == (0, 12)
    # 格距 10 mm：最多搜 4 mm（8 px），6 mm 的真实偏移落在边界外 → 不微调
    assert omr.local_shift(work, base, bbox, 50.8, max_shift_mm=8.0, pitch_mm=10.0) == (0, 0)


def test_local_shift_rejects_best_match_on_search_border():
    """最佳位置落在搜索边界上说明没有真正对上，不微调。"""
    base = np.full((200, 200), 250, np.uint8)
    base[40:160, 40:160] = _textured(120, 120)
    work = np.roll(base, 3, axis=1)  # 1.5 mm
    bbox = (20.0, 20.0, 60.0, 60.0)
    assert omr.local_shift(work, base, bbox, 50.8, max_shift_mm=2.0) == (3, 0)
    assert omr.local_shift(work, base, bbox, 50.8, max_shift_mm=1.5) == (0, 0)


def test_min_pitch():
    cells = [[(0, 0, 3, 2), (5, 0, 3, 2)], [(0, 3.3, 3, 2), (5, 3.3, 3, 2)]]
    assert omr.min_pitch(cells) == pytest.approx(3.3)
    assert omr.min_pitch([[(0, 0, 3, 2)]]) is None
