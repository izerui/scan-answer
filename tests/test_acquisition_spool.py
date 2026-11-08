import json

import numpy as np
import pytest
from PIL import Image


def spool_module():
    from scan_answer.source import spool

    return spool


def test_pages_are_durable_before_descriptor_is_returned(tmp_path):
    spool = spool_module()
    store = spool.AcquisitionSpool(tmp_path, faces=2, target_seq=10001)
    pixels = np.full((8, 12), 71, dtype=np.uint8)
    page = store.append(1, pixels, 300.0)
    assert page.path.is_relative_to(tmp_path / "acquisition")
    with Image.open(page.path) as image:
        np.testing.assert_array_equal(np.asarray(image), pixels)
    assert page.origin["target_seq"] == 10001
    assert page.origin["acquisition_id"]
    recovered = list(spool.recover_spooled(tmp_path))
    assert len(recovered) == 1
    assert recovered[0].paths == [page.path]
    assert recovered[0].origins == [page.origin]
    assert recovered[0].dpis == [300.0]
    assert recovered[0].images == []


def test_recovery_never_pairs_pages_across_rounds_or_runs(tmp_path):
    spool = spool_module()
    pixels = np.zeros((8, 12), dtype=np.uint8)
    store = spool.AcquisitionSpool(tmp_path, faces=2)
    first = store.append(1, pixels, 200)
    second = store.append(2, pixels, 300)
    third = store.append(2, pixels, 300)
    other = spool.AcquisitionSpool(tmp_path, faces=2)
    fourth = other.append(1, pixels, 400)
    recovered = list(spool.recover_spooled(tmp_path))
    groups = {tuple(s.paths) for s in recovered}
    assert groups == {(first.path,), (second.path, third.path), (fourth.path,)}
    assert len({o["acquisition_id"] for s in recovered for o in s.origins}) == 4
    assert all("target_seq" not in o for s in recovered for o in s.origins)
    assert [s.origins for s in recovered] == [
        s.origins for s in spool.recover_spooled(tmp_path)
    ]


def test_recovery_preserves_pair_positions_when_a_page_is_missing(tmp_path):
    spool = spool_module()
    store = spool.AcquisitionSpool(tmp_path, faces=2)
    pages = [store.append(1, np.zeros((3, 4), dtype=np.uint8), 300) for _ in range(4)]
    pages[1].path.unlink()
    recovered = list(spool.recover_spooled(tmp_path))
    assert [s.paths for s in recovered] == [
        [pages[0].path], [pages[2].path, pages[3].path],
    ]


def test_failed_publish_does_not_produce_a_recoverable_partial_page(tmp_path, monkeypatch):
    spool = spool_module()
    store = spool.AcquisitionSpool(tmp_path, faces=1)

    def fail_replace(*args):
        raise OSError("disk full")

    monkeypatch.setattr(spool.os, "replace", fail_replace)
    with pytest.raises(OSError, match="disk full"):
        store.append(1, np.zeros((8, 12), dtype=np.uint8), 300)
    assert list(spool.recover_spooled(tmp_path)) == []


def test_corrupt_page_remains_evidence_without_breaking_other_sheets(tmp_path):
    spool = spool_module()
    store = spool.AcquisitionSpool(tmp_path, faces=1, target_seq=8)
    first = store.append(1, np.zeros((8, 12), dtype=np.uint8), 300)
    second = store.append(1, np.ones((8, 12), dtype=np.uint8), 300)
    first.path.write_bytes(b"corrupted")
    recovered = list(spool.recover_spooled(tmp_path))
    assert [s.paths for s in recovered] == [[first.path], [second.path]]
    assert all(s.origins[0]["target_seq"] == 8 for s in recovered)


def test_run_metadata_is_available_before_any_pages(tmp_path):
    spool = spool_module()
    store = spool.AcquisitionSpool(tmp_path, faces=2, target_seq=7)
    metadata = json.loads((store.run_dir / "run.json").read_text())
    assert metadata == {
        "version": 1, "run_id": store.run_id, "faces": 2, "target_seq": 7,
    }
    assert list(spool.recover_spooled(tmp_path)) == []
