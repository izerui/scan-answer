from pathlib import Path

from PySide6.QtGui import QStandardItem
from scan_answer.app.results import StudentRow
from scan_answer.ui.pages.results_page import ResultsPage


def test_retry_callback_refreshes_only_owning_result(qtbot, tmp_path, monkeypatch):
    page = ResultsPage()
    qtbot.addWidget(page)
    current = tmp_path / "students" / "0002" / "result.json"
    page.batch_dir = tmp_path
    page.current = StudentRow(2, "new", "ok", None, 0, current, None, "")
    refreshed = []
    monkeypatch.setattr(page, "_select_row", lambda i: refreshed.append(i))
    page.model.appendRow([QStandardItem("2")])
    page.table.setCurrentIndex(page.model.index(0, 0))
    refreshed.clear()
    page._retry_done(str(tmp_path / "students" / "0001" / "result.json"), True, "done")
    assert refreshed == []


def test_retry_completion_announces_owning_batch_even_after_navigation(qtbot, tmp_path):
    page = ResultsPage()
    qtbot.addWidget(page)
    original = tmp_path / "original"
    page.batch_dir = tmp_path / "new"
    changed = []
    page.ocrChanged.connect(changed.append)
    page._retry_done(str(original), True, "done")
    assert [Path(p) for p in changed] == [original]
