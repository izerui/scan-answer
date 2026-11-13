import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from conftest import save_now, settle, wait_ctrl
from scan_answer.app import library
from scan_answer.app.library import ExamInfo, create_exam, list_exams, load_exam
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.session import BatchSession
from scan_answer.template import load_template


@pytest.fixture
def source_template(tmp_path):
    directory = tmp_path / "source"
    directory.mkdir()
    (directory / "assets").mkdir()
    Image.fromarray(np.full((118, 118), 255, dtype=np.uint8)).save(directory / "assets" / "blank.png")
    write_json_atomic(directory / "template.json", {
        "schema_version": "1.0", "name": "Snapshot card", "version": 3,
        "paper": [10, 10], "dpi": 300,
        "faces": {"front": {"image": "assets/blank.png",
                            "align": {"method": "frames", "frames": [[1, 1, 8, 8]]}}},
        "exam_no": None, "absent": None, "barcodes": [], "fields": [], "objective": [],
        "subjective": [], "extension": {"preserved": True},
    })
    return directory


def change_template(directory, **changes):
    path = directory / "template.json"
    data = json.loads(path.read_text())
    data.update(changes)
    write_json_atomic(path, data)


def legacy_exam(tmp_path, source):
    directory = tmp_path / "out" / "legacy"
    write_json_atomic(directory / "exam.json", {
        "info": {"name": "Legacy exam", "extra": {"school": "A"}},
        "template_dir": str(source), "created": "2026-11-07T10:00:00+08:00",
        "extension": {"preserved": True},
    })
    return load_exam(directory)


def test_new_exam_pins_complete_template_and_metadata(source_template, tmp_path):
    original = json.loads((source_template / "template.json").read_text())
    exam = create_exam(tmp_path / "out", ExamInfo("Exam"), source_template)
    assert exam.template_dir == exam.directory / "template"
    meta = json.loads((exam.directory / "exam.json").read_text())
    assert meta["template_dir"] == "template"
    assert meta["template"]["name"] == "Snapshot card"
    assert meta["template"]["version"] == 3
    assert Path(meta["template"]["source"]) == source_template
    assert json.loads((exam.template_dir / "template.json").read_text()) == original
    assert (exam.template_dir / "assets/blank.png").read_bytes() == (source_template / "assets/blank.png").read_bytes()
    shutil.rmtree(source_template)
    loaded = load_exam(exam.directory)
    assert load_template(loaded.template_dir)[0].version == 3
    assert "Snapshot card" in loaded.template_label and "v3" in loaded.template_label


def test_snapshot_relative_reference_survives_moving_exam(source_template, tmp_path):
    exam = create_exam(tmp_path / "out", ExamInfo("Exam"), source_template)
    moved = tmp_path / "moved"
    shutil.move(exam.directory, moved)
    loaded = load_exam(moved)
    assert loaded.template_dir == moved / "template"
    assert library.ensure_exam_snapshot(loaded) == moved / "template"


@pytest.mark.parametrize("failure", ["missing_image", "bad_json", "publish"])
def test_failed_creation_leaves_no_half_exam(source_template, tmp_path, monkeypatch, failure):
    if failure == "missing_image":
        (source_template / "assets/blank.png").unlink()
    elif failure == "bad_json":
        (source_template / "template.json").write_text("{")
    else:
        def fail_publish(*args, **kwargs):
            raise OSError("publication failed")
        monkeypatch.setattr(library, "write_json_atomic", fail_publish)
    root = tmp_path / "out"
    with pytest.raises((OSError, ValueError)):
        create_exam(root, ExamInfo("Exam"), source_template)
    assert list_exams(root) == []
    assert not root.exists() or list(root.iterdir()) == []


