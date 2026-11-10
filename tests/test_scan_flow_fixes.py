"""扫描与识别段审查问题的回归测试：配对、串卷警告、重复导入、作废/恢复、重新识别、忙碌拦截、并发停止等。"""
import shutil
import threading
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtWidgets import QFileDialog, QMessageBox

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings
from scan_answer.engine.align import manual_alignment, reference_points
from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import PhysicalError, analyze_sheet, decide_sheet
from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.events import SheetEvent, SheetQueued, SheetSkipped, UnvoidEvent
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.control import ControlledSource
from scan_answer.source.documents import DocumentSource
from scan_answer.source.folder import pair_images, pairing_note
from scan_answer.synth.generate import make_scans, make_sheet
from scan_answer.ui.controller import (FINALIZED, IDLE, PAUSED, RELOCATING, RESCANNING, RUNNING, STOPPING,
                                       ScanController)
from scan_answer.ui.pages.scan_page import STATE_TEXT, ExceptionItem, ScanPage
from scan_answer.ui.widgets.mosaic import ERR, VOID, MosaicLegend

from conftest import TEMPLATE_DIR
from test_document_batch import scans_to_pdf
from test_manual_align import _clicks, _tilted


def _copy(src: Path, dst: Path, names) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(src / name, dst / name)
    return dst


@pytest.fixture
def ctrl(qtbot):
    c = ScanController(workers=0)
    yield c
    c.shutdown()


@pytest.fixture
def page(qtbot, ctrl, app_home, tmp_path):
    p = ScanPage(ctrl, AppSettings(output_root=str(tmp_path / "out")))
    qtbot.addWidget(p)
    p._timer.stop()
    return p


def _hints(no_dialogs):
    return [text for _kind, text in no_dialogs]


# ---------- 1. 配对规则 ----------

def test_serial_numbers_are_not_face_suffixes():
    paths = [Path(f"IMG_{i:04d}.jpg") for i in range(1, 201)]
    groups = pair_images(paths, 2)
    assert len(groups) == 100 and all(len(g) == 2 for g in groups)
    assert groups[0] == paths[:2]
    assert "流水号" in pairing_note(paths, 2)


def test_face_suffix_pairing_still_groups_by_prefix_and_needs_no_note():
    paths = [Path(f"{n}.jpg") for n in ("b_2", "a_1", "a_2", "b_1", "c_1")]
    groups = pair_images(sorted(paths), 2)
    assert [[p.stem for p in g] for g in groups] == [["a_1", "a_2"], ["b_1", "b_2"], ["c_1"]]
    assert pairing_note(sorted(paths), 2) is None


def test_mixed_naming_falls_back_and_explains():
    paths = [Path(n) for n in ("0001_1.jpg", "0001_2.jpg", "scan.jpg")]
    assert pair_images(paths, 2) == [paths[:2], paths[2:]]
    note = pairing_note(paths, 2)
    assert "命名不统一" in note and "不是 2 的整数倍" in note


def test_document_source_exposes_pairing_note_and_sheet_count(small_dataset, tmp_path):
    folder = tmp_path / "serial"
    folder.mkdir()
    for i, f in enumerate(sorted(small_dataset.glob("*.jpg"))[:4], 1):
        shutil.copy(f, folder / f"IMG_{i:04d}.jpg")
    pdf = scans_to_pdf(tmp_path / "more.pdf", sorted(small_dataset.glob("*.jpg"))[:3])
    src = DocumentSource([folder, pdf], 2)
    assert "流水号" in src.pairing_note
    assert src.sheet_count() == 2 + 2  # 图片 2 份 + PDF 3 页（2 份，最后一份缺面）


def test_scan_page_note_describes_real_pairing_rule(page):
    assert "否则按文件名排序" in page.folder_note.toolTip() and "文件名顺序" in page.folder_note.text()


# ---------- 2. 正反面顺序颠倒的警告 ----------

