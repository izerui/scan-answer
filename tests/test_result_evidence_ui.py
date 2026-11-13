"""Result evidence, lazy decoding, and read-only delivery UI contracts."""
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtWidgets import QPushButton

from scan_answer.app.results import StudentRow, load_rows
from scan_answer.app.settings import AppSettings
from scan_answer.app.trial import TrialSheet
from scan_answer.ui.editor.trial_preview import TrialPreview
from scan_answer.ui.pages.results_page import ResultsPage
from conftest import TEMPLATE_DIR, settle, track_image_reads


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def batch(tmp_path):
    result = {
        "pages": {"front": "front.png"},
        "student": {"fields": {"school": {"label": "School", "image": "school.png",
                                         "text": None, "ocr_status": "failed"}}},
        "objective": [],
        "subjective": [{"no": "2", "sub": "a", "images": ["sub1.png", "sub2.png"],
                        "ocr_status": "failed", "text": None}],
        "input_pages": [{"kind": "pdf", "source": "exam.pdf", "page": 3}],
    }
    result_path = tmp_path / "students" / "0001" / "result.json"
    write_json(result_path, result)
    for name in ("front", "school", "sub1", "sub2"):
        Image.new("L", (31, 43), 180).save(result_path.parent / f"{name}.png")
    write_json(tmp_path / "template.json",
               json.loads((TEMPLATE_DIR / "template.json").read_text()))
    write_json(tmp_path / "manifest.json", {"sheets": [
        {"seq": 1, "status": "ok", "warnings": 0, "source": "exam.pdf",
         "result": "students/0001/result.json"},
        {"seq": 2, "status": "physical", "source": "missing.pdf"},
    ]})
    return tmp_path


def test_ocr_only_failure_is_a_warning_with_legacy_row_constructor(qtbot, batch):
    assert StudentRow(1, "", "ok", None, 0, None, None, "").ocr_failed == 0
    rows = load_rows(batch)
    assert rows[0].ocr_failed == 2
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch)
    settle()  # 批次在后台读取
    settle()  # 转写状态在后台统计
    page.filter.set_value("warn")
    assert [r.seq for r in page.visible_rows()] == [1]
    page._refill()
    assert page.model.item(0, 2).text()


def test_shared_preview_covers_all_evidence_and_sources(qtbot):
    result = {
        "pages": {"front": "full", "raw_1": "raw"},
        "student": {"exam_no": {"image": "exam", "value": "123"},
                    "omr_fields": {"group": {"image": "omr", "value": "A"}},
                    "barcodes": {"student": {"image": "bc1"}, "book": {"image": "bc2"}},
                    "fields": {"school": {"image": "field", "text": "School"}}},
        "objective": [{"no": "1", "image": "obj", "answer": "B"}],
        "subjective": [{"no": "2", "sub": "a", "regions": [
            {"image": "sub1", "face": "front"}, {"image": "sub2", "face": "back"}]}],
        "input_pages": [{"source": "exam.pdf", "page": 7, "kind": "pdf"}],
    }
    paths = ["full", "raw", "exam", "omr", "bc1", "bc2", "field", "obj", "sub1", "sub2"]
    preview = TrialPreview()
    qtbot.addWidget(preview)
    preview.set_sheet(TrialSheet(["exam.pdf"], True, "", None, {}, result=result,
                                 images={p: np.ones((5, 9), dtype=np.uint8) for p in paths}))
    assert {preview.image_combo.itemData(i) for i in range(preview.image_combo.count())} == set(paths)
    preview.image_combo.setCurrentIndex(preview.image_combo.findData("obj"))
    assert "1" in preview.image_combo.currentText() and "B" in preview.summary.text()
    preview.image_combo.setCurrentIndex(preview.image_combo.findData("sub2"))
    assert "2" in preview.image_combo.currentText() and "a" in preview.image_combo.currentText()
    assert "exam.pdf" in preview.sources.text() and "7" in preview.sources.text()
    assert json.loads(preview.json_view.toPlainText()) == result


