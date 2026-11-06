"""完整回归：200 张模拟卡，验收指标见设计文档 9.2。运行：pytest -m slow"""
import pytest

from scan_answer.batch import run_batch
from scan_answer.evaluate import evaluate_dirs
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR


@pytest.mark.slow
def test_regression_200(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 200, seed=2026)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="r")
    m = evaluate_dirs(tmp_path / "ds", bd)
    rep = m.report()
    assert rep["silent_errors"] == 0, m.details
    assert rep["physical_false"] == 0, m.details
    assert rep["physical_detect_rate"] in (None, 1.0), m.details
    assert rep["normal_accuracy"] >= 0.999, m.details
    assert rep["objective_accuracy"] >= 0.999, m.details
    assert rep["exam_digit_accuracy"] >= 0.999, m.details
    assert rep["barcode_accuracy"] >= 0.99, m.details