def test_swapped_faces_add_warning(ctx):
    t = ctx.tpl
    rng = np.random.default_rng(3)
    paper, truth = make_sheet(t, ctx.bases, rng)
    swapped = analyze_sheet(ctx, make_scans(t, paper, truth, rng, 300, "swap"), [300, 300])
    warnings = decide_sheet(ctx, swapped, Thresholds()).warnings
    assert [w for w in warnings if w["code"] == "FACE_SWAPPED"][0]["msg"].startswith("正反面顺序与导入顺序相反")
    normal = analyze_sheet(ctx, make_scans(t, paper, truth, rng, 300, None), [300, 300])
    assert not [w for w in decide_sheet(ctx, normal, Thresholds()).warnings if w["code"] == "FACE_SWAPPED"]


# ---------- 3 / 6 / 8 / 9. 流水线：重复导入、追加到已结算批次、恢复作废、重新识别 ----------

def test_rerun_skips_already_registered_inputs_and_append_invalidates_settlement(small_dataset, tmp_path):
    first = _copy(small_dataset, tmp_path / "first", ["0001_1.jpg", "0001_2.jpg"])
    both = _copy(small_dataset, tmp_path / "both", ["0001_1.jpg", "0001_2.jpg", "0002_1.jpg", "0002_2.jpg"])
    renamed = tmp_path / "renamed"
    renamed.mkdir()
    shutil.copy(small_dataset / "0001_1.jpg", renamed / "x_1.jpg")  # 换目录改名，内容相同
    shutil.copy(small_dataset / "0001_2.jpg", renamed / "x_2.jpg")
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(first, s.faces))
        assert p.finalize().completed and s.manifest["finalized"]
        events.clear()
        p.run(DocumentSource(both, s.faces))  # 中途结束后重新点“开始识别”：只登记新的那份
        assert sorted(s.sheets) == [1, 2]
        assert [e.existing_seq for e in events if isinstance(e, SheetSkipped)] == [1]
        # 已结算的批次追加答卷后结算失效，需要重新结束批次
        assert not s.manifest["finalized"] and s.manifest["finalization"]["status"] == "dirty"
        events.clear()
        p.run(DocumentSource(renamed, s.faces))
        assert sorted(s.sheets) == [1, 2]
        assert [e.existing_seq for e in events if isinstance(e, SheetSkipped)] == [1]
    assert all(item.get("input_keys") for item in read_json(s.batch_dir / "manifest.json")["sheets"])


def test_regrouped_pages_are_registered_with_overlap_notice(small_dataset, tmp_path):
    """第一次配错（0001 正面 + 0002 背面），改正后重新导入：页面部分重叠但配组不同，不能当成重复跳过。"""
    from scan_answer.pipeline.events import DeviceNotice
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    shutil.copy(small_dataset / "0001_1.jpg", wrong / "p1.jpg")
    shutil.copy(small_dataset / "0002_2.jpg", wrong / "p2.jpg")
    fixed = _copy(small_dataset, tmp_path / "fixed", ["0001_1.jpg", "0001_2.jpg"])
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(wrong, s.faces))
        events.clear()
        p.run(DocumentSource(fixed, s.faces))
    assert sorted(s.sheets) == [1, 2]
    assert not any(isinstance(e, SheetSkipped) for e in events)
    notes = [e for e in events if isinstance(e, DeviceNotice) and e.kind == "input_overlap"]
    assert len(notes) == 1 and "#0001" in notes[0].message
    assert any(ev["kind"] == "input_overlap" for ev in s.manifest["events"])


def test_duplicate_inputs_within_one_round_are_skipped(small_dataset, tmp_path):
    src = tmp_path / "dup"
    src.mkdir()
    for prefix in ("x", "y"):  # 同一份答卷换名出现两次
        shutil.copy(small_dataset / "0001_1.jpg", src / f"{prefix}_1.jpg")
        shutil.copy(small_dataset / "0001_2.jpg", src / f"{prefix}_2.jpg")
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(src, s.faces))
    assert sorted(s.sheets) == [1]
    assert [e.existing_seq for e in events if isinstance(e, SheetSkipped)] == [1]


