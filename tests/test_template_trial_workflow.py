"""Template editing and trial evidence must stay in the same content revision."""
import json

import numpy as np
import pytest

from scan_answer.app.settings import AppSettings
from scan_answer.app.trial import TrialSheet, trial_documents
from scan_answer.template.doc import RegionRef
from scan_answer.ui.pages.template_page import TemplatePage
from conftest import TEMPLATE_DIR


@pytest.fixture
def page(qtbot, app_home, no_dialogs):
    widget = TemplatePage(AppSettings())
    qtbot.addWidget(widget)
    assert widget.open_editor(TEMPLATE_DIR)
    return widget


def test_subjective_target_survives_face_switch_and_explicit_new(page):
    ref = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    page.select_refs([ref])
    n = len(page.doc.template.subjective)
    regions = len(page.doc.template.subjective[ref.index].regions)
    page.set_face("back")
    page._on_region_drawn("subjective", (20, 20, 30, 15))
    assert len(page.doc.template.subjective) == n
    assert len(page.doc.template.subjective[ref.index].regions) == regions + 1
    assert page.doc.template.subjective[ref.index].regions[-1].face == "back"
    page.subjective_target.setCurrentIndex(0)
    page._on_region_drawn("subjective", (30, 40, 30, 15))
    assert len(page.doc.template.subjective) == n + 1
    page.undo()
    assert len(page.doc.template.subjective) == n


def test_region_order_controls_follow_region_across_faces(page):
    ref = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    other = page.doc.add_subjective_region(ref.index, "back", (20, 20, 30, 15))
    page._after_change(select=[ref])
    page.props.regionMoveRequested.emit(other, 0)
    assert page.doc.template.subjective[ref.index].regions[0].face == "back"
    assert page.face == "back"
    assert page.canvas.selected() == [RegionRef("subjective", ref.index, 0)]
    page.undo()
    assert page.doc.template.subjective[ref.index].regions[-1].face == "back"


def test_manual_alignment_selector_is_undoable(page):
    before = page.doc.template.faces["front"].align.method
    page.alignment_combo.setCurrentIndex(page.alignment_combo.findData("frames"))
    assert page.doc.template.faces["front"].align.method == "frames"
    page.undo()
    assert page.doc.template.faces["front"].align.method == before
    assert page.alignment_combo.currentData() == before


def test_edit_invalidates_pending_trial_and_late_callback(page, monkeypatch):
    class DeferredThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr("scan_answer.ui.pages.template_page.threading.Thread", DeferredThread)
    page.run_trial(["sample.jpg"])
    token = page._trial_token
    page._on_region_drawn("field", (10, 10, 20, 5))
    assert not page.trial_running()
    page._trial_done(token, [TrialSheet(["sample.jpg"], True, "", None, {})])
    assert page.trial_list.count() == 0
    assert page._trial_results == []
    assert page.props_stack.currentIndex() == 0
    assert page.trial_btn.isEnabled()


def test_trial_outputs_use_production_schema_and_all_crops(small_dataset, tpl_bases):
    t, bases = tpl_bases
    sheet = trial_documents(t, bases, [str(p) for p in sorted(small_dataset.glob("*.jpg"))[:2]])[0]
    assert sheet.ok, sheet.message
    assert sheet.result["template"] == {"name": t.name, "version": t.version}
    assert len(sheet.result["objective"]) == sum(len(b.grid.cells) for b in t.objective)
    for question in sheet.result["objective"] + sheet.result["subjective"]:
        for image in question["images"]:
            assert sheet.images[image].size > 0
    assert sheet.result["input_pages"] == sheet.input_pages


def test_preview_lists_every_image_and_clears_stale_selection(qtbot):
    from scan_answer.ui.editor.trial_preview import TrialPreview
    preview = TrialPreview()
    qtbot.addWidget(preview)
    result = {"objective": [{"no": "1", "answer": "B", "status": "ok",
                             "images": ["images/objective/q-1.jpg"]}],
              "subjective": [], "student": {}}
    images = {"images/objective/q-1.jpg": np.zeros((31, 47), dtype=np.uint8),
              "images/fields/custom.jpg": np.ones((9, 11), dtype=np.uint8)}
    preview.set_sheet(TrialSheet([], True, "", None, {}, result=result, images=images))
    assert preview.image_combo.count() == 2
    assert preview.view.image_size() == (47, 31)
    assert json.loads(preview.json_view.toPlainText()) == result
    preview.image_combo.setCurrentIndex(1)
    assert preview.view.image_size() == (11, 9)
    preview.set_sheet(None)
    assert preview.image_combo.count() == 0
    assert preview.view.image_size() == (0, 0)
    assert not preview.json_view.toPlainText()


