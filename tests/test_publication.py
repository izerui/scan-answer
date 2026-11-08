from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from scan_answer.output.publication import publish_directory


@pytest.mark.parametrize("kind", ["empty_directory", "file", "broken_symlink"])
def test_native_publication_preserves_every_existing_destination(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("new")
    target = tmp_path / "target"
    if kind == "empty_directory":
        target.mkdir()
    elif kind == "file":
        target.write_text("old")
    else:
        target.symlink_to(tmp_path / "missing")
    inode = target.lstat().st_ino
    with pytest.raises(FileExistsError):
        publish_directory(source, target)
    assert target.lstat().st_ino == inode
    assert (source / "payload").read_text() == "new"


def test_simultaneous_publishers_cannot_replace_the_winner(tmp_path):
    sources = [tmp_path / f"source-{i}" for i in range(2)]
    for i, source in enumerate(sources):
        source.mkdir()
        (source / "payload").write_text(str(i))
    target = tmp_path / "delivery-\u4e00"
    barrier = threading.Barrier(2)

    def publish(source):
        barrier.wait(timeout=3)
        try:
            publish_directory(source, target)
            return True
        except FileExistsError:
            return False

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(publish, sources))
    assert outcomes.count(True) == outcomes.count(False) == 1
    winner = outcomes.index(True)
    assert (target / "payload").read_text() == str(winner)
    assert not sources[winner].exists()
    assert (sources[1 - winner] / "payload").read_text() == str(1 - winner)
