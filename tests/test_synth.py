import json

import numpy as np
from PIL import Image

from scan_answer.synth.draw import paste_barcode, pencil_mark
from scan_answer.synth.generate import generate_dataset, make_scans, make_sheet
from scan_answer.synth.scan import ScanParams, scanify


def test_pencil_mark_darkens_only_inside_cell():
    img = np.full((300, 300), 250, np.uint8)
    rng = np.random.default_rng(0)
    pencil_mark(img, (5.0, 5.0, 10.0, 6.0), 300, "normal", rng)  # 300dpi: 5mm≈59px
    inside = img[85:130, 75:160]
    assert inside.mean() < 120
    assert img[:40, :].min() == 250 and img[200:, :].min() == 250


def test_paste_barcode_readable():
    from scan_answer.engine.barcode import read_barcode
    img = np.full((400, 900), 250, np.uint8)
    paste_barcode(img, (2.0, 2.0, 70.0, 26.0), 300, "100015000123456")
    assert read_barcode(img) == ("100015000123456", "Code128")


def test_make_sheet_truth(tpl_bases):
    t, bases = tpl_bases
    paper, truth = make_sheet(t, bases, np.random.default_rng(1))
    assert set(paper) == {"front", "back"}
    assert len(truth["objective"]) == 55
    assert len(truth["exam_no"]["value"]) == 15
    for o in truth["objective"]:
        assert o["status"] in {"ok", "blank", "multiple", "uncertain"}
    assert (paper["front"] < bases["front"]).sum() > 1000  # 确实画上了东西


def test_scanify_adds_margin_and_keeps_dpi():
    paper = np.full((1181, 1181), 250, np.uint8)  # 100mm @300dpi
    out = scanify(paper, 300, 200, ScanParams(margin_mm=10), np.random.default_rng(0))
    assert out.shape == (944, 944)  # (100+20)mm @200dpi


def test_special_cases(tpl_bases):
    t, bases = tpl_bases
    rng = np.random.default_rng(2)
    paper, truth = make_sheet(t, bases, rng)
    scans = make_scans(t, paper, truth, rng, 200, "blank")
    assert truth["physical"] == "BLANK_PAGE" and scans[-1].std() < 10


def test_generate_dataset_files(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path, 2, seed=3, out_dpi=200)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["0001.truth.json", "0001_1.jpg", "0001_2.jpg", "0002.truth.json", "0002_1.jpg", "0002_2.jpg"]
    with Image.open(tmp_path / "0001_1.jpg") as im:
        assert round(im.info["dpi"][0]) == 200
    assert "objective" in json.loads((tmp_path / "0001.truth.json").read_text(encoding="utf-8"))