def test_pdf_reimport_is_skipped_even_for_old_batches_without_keys(small_dataset, tmp_path):
    pdf = scans_to_pdf(tmp_path / "a.pdf", sorted(small_dataset.glob("0001_*.jpg")))
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(pdf, s.faces))
        s.sheets[1].pop("input_keys")  # 旧版批次：只有 input_pages
        p.run(DocumentSource(pdf, s.faces))
    assert list(s.sheets) == [1] and any(isinstance(e, SheetSkipped) for e in events)


def test_pdf_reimport_skips_without_rendering(small_dataset, tmp_path, monkeypatch):
    """结束本轮后重新导入同一 PDF：已登记的页不再渲染，只渲染新的页。"""
    import scan_answer.source.documents as documents
    pdf = scans_to_pdf(tmp_path / "a.pdf", sorted(small_dataset.glob("000[12]_*.jpg")))
    first = scans_to_pdf(tmp_path / "first.pdf", sorted(small_dataset.glob("0001_*.jpg")))
    rendered = []
    original = documents.iter_pdf_pages

    def counting(path, dpi=300, skip=None):
        for number, image in original(path, dpi, skip):
            if image is not None:
                rendered.append((Path(path).name, number))
            yield number, image

    monkeypatch.setattr(documents, "iter_pdf_pages", counting)
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(pdf, s.faces))
        rendered.clear()
        events.clear()
        p.run(DocumentSource(pdf, s.faces))
        assert rendered == [] and sorted(s.sheets) == [1, 2]
        assert sorted(e.existing_seq for e in events if isinstance(e, SheetSkipped)) == [1, 2]
        p.run(DocumentSource(first, s.faces))  # 不同文件（摘要不同）照常渲染登记
    assert ("first.pdf", 1) in rendered and sorted(s.sheets) == [1, 2, 3]


def test_void_can_be_undone_and_invalidates_settlement(small_dataset, tmp_path):
    folder = _copy(small_dataset, tmp_path / "in", ["0003_1.jpg", "0003_2.jpg"])  # 空白页 → 物理异常
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(folder, s.faces))
        assert s.sheets[1]["status"] == "physical"
        p.void(1)
        assert p.finalize().completed
        p.unvoid(1)
    item = read_json(s.batch_dir / "manifest.json")["sheets"][0]
    assert item["status"] == "physical" and item["physical"] == "BLANK_PAGE" and "voided" not in item
    assert not s.manifest["finalized"] and isinstance(events[-1], UnvoidEvent)
    with pytest.raises(ValueError):
        s.unvoid(1)  # 只能恢复已作废的卡


def test_reconciled_sheet_gets_input_pages_and_can_be_reprocessed(small_dataset, tmp_path):
    folder = _copy(small_dataset, tmp_path / "in", ["0001_1.jpg", "0001_2.jpg"])
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(DocumentSource(folder, s.faces))
    # 模拟崩溃：结果与 manifest 条目都还没写出，只有 raw/ 里的原图
    shutil.rmtree(s.batch_dir / "students")
    (s.batch_dir / "state" / "0001.json").unlink()
    manifest = read_json(s.batch_dir / "manifest.json")
    manifest["sheets"] = []
    write_json_atomic(s.batch_dir / "manifest.json", manifest)
    s2 = BatchSession.open(s.batch_dir)
    item = s2.sheets[1]
    assert item["status"] == "physical" and "重新识别" in item["message"]
    assert [o["raw"] for o in item["input_pages"]] == ["raw/0001_0001_1.jpg", "raw/0001_0001_2.jpg"]
    events = []
    with BatchPipeline(s2, listener=events.append) as p:
        ev = p.reprocess(1)
    assert ev.ok and s2.sheets[1]["status"] == "ok"
    assert isinstance(events[0], SheetQueued) and events[-1].ok


