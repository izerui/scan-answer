"""试识别测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"


def test_trial_ok_and_failures(small_dataset, tmp_path):
    """正常卡、空白页卡、损坏文件三种情况。"""
    from scan_answer.app.trial import trial_recognize
    from scan_answer.template import load_template

    t, bases = load_template(TEMPLATE_DIR)

    # 排序图片以确保配对正确
    images = sorted(small_dataset.iterdir())

    # 正常卡：第 1 张（0001_1.jpg + 0001_2.jpg）
    normal_pair = [str(p) for p in images if p.stem.startswith("0001")]
    assert len(normal_pair) == 2

    # 空白页卡：第 3 张（0003_1.jpg + 0003_2.jpg）
    blank_pair = [str(p) for p in images if p.stem.startswith("0003")]
    assert len(blank_pair) == 2

    # 损坏文件
    bad1 = tmp_path / "bad_1.jpg"
    bad2 = tmp_path / "bad_2.jpg"
    bad1.write_bytes(b"not an image")
    bad2.write_bytes(b"not an image either")

    results = trial_recognize(t, bases, [normal_pair, blank_pair, [str(bad1), str(bad2)]])
    assert len(results) == 3

    # 正常卡
    r0 = results[0]
    assert r0.ok
    assert r0.decision is not None
    n_obj = sum(len(blk.grid.cells) for blk in t.objective)
    assert len(r0.decision.objective) == n_obj  # 55 道客观题
    assert "front" in r0.warped

    # 空白页卡
    r1 = results[1]
    assert not r1.ok
    assert r1.message.startswith("BLANK_PAGE") or "BLANK_PAGE" in r1.message

    # 损坏文件
    r2 = results[2]
    assert not r2.ok
    assert "无法读取" in r2.message
