import json
import shutil

import pytest

from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.documents import DocumentSource
from conftest import TEMPLATE_DIR


@pytest.fixture(scope="module")
def delivery_seed(tmp_path_factory, small_dataset):
    session = BatchSession.create(tmp_path_factory.mktemp("delivery-seed"), TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(session) as pipeline:
        pipeline.run(DocumentSource(sorted(small_dataset.glob("*.jpg"))[:2], 2))
        pipeline.finalize()
    return session.batch_dir


@pytest.fixture
def batch(delivery_seed, tmp_path):
    path = tmp_path / "working"
    shutil.copytree(delivery_seed, path)
    return path


def test_delivery_snapshot_is_portable_and_independent(batch, tmp_path):
    from scan_answer.output.audit import audit_batch
    from scan_answer.output.delivery import export_delivery
    target = tmp_path / "ready"
    assert export_delivery(batch, target) == target
    report = audit_batch(target)
    assert report.ready, report.issues
    manifest = json.loads((target / "manifest.json").read_text())
    assert manifest["delivery"]["sealed"] is True
    assert manifest["delivery"]["revision"] == json.loads((target / "delivery.json").read_text())["revision"]
    assert not any((target / name).exists() for name in ("state", "attempts", "acquisition"))
    result = next(target.glob("students/*/result.json"))
    before = result.read_bytes()
    source = batch / result.relative_to(target)
    source.write_text("{}")
    assert result.read_bytes() == before
    moved = tmp_path / "moved"
    shutil.move(target, moved)
    assert audit_batch(moved).ready


def test_delivery_refuses_pending_ocr_and_missing_images(batch, tmp_path):
    from scan_answer.output.delivery import export_delivery
    result = next(batch.glob("students/*/result.json"))
    data = json.loads(result.read_text())
    data["subjective"][0]["ocr_status"] = "pending"
    result.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        export_delivery(batch, tmp_path / "pending")
    assert not (tmp_path / "pending").exists()
    data["subjective"][0]["ocr_status"] = "none"
    result.write_text(json.dumps(data))
    (result.parent / data["objective"][0]["image"]).unlink()
    with pytest.raises(ValueError):
        export_delivery(batch, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


def test_existing_or_nested_destination_is_never_overwritten(batch, tmp_path):
    from scan_answer.output.delivery import export_delivery
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_text("original")
    with pytest.raises((FileExistsError, ValueError)):
        export_delivery(batch, target)
    assert (target / "keep").read_text() == "original"
    with pytest.raises(ValueError):
        export_delivery(batch, batch / "nested")
    assert not (batch / "nested").exists()


def test_existing_broken_symlink_is_not_used_as_export_destination(batch, tmp_path):
    from scan_answer.output.delivery import export_delivery
    target = tmp_path / "existing-link"
    destination = tmp_path / "missing"
    target.symlink_to(destination, target_is_directory=True)
    with pytest.raises(FileExistsError):
        export_delivery(batch, target)
    assert target.is_symlink() and not destination.exists()


def test_export_copy_error_does_not_publish_half_directory(batch, tmp_path, monkeypatch):
    from scan_answer.output import delivery
    copyfile = delivery.shutil.copyfile

    def fail_image(source, target):
        if "images" in str(source):
            raise OSError("disk full")
        return copyfile(source, target)

    monkeypatch.setattr(delivery.shutil, "copyfile", fail_image)
    target = tmp_path / "broken"
    with pytest.raises(OSError, match="disk full"):
        delivery.export_delivery(batch, target)
    assert not target.exists()
    assert set(p.name for p in tmp_path.iterdir()) == {"working"}


def test_export_rejects_source_changes_during_copy(batch, tmp_path, monkeypatch):
    from scan_answer.output import delivery
    copyfile = delivery.shutil.copyfile
    source_result = next(batch.glob("students/*/result.json"))
    changed = False

    def racing_copy(source, target):
        nonlocal changed
        out = copyfile(source, target)
        if not changed:
            changed = True
            data = json.loads(source_result.read_text())
            data["manual"] = {"note": "changed while exporting"}
            source_result.write_text(json.dumps(data))
        return out

    monkeypatch.setattr(delivery.shutil, "copyfile", racing_copy)
    target = tmp_path / "raced"
    with pytest.raises(ValueError, match="变化|改变|一致"):
        delivery.export_delivery(batch, target)
    assert not target.exists()


def test_sealed_checksum_detects_semantically_valid_edit(batch, tmp_path):
    from scan_answer.output.audit import audit_batch
    from scan_answer.output.delivery import export_delivery
    target = export_delivery(batch, tmp_path / "sealed")
    result = next(target.glob("students/*/result.json"))
    data = json.loads(result.read_text())
    data["manual"] = {"note": "post-delivery edit"}
    result.write_text(json.dumps(data))
    assert not audit_batch(target).ready


def test_publication_never_replaces_concurrently_created_empty_target(batch, tmp_path, monkeypatch):
    from scan_answer.output import delivery
    target = tmp_path / "racing-target"
    rename = delivery.publish_directory
    created = {}

    def racing_rename(source, destination):
        if destination == target:
            target.mkdir()
            created["inode"] = target.stat().st_ino
        return rename(source, destination)

    monkeypatch.setattr(delivery, "publish_directory", racing_rename)
    with pytest.raises(FileExistsError):
        delivery.export_delivery(batch, target)
    assert target.stat().st_ino == created["inode"]
    assert list(target.iterdir()) == []
