from scan_answer.source.base import SheetImages
from scan_answer.source.folder import FolderSource, choose_dpi
from scan_answer.synth.generate import generate_dataset


def test_folder_source_groups_faces_and_reads_dpi(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path, 3, seed=5, out_dpi=200)
    (tmp_path / "0002_2.jpg").unlink()
    sheets = list(FolderSource(tmp_path, 2).sheets())
    assert [s.source for s in sheets] == ["0001", "0002", "0003"]
    assert all(isinstance(s, SheetImages) for s in sheets)
    assert sheets[0].names == ["0001_1.jpg", "0001_2.jpg"] and len(sheets[1].paths) == 1
    assert sheets[0].dpis == [200.0, 200.0]


def test_choose_dpi_handles_zero_estimate():
    assert choose_dpi(None, 0) == 0
    assert choose_dpi(300.0, 0) == 300.0
