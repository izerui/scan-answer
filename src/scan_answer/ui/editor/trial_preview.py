"""In-memory previews of the same evidence and JSON produced by a batch."""
from __future__ import annotations

from PySide6.QtWidgets import QLabel

from ...app.trial import TrialSheet
from ..widgets.evidence_preview import EvidencePreview


class TrialPreview(EvidencePreview):
    """Compatibility wrapper for the editor's set_sheet and public controls."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sheet: TrialSheet | None = None
        status = QLabel("试识别结果不会保存，也不算进任何批次")
        status.setObjectName("muted")
        self.layout().insertWidget(0, status)

    def set_sheet(self, sheet: TrialSheet | None) -> None:
        self._sheet = sheet
        self.set_result(sheet.result if sheet else None,
                        images=sheet.images if sheet else None,
                        sources=sheet.sources if sheet else None)
