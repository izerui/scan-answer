"""界面易懂性第三批：剩余的改名、说明与小反馈，以及术语统一的一致性检查。"""
import ast
import re
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QToolButton

from scan_answer.app.settings import AppSettings
from scan_answer.ui import help_text
from scan_answer.ui.help_text import CONCEPTS, GUIDE_MD, PAGE_GUIDES
from scan_answer.ui.pages.results_page import FILL_TIP, fill_text, fill_text_full
from scan_answer.ui.pages.scan_page import ALIGN_TEXT
from scan_answer.ui.pages.template_page import TemplatePage, trial_reason
from scan_answer.ui.widgets.delivery_panel import ISSUE_FALLBACK, DeliveryPanel, issue_label
from scan_answer.ui.widgets.guide import HelpButton
from conftest import ROOT, TEMPLATE_DIR
from test_results_flow_fixes import report

UI = ROOT / "src" / "scan_answer" / "ui"


# ══════════ 主窗口：状态栏“使用说明 F1” ══════════
def test_status_help_button_is_plain_and_shows_page_guide(qtbot, app_home, no_dialogs):
    from scan_answer.ui.controller import ScanController
    from scan_answer.ui.main_window import MainWindow
    controller = ScanController()
    window = MainWindow(AppSettings(), controller)
    qtbot.addWidget(window)
    try:
        btn = window.help_btn
        assert isinstance(btn, QToolButton) and btn.text() == "使用说明 F1" and not btn.autoRaise()
        assert "本页怎么用" in btn.toolTip() and PAGE_GUIDES["exam"][0] in btn.toolTip()
        window.show_page("settings")
        assert PAGE_GUIDES["settings"][0] in btn.toolTip() and PAGE_GUIDES["exam"][0] not in btn.toolTip()
        assert "F1" in btn.toolTip()
    finally:
        controller.shutdown()


# ══════════ 扫描页 ══════════
def test_scan_alignment_readout_uses_plain_words():
    assert ALIGN_TEXT["features"] == "按版面比对" and ALIGN_TEXT["derived"] == "由另一面推算"
    assert not any(w in "".join(ALIGN_TEXT.values()) for w in ("特征匹配", "仿射"))


# ══════════ 模板页 ══════════
@pytest.fixture
def tpage(qtbot, app_home, no_dialogs):
    widget = TemplatePage(AppSettings())
    qtbot.addWidget(widget)
    widget.resize(1400, 860)
    widget.show()
    assert widget.open_editor(TEMPLATE_DIR)
    return widget


def test_tool_dock_uses_unified_region_names(tpage):
    names = {k: b.text() for k, b in tpage.tool_btns.items() if k}
    assert names["identity"] == "科目标识区" and names["ignore"] == "不识别区"
    assert names["omr_field"] == "其他填涂项" and names["choose_one"] == "选做题号"
    assert [a.text() for a in tpage.locate_btn.menu().actions()] == ["自动找定位块", "自动检测外框", "自动识别卡边刻度块"]


def test_props_field_labels_are_plain(tpage):
    ref = next(r for f in ("front", "back") for r in tpage.doc.regions(f) if r.kind == "field")
    if tpage.doc.face_of(ref) != tpage.face:
        tpage.set_face(tpage.doc.face_of(ref))
    tpage.canvas.select([ref])
    texts = [w.text() for w in tpage.props.findChildren(QLabel)]
    assert "字段名（英文）" in texts and "显示名称" in texts
    assert "键" not in texts and "标签" not in texts
    assert "位置（毫米，从纸张左上角量起）" in texts


def test_trial_failure_hides_internal_code():
    assert trial_reason("WRONG_CARD: 卡面标识与模板不符") == "别科的卡（混卡）：卡面标识与模板不符"
    assert "WRONG_CARD" not in trial_reason("WRONG_CARD: 卡面标识与模板不符")
    assert trial_reason("ALIGN_FAILED: 找不到定位块") == "定位失败：找不到定位块"
    assert trial_reason("读不到文件") == "读不到文件"


def test_trial_legend_and_preview_say_what_they_mean(tpage):
    texts = " ".join(w.text() for w in tpage.findChildren(QLabel))
    assert "识别标记：" in texts and "叠层" not in texts
    assert "试识别结果不会保存，也不算进任何批次" in texts


