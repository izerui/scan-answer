"""结果浏览的数据层：读批次 manifest / result.json，算出卡面叠层（mm 坐标），与界面无关、可单测。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..template.io import template_from_dict
from ..engine.result import sampling_rect
from ..output.audit import duplicate_exam_numbers
from ..pipeline.paths import resolve_result_path
from ..template.model import Rect, Template


@dataclass
class StudentRow:
    seq: int
    source: str
    status: str  # ok / physical / voided / processing / broken（manifest 记录的结果路径不可用）
    exam_no: str | None
    warnings: int
    result: Path | None
    physical: str | None
    message: str
    ocr_failed: int = 0
    ocr_pending: int = 0  # 等待转写（阻止交付）
    duplicate_of: tuple[int, ...] = ()  # 识别出同一考号的其他有效答卷序号


def _ocr_counts(result_path: Path | None) -> tuple[int, int]:
    """(转写失败项数, 等待转写项数)，主观题与信息栏都计入；读不了按 0 处理。"""
    if result_path is None:
        return 0, 0
    try:
        return ocr_counts_in(json.loads(result_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, AttributeError, TypeError):
        return 0, 0


def ocr_counts_in(result: dict) -> tuple[int, int]:
    """已读出的 result.json 内容中的 (转写失败项数, 等待转写项数)；结构不对时抛 AttributeError/TypeError。"""
    items = [*(result.get("subjective") or []),
             *((result.get("student") or {}).get("fields") or {}).values()]
    states = [item.get("ocr_status") for item in items if isinstance(item, dict)]
    return states.count("failed"), states.count("pending")


def _ocr_failures(result_path: Path | None) -> int:
    return _ocr_counts(result_path)[0]


BROKEN_RESULT_HINT = ("manifest 记录的结果文件路径无效，读不到识别结果。"
                      "可在考试页对该批次点“继续扫描”：程序对账时能从识别状态恢复的会补建结果，"
                      "否则转为待重扫的异常卡，再在扫描页重扫或作废；也可从备份恢复批次目录。")


def load_rows(batch_dir: str | Path, ocr: bool = True) -> list[StudentRow]:
    """读取批次的学生行。ocr=False 时不读各份 result.json，转写失败/等待项记为 0（只读 manifest，很快）。"""
    b = Path(batch_dir).resolve()
    m = json.loads((b / "manifest.json").read_text(encoding="utf-8"))
    rows = []
    for s in m.get("sheets", []):
        try:
            result = resolve_result_path(b, s["result"], int(s["seq"])) if s.get("result") else None
        except (OSError, ValueError, TypeError):
            # manifest 里是成功卡，扫描页没有可处置的异常；单独归为“结果不可用”并给出可行指引
            rows.append(StudentRow(int(s["seq"]), s.get("source", ""), "broken", s.get("exam_no"), 0,
                                   None, None, BROKEN_RESULT_HINT))
            continue
        failed, pending = _ocr_counts(result) if ocr else (0, 0)
        rows.append(StudentRow(int(s["seq"]), s.get("source", ""), s.get("status", ""), s.get("exam_no"),
                               int(s.get("warnings", 0)), result,
                               s.get("physical"), s.get("message", ""), failed, pending))
    dup = {seq: seqs for seqs in duplicate_exam_numbers(m.get("sheets", [])).values() for seq in seqs}
    for r in rows:
        if r.seq in dup:
            r.duplicate_of = tuple(s for s in dup[r.seq] if s != r.seq)
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
    status_text = {"blank": "未答", "multiple": "多涂", "uncertain": "存疑", "failed": "未识别"}
    objective = {o["no"]: o for o in result.get("objective", [])}
    for blk in tpl.objective:
        if blk.grid.face != face:
            continue
        for k, cells in enumerate(blk.grid.cells):
            o = objective.get(str(blk.start + k))
            if not o:
                continue
            cells = [sampling_rect(c, o.get("sampling")) for c in cells]
            for i, lab in enumerate(blk.grid.labels):
                if lab in o.get("answer", ""):
                    out.append(OverlayMM(cells[i], "answer" if o["status"] == "ok" else "bad"))
            if o["status"] != "ok":
                out.append(OverlayMM(_union(cells), "bad", f"{o['no']} {status_text.get(o['status'], o['status'])}"))
    exam = result.get("student", {}).get("exam_no")
    if tpl.exam_no and tpl.exam_no.grid.face == face and exam:
        bad_pos = {d["pos"] for d in exam.get("digits", [])}
        for pos, (ch, cells) in enumerate(zip(exam.get("value", ""), tpl.exam_no.grid.cells), start=1):
            cells = [sampling_rect(c, exam.get("sampling")) for c in cells]
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
            cells = [sampling_rect(c, info.get("sampling")) for c in cells]
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
        effective_cells = [sampling_rect(c, info.get("sampling")) for c in co.cells]
        for i, q in enumerate(co.questions):
            if q in info.get("chosen", []):
                out.append(OverlayMM(effective_cells[i], kind))
        if info.get("status") != "ok":
            all_cells = effective_cells
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
    "ALIGN_DERIVED": None, "ANCHOR_MISSING": None, "ALIGN_MANUAL": None, "BARCODE_RELOCATED": "条码在区域外找到（贴歪或贴出框）",
    "OBJ_FAILED": "第 {no} 题采样失败，不能确定答案",
    "SAMPLE_INVALID": "填涂采样区域不完整或无法定位，请核对原图",
    "BARCODE_AMBIGUOUS": "条码候选有歧义，请核对候选图片",
    "FACE_SWAPPED": "正反面顺序与导入顺序相反，请核对是否串卷",
    "IDENTITY_PARTIAL": "部分标识区与模板不符，请核对是否混卡",
    "IDENTITY_CONFIRMED": "操作员确认不是混卡后按本模板识别，请核对",
}


_WARNING_FALLBACK = {
    "ALIGN_DERIVED": "背面定位失败，按正面推算切图",
    "OMR_FIELD_PARTIAL": "填涂字段有位未识别",
    "OMR_FIELD_BLANK": "填涂字段未填涂",
}

PHYSICAL_TEXT = {"ALIGN_FAILED": "定位失败", "FACE_MISMATCH": "正反面无法匹配", "BLANK_PAGE": "空白页",
                 "MISSING_FACE": "缺少一面", "SKEW_TOO_LARGE": "倾斜过大",
                 "WRONG_CARD": "混卡（卡面与模板不符）"}


def warning_text(w: dict) -> str:
    """把 warnings 条目转成界面文案；未知代码原样显示。"""
    code, field, msg = w.get("code", ""), w.get("field", ""), w.get("msg") or ""
    tpl = WARNING_TEXT.get(code)
    if tpl and "{no}" in tpl and field.startswith("objective."):
        return tpl.format(no=field.split(".", 1)[1])
    if tpl:
        return tpl
    return msg or _WARNING_FALLBACK.get(code, "") or code
