"""同一批次里多份有效答卷识别出同一考号：交付检查列为说明项，结果页归入“有警告”。"""
import json
import shutil

import pytest

from scan_answer.app.results import load_rows
from scan_answer.output.audit import audit_batch, duplicate_exam_numbers
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.documents import DocumentSource
from conftest import TEMPLATE_DIR


@pytest.fixture(scope="module")
def two_sheet_seed(tmp_path_factory, small_dataset):
    session = BatchSession.create(tmp_path_factory.mktemp("dup-seed"), TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(session) as pipeline:
        pipeline.run(DocumentSource(sorted(small_dataset.glob("*.jpg"))[:4], 2))
        pipeline.finalize()
    return session.batch_dir


@pytest.fixture
def batch(two_sheet_seed, tmp_path):
    path = tmp_path / "working"
    shutil.copytree(two_sheet_seed, path)
    return path


def test_duplicate_exam_numbers_ignores_partial_and_unsettled():
    sheets = [
        {"seq": 1, "status": "ok", "exam_no": "1001"},
        {"seq": 2, "status": "ok", "exam_no": "1001"},
        {"seq": 3, "status": "voided", "exam_no": "1001"},
        {"seq": 4, "status": "ok", "exam_no": "10?1"},
        {"seq": 5, "status": "ok", "exam_no": "10?1"},
        {"seq": 6, "status": "ok", "exam_no": None},
        {"seq": 7, "status": "ok", "exam_no": None},
        {"seq": 8, "status": "ok", "exam_no": "2002"},
    ]
    assert duplicate_exam_numbers(sheets) == {"1001": [1, 2]}


def _set_exam_no(batch, value):
    manifest = json.loads((batch / "manifest.json").read_text())
    for sheet in manifest["sheets"]:
        sheet["exam_no"] = value
        result = batch / sheet["result"]
        data = json.loads(result.read_text())
        data["student"]["exam_no"]["value"] = value
        result.write_text(json.dumps(data))
    (batch / "manifest.json").write_text(json.dumps(manifest))
    return [s["seq"] for s in manifest["sheets"]]


def test_audit_and_results_flag_duplicate_exam_no(batch):
    seqs = _set_exam_no(batch, "100015000123456")
    report = audit_batch(batch)
    dup = [i for i in report.issues if i["code"] == "exam_no_duplicate"]
    assert len(dup) == 1 and dup[0]["severity"] == "warning"
    assert "100015000123456" in dup[0]["message"]
    rows = {r.seq: r for r in load_rows(batch)}
    assert rows[seqs[0]].duplicate_of == (seqs[1],)
    assert rows[seqs[1]].duplicate_of == (seqs[0],)


def test_distinct_exam_numbers_are_not_flagged(batch):
    assert not any(i["code"] == "exam_no_duplicate" for i in audit_batch(batch).issues)
    assert all(r.duplicate_of == () for r in load_rows(batch))
