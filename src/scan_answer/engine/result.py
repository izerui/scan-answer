"""把识别结果组装成 result.json，并裁出要输出的图片。"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from .align import crop_region, warp_to_template
from ..output.evidence import image_path, question_id
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


def _objective_rect(cells):
    bx = grid_bbox([cells])
    x0 = bx[0] - 9.0  # 带上题号
    return _pad((x0, bx[1], bx[0] + bx[2] - x0, bx[3]), 1.5)


def sampling_rect(rect, metadata):
    dx, dy = (metadata or {}).get("shift_mm", (0., 0.))
    return (rect[0] + dx, rect[1] + dy, rect[2], rect[3])


def render_images(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, images: list[np.ndarray]) -> dict[str, np.ndarray]:
    """返回 {相对路径: 图像}。切图从原分辨率图像上直接裁取。"""
    t = ctx.tpl
    src = {f: oriented(images[p.source], p.rotated) for f, p in a.placements.items()}

    def crop(face, rect, sampling_key=None):
        p = a.placements[face]
        rect = sampling_rect(rect, a.sampling.get(sampling_key))
        img = crop_region(src[face], p.align.H, rect, p.dpi)
        return img if face == "front" or a.evidence_version >= 2 else clean_show_through(img)

    out: dict[str, np.ndarray] = {}

    def put(path: str, image: np.ndarray) -> None:
        if path in out:
            raise ValueError(f"图片路径重复，请检查题号或字段键：{path}")
        out[path] = image

    for f, p in a.placements.items():
        put(image_path("page", f), warp_to_template(src[f], p.align.H, t.paper, p.dpi))
    if t.exam_no:
        put(image_path("exam_no"), crop(t.exam_no.grid.face, _pad(grid_bbox(t.exam_no.grid.cells)), "exam_no"))
    for bc in t.barcodes:
        put(image_path("barcode", bc.key), crop(bc.face, bc.rect))
        for index, candidate in enumerate(a.barcode_evidence.get(bc.key, {}).get("candidates", []), 1):
            put(image_path("barcode_candidate", bc.key, region=index),
                crop(candidate["face"], _pad(candidate["rect"])))
    for fd in t.fields:
        put(image_path("field", fd.key), crop(fd.face, fd.rect))
    for sq in t.subjective:
        if not sq.regions:
            raise ValueError(f"主观题 {sq.key} 没有作答区域，不能生成题图")
        for i, r in enumerate(sq.regions, start=1):
            put(image_path("subjective", sq.no, sub=sq.sub, region=i), crop(r.face, r.rect))
    for index, blk in enumerate(t.objective):
        for k, cells in enumerate(blk.grid.cells):
            no = str(blk.start + k)
            put(image_path("objective", no), crop(blk.grid.face, _objective_rect(cells), f"objective/{index}"))
    # 自定义填涂字段切图
    for of in t.omr_fields:
        put(image_path("omr_field", of.key), crop(of.grid.face, _pad(grid_bbox(of.grid.cells)), f"omr_field/{of.key}"))
    return out


def build_result(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, *, seq: int, batch_id: str,
                 exam: dict, scanner: str, raw_paths: list[str], image_paths: set[str],
                 scan_time: datetime | None = None, ocr: bool = False,
                 input_pages: list[dict] | None = None) -> dict:
    """ocr=True 表示已配置手写转写：主观题与信息栏的 ocr_status 置为 pending，等待后台转写。"""
    t = ctx.tpl

    def required(kind: str, key: str = "", **kwargs) -> str:
        path = image_path(kind, key, **kwargs)
        if path not in image_paths:
            raise ValueError(f"缺少结果引用的图片：{path}")
        return path

    ocr_status = "pending" if ocr else "none"
    faces_info = {f: {"source": p.source + 1, "rotated": p.rotated, "align_method": p.align.method,
                      "skew_deg": round(p.align.skew_deg, 2), "residual_mm": round(p.align.residual_mm, 3),
                      "dpi": round(p.dpi)} for f, p in a.placements.items()}
    for f, p in a.placements.items():
        if p.align.method == "manual":  # 人工定位：missing 是操作员跳过的参照点
            faces_info[f]["manual_skipped"] = [k + 1 for k in p.align.missing]
        elif p.align.missing:  # 模板 4 个定位块、扫描图上只找到 3 个
            faces_info[f]["missing_anchors"] = [k + 1 for k in p.align.missing]
    student: dict = {"exam_no": None, "omr_fields": {}, "barcodes": {}, "absent": d.absent, "fields": {},
                     "consistent": d.consistent}

    def measured(info, key, row=None):
        item = dict(info)
        metadata = a.sampling.get(key)
        if metadata:
            item["sampling"] = {**metadata}
            if row is not None:
                item["sampling"]["reasons"] = [metadata["reasons"][row]]
            reasons = item["sampling"]["reasons"]
            if info.get("status") == "failed" and all(reason is None for cells in reasons for reason in cells):
                item["sampling"]["reasons"] = [["measurement_invalid"] * len(cells) for cells in reasons]
        return item

    if d.exam_no:
        student["exam_no"] = {**measured(d.exam_no, "exam_no"), "image": required("exam_no")}
    if d.absent:
        student["absent"] = measured(d.absent, "absent")
    for key, info in d.omr_fields.items():
        student["omr_fields"][key] = {**measured(info, f"omr_field/{key}"), "image": required("omr_field", key)}
    for key, bc in d.barcodes.items():
        item = {**bc, "image": required("barcode", key)}
        if key in a.barcode_evidence:
            candidates = [
                {**candidate, "image": required("barcode_candidate", key, region=index),
                 "image_rect": list(_pad(candidate["rect"]))}
                for index, candidate in enumerate(a.barcode_evidence[key].get("candidates", []), 1)
            ]
            item["candidates"] = candidates
            item["hit_image"] = next((c["image"] for c in candidates if c["id"] == item.get("selected_id")), None)
        student["barcodes"][key] = item
    for fd in t.fields:
        student["fields"][fd.key] = {"label": fd.label, "image": required("field", fd.key),
                                     "text": None, "ocr_status": ocr_status}
    objective_regions = {
        str(blk.start + k): (blk.grid.face, _objective_rect(cells), f"objective/{index}", k)
        for index, blk in enumerate(t.objective) for k, cells in enumerate(blk.grid.cells)
    }
    objective = []
    for o in d.objective:
        path = required("objective", o["no"])
        face, rect, key, row = objective_regions[o["no"]]
        item = measured(o, key, row)
        region = {"face": face, "rect": list(sampling_rect(rect, a.sampling.get(key))), "image": path, "order": 1}
        if key in a.sampling:
            region["template_rect"] = list(rect)
        item.update({"question_id": question_id("objective", o["no"]), "image": path, "images": [path],
                     "regions": [region]})
        objective.append(item)
    subjective = [{"no": sq.no, "sub": sq.sub, "type": sq.qtype, "content": sq.content,
                   "question_id": question_id("subjective", sq.no, sq.sub),
                   "images": [required("subjective", sq.no, sub=sq.sub, region=i)
                              for i in range(1, len(sq.regions) + 1)],
                   "regions": [{"face": r.face, "rect": list(r.rect), "order": i,
                                "image": required("subjective", sq.no, sub=sq.sub, region=i)}
                               for i, r in enumerate(sq.regions, start=1)],
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
        "sheet_id": f"{batch_id}:{seq:04d}",
        "input_pages": input_pages or [
            {"kind": "image" if scanner == "folder" else "scanner",
             "raw": path, "page": i + 1}
            for i, path in enumerate(raw_paths)
        ],
        "template": {"name": t.name, "version": t.version},
        "exam": exam,
        "scan": {"time": (scan_time or datetime.now().astimezone()).isoformat(timespec="seconds"),
                 "scanner": scanner, "faces": faces_info, "swapped": a.swapped,
                 "processing": {"evidence_version": a.evidence_version, "resampling": "perspective_linear",
                                "back_whitening": a.evidence_version < 2,
                                "back_whitening_level": 185 if a.evidence_version < 2 else None,
                                "raw_stage": "as_received"}},
        "pages": {**{f: required("page", f) for f in a.placements},
                  **{f"raw_{i + 1}": p for i, p in enumerate(raw_paths)}},
        "student": student,
        "objective": objective,
        "subjective": subjective,
        "choose_one": [measured(item, f"choose/{item['key']}") for item in d.choose_one],
        "manual": None,
        "warnings": d.warnings,
    }