def test_reprocess_failure_and_commit_failure_restore_cell(small_dataset, tmp_path, monkeypatch):
    folder = _copy(small_dataset, tmp_path / "in", ["0003_1.jpg", "0003_2.jpg", "0001_1.jpg", "0001_2.jpg"])
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b")
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(DocumentSource(folder, s.faces))
        blank = next(seq for seq, item in s.sheets.items() if item["status"] == "physical")
        events.clear()
        ev = p.reprocess(blank)  # 空白页再识别仍失败：保留原记录
        assert not ev.ok and ev.rescan and "重新识别失败" in ev.message
        assert s.sheets[blank]["physical"] == "BLANK_PAGE"
        assert any(e["kind"] == "reprocess_failed" for e in s.manifest["events"])
        # 重扫提交失败：已发出“识别中”，必须再发一次事件把格子恢复为异常
        from scan_answer.pipeline import attempts

        def broken(self, outcome):
            raise OSError("磁盘写满")
        monkeypatch.setattr(attempts.RescanAttempt, "commit", broken)
        good = sorted(small_dataset.glob("0001_*.jpg"))
        from scan_answer.source.base import SheetImages
        events.clear()
        with pytest.raises(OSError):
            p.rescan(blank, SheetImages("fix", [f.name for f in good], [None, None], paths=good))
    assert isinstance(events[0], SheetQueued)
    assert isinstance(events[-1], SheetEvent) and not events[-1].ok and events[-1].rescan
    assert "磁盘写满" in events[-1].message and s.sheets[blank]["status"] == "physical"


# ---------- 4 / 7 / 13. 忙碌时的拦截与提示 ----------

def test_void_rejected_while_scanning(ctrl, tmp_path):
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    ctrl.new_batch(exam)
    ctrl._operation = "scan"  # 模拟识别进行中（含暂停）
    try:
        with pytest.raises(RuntimeError):
            ctrl.void(1)
        with pytest.raises(RuntimeError):
            ctrl.unvoid(1)
    finally:
        ctrl._operation = None
    assert ctrl._cmds.empty()


@pytest.mark.parametrize(("state", "words"), [
    (PAUSED, "结束本轮"), (RUNNING, "结束本轮"), (RESCANNING, "正在重扫另一张卡"),
    (RELOCATING, "正在手动定位另一张卡"), (STOPPING, "等待设备释放"),
])
def test_scan_page_busy_actions_explain_what_to_do(page, ctrl, no_dialogs, monkeypatch, state, words):
    calls = []
    for name in ("void", "unvoid", "rescan", "relocate", "reprocess"):
        monkeypatch.setattr(ctrl, name, lambda *a, n=name: calls.append(n))
    ctrl.state = state
    for action in (page._void, page._restore, page._rescan, page._relocate, page._reprocess):
        action(1)
    ctrl.state = IDLE
    assert calls == []
    texts = _hints(no_dialogs)
    assert len(texts) == 5 and all(words in t for t in texts), texts


def test_source_switch_disabled_while_busy_and_stop_available_for_files(page):
    page._on_state(RUNNING)
    assert not page.src_seg.isEnabled()
    page._on_state(PAUSED)
    assert not page.src_seg.isEnabled() and page.stop_btn.isEnabled()
    page._on_state(IDLE)
    assert page.src_seg.isEnabled()
    page._on_source_changed("folder")
    assert not page.stop_btn.isHidden() and "跳过已导入的答卷" in page.stop_btn.toolTip()


def test_state_text_distinguishes_relocating(ctrl, qtbot, tmp_path):
    assert STATE_TEXT[RELOCATING] == "手动定位中"
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    ctrl.new_batch(exam)
    states, errors = [], []
    ctrl.stateChanged.connect(states.append)
    ctrl.errorOccurred.connect(errors.append)
    ctrl.relocate(99, {})  # 序号不存在：后台报错并回到就绪
    qtbot.waitUntil(lambda: bool(errors) and ctrl.state == IDLE, timeout=10000)
    assert states[0] == RELOCATING


def test_empty_input_prompts_instead_of_using_working_directory(page, ctrl, no_dialogs):
    page.folder_edit.setText("  ")
    page._main_clicked()
    assert ctrl.session is None and "请先选择要导入" in _hints(no_dialogs)[-1]


# ---------- 5. 文件导入暂停时结束批次 ----------

