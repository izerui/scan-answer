import numpy as np

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