def test_card_view_copies_noncontiguous_array(qtbot):
    from scan_answer.ui.widgets.card_view import CardView
    view = CardView()
    qtbot.addWidget(view)
    image = np.full((20, 40), 123, dtype=np.uint8)[:, ::2]
    assert view.set_array(image)
    image[:] = 0
    assert view.image_size() == (20, 20)
    assert view._pix.pixmap().toImage().pixelColor(0, 0).red() == 123


def test_new_dialog_keeps_mismatched_faces_open(qtbot, monkeypatch):
    from scan_answer.template.auto import ImportedBase
    from scan_answer.ui.editor.new_dialog import NewTemplateDialog
    dialog = NewTemplateDialog()
    qtbot.addWidget(dialog)
    dialog.name_edit.setText("Mismatch")
    dialog.faces = {
        "front": ImportedBase(np.zeros((3508, 2480), dtype=np.uint8), (210., 297.), 0., "none"),
        "back": ImportedBase(np.zeros((2480, 3508), dtype=np.uint8), (297., 210.), 0., "none"),
    }
    monkeypatch.setattr(dialog, "load", lambda: True)
    dialog._create()
    assert dialog.result() == 0
    assert "方向" in dialog.error.text()


def test_completed_trial_expires_on_undo_but_not_save_metadata(page):
    page._on_region_drawn("field", (10, 10, 20, 5))
    page._trial_revision = (page.doc, page.doc.revision)
    page._trial_template = page.doc.template
    sheet = TrialSheet([], True, "", None, {})
    page._trial_done(page._trial_token, [sheet])
    assert page.trial_list.count() == 1
    page.doc.template.version += 1
    page._after_change()
    assert page.trial_list.count() == 1
    page.undo()
    assert page.trial_list.count() == 0
    assert page._trial_current is None


def test_undo_rename_preserves_cross_face_append_target(page):
    ref = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    page.select_refs([ref])
    old_key = page.doc.template.subjective[ref.index].key
    page._on_params(ref, {"no": "999"})
    page.undo()
    assert page.subjective_target.currentData() == old_key
    page.redo()
    assert page.subjective_target.currentData() == "999"
    page.undo()
    count = len(page.doc.template.subjective)
    page.set_face("back")
    page._on_region_drawn("subjective", (20, 20, 30, 15))
    assert len(page.doc.template.subjective) == count
    assert page.doc.template.subjective[ref.index].regions[-1].rect == (20, 20, 30, 15)


def test_rejected_params_and_referenced_delete_preserve_redo(page):
    page.doc.add("choose_one", "front", (20, 20, 30, 15))
    page._after_change()
    page._on_region_drawn("field", (10, 10, 20, 5))
    page.undo()
    before = page.doc.snapshot()
    ref = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    page._on_params(ref, {"no": ""})
    assert page.undo_stack.can_redo()
    assert page.doc.snapshot() == before
    refs = [RegionRef("subjective", ref.index, i)
            for i in range(len(page.doc.template.subjective[ref.index].regions))]
    page._remove(refs)
    assert page.undo_stack.can_redo()
    assert page.doc.snapshot() == before
    page.redo()
    assert len(page.doc.template.fields) == len(before["fields"]) + 1


def test_out_of_paper_append_reports_error_without_changing_history(page):
    page._on_region_drawn("field", (10, 10, 20, 5))
    page.undo()
    ref = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    page.select_refs([ref])
    before, revision = page.doc.snapshot(), page.doc.revision
    page.set_tool("subjective")
    page.canvas.regionDrawn.emit("subjective", (-1, 10, 20, 10))
    assert "无法添加" in page.msg_label.text()
    assert page.doc.snapshot() == before
    assert page.doc.revision == revision
    assert page.undo_stack.can_redo()
    assert page.canvas._tool is None
