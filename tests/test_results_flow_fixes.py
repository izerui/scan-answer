"""结果核对、手写转写与交付段的修复：重试反馈、刷新、放弃转写、交付明细与目标目录、失败原因来源等。"""
import json
import threading
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QThreadPool
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMessageBox, QPushButton

from scan_answer.app.results import load_rows
from scan_answer.app.settings import AppSettings
from scan_answer.cli import main as cli_main
from scan_answer.ocr.queue import _apply, cancel_ocr, enable_ocr, fail_unclaimed, reset_failed, skip_pending
from scan_answer.output.audit import AuditReport, audit_batch
from scan_answer.output.jsonfile import read_json
from scan_answer.template.model import subjective_item_key
from scan_answer.ui.pages.results_page import ResultsPage
from scan_answer.ui.widgets.delivery_panel import DeliveryPanel
from conftest import TEMPLATE_DIR, settle
from ocr_mock import MockOcrServer
from test_delivery_export import batch as ready_batch, delivery_seed as delivery_seed  # noqa: F401

__all__ = ["ready_batch", "delivery_seed"]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def make_result(status="failed"):
    return {
        "pages": {"front": "front.png"},
        "student": {"fields": {"school": {"label": "学校", "image": "school.png",
                                         "text": None, "ocr_status": status}}},
        "objective": [],
        "subjective": [{"no": "2", "sub": "a", "images": ["sub1.png", "sub2.png"],
                        "ocr_status": status, "text": None}],
        "warnings": [],
    }


@pytest.fixture
def two(tmp_path):
    """两份成功答卷（转写都失败）的最小批次，结构与结果页测试一致。"""
    root = tmp_path / "exam" / "b1"
    sheets = []
    for seq in (1, 2):
        path = root / "students" / f"{seq:04d}" / "result.json"
        write_json(path, make_result())
        for name in ("front", "school", "sub1", "sub2"):
            Image.new("L", (31, 43), 180).save(path.parent / f"{name}.png")
        sheets.append({"seq": seq, "status": "ok", "warnings": 0, "source": f"s{seq}.jpg",
                       "result": f"students/{seq:04d}/result.json"})
    write_json(root / "template.json", json.loads((TEMPLATE_DIR / "template.json").read_text()))
    write_json(root / "manifest.json", {"sheets": sheets})
    return root


def ocr_settings(url):
    settings = AppSettings()
    settings.ocr = {"base_url": url, "model": "mock-vl", "api_key": "k", "max_retries": 0}
    return settings


def result_path(root, seq=1):
    return root / "students" / f"{seq:04d}" / "result.json"


KEY = subjective_item_key({"no": "2", "sub": "a"})


def live_buttons(page):
    """已 deleteLater 的旧按钮先真正删除，只看当前显示的按钮。"""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    return page.findChildren(QPushButton)


def wait_idle(qtbot, page):
    """转写结束后结果页还会在后台重读批次，读完回到界面线程才重画：等后台线程都空闲、回调都送达。"""
    pool = QThreadPool.globalInstance()

    def idle() -> bool:
        QCoreApplication.processEvents()
        return page._retry_owner is None and pool.activeThreadCount() == 0

    qtbot.waitUntil(idle, timeout=20000)
    QCoreApplication.processEvents()


# ---------- 1. 重试的开始、进度与结束反馈 ----------
def test_single_retry_disables_buttons_and_reports_success(qtbot, two):
    with MockOcrServer("ok") as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        messages = []
        page.statusMessage.connect(messages.append)
        assert page.open_batch(two, select_seq=1)
        page._retry_single("subjective", KEY, "第 2 题（a）")
        assert "正在转写" in page.notice.text()
        assert not page.retry_all_btn.isEnabled()
        assert all(not b.isEnabled() for b in live_buttons(page) if "转写" in b.text())
        wait_idle(qtbot, page)
    assert "转写完成" in page.notice.text() and messages
    assert page.retry_all_btn.isEnabled()
    assert read_json(result_path(two))["subjective"][0]["ocr_status"] == "done"