# ══════════ 结果页 ══════════
def test_fill_column_marks_filled_and_unfilled():
    values = {"A": 0.62, "B": 0.05, "C": 0.31}
    assert fill_text(values, "A") == "A 0.62 ✓ · C 0.31"
    assert fill_text({"22": 0.7, "23": 0.2}, ["22"]) == "22 0.70 ✓ · 23 0.20"
    assert fill_text({"A": 0.01}, "") == ""
    # compact text width: 3 options should fit in ~30 chars
    text3 = fill_text({"A": 0.62, "C": 0.31, "D": 0.18}, "A")
    assert len(text3) <= 30, text3
    # full text preserved in fill_text_full
    assert fill_text_full(values, "A") == "A 0.62 已涂 / C 0.31 未涂"
    assert "0～1" in FILL_TIP and "越大越黑" in FILL_TIP
    assert "存疑时最接近界线的那一项也算已涂" in FILL_TIP


def test_results_page_wording(qtbot, app_home):
    from test_results_page_guidance import make_page
    page = make_page(qtbot, AppSettings())
    assert page.answers.horizontalHeaderItem(3).text() == "涂黑程度"
    assert page.answers.horizontalHeaderItem(3).toolTip() == FILL_TIP
    assert any(b.text_html == CONCEPTS["fill"] for b in page.findChildren(HelpButton))
    assert page.card_tabs.tabText(1) == "切图与明细"
    assert [page.evidence.tabs.tabText(i) for i in range(2)] == ["切图", "原始数据"]
    assert page.search.placeholderText() == "序号、考号或原图文件名"
    assert page.legend.head.text() == "识别标记："
    page._notify("已刷新")
    assert page.hint_head.text() == "操作结果"