@pytest.mark.parametrize("changes, message", [
    pytest.param({"dpi": 0}, "DPI", id="invalid_dpi"),
    pytest.param({"objective": [{
        "name": "Questions", "qtype": "single", "start": 1,
        "grid": {"face": "front", "labels": ["A", "B"], "cells": [[[1, 1, 1, 1]]]},
    }]}, "格数", id="invalid_grid"),
    pytest.param({"choose_one": [{
        "key": "choice", "label": "Choice", "face": "front",
        "questions": ["1", "2"], "cells": [[1, 1, 1, 1], [3, 1, 1, 1]],
    }]}, "不在客观题或主观题中", id="invalid_choice"),
])
@pytest.mark.parametrize("workflow", ["new", "legacy_source", "legacy_history"])
def test_readable_invalid_template_is_rejected_before_snapshot_publication(
        source_template, tmp_path, changes, message, workflow):
    root = tmp_path / "out"
    target = source_template
    if workflow != "new":
        exam = legacy_exam(tmp_path, source_template)
        if workflow == "legacy_history":
            historic = BatchSession.create(exam.directory, source_template, batch_id="historic")
            target = historic.template_dir
        before = (exam.directory / "exam.json").read_bytes()
        entries = set(exam.directory.iterdir())
    change_template(target, **changes)
    load_template(target)  # 文件可读，但结构不适合固定为考试模板。

    with pytest.raises(ValueError, match=message):
        if workflow == "new":
            create_exam(root, ExamInfo("Invalid exam"), source_template)
        else:
            library.ensure_exam_snapshot(exam)

    if workflow == "new":
        assert list_exams(root) == []
        assert not root.exists() or list(root.iterdir()) == []
    else:
        assert (exam.directory / "exam.json").read_bytes() == before
        assert set(exam.directory.iterdir()) == entries
        assert exam.template_dir == source_template
        assert not (exam.directory / "template").exists()
        assert load_exam(exam.directory).info.extra == {"school": "A"}