def test_single_retry_failure_shows_reason(qtbot, two):
    with MockOcrServer("error") as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        assert page.open_batch(two, select_seq=1)
        page._retry_single("subjective", KEY)
        wait_idle(qtbot, page)
    assert "转写失败" in page.notice.text()
    item = read_json(result_path(two))["subjective"][0]
    assert item["ocr_status"] == "failed" and item["ocr_error"]
    texts = [w.text() for w in page.findChildren(type(page.title)) if w.text().startswith("转写失败：")]
    assert texts, "失败原因应显示在该题下方"


def test_cancelled_retry_is_reported_as_cancelled(qtbot, two):
    gate = threading.Event()
    with MockOcrServer("ok", gate=gate) as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        assert page.open_batch(two, select_seq=1)
        page._retry_single("subjective", KEY)
        qtbot.waitUntil(lambda: srv.calls >= 1, timeout=10000)
        cancel_ocr(two)  # 扫描页“继续扫描”同一批次时会这样取消
        gate.set()
        wait_idle(qtbot, page)
    assert "已取消" in page.notice.text()
    assert read_json(result_path(two))["subjective"][0]["ocr_status"] == "pending"


def test_batch_retry_reports_progress(qtbot, two):
    with MockOcrServer("ok") as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        seen = []
        page.statusMessage.connect(seen.append)
        assert page.open_batch(two)
        page._retry_all_failed()
        assert not page.retry_all_btn.isEnabled()
        wait_idle(qtbot, page)
    assert any("已完成" in m for m in seen)
    assert "完成 4" in page.notice.text() and "共 4 项" in page.notice.text()