def test_remaining_inputs_and_finalize_confirmation(page, ctrl, no_dialogs, small_dataset, tmp_path, monkeypatch):
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    ctrl.new_batch(exam)
    folder = _copy(small_dataset, tmp_path / "in", ["0001_1.jpg", "0001_2.jpg", "0002_1.jpg", "0002_2.jpg"])
    src = ControlledSource(DocumentSource(folder, 2))
    with ctrl._state_lock:
        ctrl._source, ctrl._operation = src, "scan"
    assert ctrl.remaining_inputs() == 2
    src.taken = 1  # 图像源已交出一份，但流水线还没登记（暂停时握在手里）：仍算没导入
    assert ctrl.remaining_inputs() == 2
    ctrl._pipeline.consumed = 1
    assert ctrl.remaining_inputs() == 1
    with ctrl._state_lock:
        ctrl._source, ctrl._operation = None, None
    assert ctrl.remaining_inputs() is None
    calls = []
    monkeypatch.setattr(ctrl, "remaining_inputs", lambda: 3)
    monkeypatch.setattr(ctrl, "finalize", lambda: calls.append("finalize"))
    ctrl.state = PAUSED
    no_dialogs.question_answer = QMessageBox.StandardButton.No
    page.finalize()
    assert calls == [] and "还有 3 份答卷的文件没有导入" in _hints(no_dialogs)[-1]
    no_dialogs.question_answer = QMessageBox.StandardButton.Yes
    page.finalize()
    ctrl.state = IDLE
    assert calls == ["finalize"]


# ---------- 6. 已结束的批次再点“开始识别” ----------

@pytest.mark.parametrize(("choice", "same", "ok"), [("append", True, True), ("new", False, True), (None, True, False)])
def test_finalized_batch_asks_append_or_new(page, ctrl, no_dialogs, tmp_path, monkeypatch, choice, same, ok):
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    page.exam = exam
    old = ctrl.new_batch(exam)
    ctrl._set_state(FINALIZED)
    asked = []
    monkeypatch.setattr(page, "_ask_finalized_action", lambda: asked.append(1) or choice)
    assert page._ensure_batch() is ok
    assert asked == [1] and (ctrl.session is old) is same


# ---------- 8. 作废后可恢复 ----------

def test_voided_card_moves_to_void_list_and_back(page, ctrl, no_dialogs):
    page._on_done(SheetEvent(2, "x", False, "BLANK_PAGE", "检测到空白页", None, 0), animate=False)
    page._void(2)
    assert any("可以在左栏“已作废”里恢复" in t for t in _hints(no_dialogs))  # 确认框说明可恢复
    page._on_voided(2)
    assert page.exc_box.count() == 1 and page.void_box.count() == 1
    item = page.void_box.itemAt(0).widget()
    assert item.voided and page.mosaic.cells[2].state == VOID and not page.void_title.isHidden()
    page._on_unvoided(2)
    assert page.exc_box.count() == 2 and page.void_box.count() == 0
    assert page.mosaic.cells[2].state == ERR and page.t_err.value.text() == "1"


def test_voided_item_offers_rescan_and_restore(qtbot):
    from PySide6.QtWidgets import QPushButton
    item = ExceptionItem(1, "ALIGN_FAILED", "", None, voided=True)
    qtbot.addWidget(item)
    assert [b.text() for b in item.findChildren(QPushButton)] == ["重扫", "恢复"]
    item = ExceptionItem(1, "ALIGN_FAILED", "", None, raw_ok=False)
    qtbot.addWidget(item)
    assert [b.text() for b in item.findChildren(QPushButton)] == ["重扫", "作废"]  # 原图不完整：只能重扫


# ---------- 10. 多选文件、PDF 重扫 ----------

def test_pick_multiple_files(page, monkeypatch, tmp_path):
    files = [str(tmp_path / "a_1.jpg"), str(tmp_path / "a_2.jpg")]
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(lambda *a, **k: (files, "")))
    page._pick_input_file()
    assert page._input_value() == files and "a_1.jpg" in page.folder_edit.text()
    page.folder_edit.setText(str(tmp_path))
    assert page._input_value() == str(tmp_path)