def test_listing_does_not_migrate_legacy_exam(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    before = (exam.directory / "exam.json").read_bytes()
    assert len(list_exams(exam.directory.parent)) == 1
    assert (exam.directory / "exam.json").read_bytes() == before
    assert not (exam.directory / "template").exists()
    assert load_exam(exam.directory).template_dir == source_template


def test_legacy_without_batches_pins_current_template_once(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    snap = library.ensure_exam_snapshot(exam)
    assert snap == exam.directory / "template"
    assert exam.template_dir == snap
    change_template(source_template, version=4)
    assert library.ensure_exam_snapshot(exam) == snap
    assert load_template(snap)[0].version == 3
    meta = json.loads((exam.directory / "exam.json").read_text())
    assert meta["extension"] == {"preserved": True}
    assert meta["created"] == "2026-11-07T10:00:00+08:00"
    assert load_exam(exam.directory).info.extra == {"school": "A"}


def test_legacy_migration_uses_consistent_history_not_current_source(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    first = BatchSession.create(exam.directory, source_template, batch_id="first")
    BatchSession.create(exam.directory, source_template, batch_id="second")
    change_template(source_template, version=8)
    snap = library.ensure_exam_snapshot(exam)
    assert load_template(snap)[0].version == 3
    assert json.loads((snap / "template.json").read_text()) == json.loads(
        (first.template_dir / "template.json").read_text())
    assert Path(json.loads((exam.directory / "exam.json").read_text())["template"]["source"]) == source_template


def test_history_can_restore_exam_without_source_and_stale_objects_reuse_pin(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    stale = load_exam(exam.directory)
    BatchSession.create(exam.directory, source_template, batch_id="historic")
    shutil.rmtree(source_template)
    snapshot = library.ensure_exam_snapshot(exam)
    before = (exam.directory / "exam.json").read_bytes()
    assert library.ensure_exam_snapshot(stale) == snapshot
    assert stale.template_dir == snapshot
    assert (exam.directory / "exam.json").read_bytes() == before
    assert load_template(snapshot)[0].version == 3


@pytest.mark.parametrize("source_name", ["source", "template"])
def test_legacy_relative_source_paths_keep_original_working_directory_semantics(
        source_template, tmp_path, monkeypatch, source_name):
    if source_name != source_template.name:
        source_template.rename(tmp_path / source_name)
    monkeypatch.chdir(tmp_path)
    exam = legacy_exam(tmp_path, Path(source_name))
    assert exam.template_dir == Path(source_name)
    assert load_template(library.ensure_exam_snapshot(exam))[0].version == 3


def test_history_without_complete_snapshot_never_falls_back_to_source(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    historic = BatchSession.create(exam.directory, source_template, batch_id="historic")
    shutil.rmtree(historic.template_dir)
    before = (exam.directory / "exam.json").read_bytes()
    with pytest.raises(ValueError, match="新建考试"):
        library.ensure_exam_snapshot(exam)
    assert (exam.directory / "exam.json").read_bytes() == before
    assert not (exam.directory / "template").exists()


@pytest.mark.parametrize("conflict", ["version", "content", "image", "missing", "malformed"])
def test_ambiguous_history_is_rejected_without_mutation(source_template, tmp_path, conflict):
    exam = legacy_exam(tmp_path, source_template)
    BatchSession.create(exam.directory, source_template, batch_id="first")
    second = BatchSession.create(exam.directory, source_template, batch_id="second")
    if conflict == "version":
        change_template(second.template_dir, version=4)
    elif conflict == "content":
        change_template(second.template_dir, fields=[{"key": "name", "label": "Name",
                                                      "face": "front", "rect": [1, 1, 2, 2]}])
    elif conflict == "image":
        Image.fromarray(np.zeros((118, 118), dtype=np.uint8)).save(second.template_dir / "assets/blank.png")
    elif conflict == "missing":
        (second.template_dir / "assets/blank.png").unlink()
    else:
        (second.batch_dir / "manifest.json").write_text("{")
    before = (exam.directory / "exam.json").read_bytes()
    with pytest.raises(ValueError, match="新建考试"):
        library.ensure_exam_snapshot(exam)
    assert (exam.directory / "exam.json").read_bytes() == before
    assert exam.template_dir == source_template
    assert not (exam.directory / "template").exists()


def test_equivalent_history_accepts_different_asset_names(source_template, tmp_path):
    exam = legacy_exam(tmp_path, source_template)
    BatchSession.create(exam.directory, source_template, batch_id="first")
    second = BatchSession.create(exam.directory, source_template, batch_id="second")
    data = json.loads((second.template_dir / "template.json").read_text())
    (second.template_dir / "assets/blank.png").rename(second.template_dir / "other.png")
    data["faces"]["front"]["image"] = "other.png"
    (second.template_dir / "template.json").write_text(json.dumps(data, sort_keys=True))
    assert load_template(library.ensure_exam_snapshot(exam))[0].version == 3


def test_failed_migration_preserves_legacy_and_can_retry(source_template, tmp_path, monkeypatch):
    exam = legacy_exam(tmp_path, source_template)
    before = (exam.directory / "exam.json").read_bytes()
    with monkeypatch.context() as patch:
        def fail_publish(*args, **kwargs):
            raise OSError("publication failed")
        patch.setattr(library, "write_json_atomic", fail_publish)
        with pytest.raises(OSError, match="publication failed"):
            library.ensure_exam_snapshot(exam)
    assert (exam.directory / "exam.json").read_bytes() == before
    assert exam.template_dir == source_template
    assert list(exam.directory.iterdir()) == [exam.directory / "exam.json"]
    assert load_template(library.ensure_exam_snapshot(exam))[0].version == 3


def test_missing_pinned_snapshot_never_upgrades_from_source(source_template, tmp_path):
    exam = create_exam(tmp_path / "out", ExamInfo("Exam"), source_template)
    assert exam.template_dir != source_template
    shutil.rmtree(exam.template_dir)
    change_template(source_template, version=4)
    with pytest.raises((OSError, ValueError)):
        library.ensure_exam_snapshot(load_exam(exam.directory))
    assert not (exam.directory / "template").exists()


def test_controller_migrates_before_new_batch_and_keeps_all_batches_pinned(
        source_template, tmp_path, qtbot):
    from scan_answer.ui.controller import ScanController
    exam = legacy_exam(tmp_path, source_template)
    BatchSession.create(exam.directory, source_template, batch_id="historic")
    change_template(source_template, version=4)
    ctrl = ScanController(workers=0)
    try:
        first = ctrl.new_batch(exam)
        change_template(source_template, version=5)
        second = ctrl.new_batch(exam)
        assert first.manifest["template"]["version"] == second.manifest["template"]["version"] == 3
        assert first.template_dir != second.template_dir
        assert first.manifest["exam"]["extra"] == {"school": "A"}
    finally:
        ctrl.shutdown()


def test_controller_conflict_keeps_current_session_and_creates_no_batch(source_template, tmp_path, qtbot):
    from scan_answer.ui.controller import ScanController
    current = create_exam(tmp_path / "out", ExamInfo("Current"), source_template)
    legacy = legacy_exam(tmp_path, source_template)
    BatchSession.create(legacy.directory, source_template, batch_id="first")
    change_template(source_template, version=4)
    BatchSession.create(legacy.directory, source_template, batch_id="second")
    before = set(legacy.directory.iterdir())
    ctrl = ScanController(workers=0)
    try:
        session = ctrl.new_batch(current)
        with pytest.raises(ValueError, match="新建考试"):
            ctrl.new_batch(legacy)
        assert ctrl.session is session
        assert set(legacy.directory.iterdir()) == before
    finally:
        ctrl.shutdown()


def test_exam_page_preserves_template_selection_and_can_select_saved_template(
        source_template, tmp_path, app_home, qtbot):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.exam_page import ExamPage
    earlier = tmp_path / "earlier"
    shutil.copytree(source_template, earlier)
    change_template(earlier, name="Earlier card")
    settings = AppSettings(output_root=str(tmp_path / "out"),
                           template_dirs=[str(source_template), str(earlier)])
    page = ExamPage(settings)
    qtbot.addWidget(page)
    settle()  # 考试页在后台读取模板与考试
    page.f_template.setCurrentIndex(page.f_template.findData(str(source_template)))
    assert page.f_template.currentIndex() > 0
    page.refresh()
    settle()  # 考试页在后台刷新
    assert page.f_template.currentData() == str(source_template)
    saved = tmp_path / "saved"
    shutil.copytree(source_template, saved)
    change_template(saved, name="Another card")
    settings.template_dirs.append(str(saved))
    page.refresh(selected_template=saved)
    settle()
    assert page.f_template.currentData() == str(saved)
    page.refresh()
    settle()  # 考试页在后台刷新
    assert page.f_template.currentData() == str(saved)


def test_template_labels_use_exam_and_active_batch_metadata(source_template, tmp_path, app_home, qtbot):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.controller import ScanController
    from scan_answer.ui.main_window import MainWindow
    exam = create_exam(tmp_path / "out", ExamInfo("Exam"), source_template)
    settings = AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(source_template)], workers=0)
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    try:
        win.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成
        for text in (win.exam_page.exam_table.item(0, 3).text(),
                     win.scan_page.template_label.text(), win.status_labels[1].text()):
            assert "Snapshot card" in text and "v3" in text
        change_template(source_template, name="Historic card", version=2)
        old = BatchSession.create(exam.directory, source_template, batch_id="historic")
        win.continue_batch(exam, str(old.batch_dir))
        wait_ctrl(ctrl)  # 批次在扫描线程上打开
        for text in (win.scan_page.template_label.text(), win.scan_page.meta_label.text(),
                     win.status_labels[1].text()):
            assert "Historic card" in text and "v2" in text
    finally:
        ctrl.shutdown()


def test_saved_template_signal_selects_saved_directory_and_directory_add_keeps_selection(
        source_template, tmp_path, app_home, no_dialogs, qtbot):
    from scan_answer.app.settings import AppSettings
    from scan_answer.template.doc import TemplateDoc
    from scan_answer.ui.controller import ScanController
    from scan_answer.ui.main_window import MainWindow
    from conftest import TEMPLATE_DIR
    settings = AppSettings(output_root=str(tmp_path / "out"),
                           template_dirs=[str(source_template)], workers=0)
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    settle()  # 考试页在后台读取模板与考试
    try:
        combo = win.exam_page.f_template
        combo.setCurrentIndex(combo.findData(str(source_template)))
        win.template_page._enter(TemplateDoc.load(TEMPLATE_DIR))
        assert save_now(win.template_page), no_dialogs
        settle()
        saved = win.template_page.doc.directory
        assert saved != source_template
        assert combo.currentData() == str(saved)
        assert f"v{win.template_page.doc.template.version}" in combo.currentText()
        win.template_page.close_editor()
        win.template_page.add_dir(str(tmp_path / "another_library"))
        settle()
        assert combo.currentData() == str(saved)
    finally:
        ctrl.shutdown()


def test_exam_page_reports_invalid_template_without_creating_exam(
        source_template, tmp_path, app_home, no_dialogs, qtbot):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.exam_page import ExamPage
    page = ExamPage(AppSettings(output_root=str(tmp_path / "out"),
                               template_dirs=[str(source_template)]))
    qtbot.addWidget(page)
    settle()  # 考试页在后台读取模板与考试
    page.f_name.setText("Exam")
    page.f_template.setCurrentIndex(page.f_template.findData(str(source_template)))
    (source_template / "template.json").write_text("{")
    page._create()
    settle()  # 后台准备（模板检查、创建批次）完成
    assert no_dialogs and no_dialogs[-1][0] == "warning"
    assert list_exams(tmp_path / "out") == []
