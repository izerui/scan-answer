import hashlib
import shutil
from pathlib import PureWindowsPath

import pytest

from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.worker import (
    FinalizeJob, SheetJob, finalize_sheet, process_sheet, state_path,
)


@pytest.fixture(scope="session")
def worker_batch(tmp_path_factory, small_dataset, ext_template):
    """Reuse the real scan fixture and recognize once; no OCR service is used."""
    batch = tmp_path_factory.mktemp("worker_source")
    (batch / "raw").mkdir()
    raw = []
    for face in (1, 2):
        target = batch / "raw" / f"0001_{face}.jpg"
        shutil.copy2(small_dataset / target.name, target)
        raw.append(str(target))
    outcome = process_sheet(SheetJob(
        1, raw, [200.0, 200.0], 0.45, 0.2025, str(batch), str(ext_template),
        "portable-batch", ocr=True,
    ))
    assert outcome.ok
    return batch, ext_template, outcome.result


@pytest.fixture
def copied_batch(worker_batch, tmp_path):
    source, template, result = worker_batch
    batch = tmp_path / "batch"
    shutil.copytree(source, batch)
    return batch, template, result


def test_new_state_stores_batch_relative_raw_paths(worker_batch):
    batch, _, _ = worker_batch
    assert read_json(state_path(batch, 1))["raw_paths"] == [
        "raw/0001_1.jpg", "raw/0001_2.jpg",
    ]


def test_process_accepts_runner_paths_with_relative_batch_directory(copied_batch, tmp_path, monkeypatch):
    batch, template, _ = copied_batch
    monkeypatch.chdir(tmp_path)
    relative = batch.relative_to(tmp_path)
    outcome = process_sheet(SheetJob(
        1, [str(relative / "raw" / f"0001_{face}.jpg") for face in (1, 2)],
        [200.0, 200.0], 0.45, 0.2025, str(relative), str(template), "portable-batch",
    ))
    assert outcome.ok
    assert read_json(state_path(batch, 1))["raw_paths"] == ["raw/0001_1.jpg", "raw/0001_2.jpg"]


@pytest.mark.parametrize("path_style", ["relative", "posix", "windows", "unc"])
def test_relocated_state_reads_local_raw_even_when_old_origin_exists(copied_batch, tmp_path, path_style):
    batch, template, result_ref = copied_batch
    origin = tmp_path / "old-origin"
    (origin / "raw").mkdir(parents=True)
    # An existing origin must never be used as fallback or preferred over local raw.
    for face in (1, 2):
        shutil.copy2(batch / "raw" / f"0001_{3 - face}.jpg", origin / "raw" / f"0001_{face}.jpg")
    names = ["0001_1.jpg", "0001_2.jpg"]
    paths = {
        "relative": [f"raw/{name}" for name in names],
        "posix": [str(origin / "raw" / name) for name in names],
        "windows": [str(PureWindowsPath("C:/old/batch/raw") / name) for name in names],
        "unc": [str(PureWindowsPath("//old-server/share/batch/raw") / name) for name in names],
    }[path_style]
    state = read_json(state_path(batch, 1))
    state["raw_paths"] = paths
    write_json_atomic(state_path(batch, 1), state)
    result_path = batch / result_ref
    old = read_json(result_path)
    crop = result_path.parent / old["objective"][0]["image"]
    expected = crop.read_bytes()
    crop.unlink()
    outcome = finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert outcome.changed
    assert crop.read_bytes() == expected
    new = read_json(result_path)
    assert new["pages"]["raw_1"] == "../../raw/0001_1.jpg"


@pytest.mark.parametrize("reference", [
    "../outside.jpg", "raw/../../outside.jpg", r"raw\..\..\outside.jpg",
    "/old/raw/../outside.jpg", r"C:\old\raw\..\outside.jpg", "C:raw.jpg",
])
def test_finalize_rejects_escaping_raw_even_when_decision_is_unchanged(copied_batch, reference):
    batch, template, _ = copied_batch
    state = read_json(state_path(batch, 1))
    state["raw_paths"][0] = reference
    write_json_atomic(state_path(batch, 1), state)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))


def test_finalize_rejects_raw_symlink_outside_batch(copied_batch, tmp_path):
    batch, template, _ = copied_batch
    raw = batch / "raw" / "0001_1.jpg"
    outside = tmp_path / "outside.jpg"
    shutil.copy2(raw, outside)
    raw.unlink()
    raw.symlink_to(outside)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))


def test_finalize_rejects_result_outside_batch(copied_batch, tmp_path):
    batch, template, result_ref = copied_batch
    outside = tmp_path / "result.json"
    shutil.copy2(batch / result_ref, outside)
    state = read_json(state_path(batch, 1))
    state["result"] = "../result.json"
    write_json_atomic(state_path(batch, 1), state)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))


def test_missing_local_raw_never_falls_back_to_existing_origin(copied_batch, worker_batch):
    batch, template, result_ref = copied_batch
    origin, _, _ = worker_batch
    state = read_json(state_path(batch, 1))
    state["raw_paths"] = [str(origin / "raw" / f"0001_{face}.jpg") for face in (1, 2)]
    write_json_atomic(state_path(batch, 1), state)
    path = batch / result_ref
    result = read_json(path)
    (path.parent / result["objective"][0]["image"]).unlink()
    (batch / "raw" / "0001_1.jpg").unlink()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="raw"):
        finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert path.read_bytes() == before