def test_delivery_groups_badge_and_fallback(qtbot):
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.batch_dir = Path("/tmp/batch")  # 徽标只在有批次时显示
    panel._show_report(report([
        {"severity": "error", "code": "sampling_invalid", "message": "采样位置或有效性记录不一致：第 3 题", "seq": 1},
        {"severity": "error", "code": "result_reused", "message": "不同答卷共用了结果文件", "seq": 2},
        {"severity": "warning", "code": "recognition_warning", "message": "识别结果有警告：第 3 题未作答", "seq": 1},
    ]))
    texts = [w.text() for w in panel.issues.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "必须先处理 2 项" in joined and "仅作记录 1 项（不影响交付，会写进交付记录）" in joined
    assert "部分填涂位置记录有问题：第 3 题" in joined and "采样位置或有效性记录不一致" not in joined
    assert "处理方法：" + ISSUE_FALLBACK in texts  # 没有专门去处的类别给通用办法
    assert panel.badge() == ("2 项要处理", "ERR")
    assert "2 项要处理" in panel.status.text() and "1 项仅作记录" in panel.status.text()
    assert issue_label("not_finalized") == "批次还没结束" and issue_label("unknown_code") == "unknown_code"


# ══════════ 设置页、对话框 ══════════
def test_manual_align_face_choices(qtbot, ctx, tmp_path):
    from test_manual_align_ui import _dialog
    dlg, *_ = _dialog(qtbot, ctx, tmp_path)
    combo = dlg.image_tabs[0].face_combo
    items = [combo.itemText(i) for i in range(combo.count())]
    assert items[-1] == "交给程序自动找" and all(t.endswith("（我来点）") for t in items[:-1])
    texts = " ".join(w.text() for w in dlg.findChildren(QLabel))
    assert "参照点" not in texts and "定位块（或外框的角）" in texts


# ══════════ 术语统一：界面文字、“?”、使用说明、gui.md 里不再出现旧叫法 ══════════
# 旧叫法（见 docs/reviews/2026-11-12-ux-clarity-audit.md 术语统一表）
OLD_TERMS = ["物理异常", "数据警告", "结算", "确认无误", "补做和重试", "放弃未完成转写", "叠层", "涂黑度", "仿射", "纠偏",
             "特征匹配", "参照点", "定位点", "模板快照", "阈值提示", "本轮", "识别进程数", "并发数", "阻断项", "说明项",
             "证据 / JSON", "添加模板目录", "移除目录", "尚未写入批次", "自动找定位点", "自动识别同步头"]
REGION_OLD = [re.compile(r"(?<!科目)标识区"), re.compile("忽略区"), re.compile("选做组"), re.compile("填涂字段"),
              re.compile(r"(?<![（，也叫])同步头")]


def _ui_strings() -> list[tuple[str, int, str]]:
    """ui 包里的字符串字面量（不含文档字符串和注释）。"""
    out = []
    for path in sorted(UI.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = {id(n.body[0].value) for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
                out.append((path.name, n.lineno, n.value))
    return out


def _py_strings(path: Path) -> list[tuple[str, int, str]]:
    """指定 .py 文件里的字符串字面量（不含文档字符串和注释）。"""
    out = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = {id(n.body[0].value) for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            out.append((path.name, n.lineno, n.value))
    return out


def test_ui_strings_use_unified_terms():
    bad = [(f, line, t) for f, line, s in _ui_strings() for t in OLD_TERMS if t in s]
    bad += [(f, line, r.pattern) for f, line, s in _ui_strings() for r in REGION_OLD if r.search(s)]
    assert bad == []


def test_detect_layout_strings_use_unified_terms():
    # TODO: doc.py:~1104 也有 "填涂字段" 用词待改，目前有未提交的外部改动，暂不扫描
    detect = ROOT / "src" / "scan_answer" / "template" / "detect_layout.py"
    strings = _py_strings(detect)
    bad = [(f, line, t) for f, line, s in strings for t in OLD_TERMS if t in s]
    bad += [(f, line, r.pattern) for f, line, s in strings for r in REGION_OLD if r.search(s)]
    assert bad == []


def test_help_texts_and_gui_doc_use_unified_terms():
    texts = {"PAGE_GUIDES": str(PAGE_GUIDES), "CONCEPTS": str(CONCEPTS), "GUIDE_MD": GUIDE_MD,
             "SCAN_PARAMS": str(help_text.SCAN_PARAM_LABELS)}
    gui = (ROOT / "docs" / "gui.md").read_text(encoding="utf-8")
    # gui.md 最后一节讲命令行，命令行输出里仍叫“物理异常 / 阻断项”，不算
    texts["gui.md"] = gui.split("## 命令行交付")[0]
    bad = [(k, t) for k, s in texts.items() for t in OLD_TERMS if t in s]
    bad += [(k, r.pattern) for k, s in texts.items() for r in REGION_OLD if r.search(s)]
    assert bad == []


# ══════════ 模板验证提醒用语 ══════════
def test_check_identity_uses_batch3_wording():
    """check_identity 的提醒用"科目标识区"和"发现不了"而非旧称。"""
    from scan_answer.template.validate import check_identity
    from scan_answer.template.model import Template, Region
    import numpy as np
    t = Template.__new__(Template)
    t.dpi = 300
    r = Region.__new__(Region)
    r.face = "front"
    r.rect = (100, 100, 200, 60)
    t.identity = [r]
    bases = {"front": np.full((3508, 2480), 255, np.uint8)}
    msgs = check_identity(t, bases)
    assert msgs and "科目标识区" in msgs[0] and "发现不了" in msgs[0]
    assert "标识区1内" not in msgs[0] and "检出混卡" not in msgs[0]


# ══════════ 新建模板预览：角度极小时显示"不用摆正" ══════════
def test_new_dialog_describe_tiny_angle():
    from scan_answer.ui.editor.new_dialog import describe
    from dataclasses import dataclass

    @dataclass
    class FakeIB:
        paper: tuple[float, float]
        skew_deg: float
        method: str

    tiny = FakeIB((210.0, 297.0), 0.03, "anchors")
    lines = describe({"front": tiny})
    assert "不用摆正" in lines[0] and "摆正 0" not in lines[0]

    big = FakeIB((210.0, 297.0), 1.25, "anchors")
    lines = describe({"front": big})
    assert "已自动摆正 1.25°" in lines[0]


# ══════════ 扫描页双面卡注释简短 ══════════
def test_scan_page_duplex_note_fits_two_lines():
    from PySide6.QtGui import QFontMetrics, QFont
    import re as _re
    src = Path(ROOT / "src" / "scan_answer" / "ui" / "pages" / "scan_page.py").read_text("utf-8")
    m = _re.search(r'self\.folder_note\s*=\s*QLabel\("(.+?)"\)', src)
    text = m.group(1) if m else ""
    fm = QFontMetrics(QFont("sans-serif", 12))
    width = fm.horizontalAdvance(text)
    assert width <= 380 * 2, f"text {len(text)} chars, width {width}px, may exceed 2 lines"