def test_disk_preview_decodes_only_selected_and_clears_missing(qtbot, batch, monkeypatch):
    from scan_answer.ui.widgets.evidence_preview import EvidencePreview
    preview = EvidencePreview()
    qtbot.addWidget(preview)
    path = batch / "students/0001/result.json"
    reads = track_image_reads(monkeypatch)
    preview.set_result(json.loads(path.read_text()), base=path.parent)
    assert [Path(p).name for p in reads] == ["front.png"]
    preview.image_combo.setCurrentIndex(preview.image_combo.findData("sub2.png"))
    assert [Path(p).name for p in reads] == ["front.png", "sub2.png"]
    settle()
    assert preview.view.image_size() == (31, 43)
    (path.parent / "school.png").unlink()
    preview.image_combo.setCurrentIndex(preview.image_combo.findData("school.png"))
    settle()
    assert preview.view.image_size() == (0, 0)
    assert preview.summary.text()
    preview.set_result(None)
    assert preview.view.image_size() == (0, 0)
    assert not preview.json_view.toPlainText() and not preview.sources.text()


def test_results_card_clears_on_unavailable_face_and_new_row(qtbot, batch):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()
    assert page.card.image_size() == (31, 43)
    page._set_face("back")
    assert page.card.image_size() == (0, 0)
    assert page.select_seq(2)
    assert page.evidence.view.image_size() == (0, 0)
    assert not page.evidence.json_view.toPlainText()


def test_results_evidence_replaces_eager_subjective_images(qtbot, batch, monkeypatch):
    page = ResultsPage()
    qtbot.addWidget(page)
    reads = track_image_reads(monkeypatch)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    assert not any(Path(p).name.startswith("sub") for p in reads)
    assert page.evidence.image_combo.findData("school.png") >= 0
    buttons = [b for b in page.findChildren(QPushButton) if "看切图" in b.text()]
    assert buttons
    buttons[0].click()
    settle()
    assert page.evidence.view.image_size() == (31, 43)
    assert "sub" in str(page.evidence.image_combo.currentData())


@pytest.mark.parametrize("blocked", ["sealed", "active"])
def test_retry_guards_cover_batch_and_item_buttons(qtbot, batch, monkeypatch, no_dialogs, blocked):
    manifest_path = batch / "manifest.json"
    if blocked == "sealed":
        manifest = json.loads(manifest_path.read_text())
        manifest["delivery"] = {"sealed": True}
        write_json(manifest_path, manifest)
    settings = AppSettings()
    settings.ocr = {"base_url": "http://unused", "model": "unused", "api_key": "unused"}
    page = ResultsPage(settings)
    qtbot.addWidget(page)
    if blocked == "active":
        page._active_batch_fn = lambda: str(batch / ".")
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    assert not page.retry_all_btn.isEnabled()
    item_buttons = [b for b in page.findChildren(QPushButton) if b.text() == "重试转写"]
    assert item_buttons and all(not b.isEnabled() for b in item_buttons)

    def forbidden(*args, **kwargs):
        pytest.fail("A read-only or active batch must not create an OCR queue")

    monkeypatch.setattr("scan_answer.ui.pages.results_page.OcrQueue", forbidden)
    page._retry_single("subjective", "2:a")
    settle()
    page._retry_all_failed()
    settle()


def test_missing_result_and_filter_clear_previous_evidence(qtbot, batch):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    (batch / "students/0001/result.json").unlink()
    assert page.select_seq(1)
    assert page.card.image_size() == (0, 0)
    assert page.evidence.view.image_size() == (0, 0)
    page.search.setText("no match")
    assert page.current is None and page.result is None


def test_ocr_completion_refreshes_warning_filter(qtbot, batch):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    assert "转写失败" in page.anomalies.text()
    page.filter.set_value("warn")
    page._refill()
    assert page.select_seq(1)
    path = batch / "students/0001/result.json"
    result = json.loads(path.read_text())
    result["subjective"][0]["ocr_status"] = "done"
    result["student"]["fields"]["school"]["ocr_status"] = "done"
    write_json(path, result)
    page._retry_done(str(path), True, "done")
    settle()
    assert not page.visible_rows()
    assert page.model.rowCount() == 0
    assert page.evidence.view.image_size() == (0, 0)


def test_delivery_panel_receives_batch_and_live_busy_callback(qtbot, batch, monkeypatch):
    import sys
    from types import ModuleType
    from PySide6.QtWidgets import QWidget

    # The parent owns this module; exercise the agreed integration protocol.
    module = ModuleType("scan_answer.ui.widgets.delivery_panel")

    class DeliveryPanel(QWidget):
        def set_batch(self, path, sealed=None):
            self.batch = Path(path)

    module.DeliveryPanel = DeliveryPanel
    monkeypatch.setitem(sys.modules, module.__name__, module)
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch)
    settle()  # 批次在后台读取
    assert page.delivery_panel.batch == batch
    assert page.delivery_panel.active_batch_fn() is None
    page._active_batch_fn = lambda: str(batch)
    assert page.delivery_panel.active_batch_fn() == str(batch)
    assert page.delivery_panel.parentWidget() is not None


