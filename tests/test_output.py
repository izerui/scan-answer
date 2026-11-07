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


def test_write_json_atomic_retries_permission_error(tmp_path, monkeypatch):
    """Windows 上目标文件被读者占用时 os.replace 抛 PermissionError：退避重试。"""
    import os

    import pytest

    from scan_answer.output import local

    monkeypatch.setattr(local.time, "sleep", lambda s: None)
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("被占用")
        return real(src, dst)

    monkeypatch.setattr(local.os, "replace", flaky)
    p = tmp_path / "m.json"
    local.write_json_atomic(p, {"a": 1})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1} and calls["n"] == 3
    assert not list(tmp_path.glob("*.tmp"))

    def always(src, dst):
        raise PermissionError("一直被占用")

    monkeypatch.setattr(local.os, "replace", always)
    with pytest.raises(PermissionError):
        local.write_json_atomic(p, {"a": 2})
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
