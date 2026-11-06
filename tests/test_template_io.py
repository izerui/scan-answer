import json

import cv2
import numpy as np
import pytest

from scan_answer.template import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid,
                                  Region, SubjectiveSpec, Template, load_template, save_template,
                                  template_from_dict, template_to_dict)
from scan_answer.template.layout import grid_cells


def tiny() -> Template:
    cells = grid_cells((20.0, 30.0), 2, 4, 6.0, 4.0, (3.8, 2.4))
    return Template(
        schema_version="1.0", name="小卡", version=2, paper=(100.0, 80.0), dpi=300,
        faces={"front": Face(image="front.png", align=AlignSpec(method="anchors", anchors=[(5, 5, 4, 3), (90, 5, 4, 3), (90, 70, 4, 3), (5, 70, 4, 3)]))},
        exam_no=ExamNoSpec(grid=OmrGrid(face="front", labels=list("0123456789"),
                                        cells=grid_cells((60.0, 20.0), 3, 10, 3.6, 5.1, (3.6, 2.2), options_horizontal=False))),
        absent=Region(face="front", rect=(10.0, 60.0, 4.6, 2.5)),
        barcodes=[BarcodeSpec(key="student_barcode", face="front", rect=(10.0, 10.0, 40.0, 15.0))],
        fields=[FieldSpec(key="name", label="姓名", face="front", rect=(10.0, 40.0, 30.0, 10.0))],
        objective=[ObjectiveBlock(name="1-2", qtype="single", start=1, grid=OmrGrid(face="front", labels=list("ABCD"), cells=cells))],
        subjective=[SubjectiveSpec(no="3", qtype="short", content="zh", regions=[Region(face="front", rect=(50.0, 50.0, 40.0, 20.0))])],
    )


def test_round_trip_and_load(tmp_path):
    t = tiny()
    save_template(t, tmp_path)
    cv2.imwrite(str(tmp_path / "front.png"), np.full((945, 1181), 250, np.uint8))
    again, bases = load_template(tmp_path)
    assert again == t
    assert bases["front"].shape == (945, 1181)


def test_rejects_unknown_schema():
    d = template_to_dict(tiny())
    d["schema_version"] = "9.9"
    with pytest.raises(ValueError):
        template_from_dict(d)


def test_missing_base_image(tmp_path):
    save_template(tiny(), tmp_path)
    with pytest.raises(FileNotFoundError):
        load_template(tmp_path)
