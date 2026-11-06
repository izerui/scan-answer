import json

import numpy as np

from scan_answer.output.local import LocalExporter


def test_student_dir_naming_and_atomic_json(tmp_path):
    ex = LocalExporter(tmp_path / "b")
    p = ex.write_student(7, "100015000123456", {"a": 1}, {"images/x.jpg": np.zeros((10, 10), np.uint8)})
    assert p.parent.name == "0007_100015000123456"
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    assert (p.parent / "images" / "x.jpg").exists()
    assert not list(p.parent.glob("*.tmp"))
    assert ex.student_dir(8, "10001500012345?").name == "0008"
    assert ex.student_dir(9, None).name == "0009"


def test_error_and_manifest(tmp_path):
    ex = LocalExporter(tmp_path / "b")
    e = ex.write_error(3, "BLANK_PAGE", "空白页", [np.zeros((5, 5), np.uint8)])
    assert json.loads(e.read_text(encoding="utf-8"))["code"] == "BLANK_PAGE"
    ex.write_manifest({"sheets": []})
    assert (tmp_path / "b" / "manifest.json").exists()
