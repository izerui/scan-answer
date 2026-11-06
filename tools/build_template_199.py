"""从 samples/raw/8k-199.pdf 生成 templates/guanzong199（空白底图 + template.json）。

坐标来自对真实空白卡的测量（300 DPI，正面按四角定位块纠偏，背面按两个作文外框纠偏）。
用法: python tools/build_template_199.py samples/raw/8k-199.pdf templates/guanzong199
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from scan_answer.engine.align import align_primary, warp_to_template
from scan_answer.template import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid,
                                  Region, SubjectiveSpec, Template, save_template)
from scan_answer.template.layout import grid_cells

DPI = 300
PAPER = (390.0, 270.0)


def anchor(cx, cy, w=4.7, h=3.5):
    return (round(cx - w / 2, 2), round(cy - h / 2, 2), w, h)


def build() -> Template:
    front_align = AlignSpec(method="anchors", anchors=[
        anchor(19.8, 13.15), anchor(374.95, 13.15), anchor(374.95, 257.4), anchor(19.8, 257.4)])
    back_align = AlignSpec(method="frames", frames=[(19.5, 25.7, 166.0, 225.9), (199.0, 25.7, 166.4, 225.9)])

    # 考生编号：15 位，每位 0-9 纵向排列；第 1 位"0"格中心 (115.55, 51.2)，位距 5.10，行距 3.62
    exam_cells = grid_cells((115.55, 51.2), n_groups=15, n_options=10, option_pitch=3.62, group_pitch=5.10,
                            cell_size=(3.6, 2.2), options_horizontal=False)

    # 选择题：4 列，A 选项中心 x 依次为 36.7 / 76.45 / 115.85 / 154.95；第 1 题 y=155.9；
    # 题距 3.625，每 5 题额外空 (20.25 - 5*3.625) = 2.125；选项间距 6.22
    blocks = []
    for c, ax in enumerate([36.7, 76.45, 115.85, 154.95]):
        start = 1 + 15 * c
        count = 15 if c < 3 else 10
        cells = grid_cells((ax, 155.9), n_groups=count, n_options=5, option_pitch=6.22, group_pitch=3.625,
                           cell_size=(3.8, 2.4), options_horizontal=True, block_every=5, block_gap=2.125)
        blocks.append(ObjectiveBlock(name=f"{start}-{start + count - 1}", qtype="single", start=start,
                                     grid=OmrGrid(face="front", labels=list("ABCDE"), cells=cells)))

    return Template(
        schema_version="1.0", name="管综199", version=1, paper=PAPER, dpi=DPI,
        faces={"front": Face(image="front.png", align=front_align), "back": Face(image="back.png", align=back_align)},
        exam_no=ExamNoSpec(grid=OmrGrid(face="front", labels=[str(d) for d in range(10)], cells=exam_cells)),
        absent=Region(face="front", rect=(51.0, 130.9, 4.6, 2.5)),
        barcodes=[BarcodeSpec(key="student_barcode", face="front", rect=(225.0, 8.0, 62.0, 26.0)),
                  BarcodeSpec(key="paper_barcode", face="front", rect=(298.0, 8.0, 48.0, 26.0))],
        fields=[FieldSpec(key="unit", label="报考单位", face="front", rect=(23.8, 44.1, 87.2, 16.0)),
                FieldSpec(key="name", label="考生姓名", face="front", rect=(23.8, 70.3, 87.3, 16.0)),
                FieldSpec(key="name_back", label="考生姓名（背面）", face="back", rect=(148.0, 13.0, 38.0, 12.0))],
        objective=blocks,
        subjective=[
            SubjectiveSpec(no="56", qtype="essay", content="zh", regions=[
                Region(face="front", rect=(215.5, 43.5, 151.0, 200.0)),
                Region(face="back", rect=(26.5, 33.5, 152.5, 80.5))]),
            SubjectiveSpec(no="57", qtype="essay", content="zh", regions=[
                Region(face="back", rect=(26.5, 131.5, 152.5, 113.5)),
                Region(face="back", rect=(206.0, 33.0, 153.0, 212.0))]),
        ],
        ignore=[Region(face="front", rect=(23.5, 223.8, 166.6, 22.4))],
    )


def render(pdf: Path) -> list[np.ndarray]:
    doc = pymupdf.open(pdf)
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
    return out


def main(pdf: str, out: str) -> None:
    t = build()
    front_raw, back_raw = render(Path(pdf))
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, raw in (("front", front_raw), ("back", back_raw)):
        res = align_primary(raw, t.faces[name].align, DPI)
        if not res.ok:
            raise SystemExit(f"{name} 对齐失败: {res.reason}")
        print(f"{name}: {res.method} 倾斜 {res.skew_deg:+.2f}° 残差 {res.residual_mm:.2f} mm")
        cv2.imwrite(str(out_dir / t.faces[name].image), warp_to_template(raw, res.H, t.paper, DPI))
    save_template(t, out_dir)
    print("模板已写入", out_dir)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