def test_corrupt_image_clears_previous_selection(qtbot, batch):
    from scan_answer.ui.widgets.evidence_preview import EvidencePreview
    preview = EvidencePreview()
    qtbot.addWidget(preview)
    path = batch / "students/0001/result.json"
    preview.set_result(json.loads(path.read_text()), base=path.parent)
    settle()
    assert preview.view.image_size() == (31, 43)
    (path.parent / "sub2.png").write_bytes(b"broken")
    preview.select_image("sub2.png")
    settle()
    assert preview.view.image_size() == (0, 0)


@pytest.mark.parametrize("action", ["single", "all"])
def test_retry_rechecks_seal_after_open(qtbot, batch, monkeypatch, no_dialogs, action):
    page = ResultsPage(AppSettings())
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    manifest = json.loads((batch / "manifest.json").read_text())
    manifest["delivery"] = {"sealed": True}
    write_json(batch / "manifest.json", manifest)

    def forbidden(*args, **kwargs):
        pytest.fail("Sealing after navigation must still block OCR")

    monkeypatch.setattr("scan_answer.ui.pages.results_page.OcrQueue", forbidden)
    if action == "single":
        page._retry_single("subjective", "2:a")
    else:
        page._retry_all_failed()
    settle()  # 转写前在后台重新读 manifest
    assert "只读" in no_dialogs[-1][1]


@pytest.fixture(params=["absolute", "traversal", "symlink", "symlink_dir", "windows"])
def escaped_image(batch, tmp_path_factory, request):
    outside = tmp_path_factory.mktemp("outside_evidence") / "private.png"
    Image.new("L", (17, 23), 90).save(outside)
    base = batch / "students/0001"
    if request.param == "absolute":
        return str(outside)
    if request.param == "traversal":
        import os
        return os.path.relpath(outside, base)
    if request.param == "symlink":
        (base / "escape.png").symlink_to(outside)
        return "escape.png"
    if request.param == "symlink_dir":
        (base / "escape").symlink_to(outside.parent, target_is_directory=True)
        return "escape/private.png"
    return r"C:\private\image.png"


def test_standalone_preview_blocks_escape_before_image_read(qtbot, batch, escaped_image, monkeypatch):
    from scan_answer.ui.widgets.evidence_preview import EvidencePreview
    preview = EvidencePreview()
    qtbot.addWidget(preview)
    preview.set_result({"pages": {"front": "front.png", "back": escaped_image}},
                       base=batch / "students/0001")
    settle()
    assert preview.view.image_size() == (31, 43)
    reads = track_image_reads(monkeypatch, allow=lambda _p: False)
    preview.select_image(escaped_image)
    settle()
    assert reads == []
    assert preview.view.image_size() == (0, 0)
    assert "路径无效" in preview.summary.text()


@pytest.mark.parametrize("surface", ["card", "subjective"])
def test_results_images_block_escape_before_image_read(qtbot, batch, escaped_image, monkeypatch, surface):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    safe = (batch / "students/0001/front.png").resolve()
    # Only the initial legitimate page may reach the decoder.
    reads = track_image_reads(monkeypatch, allow=lambda path: Path(path).absolute() == safe)
    if surface == "card":
        page.result["pages"]["front"] = escaped_image
        page._show_card()
        settle()
        assert page.card.image_size() == (0, 0)
    else:
        page.result["subjective"][0]["images"] = [escaped_image]
        page._show()
        rows = [page.subj_box.itemAt(i).widget() for i in range(page.subj_box.count())]
        button = next(b for w in rows if w is not None for b in [w, *w.findChildren(QPushButton)]
                      if isinstance(b, QPushButton) and "看切图" in b.text())
        button.click()
        settle()
        assert page.evidence.view.image_size() == (0, 0)
        assert "路径无效" in page.evidence.summary.text()
    assert reads == []


