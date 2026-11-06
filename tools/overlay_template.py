"""把模板区域画到空白底图上，用于人工核对坐标。
用法: python tools/overlay_template.py templates/guanzong199 out_dir
"""
import sys
from pathlib import Path

import cv2

from scan_answer.template import load_template

COLORS = {"anchor": (0, 0, 255), "exam": (200, 80, 0), "obj": (0, 160, 0), "absent": (0, 0, 255),
          "barcode": (160, 0, 160), "field": (90, 90, 90), "sub": (0, 120, 255)}


def main(tpl_dir: str, out: str) -> None:
    t, bases = load_template(tpl_dir)
    s = t.dpi / 25.4
    canv = {k: cv2.cvtColor(v, cv2.COLOR_GRAY2BGR) for k, v in bases.items()}

    def box(face, r, kind, th=2):
        x, y, w, h = r
        cv2.rectangle(canv[face], (int(x * s), int(y * s)), (int((x + w) * s), int((y + h) * s)), COLORS[kind], th)

    for name, f in t.faces.items():
        for r in f.align.anchors + f.align.frames:
            box(name, r, "anchor", 4)
    if t.exam_no:
        for row in t.exam_no.grid.cells:
            for c in row:
                box(t.exam_no.grid.face, c, "exam")
    if t.absent:
        box(t.absent.face, t.absent.rect, "absent", 3)
    for b in t.barcodes:
        box(b.face, b.rect, "barcode", 4)
    for fd in t.fields:
        box(fd.face, fd.rect, "field", 4)
    for blk in t.objective:
        for row in blk.grid.cells:
            for c in row:
                box(blk.grid.face, c, "obj")
    for sq in t.subjective:
        for r in sq.regions:
            box(r.face, r.rect, "sub", 5)
    Path(out).mkdir(parents=True, exist_ok=True)
    for name, img in canv.items():
        cv2.imwrite(str(Path(out) / f"overlay_{name}.png"), img)
    print("已输出", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
