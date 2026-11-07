import json
import os
import threading
import time

import pytest

from scan_answer.output.jsonfile import file_lock, read_json, update_json
from scan_answer.output.local import write_json_atomic


def test_update_json_serializes_concurrent_writers(tmp_path):
    p = tmp_path / "r.json"
    write_json_atomic(p, {"n": 0})

    def bump():
        for _ in range(20):
            update_json(p, lambda d: d.update(n=d["n"] + 1))

    ts = [threading.Thread(target=bump) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert read_json(p)["n"] == 100
    assert not list(tmp_path.glob("*.lock")) and not list(tmp_path.glob("*.tmp"))


def test_update_json_accepts_returned_dict(tmp_path):
    p = tmp_path / "r.json"
    write_json_atomic(p, {"a": 1})
    assert update_json(p, lambda d: {"b": 2}) == {"b": 2}
    assert json.loads(p.read_text(encoding="utf-8")) == {"b": 2}


def test_stale_lock_is_broken_and_live_lock_times_out(tmp_path):
    p = tmp_path / "r.json"
    lock = tmp_path / "r.json.lock"
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with file_lock(p, timeout=1):  # 过期锁被清除
        pass
    lock.write_text("")
    with pytest.raises(TimeoutError):
        with file_lock(p, timeout=0.2):
            pass
