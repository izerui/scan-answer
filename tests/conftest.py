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


@pytest.fixture
def no_dialogs(monkeypatch):
    """界面测试中替换模态对话框：不阻塞，记录弹出的内容，确认类对话框默认回答"是"。"""
    from PySide6.QtWidgets import QMessageBox
    shown: list[tuple[str, str]] = []

    def fake(kind, answer):
        def f(_parent, title, text, *a, **k):
            shown.append((kind, f"{title}：{text}"))
            return answer
        return f

    monkeypatch.setattr(QMessageBox, "information", staticmethod(fake("information", QMessageBox.StandardButton.Ok)))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(fake("warning", QMessageBox.StandardButton.Ok)))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake("question", QMessageBox.StandardButton.Yes)))
    return shown