def test_success_under_warning_filter_selects_neighbor(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(two)
    settle()  # 转写状态在后台统计
    page.filter.set_value("warn")
    page._refill()
    assert page.select_seq(1)
    path = result_path(two)
    data = read_json(path)
    data["subjective"][0]["ocr_status"] = data["student"]["fields"]["school"]["ocr_status"] = "done"
    write_json(path, data)
    page._retry_done(str(path), True, "#0001 转写完成")
    settle()
    assert [r.seq for r in page.visible_rows()] == [2]
    assert page.current is not None and page.current.seq == 2
    assert "#0001 已不在“有警告”" in page.notice.text()


# ---------- 3/4. refresh() 与状态重算 ----------
def test_refresh_reloads_rows_and_keeps_selection(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    page.refresh()  # 没有打开批次：什么也不做
    assert page.open_batch(two, select_seq=2)
    settle()
    assert "转写失败 2" in page.model.item(1, 2).text()
    path = result_path(two, 2)
    data = read_json(path)
    data["subjective"][0]["ocr_status"] = "done"
    write_json(path, data)
    page.refresh()
    settle()
    assert page.current.seq == 2 and page.current.ocr_failed == 1
    assert page.model.item(1, 2).text() == "转写失败 1"
    page.refresh_btn.click()
    settle()
    assert page.notice.text() == "已刷新"


def test_settings_and_scan_state_recompute_retry_controls(qtbot, two):
    settings = AppSettings()
    page = ResultsPage(settings)
    qtbot.addWidget(page)
    assert page.open_batch(two, select_seq=1)
    assert not page.retry_all_btn.isEnabled()
    settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page.on_settings_changed()
    assert page.retry_all_btn.isEnabled()
    retry = [b for b in live_buttons(page) if b.text() == "重试转写"]
    assert retry and all(b.isEnabled() for b in retry)
    page._active_batch_fn = lambda: str(two)
    page.on_scan_state_changed()
    assert not page.retry_all_btn.isEnabled()
    assert "结束批次" in page.retry_all_btn.toolTip()


# ---------- 5. 一份坏 result.json 不中断整批 ----------
def test_batch_retry_skips_corrupt_result_and_finishes_others(qtbot, two):
    result_path(two, 2).write_text("{broken", encoding="utf-8")
    with MockOcrServer("ok") as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        assert page.open_batch(two)
        page._retry_all_failed()
        wait_idle(qtbot, page)
    first = read_json(result_path(two))
    assert first["subjective"][0]["ocr_status"] == "done"
    assert first["student"]["fields"]["school"]["ocr_status"] == "done"
    assert "1 份无法处理" in page.notice.text() and "#0002" in page.notice.text()


def test_enqueue_error_after_reset_does_not_leave_orphan_pending(qtbot, two, monkeypatch):
    def broken_enqueue(self, path):
        if Path(path).parent.name == "0002":
            raise OSError("磁盘错误")
        return original(self, path)

    from scan_answer.ocr.queue import OcrQueue
    original = OcrQueue.enqueue
    monkeypatch.setattr(OcrQueue, "enqueue", broken_enqueue)
    with MockOcrServer("ok") as srv:
        page = ResultsPage(ocr_settings(srv.url))
        qtbot.addWidget(page)
        assert page.open_batch(two)
        page._retry_all_failed()
        wait_idle(qtbot, page)
    second = read_json(result_path(two, 2))
    assert second["subjective"][0]["ocr_status"] == "failed"
    assert "磁盘错误" in second["subjective"][0]["ocr_error"]
    assert read_json(result_path(two))["subjective"][0]["ocr_status"] == "done"


def test_fail_unclaimed_restores_failed_with_reason(two):
    path = result_path(two)
    assert reset_failed(path) == 2
    assert fail_unclaimed(path, "未能开始转写：x") == 2
    data = read_json(path)
    assert data["subjective"][0]["ocr_status"] == "failed"
    assert sum(w["code"] == "OCR_FAILED" for w in data["warnings"]) == 2


# ---------- 6. 放弃未完成转写 ----------
def test_skip_pending_from_results_page(qtbot, two, no_dialogs):
    for seq in (1, 2):
        write_json(result_path(two, seq), make_result("pending"))
    page = ResultsPage(AppSettings())  # 没有 OCR 配置：补做按钮灰，但放弃按钮可用
    qtbot.addWidget(page)
    assert page.open_batch(two, select_seq=1)
    settle()
    assert not page.retry_all_btn.isEnabled() and page.skip_btn.isEnabled()
    no_dialogs.question_answer = QMessageBox.StandardButton.No
    page._skip_pending()
    assert read_json(result_path(two))["subjective"][0]["ocr_status"] == "pending"
    no_dialogs.question_answer = QMessageBox.StandardButton.Yes
    page._skip_pending()
    data = read_json(result_path(two))
    assert data["subjective"][0]["ocr_status"] == "none"
    assert data["subjective"][0]["ocr_skipped"]["reason"]
    assert "已把 4 项" in page.notice.text()
    assert not page.skip_btn.isEnabled()
    assert any("已放弃转写" in w.text() for w in page.findChildren(type(page.title)))


def test_skipped_ocr_unblocks_delivery_and_is_recorded(ready_batch):
    manifest = read_json(ready_batch / "manifest.json")
    path = ready_batch / next(s["result"] for s in manifest["sheets"] if s["status"] == "ok")
    data = read_json(path)
    data["subjective"][0]["ocr_status"] = "pending"
    write_json(path, data)
    assert not audit_batch(ready_batch).ready
    assert skip_pending(path) == (1, 0)
    report = audit_batch(ready_batch)
    assert report.ready, report.issues
    assert report.summary["ocr_skipped"] == 1
    assert any(i["code"] == "ocr_skipped" and i["severity"] == "warning" for i in report.issues)
    # 重新补做时去掉放弃标记
    assert enable_ocr(path) >= 1
    assert "ocr_skipped" not in read_json(path)["subjective"][0]


def test_cli_ocr_skip(ready_batch, capsys):
    manifest = read_json(ready_batch / "manifest.json")
    path = ready_batch / next(s["result"] for s in manifest["sheets"] if s["status"] == "ok")
    data = read_json(path)
    data["subjective"][0]["ocr_status"] = "pending"
    write_json(path, data)
    assert cli_main(["ocr-skip", "--batch", str(ready_batch)]) == 0
    assert "已把 1 项" in capsys.readouterr().out
    assert audit_batch(ready_batch).ready


# ---------- 9. 转写失败原因只有一个来源，交付清单不重复 ----------
def test_apply_records_and_clears_ocr_error():
    result = make_result("pending")
    _apply(result, "subjective", KEY, None, "failed", None, None, "超时")
    item = result["subjective"][0]
    assert item["ocr_error"] == "超时"
    assert [w["msg"] for w in result["warnings"]] == ["手写转写失败：超时"]
    _apply(result, "subjective", KEY, "答案", "done", "m", 0.9, None)
    assert "ocr_error" not in item and result["warnings"] == []


@pytest.mark.parametrize("legacy", [False, True])
def test_audit_reports_ocr_failure_once_with_reason(ready_batch, legacy):
    manifest = read_json(ready_batch / "manifest.json")
    path = ready_batch / next(s["result"] for s in manifest["sheets"] if s["status"] == "ok")
    data = read_json(path)
    item = data["subjective"][0]
    item["ocr_status"] = "pending"
    _apply(data, "subjective", subjective_item_key(item), None, "failed", None, None, "接口返回 500")
    if legacy:
        del item["ocr_error"]  # 旧结果只有 OCR_FAILED 警告
    write_json(path, data)
    report = audit_batch(ready_batch)
    assert report.ready
    failed = [i for i in report.issues if i["code"] == "ocr_failed"]
    assert len(failed) == 1 and "接口返回 500" in failed[0]["message"]
    assert not any(i["code"] == "recognition_warning" and "OCR_FAILED" in i["message"] for i in report.issues)
    assert not any(i["code"] == "recognition_warning" and "接口返回 500" in i["message"] for i in report.issues)


# ---------- 8. 信息栏可见、可单项重试 ----------
def test_field_ocr_status_listed_with_single_retry(qtbot, two, monkeypatch):
    page = ResultsPage(ocr_settings("http://unused"))
    qtbot.addWidget(page)
    assert page.open_batch(two, select_seq=1)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    labels = [w.text() for w in page.findChildren(type(page.title))]
    assert "信息栏 学校" in labels
    calls = []
    monkeypatch.setattr(page, "_retry_single", lambda kind, key, label="": calls.append((kind, key)))
    page._fill_subjective(page.result)
    for b in [b for b in live_buttons(page) if b.text() == "重试转写"]:
        b.click()
    assert ("field", "school") in calls and ("subjective", KEY) in calls


# ---------- 10. 结果路径无效 ----------
def test_invalid_result_path_has_its_own_category_and_guidance(qtbot, two):
    manifest = read_json(two / "manifest.json")
    manifest["sheets"][1]["result"] = "../escape/result.json"
    write_json(two / "manifest.json", manifest)
    rows = load_rows(two)
    assert rows[1].status == "broken" and rows[1].physical is None
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(two)
    page.filter.set_value("err")
    page._refill()
    assert [r.seq for r in page.visible_rows()] == [2]
    assert page.model.item(0, 1).text() == "结果不可用"
    assert "INVALID_RESULT" not in page.model.item(0, 1).text()
    assert page.select_seq(2)
    assert "继续扫描" in page._answers_empty.text() and "物理异常" not in page._answers_empty.text()
    assert page.info.text() == "结果文件不可用"


# ---------- 12. PgUp/PgDn 在卡面、答案表上也切换学生 ----------
def test_page_keys_switch_students_from_scroll_widgets(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    page.resize(1400, 900)
    page.show()
    qtbot.waitExposed(page)
    assert page.open_batch(two)
    for widget in (page.card, page.answers, page.table):
        page.table.selectRow(0)
        assert page.current.seq == 1
        widget.setFocus()
        QTest.keyClick(widget, Qt.Key.Key_PageDown)
        assert page.current.seq == 2, type(widget).__name__


# ---------- 2/7/11. 交付面板 ----------
def report(issues):
    errors = sum(i["severity"] == "error" for i in issues)
    return AuditReport(not errors, "blocked" if errors else "warnings", issues, {},
                       {"ok": 1, "errors": errors, "warnings": len(issues) - errors}, "2026-11-10T10:00:00+08:00")


def test_delivery_details_group_blocking_and_notes_with_guidance(qtbot):
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel._show_report(report([
        {"severity": "error", "code": "not_finalized", "message": "批次尚未按最终阈值完成重新判定，请先结束批次",
         "seq": None, "path": "manifest.json"},
        {"severity": "error", "code": "ocr_pending", "message": "文字转写尚未完成：subjective/2a",
         "seq": 1, "path": "students/0001/result.json"},
        {"severity": "warning", "code": "recognition_warning", "message": "识别结果有警告：第 3 题未作答",
         "seq": 1, "path": "students/0001/result.json"},
    ]))
    assert "2 项阻断" in panel.status.text() and "1 项说明" in panel.status.text()
    text = "\n".join(w.text() for w in panel.issues.findChildren(QLabel))  # 交付清单（阻断项 / 说明项）
    assert text.index("阻断项 2") < text.index("说明项 1")
    assert "文字转写尚未完成：subjective/2a" in text
    assert "第 3 题未作答" in text
    assert "结束批次" in text and "放弃未完成转写" in text


def test_delivery_conclusion_marked_stale_after_data_change(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(two, select_seq=1)
    panel = page.delivery_panel
    panel._show_report(report([]))
    assert "可交付" in panel.status.text()
    path = result_path(two)
    data = read_json(path)
    data["subjective"][0]["ocr_status"] = "done"
    write_json(path, data)
    page._retry_done(str(path), True, "转写完成")
    settle()
    assert panel.stale and "过期" in panel.status.text() and "可交付" not in panel.status.text()


def test_export_success_after_switching_batch_is_still_announced(qtbot, ready_batch, tmp_path):
    page = ResultsPage()
    qtbot.addWidget(page)
    seen = []
    page.statusMessage.connect(seen.append)
    page._attach_delivery_panel()
    panel = page.delivery_panel
    panel.set_batch(ready_batch)
    target = tmp_path / "out" / "delivery"
    panel.export_to(target)
    panel.set_batch(tmp_path / "other")  # 导出期间切换批次
    qtbot.waitUntil(lambda: not panel.busy, timeout=60000)
    assert (target / "delivery.json").is_file()
    assert any(str(target) in m and ready_batch.name in m for m in seen)
    assert str(target) in page.notice.text()


def test_delivery_target_inside_exam_or_output_root_is_rejected(qtbot, two, tmp_path, monkeypatch):
    write_json(two.parent / "exam.json", {"name": "x"})
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.set_batch(two)
    extra = tmp_path / "root"
    panel.protected_dirs_fn = lambda: [(extra, "输出根目录")]
    assert "考试目录" in panel.target_problem(two.parent / "d")
    assert "输出根目录" in panel.target_problem(two.parent.parent / "d")
    assert "输出根目录" in panel.target_problem(extra / "x" / "d")
    assert panel.target_problem(tmp_path.parent / "elsewhere") == ""
    started = []
    monkeypatch.setattr(panel, "_start", lambda *a: started.append(a))
    panel.export_to(two.parent / "delivery")
    assert not started and "不能放在" in panel.status.text()
    # 文件对话框：有默认起始目录；选到考试目录内时拒绝
    shown = {}

    def fake_dialog(_parent, _title, start):
        shown["start"] = start
        return str(two.parent)

    monkeypatch.setattr("scan_answer.ui.widgets.delivery_panel.QFileDialog.getExistingDirectory", fake_dialog)
    warnings = []
    monkeypatch.setattr("scan_answer.ui.widgets.delivery_panel.QMessageBox.warning",
                        lambda *a: warnings.append(a[2]))
    panel._choose_target()
    assert shown["start"] and Path(shown["start"]).is_dir()
    assert warnings and not started


def test_ocr_counts_pending_state_is_explicit(qtbot, two):
    """打开批次后转写状态在后台统计：统计回来前不说“没有等待转写”“可以检查交付”，回来后按实际显示。"""
    for seq in (1, 2):
        write_json(result_path(two, seq), make_result("pending"))
    manifest = read_json(two / "manifest.json")
    manifest["finalized"] = True
    write_json(two / "manifest.json", manifest)
    page = ResultsPage(AppSettings())
    qtbot.addWidget(page)
    assert page.open_batch(two)
    assert "正在读取" in page.skip_btn.toolTip() and not page.skip_btn.isEnabled()
    assert "检查交付" not in page.notice.text()
    settle()
    assert page.skip_btn.isEnabled() and "等待转写" in page.notice.text()
