"""空白页判定：墨量少但能按定位块对上模板的卡面（如 408 背面不作答）不是空白页；真正的白纸仍判空白页。"""
from pathlib import Path

import numpy as np
import pytest

from scan_answer.engine.recognize import PhysicalError, TemplateContext, analyze_sheet, is_blank
from scan_answer.template.io import load_template

CS408 = Path(__file__).resolve().parents[1] / "templates" / "kaoyan_cs408"


@pytest.fixture(scope="module")
def cs408():
    tpl, bases = load_template(CS408)
    return tpl, bases, TemplateContext(tpl, bases)


def test_faint_back_face_is_not_blank_page(cs408):
    tpl, bases, ctx = cs408
    assert is_blank(bases["back"])  # 背面印刷很淡，墨量低于空白阈值
    analyze_sheet(ctx, [bases["front"], bases["back"]], [tpl.dpi] * 2)


def test_white_paper_is_still_blank_page(cs408):
    tpl, bases, ctx = cs408
    white = np.full_like(bases["back"], 255)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, [bases["front"], white], [tpl.dpi] * 2)
    assert e.value.code == "BLANK_PAGE"
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, [white, white], [tpl.dpi] * 2)
    assert e.value.code == "BLANK_PAGE"
