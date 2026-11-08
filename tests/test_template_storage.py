"""Template JSON publication and confined, immutable image resources."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pytest

from scan_answer.template import io
from scan_answer.template.model import AlignSpec, Face
from test_template_validation_workflow import valid_template


def images(two_faces=False, value=240):
    result = {"front": np.full((24, 32), value, np.uint8)}
    if two_faces:
        result["back"] = np.full((24, 32), value - 1, np.uint8)
    return result


def assert_loaded(directory, expected, bases):
    actual, loaded = io.load_template(directory)
    assert actual == expected
    assert set(loaded) == set(bases)
    for name in bases:
        np.testing.assert_array_equal(loaded[name], bases[name])


def test_resources_use_content_digest_and_are_reused(tmp_path):
    t = valid_template()
    io.save_template(t, tmp_path, images())
    resource = tmp_path / t.faces["front"].image
    digest = hashlib.sha256(resource.read_bytes()).hexdigest()
    assert digest in resource.name
    assert resource.name != "front.png"
    before = resource.stat()
    io.save_template(t, tmp_path, images())
    assert resource.stat().st_ino == before.st_ino
    assert resource.stat().st_mtime_ns == before.st_mtime_ns
    assert_loaded(tmp_path, t, images())


def test_save_as_uses_declared_resources_and_preserves_old_assets(tmp_path):
    t = valid_template()
    source, target = tmp_path / "source", tmp_path / "target"
    io.save_template(t, source, images())
    old = deepcopy(t)
    old_resource = source / old.faces["front"].image
    old_bytes = old_resource.read_bytes()
    loaded, bases = io.load_template(source)
    io.save_template(loaded, target, bases)
    assert_loaded(target, loaded, bases)
    io.save_template(t, source, images(value=120))
    assert t.faces["front"].image != old.faces["front"].image
    assert old_resource.read_bytes() == old_bytes
    assert_loaded(source, t, images(value=120))


@pytest.mark.parametrize("failure", [
    "encode_false", "encode_raise", "json_replace", "json_encode", "json_fsync",
])
def test_failed_save_preserves_published_template_and_in_memory_references(tmp_path, monkeypatch, failure):
    t = valid_template()
    t.faces["back"] = Face("back.png", AlignSpec("frames", frames=[(5, 5, 200, 287)]))
    old_images = images(two_faces=True)
    io.save_template(t, tmp_path, old_images)
    published = deepcopy(t)
    old_json = (tmp_path / "template.json").read_bytes()
    old_resources = {f.image: (tmp_path / f.image).read_bytes() for f in t.faces.values()}
    old_imwrite = io.imwrite
    writes = 0

    def fail_second_image(path, img):
        nonlocal writes
        writes += 1
        if writes == 2:
            if failure == "encode_raise":
                raise OSError("encoding failed")
            return False
        return old_imwrite(path, img)

    old_replace = os.replace

    def fail_json_replace(src, dst):
        if Path(dst).name == "template.json":
            raise OSError("publication failed")
        return old_replace(src, dst)

    if failure.startswith("encode"):
        monkeypatch.setattr(io, "imwrite", fail_second_image)
    elif failure == "json_replace":
        monkeypatch.setattr(io.os, "replace", fail_json_replace)
    elif failure == "json_fsync":
        def fail_fsync(fd):
            raise OSError("flush failed")
        monkeypatch.setattr(io.os, "fsync", fail_fsync)
    else:
        t.name = object()
    with pytest.raises((OSError, TypeError, ValueError)):
        io.save_template(t, tmp_path, images(two_faces=True, value=120))
    assert (tmp_path / "template.json").read_bytes() == old_json
    assert {k: f.image for k, f in t.faces.items()} == {k: f.image for k, f in published.faces.items()}
    for reference, data in old_resources.items():
        assert (tmp_path / reference).read_bytes() == data
    assert_loaded(tmp_path, published, old_images)
    assert not list(tmp_path.glob("*.tmp"))
    # Completed content-addressed resources may remain after a failed publication.
    for path in tmp_path.iterdir():
        if path.name != "template.json":
            assert hashlib.sha256(path.read_bytes()).hexdigest() in path.name


@pytest.mark.parametrize("reference", [
    "../outside.png", "/tmp/outside.png", "nested/../../outside.png",
    r"..\outside.png", r"C:\outside.png", r"\\host\share\outside.png",
    "C:outside.png", "", ".", "bad\x00.png",
])
@pytest.mark.parametrize("operation", ["load", "save", "save_with_bases"])
def test_resource_paths_must_be_confined(tmp_path, reference, operation):
    t = valid_template()
    t.faces["front"].image = reference
    (tmp_path / "template.json").write_text(json.dumps(io.template_to_dict(t)))
    before = (tmp_path / "template.json").read_bytes()
    with pytest.raises(ValueError, match="底图|资源|路径"):
        if operation == "load":
            io.load_template(tmp_path)
        else:
            io.save_template(t, tmp_path, images() if operation == "save_with_bases" else None)
    assert (tmp_path / "template.json").read_bytes() == before


@pytest.mark.parametrize("operation", ["load", "save", "save_with_bases"])
def test_symlink_resource_escape_is_rejected(tmp_path, operation):
    root = tmp_path / "template"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    t = valid_template()
    t.faces["front"].image = "linked/image.png"
    (root / "template.json").write_text(json.dumps(io.template_to_dict(t)))
    with pytest.raises(ValueError, match="底图|资源|路径"):
        if operation == "load":
            io.load_template(root)
        else:
            io.save_template(t, root, images() if operation == "save_with_bases" else None)
    assert not list(outside.iterdir())


def test_no_bases_preserves_legacy_nested_reference(tmp_path):
    t = valid_template()
    t.faces["front"].image = "images/custom.png"
    (tmp_path / "images").mkdir()
    io.imwrite(tmp_path / t.faces["front"].image, images()["front"])
    io.save_template(t, tmp_path)
    assert t.faces["front"].image == "images/custom.png"
    assert_loaded(tmp_path, t, images())


@pytest.mark.parametrize("bases", [{}, {"middle": np.zeros((2, 2), np.uint8)}])
def test_incomplete_or_unknown_bases_do_not_publish(tmp_path, bases):
    t = valid_template()
    with pytest.raises(ValueError):
        io.save_template(t, tmp_path, bases)
    assert not (tmp_path / "template.json").exists()


def test_json_temporary_path_cannot_overwrite_existing_file(tmp_path):
    t = valid_template()
    sentinel = tmp_path / "template.json.tmp"
    sentinel.write_text("owned by another save")
    io.save_template(t, tmp_path, images())
    assert sentinel.read_text() == "owned by another save"


@pytest.mark.parametrize("conflict", ["corrupt", "external_symlink"])
def test_existing_digest_target_is_never_overwritten(tmp_path, conflict):
    t = valid_template()
    destination = tmp_path / "template"
    io.save_template(t, destination, images())
    published = deepcopy(t)
    old_json = (destination / "template.json").read_bytes()
    encoded = tmp_path / "new.png"
    io.imwrite(encoded, images(value=120)["front"])
    digest = hashlib.sha256(encoded.read_bytes()).hexdigest()
    conflict_path = destination / f"front-{digest}.png"
    if conflict == "corrupt":
        conflict_path.write_bytes(b"preexisting data")
    else:
        conflict_path.symlink_to(encoded)
    before = conflict_path.read_bytes()
    with pytest.raises((ValueError, OSError)):
        io.save_template(t, destination, images(value=120))
    assert conflict_path.read_bytes() == before
    assert (destination / "template.json").read_bytes() == old_json
    assert t == published
    assert_loaded(destination, published, images())


def test_save_without_bases_can_republish_digest_references(tmp_path):
    t = valid_template()
    io.save_template(t, tmp_path, images())
    reference = t.faces["front"].image
    t.version += 1
    io.save_template(t, tmp_path)
    assert t.faces["front"].image == reference
    assert_loaded(tmp_path, t, images())
