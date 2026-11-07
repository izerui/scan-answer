import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json
from scan_answer.pipeline.events import FinalizedEvent, SheetEvent, SheetQueued
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.folder import FolderSource

from conftest import TEMPLATE_DIR


def test_queued_before_done_with_result_path(small_dataset, tmp_path):
    events = []
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(FolderSource(small_dataset, s.faces))
    kinds = [(type(e).__name__, e.seq) for e in events if isinstance(e, (SheetQueued, SheetEvent))]
    for seq in range(1, 5):
        assert kinds.index(("SheetQueued", seq)) < kinds.index(("SheetEvent", seq))
    done = {e.seq: e for e in events if isinstance(e, SheetEvent)}
    assert done[3].ok is False and done[3].result is None
    assert done[1].result and read_json(done[1].result)["seq"] == 1


def test_finalized_lists_changed_seqs(small_dataset, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    s.thresholds = Thresholds(0.97, 0.5)
    with BatchPipeline(s) as p:
        p.run(FolderSource(small_dataset, s.faces))
        ev = p.finalize()
    assert isinstance(ev, FinalizedEvent)
    assert ev.changed == len(ev.changed_seqs) and sorted(ev.changed_seqs) == [1, 2, 4]