def test_rescan_accepts_pdf_with_template_page_count(ctrl, qtbot, small_dataset, tmp_path):
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    done = []
    ctrl.sheetDone.connect(done.append)
    ctrl.start(DocumentSource(_copy(small_dataset, tmp_path / "in", ["0003_1.jpg", "0003_2.jpg"]), s.faces))
    qtbot.waitUntil(lambda: len(done) == 1 and ctrl.state == IDLE, timeout=60000)
    assert not done[0].ok
    three = scans_to_pdf(tmp_path / "three.pdf", sorted(small_dataset.glob("*.jpg"))[:3])
    with pytest.raises(RuntimeError, match="正好 2 页"):
        ctrl.rescan(1, [three])
    pdf = scans_to_pdf(tmp_path / "fix.pdf", sorted(small_dataset.glob("0001_*.jpg")))
    ctrl.rescan(1, [pdf])
    qtbot.waitUntil(lambda: len(done) == 2 and ctrl.state == IDLE, timeout=60000)
    assert done[-1].ok and s.sheets[1]["input_pages"][0]["kind"] == "pdf"


# ---------- 11. stop 与运行结束的竞态 ----------

def test_stop_racing_run_end_does_not_stick_in_stopping(ctrl):
    class Source:
        inner = object()
        mode = "file"

        def stop(self):
            # 后台恰好在此刻结束运行：没有锁时它先改成就绪，随后 stop 又改回“停止收尾中”并永久停在那里
            self.thread = threading.Thread(target=ctrl._finish_operation, args=(self,))
            self.thread.start()
            self.thread.join(0.3)

    src = Source()
    with ctrl._state_lock:
        ctrl._source, ctrl._operation = src, "scan"
        ctrl.state = RUNNING
    ctrl.stop()
    src.thread.join(5)
    assert ctrl.state == IDLE and ctrl._source is None and not ctrl.scanning


# ---------- 12. 手动定位的点击自检 ----------

def test_manual_alignment_rejects_inconsistent_clicks(ctx):
    _images, transforms, _ = _tilted(ctx, 41, 12.0)
    spec = ctx.tpl.faces["front"].align
    good = _clicks(ctx, "front", transforms[0])
    assert manual_alignment(spec, good).ok
    s = ctx.tpl.dpi / 25.4
    bad = list(good)
    bad[1] = [bad[1][0] + 12 * s, bad[1][1]]
    res = manual_alignment(spec, bad)
    assert not res.ok and "可能点错" in res.reason and "定位块" in res.reason


def test_three_point_manual_alignment_warns_cannot_self_check(ctx):
    images, transforms, _ = _tilted(ctx, 41, 12.0, back_deg=1.0)  # 背面交给自动定位
    manual = {"front": {"image": 0, "points": _clicks(ctx, "front", transforms[0], skip=(3,))}}
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert a.placements["front"].align.missing == (3,)
    msg = next(w["msg"] for w in decide_sheet(ctx, a, Thresholds()).warnings if w["code"] == "ALIGN_MANUAL")
    assert "无法自检" in msg
    refs = reference_points(ctx.tpl.faces["front"].align)
    assert len(refs) == 4
    with pytest.raises(PhysicalError):
        bad = _clicks(ctx, "front", transforms[0])
        bad[0] = [bad[0][0] + 600, bad[0][1]]
        analyze_sheet(ctx, images, [300, 300], manual={"front": {"image": 0, "points": bad}})


# ---------- 13. 其他 ----------

def test_legend_and_leaving_batch_with_exceptions(page, ctrl, no_dialogs, tmp_path):
    assert (VOID, "已作废") in MosaicLegend.ITEMS
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    page._on_done(SheetEvent(1, "x", False, "BLANK_PAGE", "", None, 0), animate=False)
    no_dialogs.question_answer = QMessageBox.StandardButton.No
    page.new_batch()
    assert ctrl.session is s and "1 张物理异常未处理" in _hints(no_dialogs)[-1]
    assert page.set_exam(exam) is False and ctrl.session is s
    no_dialogs.question_answer = QMessageBox.StandardButton.Yes
    page.new_batch()
    assert ctrl.session is None


def test_skipped_inputs_are_reported(page):
    page._run_note = None
    page._on_skipped(SheetSkipped("0001", 1))
    page._on_skipped(SheetSkipped("0002", 2))
    assert "跳过 2 份已导入的答卷" in page.notice.text() and "#0001、#0002" in page.notice.text()