def test_explicit_root_allows_raw_parent_paths_but_default_base_does_not(qtbot, batch, monkeypatch):
    from scan_answer.ui.widgets.evidence_preview import EvidencePreview
    raw = batch / "raw" / "original.png"
    raw.parent.mkdir()
    Image.new("L", (19, 29), 120).save(raw)
    reference = "../../raw/original.png"
    result = {"pages": {"front": reference}}
    preview = EvidencePreview()
    qtbot.addWidget(preview)
    preview.set_result(result, base=batch / "students/0001", root=batch)
    settle()
    assert preview.view.image_size() == (19, 29)
    reads = track_image_reads(monkeypatch, allow=lambda _p: False)
    preview.set_result(result, base=batch / "students/0001")
    settle()
    assert reads == []
    assert preview.view.image_size() == (0, 0)
    assert "路径无效" in preview.summary.text()


@pytest.mark.parametrize("reference", ["../../raw/original.png", r"..\..\raw\original.png"])
def test_results_pass_batch_root_for_legitimate_raw_images(qtbot, batch, reference):
    raw = batch / "raw" / "original.png"
    raw.parent.mkdir()
    Image.new("L", (19, 29), 120).save(raw)
    path = batch / "students/0001/result.json"
    result = json.loads(path.read_text())
    result["pages"]["front"] = reference
    write_json(path, result)
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()
    assert page.card.image_size() == (19, 29)
    assert page.evidence.view.image_size() == (19, 29)


def test_physical_raw_fallback_blocks_external_symlink(qtbot, batch, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp("outside_raw") / "private.png"
    Image.new("L", (17, 23), 90).save(outside)
    raw = batch / "raw"
    raw.mkdir()
    (raw / "0002_front.png").symlink_to(outside)
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    reads = track_image_reads(monkeypatch, allow=lambda _p: False)
    assert page.select_seq(2)
    settle()
    assert reads == []
    assert page.card.image_size() == (0, 0)


def test_subjective_text_is_not_interpreted_as_external_image_markup(qtbot, batch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    markup = '<img src="file:///outside/private.png">'
    page.result["subjective"][0]["text"] = markup
    page._fill_subjective(page.result)
    label = next(page.subj_box.itemAt(i).widget() for i in range(page.subj_box.count())
                 if isinstance(page.subj_box.itemAt(i).widget(), QLabel)
                 and page.subj_box.itemAt(i).widget().text() == markup)
    assert label.textFormat() == Qt.TextFormat.PlainText


def test_trial_memory_keys_are_not_interpreted_as_disk_paths(qtbot, monkeypatch):
    preview = TrialPreview()
    qtbot.addWidget(preview)
    key = "/memory-only/image.png"
    reads = track_image_reads(monkeypatch, allow=lambda _p: False)
    preview.set_sheet(TrialSheet([], True, "", None, {}, result={"pages": {"front": key}},
                                 images={key: np.ones((7, 11), dtype=np.uint8)}))
    assert preview.view.image_size() == (11, 7)
    assert reads == []


def test_failed_samples_display_unknown_absent_and_nullable_fills(qtbot, batch):
    path = batch / "students/0001/result.json"
    result = json.loads(path.read_text())
    result["student"]["absent"] = {"value": None, "fill": None, "status": "failed"}
    result["objective"] = [{"no": "1", "answer": "", "status": "failed", "fill": {"A": None, "B": .3}}]
    result["choose_one"] = [{"key": "c", "label": "Group", "chosen": [], "status": "failed",
                             "questions": ["1"], "fill": {"1": None}}]
    write_json(path, result)
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(batch, select_seq=1)
    settle()  # 批次在后台读取
    assert "缺考 未确定" in page.info.text()
    assert page.answers.item(0, 3).text()


def test_barcode_candidate_images_are_visible_in_preview(qtbot):
    from scan_answer.ui.widgets.evidence_preview import EvidencePreview
    result = {"student": {"barcodes": {"id": {
        "image": "region", "value": None, "status": "ambiguous", "selected_id": None,
        "candidates": [{"id": "a", "value": "123", "image": "hit-a"},
                       {"id": "b", "value": "456", "image": "hit-b"}],
    }}}}
    preview = EvidencePreview()
    qtbot.addWidget(preview)
    preview.set_result(result)
    assert preview.image_combo.findData("hit-a") >= 0
    assert preview.image_combo.findData("hit-b") >= 0
    preview.select_image("hit-b")
    assert "456" in preview.summary.text()

