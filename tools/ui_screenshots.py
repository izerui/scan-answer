"""用模拟数据跑一遍真实流程并截取各页面，用于和设计正式稿对照。

用法: QT_QPA_PLATFORM=offscreen python tools/ui_screenshots.py <输出目录> [张数]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from scan_answer.app.library import ExamInfo, create_exam  # noqa: E402
from scan_answer.app.settings import AppSettings, bundled_templates_dir  # noqa: E402
from scan_answer.synth.generate import generate_dataset  # noqa: E402
from scan_answer.template import load_template  # noqa: E402
from scan_answer.ui import theme  # noqa: E402
from scan_answer.ui.controller import ScanController  # noqa: E402
from scan_answer.ui.main_window import MainWindow  # noqa: E402


def wait_for(app: QApplication, cond, timeout: float = 300) -> None:
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise TimeoutError("等待超时")
        time.sleep(0.02)


def main(out: str, count: int = 12) -> None:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="sa_ui_"))
    os.environ["SCAN_ANSWER_HOME"] = str(work / "home")
    tpl_dir = bundled_templates_dir() / "guanzong199"
    t, bases = load_template(tpl_dir)
    generate_dataset(t, bases, work / "ds", count, seed=17)
    # 把第 5 张卡的背面换成空白页，演示物理异常
    from PIL import Image
    blank = work / "ds" / "0005_2.jpg"
    with Image.open(blank) as im:
        Image.new("L", im.size, 247).save(blank, dpi=im.info.get("dpi", (300, 300)))
    app = QApplication(sys.argv)
    theme.apply_theme(app)
    settings = AppSettings(output_root=str(work / "out"), template_dirs=[str(bundled_templates_dir())], workers=0,
                           operator="李老师")
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    win.resize(1440, 900)
    win.show()
    exam = create_exam(settings.output_root, ExamInfo("2026 年全国硕士研究生招生考试", "管理类综合能力（199）",
                                                      "199", "第一考点", "", "李老师"), tpl_dir)
    win.start_scan(exam)
    win.scan_page.folder_edit.setText(str(work / "ds"))
    win.scan_page.start_folder(work / "ds")
    wait_for(app, lambda: len(win.scan_page.events) >= count and ctrl.state == "idle")
    app.processEvents()
    win.grab().save(str(out_dir / "scan.png"))
    batch_dir = ctrl.session.batch_dir
    first_ok = next(e.seq for e in sorted(win.scan_page.events.values(), key=lambda e: e.seq) if e.ok and e.warnings)
    win.show_results(exam, str(batch_dir), first_ok)
    app.processEvents()
    win.grab().save(str(out_dir / "results.png"))
    win.show_page("exam")
    app.processEvents()
    win.grab().save(str(out_dir / "exam.png"))
    win.show_page("template")
    app.processEvents()
    win.grab().save(str(out_dir / "template.png"))
    win.show_page("settings")
    app.processEvents()
    win.grab().save(str(out_dir / "settings.png"))
    ctrl.shutdown()
    print("截图已保存到", out_dir, "；临时数据", work)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 12)