def test_finalize_rejects_crop_symlink_outside_batch(copied_batch, tmp_path):
    batch, template, result_ref = copied_batch
    path = batch / result_ref
    crop = path.parent / read_json(path)["objective"][0]["image"]
    outside = tmp_path / "outside.jpg"
    shutil.copy2(crop, outside)
    crop.unlink()
    crop.symlink_to(outside)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))


@pytest.mark.parametrize("operation", ["process", "finalize"])
def test_sealed_worker_refuses_before_any_writes(copied_batch, operation):
    batch, template, _ = copied_batch
    write_json_atomic(batch / "manifest.json", {"delivery": {"sealed": True}})
    before = {p.relative_to(batch): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in batch.rglob("*") if p.is_file()}
    with pytest.raises(ValueError, match="(?i)sealed|封存|交付"):
        if operation == "process":
            process_sheet(SheetJob(
                1, [str(batch / "raw" / f"0001_{face}.jpg") for face in (1, 2)],
                [200.0, 200.0], 0.45, 0.2025, str(batch), str(template), "portable-batch",
            ))
        else:
            finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    after = {p.relative_to(batch): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in batch.rglob("*") if p.is_file()}
    assert after == before


def _inventory(batch):
    return {p.relative_to(batch): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in batch.rglob("*") if p.is_file()}


@pytest.mark.parametrize("reference", [
    "students/0002_other/result.json",
    "students/00010_other/result.json",
    "errors/0001_other/result.json",
    "students/0001_other/nested/result.json",
    "students/0001_other/other.json",
])
def test_finalize_rejects_wrong_result_owner_before_writes(copied_batch, reference):
    batch, template, result_ref = copied_batch
    path = batch / result_ref
    target = batch / reference
    # Keep the payload's seq=1 to ensure directory ownership is checked separately.
    shutil.copytree(path.parent / "images", target.parent / "images")
    shutil.copy2(path, target)
    state = read_json(state_path(batch, 1))
    state["result"] = reference
    write_json_atomic(state_path(batch, 1), state)
    before = _inventory(batch)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert _inventory(batch) == before


@pytest.mark.parametrize(("field", "value"), [("seq", 2), ("seq", True), ("batch_id", "other-batch")])
def test_finalize_rejects_wrong_result_identity_before_writes(copied_batch, field, value):
    batch, template, result_ref = copied_batch
    write_json_atomic(batch / "manifest.json", {"batch_id": "portable-batch"})
    path = batch / result_ref
    result = read_json(path)
    result[field] = value
    (path.parent / result["objective"][0]["image"]).unlink()
    write_json_atomic(path, result)
    before = _inventory(batch)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert _inventory(batch) == before


@pytest.mark.parametrize("kind", ["result_file", "result_directory", "crop_file", "crop_directory"])
def test_finalize_rejects_cross_student_symlink_aliases(copied_batch, kind):
    batch, template, result_ref = copied_batch
    path = batch / result_ref
    other = batch / "students" / "0002_other"
    shutil.copytree(path.parent, other)
    # Even a copied payload falsely claiming seq=1 must not bypass path ownership.
    if kind == "result_file":
        path.unlink()
        path.symlink_to(other / "result.json")
    elif kind == "result_directory":
        shutil.rmtree(path.parent)
        path.parent.symlink_to(other, target_is_directory=True)
    elif kind == "crop_file":
        ref = read_json(path)["objective"][0]["image"]
        (path.parent / ref).unlink()
        (path.parent / ref).symlink_to(other / ref)
    else:
        shutil.rmtree(path.parent / "images")
        (path.parent / "images").symlink_to(other / "images", target_is_directory=True)
        # Rebuilding this missing crop would modify the other student's directory.
        (other / read_json(path)["objective"][0]["image"]).unlink()
    before = _inventory(batch)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert _inventory(batch) == before


@pytest.mark.parametrize("kind", ["reference", "prefix_collision", "symlink", "reverse_symlink", "legacy_windows"])
def test_finalize_rejects_cross_student_raw_before_writes(copied_batch, kind):
    batch, template, result_ref = copied_batch
    raw = batch / "raw" / "0001_1.jpg"
    other = batch / "raw" / ("00010_1.jpg" if kind == "prefix_collision" else "0002_1.jpg")
    shutil.copy2(raw, other)
    state = read_json(state_path(batch, 1))
    if kind == "symlink":
        raw.unlink()
        raw.symlink_to(other)
    elif kind == "reverse_symlink":
        other.unlink()
        other.symlink_to(raw)
        state["raw_paths"][0] = other.relative_to(batch).as_posix()
    elif kind == "legacy_windows":
        state["raw_paths"][0] = r"C:\old\batch\raw\0002_1.jpg"
    else:
        state["raw_paths"][0] = other.relative_to(batch).as_posix()
    write_json_atomic(state_path(batch, 1), state)
    path = batch / result_ref
    (path.parent / read_json(path)["objective"][0]["image"]).unlink()
    before = _inventory(batch)
    with pytest.raises(ValueError):
        finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(batch), str(template)))
    assert _inventory(batch) == before
