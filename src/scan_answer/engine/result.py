"""把识别结果组装成 result.json，并裁出要输出的图片。"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from .align import crop_region, warp_to_template
from .omr import grid_bbox
from .recognize import SheetAnalysis, SheetDecision, TemplateContext, oriented

SCHEMA_VERSION = "1.0"


def _pad(rect, mm=2.0):
    x, y, w, h = rect
    return (x - mm, y - mm, w + 2 * mm, h + 2 * mm)


def clean_show_through(img: np.ndarray, level: int = 185) -> np.ndarray:
    """背面去透印：比 level 更浅的像素置白。"""
    out = img.copy()
    out[out > level] = 255
    return out


def render_images(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, images: list[np.ndarray]) -> dict[str, np.ndarray]:
    """返回 {相对路径: 图像}。切图从原分辨率图像上直接裁取。"""
    t = ctx.tpl
    src = {f: oriented(images[p.source], p.rotated) for f, p in a.placements.items()}

    def crop(face, rect):
        p = a.placements[face]
        img = crop_region(src[face], p.align.H, rect, p.dpi)
        return img if face == "front" else clean_show_through(img)

    out: dict[str, np.ndarray] = {}
    for f, p in a.placements.items():
        out[f"images/page_{f}.jpg"] = warp_to_template(src[f], p.align.H, t.paper, p.dpi)
    if t.exam_no:
        out["images/exam_no.jpg"] = crop(t.exam_no.grid.face, _pad(grid_bbox(t.exam_no.grid.cells)))
    for bc in t.barcodes:
        out[f"images/{bc.key}.jpg"] = crop(bc.face, bc.rect)
    for fd in t.fields:
        out[f"images/{fd.key}.jpg"] = crop(fd.face, fd.rect)
    for sq in t.subjective:
        for i, r in enumerate(sq.regions, start=1):
            out[f"images/q{sq.no}_{i}.jpg"] = crop(r.face, r.rect)
    bad = {o["no"] for o in d.objective if o["status"] != "ok"}
    for blk in t.objective:
        for k, cells in enumerate(blk.grid.cells):
            no = str(blk.start + k)
            if no in bad:
                x0 = min(c[0] for c in cells) - 9.0  # 带上题号
                bx = grid_bbox([cells])
                out[f"images/obj_{no}.jpg"] = crop(blk.grid.face, _pad((x0, bx[1], bx[0] + bx[2] - x0, bx[3]), 1.5))
    # 自定义填涂字段切图
    for of in t.omr_fields:
        out[f"images/omr_{of.key}.jpg"] = crop(of.grid.face, _pad(grid_bbox(of.grid.cells)))
    return out


def build_result(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, *, seq: int, batch_id: str,
                 exam: dict, scanner: str, raw_paths: list[str], image_paths: set[str],
                 scan_time: datetime | None = None, ocr: bool = False) -> dict:
    """ocr=True 表示已配置手写转写：主观题与信息栏的 ocr_status 置为 pending，等待后台转写。"""
    t = ctx.tpl
    ocr_status = "pending" if ocr else "none"
    faces_info = {f: {"source": p.source + 1, "rotated": p.rotated, "align_method": p.align.method,
                      "skew_deg": round(p.align.skew_deg, 2), "residual_mm": round(p.align.residual_mm, 3),
                      "dpi": round(p.dpi)} for f, p in a.placements.items()}
    student: dict = {"exam_no": None, "omr_fields": {}, "barcodes": {}, "absent": d.absent, "fields": {},
                     "consistent": d.consistent}
    if d.exam_no:
        student["exam_no"] = {**d.exam_no, "image": "images/exam_no.jpg"}
    for key, info in d.omr_fields.items():
        student["omr_fields"][key] = {**info, "image": f"images/omr_{key}.jpg"}
    for key, bc in d.barcodes.items():
        student["barcodes"][key] = {**bc, "image": f"images/{key}.jpg"}
    for fd in t.fields:
        student["fields"][fd.key] = {"label": fd.label, "image": f"images/{fd.key}.jpg", "text": None, "ocr_status": ocr_status}
    objective = []
    for o in d.objective:
        item = dict(o)
        if f"images/obj_{o['no']}.jpg" in image_paths:
            item["image"] = f"images/obj_{o['no']}.jpg"
        objective.append(item)
    subjective = [{"no": sq.no, "type": sq.qtype, "content": sq.content,
                   "images": [f"images/q{sq.no}_{i}.jpg" for i in range(1, len(sq.regions) + 1)],
                   "text": None, "ocr_status": ocr_status, "ocr_model": None, "ocr_confidence": None}
                  for sq in t.subjective]
    # 同步头信息写入 faces
    for face_name, sync_info in a.sync.items():
        if face_name in faces_info:
            faces_info[face_name]["sync"] = sync_info
    return {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "batch_id": batch_id,
        "template": {"name": t.name, "version": t.version},
        "exam": exam,
        "scan": {"time": (scan_time or datetime.now().astimezone()).isoformat(timespec="seconds"),
                 "scanner": scanner, "faces": faces_info, "swapped": a.swapped},
        "pages": {**{f: f"images/page_{f}.jpg" for f in a.placements},
                  **{f"raw_{i + 1}": p for i, p in enumerate(raw_paths)}},
        "student": student,
        "objective": objective,
        "subjective": subjective,
        "choose_one": d.choose_one,
        "manual": None,
        "warnings": d.warnings,
    }
