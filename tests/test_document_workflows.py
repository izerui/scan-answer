"""填写 PDF 在试识别与扫描页的真实入口，不经过人工转图片。"""
from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings
from scan_answer.template import load_template

from conftest import TEMPLATE_DIR, open_now, settle
from test_document_batch import scans_to_pdf


def test_pdf_trial_reports_each_sheet_and_incomplete_tail(small_dataset, tmp_path):
    from scan_answer.app.trial import trial_documents
    images = sorted(small_dataset.glob("*.jpg"))
    pdf = scans_to_pdf(tmp_path / "trial.pdf", images[:4] + images[:1])
    t, bases = load_template(TEMPLATE_DIR)
    results = trial_documents(t, bases, [str(pdf)])
    assert [r.ok for r in results] == [True, True, False]
    assert [p["page"] for p in results[1].input_pages] == [3, 4]
    assert "MISSING_FACE" in results[2].message
    assert "trial.pdf" in results[0].sources[0]


def test_template_page_trial_accepts_pdf(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    from scan_answer.ui.pages.template_page import TemplatePage
    pdf = scans_to_pdf(tmp_path / "trial.pdf", sorted(small_dataset.glob("*.jpg"))[:2])
    page = TemplatePage(AppSettings())
    qtbot.addWidget(page)
    open_now(page, TEMPLATE_DIR)
    page.run_trial([str(pdf)])
    qtbot.waitUntil(lambda: not page.trial_running(), timeout=30000)
    assert page.trial_list.count() == 1
    assert "处理完成" in page.trial_list.item(0).text()
    assert "trial.pdf" in page.trial_list.item(0).text()


def test_scan_page_accepts_pdf_file_path(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    from scan_answer.ui.controller import ScanController
    from scan_answer.ui.main_window import MainWindow
    pdf = scans_to_pdf(tmp_path / "answers.pdf", sorted(small_dataset.glob("*.jpg"))[:2])
    settings = AppSettings(output_root=str(tmp_path / "out"))
    exam = create_exam(settings.output_root, ExamInfo(name="PDF 考试"), TEMPLATE_DIR)
    ctrl = ScanController(workers=0)
    window = MainWindow(settings, ctrl)
    qtbot.addWidget(window)
    try:
        window.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成
        window.scan_page.start_folder(pdf)
        qtbot.waitUntil(lambda: ctrl.session is not None, timeout=30000)  # 列文件、新建批次都在后台
        qtbot.waitUntil(lambda: ctrl.session.counts()["ok"] == 1, timeout=30000)
        assert ctrl.session.sheets[1]["input_pages"][0]["file"] == "answers.pdf"
        assert not [m for kind, m in no_dialogs if kind == "warning"]
    finally:
        ctrl.shutdown()


def test_bad_pdf_trial_reports_error_instead_of_leaving_worker_running(tmp_path, tpl_bases):
    from scan_answer.app.trial import trial_documents
    pdf = tmp_path / "broken.pdf"
    pdf.write_bytes(b"bad")
    t, bases = tpl_bases
    result = trial_documents(t, bases, [str(pdf)])
    assert len(result) == 1 and not result[0].ok
    assert "broken.pdf" in result[0].message
