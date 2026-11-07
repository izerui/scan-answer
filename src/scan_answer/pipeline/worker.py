"""在识别进程里执行的工作：识别一张卡并写出；结束批次时按最终阈值重新判定。

函数只接收可序列化的 job，模板在每个进程里加载一次并缓存。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2

from ..engine.omr import Thresholds
from ..engine.recognize import PhysicalError, TemplateContext, analyze_sheet, decide_sheet, estimate_dpi
from ..engine.result import build_result, render_images
from ..engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from ..output.jsonfile import read_json, update_json
from ..output.local import LocalExporter, write_jpg, write_json_atomic
from ..source.folder import choose_dpi
from ..template import load_template

_CTX: dict[str, TemplateContext] = {}


def get_context(template_dir: str) -> TemplateContext:
    if template_dir not in _CTX:
        tpl, bases = load_template(template_dir)
        _CTX[template_dir] = TemplateContext(tpl, bases)
    return _CTX[template_dir]


@dataclass
class SheetJob:
    seq: int
    raw_paths: list[str]  # 批次 raw/ 下原图的绝对路径
    dpis: list[float | None]  # 元数据 DPI
    mark: float
    uncertain: float
    batch_dir: str
    template_dir: str
    batch_id: str
    exam: dict = field(default_factory=dict)
    scanner: str = "folder"
    ocr: bool = False


@dataclass
class SheetOutcome:
    seq: int
    ok: bool
    physical: str | None = None
    message: str = ""
    result: str | None = None  # 相对批次目录
    exam_no: str | None = None
    warnings: int = 0
    fills: list[float] = field(default_factory=list)


def state_path(batch_dir: str | Path, seq: int) -> Path:
    return Path(batch_dir) / "state" / f"{seq:04d}.json"


def recognition_warnings(warnings: list[dict]) -> int:
    """识别警告数（不含手写转写失败 OCR_FAILED），manifest 与事件统一用这个口径。"""
    return sum(1 for w in warnings if w.get("code") != "OCR_FAILED")


def clear_outputs(batch_dir: str | Path, seq: int) -> None:
    """删除某个序号之前的识别输出（重扫前调用）；原图不在这里删。"""
    b = Path(batch_dir)
    for parent in (b / "students", b / "errors"):
        if parent.exists():
            for d in parent.glob(f"{seq:04d}*"):
                if d.name == f"{seq:04d}" or d.name.startswith(f"{seq:04d}_"):
                    shutil.rmtree(d, ignore_errors=True)
    state_path(b, seq).unlink(missing_ok=True)


def process_sheet(job: SheetJob) -> SheetOutcome:
    ctx = get_context(job.template_dir)
    exporter = LocalExporter(job.batch_dir)
    imgs = [cv2.imread(p, cv2.IMREAD_GRAYSCALE) for p in job.raw_paths]
    bad = [Path(p).name for p, im in zip(job.raw_paths, imgs) if im is None]
    try:
        if bad:
            raise PhysicalError("ALIGN_FAILED", f"无法读取图片: {bad}")
        dpis = [choose_dpi(m, estimate_dpi(im, ctx.tpl.paper)) for m, im in zip(job.dpis, imgs)]
        a = analyze_sheet(ctx, imgs, dpis)
    except PhysicalError as e:
        exporter.write_error(job.seq, e.code, e.msg, [im for im in imgs if im is not None])
        return SheetOutcome(job.seq, False, physical=e.code, message=e.msg)
    th = Thresholds(job.mark, job.uncertain)
    d = decide_sheet(ctx, a, th)
    images = render_images(ctx, a, d, imgs)
    raw_rel = [f"../../raw/{Path(p).name}" for p in job.raw_paths]
    result = build_result(ctx, a, d, seq=job.seq, batch_id=job.batch_id, exam=job.exam, scanner=job.scanner,
                          raw_paths=raw_rel, image_paths=set(images), ocr=job.ocr)
    exam_value = d.exam_no["value"] if d.exam_no else None
    path = exporter.write_student(job.seq, exam_value, result, images)
    write_json_atomic(state_path(job.batch_dir, job.seq),
                      {"analysis": analysis_to_dict(a), "raw_paths": job.raw_paths,
                       "result": str(path.relative_to(job.batch_dir))})
    return SheetOutcome(job.seq, True, result=str(path.relative_to(job.batch_dir)), exam_no=exam_value,
                        warnings=len(d.warnings), fills=analysis_fills(a).tolist())


@dataclass
class FinalizeJob:
    seq: int
    mark: float
    uncertain: float
    batch_dir: str
    template_dir: str


@dataclass
class FinalizeOutcome:
    seq: int
    changed: bool
    exam_no: str | None = None
    warnings: int = 0  # 识别警告数（不含 OCR_FAILED）


def _decision_key(result: dict) -> tuple:
    """判定结果中会受阈值影响的部分。"""
    exam = result["student"].get("exam_no") or {}
    absent = result["student"].get("absent") or {}
    return (tuple((o["no"], o["answer"], o["status"]) for o in result["objective"]),
            exam.get("value"), exam.get("status"), absent.get("value"))


def _carry_ocr(new: dict, old: dict) -> dict:
    """重新判定时保留已有的转写结果与 OCR 警告。"""
    old_sub = {q["no"]: q for q in old.get("subjective", [])}
    for q in new["subjective"]:
        o = old_sub.get(q["no"])
        if o:
            for k in ("text", "ocr_status", "ocr_model", "ocr_confidence"):
                q[k] = o.get(k)
    old_fields = old["student"].get("fields", {})
    for key, f in new["student"]["fields"].items():
        if key in old_fields:
            f["text"] = old_fields[key].get("text")
            f["ocr_status"] = old_fields[key].get("ocr_status")
    new["warnings"] += [w for w in old.get("warnings", []) if w.get("code") == "OCR_FAILED"]
    return new


def finalize_sheet(job: FinalizeJob) -> FinalizeOutcome:
    """按最终阈值重新判定；结果有变化才重新裁图并改写 result.json（保留转写结果）。"""
    ctx = get_context(job.template_dir)
    state = read_json(state_path(job.batch_dir, job.seq))
    a = analysis_from_dict(state["analysis"])
    d = decide_sheet(ctx, a, Thresholds(job.mark, job.uncertain))
    result_path = Path(job.batch_dir) / state["result"]
    old = read_json(result_path)
    probe = {"objective": d.objective, "student": {"exam_no": d.exam_no, "absent": d.absent}}
    exam_value = d.exam_no["value"] if d.exam_no else None
    if _decision_key(probe) == _decision_key(old):
        return FinalizeOutcome(job.seq, False, exam_value, recognition_warnings(old["warnings"]))
    imgs = [cv2.imread(p, cv2.IMREAD_GRAYSCALE) for p in state["raw_paths"]]
    images = render_images(ctx, a, d, imgs)
    for stale in (result_path.parent / "images").glob("obj_*.jpg"):
        stale.unlink()
    for rel, img in images.items():
        write_jpg(result_path.parent / rel, img)

    def rebuild(cur: dict) -> dict:
        raw = [cur["pages"][k] for k in sorted(cur["pages"]) if k.startswith("raw_")]
        new = build_result(ctx, a, d, seq=cur["seq"], batch_id=cur["batch_id"], exam=cur["exam"],
                           scanner=cur["scan"]["scanner"], raw_paths=raw, image_paths=set(images),
                           scan_time=datetime.fromisoformat(cur["scan"]["time"]))
        return _carry_ocr(new, cur)

    new = update_json(result_path, rebuild)
    return FinalizeOutcome(job.seq, True, exam_value, recognition_warnings(new["warnings"]))
