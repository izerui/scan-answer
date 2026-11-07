"""结果浏览的数据层：读批次 manifest / result.json，算出卡面叠层（mm 坐标），与界面无关、可单测。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..template.io import template_from_dict
from ..template.model import Rect, Template


@dataclass
class StudentRow:
    seq: int
    source: str
    status: str  # ok / physical / voided / processing
    exam_no: str | None
    warnings: int
    result: Path | None
    physical: str | None
    message: str


def load_rows(batch_dir: str | Path) -> list[StudentRow]:
    b = Path(batch_dir)
    m = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for s in m.get("sheets", []):
        rows.append(StudentRow(int(s["seq"]), s.get("source", ""), s.get("status", ""), s.get("exam_no"),
                               int(s.get("warnings", 0)), b / s["result"] if s.get("result") else None,
                               s.get("physical"), s.get("message", "")))
    return rows


def load_batch_template(batch_dir: str | Path) -> Template:
    """批次内的模板快照（新批次在 template/，旧批次在根目录 template.json）。"""
    b = Path(batch_dir)
    p = b / "template" / "template.json"
    if not p.exists():
        p = b / "template.json"
    return template_from_dict(json.loads(p.read_text(encoding="utf-8")))


@dataclass
class OverlayMM:
    rect: Rect  # mm
    kind: str  # answer / bad / exam / exam_bad
    label: str = ""


def _union(rects: list[Rect]) -> Rect:
    x0 = min(r[0] for r in rects)
    y0 = min(r[1] for r in rects)
    x1 = max(r[0] + r[2] for r in rects)
    y1 = max(r[1] + r[3] for r in rects)
    return (x0, y0, x1 - x0, y1 - y0)


def face_overlays(tpl: Template, result: dict, face: str) -> list[OverlayMM]:
    """某一面的叠层：识别到的选项（answer）、非 ok 的题整行（bad，带题号与状态）、考号所涂格（exam / exam_bad）。"""
    out: list[OverlayMM] = []
    status_text = {"blank": "未答", "multiple": "多涂", "uncertain": "存疑"}
    objective = {o["no"]: o for o in result.get("objective", [])}
    for blk in tpl.objective:
        if blk.grid.face != face:
            continue
        for k, cells in enumerate(blk.grid.cells):
            o = objective.get(str(blk.start + k))
            if not o:
                continue
            for i, lab in enumerate(blk.grid.labels):
                if lab in o.get("answer", ""):
                    out.append(OverlayMM(cells[i], "answer" if o["status"] == "ok" else "bad"))
            if o["status"] != "ok":
                out.append(OverlayMM(_union(cells), "bad", f"{o['no']} {status_text.get(o['status'], o['status'])}"))
    exam = result.get("student", {}).get("exam_no")
    if tpl.exam_no and tpl.exam_no.grid.face == face and exam:
        bad_pos = {d["pos"] for d in exam.get("digits", [])}
        for pos, (ch, cells) in enumerate(zip(exam.get("value", ""), tpl.exam_no.grid.cells), start=1):
            if pos in bad_pos:
                out.append(OverlayMM(_union(cells), "exam_bad", f"第{pos}位"))
            elif ch in tpl.exam_no.grid.labels:
                out.append(OverlayMM(cells[tpl.exam_no.grid.labels.index(ch)], "exam"))
    # 自定义填涂字段
    omr_fields = result.get("student", {}).get("omr_fields", {})
    for of in tpl.omr_fields:
        if of.grid.face != face:
            continue
        info = omr_fields.get(of.key)
        if not info:
            continue
        for pos, (ch, cells) in enumerate(zip(info.get("value", ""), of.grid.cells), start=1):
            bad_positions = {d["pos"] for d in info.get("digits", [])}
            if pos in bad_positions:
                out.append(OverlayMM(_union(cells), "exam_bad", f"{of.label}第{pos}位"))
            elif ch in of.grid.labels:
                out.append(OverlayMM(cells[of.grid.labels.index(ch)], "omr"))
    # 选做组
    choose_map = {c["key"]: c for c in result.get("choose_one", [])}
    for co in tpl.choose_one:
        if co.face != face:
            continue
        info = choose_map.get(co.key)
        if not info:
            continue
        kind = "choice" if info.get("status") == "ok" else "bad"
        for i, q in enumerate(co.questions):
            if q in info.get("chosen", []):
                out.append(OverlayMM(co.cells[i], kind))
        if info.get("status") != "ok":
            all_cells = co.cells
            out.append(OverlayMM(_union(all_cells), "bad",
                                 f"{co.label} {status_text.get(info.get('status', ''), info.get('status', ''))}"))
    return out


WARNING_TEXT = {
    "OBJ_BLANK": "第 {no} 题未作答", "OBJ_MULTIPLE": "第 {no} 题多涂", "OBJ_UNCERTAIN": "第 {no} 题涂得浅或擦除不净",
    "EXAM_NO_PARTIAL": None, "EXAM_NO_BLANK": "考号未填涂", "ABSENT_MARKED": "缺考标记已填涂",
    "ID_MISMATCH": "填涂考号与条码不一致", "BARCODE_MISSING": "考生条码未识别", "ALIGN_FALLBACK": None,
    "OCR_FAILED": None,
    "OMR_FIELD_PARTIAL": None, "OMR_FIELD_BLANK": None,
    "CHOICE_BLANK": "选做题未填涂", "CHOICE_MULTIPLE": "选做题多涂", "CHOICE_UNCERTAIN": "选做题涂得浅或擦除不净",
}


def warning_text(w: dict) -> str:
    """把 warnings 条目转成界面文案；未知代码原样显示。"""
    code, field, msg = w.get("code", ""), w.get("field", ""), w.get("msg") or ""
    tpl = WARNING_TEXT.get(code)
    if tpl and "{no}" in tpl and field.startswith("objective."):
        return tpl.format(no=field.split(".", 1)[1])
    if tpl:
        return tpl
    return msg or code
