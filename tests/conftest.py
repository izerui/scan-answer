import os
from pathlib import Path

import pytest

# 界面测试在无显示环境下运行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"


@pytest.fixture(scope="session")
def tpl_bases():
    from scan_answer.template import load_template
    return load_template(TEMPLATE_DIR)


@pytest.fixture(scope="session")
def ctx(tpl_bases):
    from scan_answer.engine.recognize import TemplateContext
    t, bases = tpl_bases
    return TemplateContext(t, bases)


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    """隔离的应用配置目录（settings.json 写到这里）。"""
    home = tmp_path / "home"
    monkeypatch.setenv("SCAN_ANSWER_HOME", str(home))
    return home


@pytest.fixture(scope="session")
def small_dataset(tpl_bases, tmp_path_factory):
    """4 张模拟卡：第 3 张为空白页（物理异常），其余正常。"""
    import numpy as np
    from PIL import Image

    from scan_answer.synth.generate import make_scans, make_sheet
    t, bases = tpl_bases
    out = tmp_path_factory.mktemp("small_ds")
    rng = np.random.default_rng(123)
    for seq in range(1, 5):
        paper, truth = make_sheet(t, bases, rng)
        scans = make_scans(t, paper, truth, rng, 200, "blank" if seq == 3 else None)
        for i, img in enumerate(scans, start=1):
            Image.fromarray(img).save(out / f"{seq:04d}_{i}.jpg", quality=92, dpi=(200, 200))
    return out


class _Shown(list):
    """弹出过的对话框记录；question_answer 为确认类对话框的回答（测试中可改为 No）。"""
    question_answer = None


@pytest.fixture(scope="session")
def ext_template(tmp_path_factory):
    """以 guanzong199 为基础，加入 OmrFieldSpec、ChooseOneSpec，改 objective[3] 为判断题。"""
    import copy
    import shutil

    from scan_answer.template import load_template, save_template
    from scan_answer.template.layout import grid_cells
    from scan_answer.template.model import ChooseOneSpec, OmrFieldSpec, OmrGrid

    src = ROOT / "templates" / "guanzong199"
    dst = tmp_path_factory.mktemp("ext_tpl")
    shutil.copytree(src, dst, dirs_exist_ok=True)
    t, bases = load_template(dst)

    # 加入自定义填涂字段：AB 卷
    t.omr_fields.append(OmrFieldSpec(
        "paper_type", "AB 卷",
        OmrGrid("front", ["A", "B"],
                grid_cells((40.0, 236.0), 1, 2, 6.0, 0, (3.8, 2.4))),
    ))

    # 加入选做组
    t.choose_one.append(ChooseOneSpec(
        "opt1", "选做", "front",
        ["56", "57"],
        [(120.0, 234.8, 3.8, 2.4), (130.0, 234.8, 3.8, 2.4)],
    ))

    # 把 objective[3]（46–55）改成 judge，labels=["T","F"]，只保留前两格
    blk = t.objective[3]
    blk.qtype = "judge"
    blk.grid.labels = ["T", "F"]
    blk.grid.cells = [row[:2] for row in blk.grid.cells]

    save_template(t, dst)
    return dst


@pytest.fixture
def no_dialogs(monkeypatch):
    """界面测试中替换模态对话框：不阻塞，记录弹出的内容，确认类对话框默认回答"是"。

    需要回答"否"时：no_dialogs.question_answer = QMessageBox.StandardButton.No"""
    from PySide6.QtWidgets import QMessageBox
    shown = _Shown()
    shown.question_answer = QMessageBox.StandardButton.Yes

    def fake(kind, answer):
        def f(_parent, title, text, *a, **k):
            shown.append((kind, f"{title}：{text}"))
            return answer() if callable(answer) else answer
        return f

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake("information", QMessageBox.StandardButton.Ok)))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake("warning", QMessageBox.StandardButton.Ok)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake("question", lambda: shown.question_answer)))
    return shown


def settle():
    """等后台任务（如图片解码）执行完，并把结果送回界面线程处理。"""
    from PySide6.QtCore import QThreadPool
    from PySide6.QtWidgets import QApplication
    for _ in range(3):  # 回调里可能再提交后台任务
        QThreadPool.globalInstance().waitForDone()
        QApplication.processEvents()


def track_image_reads(monkeypatch, allow=None) -> list:
    """记录 CardView 收到的图片文件路径（同步 set_image 与后台 load_image 两个入口）。

    allow 为 None：全部记录并照常加载；否则 allow(path) 为真的照常加载、不记录，其余记录并拦下（不读文件）。"""
    from scan_answer.ui.widgets.card_view import CardView
    reads = []
    real_set, real_load = CardView.set_image, CardView.load_image

    def set_image(view, path, **kw):
        if allow is None or allow(path):
            if allow is None:
                reads.append(path)
            return real_set(view, path, **kw)
        reads.append(path)
        view.clear()
        return False

    def load_image(view, path, done=None, **kw):
        if allow is None or allow(path):
            if allow is None:
                reads.append(path)
            return real_load(view, path, done, **kw)
        reads.append(path)
        view.clear()
        if done is not None:
            done(False)

    monkeypatch.setattr(CardView, "set_image", set_image)
    monkeypatch.setattr(CardView, "load_image", load_image)
    return reads
