"""在识别进程里执行的工作：识别一张卡并写出；结束批次时按最终阈值重新判定。

函数只接收可序列化的 job，模板在每个进程里加载一次并缓存。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ..engine.omr import Thresholds
from ..imageio import imread_gray
from ..template.model import subjective_item_key
from ..engine.recognize import PhysicalError, SheetAnalysis, TemplateContext, analyze_sheet, decide_sheet, estimate_dpi
from ..engine.result import build_result, render_images
from ..engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from ..output.jsonfile import read_json, update_json
from ..output.evidence import image_path
from ..output.local import LocalExporter, write_jpg, write_json_atomic
from ..output.protection import ensure_writable_batch
from ..source.folder import choose_dpi
from ..template import load_template
from .paths import resolve_local_path, resolve_raw_path, resolve_result_path

_CTX: dict[str, TemplateContext] = {}


def get_context(template_dir: str) -> TemplateContext:
    if template_dir not in _CTX:
        tpl, bases = load_template(template_dir)
        _CTX[template_dir] = TemplateContext(tpl, bases)
    return _CTX[template_dir]


@dataclass
class SheetJob:
    seq: int
    raw_paths: list[str]  # 批次相对 raw/ 路径，也兼容旧绝对路径
    dpis: list[float | None]  # 元数据 DPI
    mark: float
    uncertain: float
    batch_dir: str
    template_dir: str
    batch_id: str
    exam: dict = field(default_factory=dict)
    scanner: str = "folder"
    ocr: bool = False
    student_dir_pattern: str = "{exam_no}"
    input_pages: list[dict] = field(default_factory=list)  # 路径相对批次目录
    manual: dict | None = None  # 人工定位：{面: {"image": 序号, "points": [[x, y] | None, ...]}}


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
    ensure_writable_batch(job.batch_dir)
    batch = Path(job.batch_dir).resolve()
    # Runner jobs may include a relative batch prefix; persisted state never does.
    raw_refs = [
        Path(p).relative_to(job.batch_dir).as_posix()
        if not Path(job.batch_dir).is_absolute() and Path(p).is_relative_to(job.batch_dir) else p
        for p in job.raw_paths
    ]
    raw_paths = [resolve_raw_path(batch, p) for p in raw_refs]
    ctx = get_context(job.template_dir)
    exporter = LocalExporter(job.batch_dir)
    imgs = [imread_gray(p) for p in raw_paths]
    bad = [p.name for p, im in zip(raw_paths, imgs) if im is None]
    try:
        if bad:
            raise PhysicalError("ALIGN_FAILED", f"无法读取图片: {bad}")
        dpis = [choose_dpi(m, estimate_dpi(im, ctx.tpl.paper)) for m, im in zip(job.dpis, imgs)]
        a = analyze_sheet(ctx, imgs, dpis, manual=job.manual)
    except PhysicalError as e:
        exporter.write_error(job.seq, e.code, e.msg, [im for im in imgs if im is not None])
        return SheetOutcome(job.seq, False, physical=e.code, message=e.msg)
    th = Thresholds(job.mark, job.uncertain)
    d = decide_sheet(ctx, a, th)
    images = render_images(ctx, a, d, imgs)
    raw_state = [p.relative_to(batch).as_posix() for p in raw_paths]
    raw_rel = [f"../../{p}" for p in raw_state]
    input_pages = [
        {k: f"../../{v}" if k in ("raw", "document") else v for k, v in origin.items()}
        for origin in job.input_pages
    ]
    result = build_result(ctx, a, d, seq=job.seq, batch_id=job.batch_id, exam=job.exam, scanner=job.scanner,
                          raw_paths=raw_rel, image_paths=set(images), ocr=job.ocr, input_pages=input_pages)
    result["content_revision"] = uuid4().hex
    exam_value = d.exam_no["value"] if d.exam_no else None
    sb = d.barcodes.get("student_barcode") or {}
    path = exporter.write_student(job.seq, exam_value, result, images, barcode=sb.get("value"),
                                  pattern=job.student_dir_pattern)
    write_json_atomic(state_path(job.batch_dir, job.seq),
                      {"analysis": analysis_to_dict(a), "raw_paths": raw_state,
                       "input_pages": job.input_pages,
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
    def decision(info):
        return {k: v for k, v in (info or {}).items()
                if k not in ("image", "images", "regions", "question_id")}

    student = result["student"]
    return (
        [decision(o) for o in result["objective"]],
        decision(student.get("exam_no")), decision(student.get("absent")),
        {k: decision(v) for k, v in student.get("omr_fields", {}).items()},
        {k: decision(v) for k, v in student.get("barcodes", {}).items()},
        student.get("consistent"), result.get("choose_one") or [],
        [w for w in result.get("warnings", []) if w.get("code") != "OCR_FAILED"],
    )


def _carry_ocr(new: dict, old: dict) -> dict:
    """重新判定时保留已有的转写结果与 OCR 警告。"""
    for key in ("content_revision", "ocr_result_id"):
        if key in old:
            new[key] = old[key]
    old_sub = {subjective_item_key(q): q for q in old.get("subjective", [])}
    for q in new["subjective"]:
        o = old_sub.get(subjective_item_key(q))
        if o:
            for k in ("text", "ocr_status", "ocr_model", "ocr_confidence"):
                q[k] = o.get(k)
            for k in ("ocr_generation", "ocr_fingerprint", "ocr_error", "ocr_skipped"):
                if k in o:
                    q[k] = o[k]
    old_fields = old["student"].get("fields", {})
    for key, f in new["student"]["fields"].items():
        if key in old_fields:
            f["text"] = old_fields[key].get("text")
            f["ocr_status"] = old_fields[key].get("ocr_status")
            for k in ("ocr_generation", "ocr_fingerprint", "ocr_model", "ocr_confidence",
                      "ocr_error", "ocr_skipped"):
                if k in old_fields[key]:
                    f[k] = old_fields[key][k]
    new["warnings"] = [*new["warnings"],
                       *[w for w in old.get("warnings", []) if w.get("code") == "OCR_FAILED"]]
    return new


def _image_paths(ctx: TemplateContext, a: SheetAnalysis) -> set[str]:
    """Expected evidence names without decoding or rendering the raw pages."""
    t = ctx.tpl
    paths = {image_path("page", face) for face in a.placements}
    if t.exam_no:
        paths.add(image_path("exam_no"))
    for kind, specs in (("barcode", t.barcodes), ("field", t.fields), ("omr_field", t.omr_fields)):
        paths.update(image_path(kind, spec.key) for spec in specs)
    for key, info in a.barcode_evidence.items():
        paths.update(image_path("barcode_candidate", key, region=i)
                     for i in range(1, len(info.get("candidates", [])) + 1))
    for block in t.objective:
        paths.update(image_path("objective", str(block.start + k)) for k in range(len(block.grid.cells)))
    for q in t.subjective:
        paths.update(image_path("subjective", q.no, sub=q.sub, region=i)
                     for i in range(1, len(q.regions) + 1))
    return paths


def finalize_sheet(job: FinalizeJob) -> FinalizeOutcome:
    """完整重判并升级旧证据元数据；仅补缺图，保留已有题图和转写身份。"""
    ensure_writable_batch(job.batch_dir)
    batch = Path(job.batch_dir).resolve()
    state = read_json(resolve_local_path(batch, f"state/{job.seq:04d}.json"))
    raw_paths = [resolve_raw_path(batch, p, seq=job.seq) for p in state["raw_paths"]]
    result_path = resolve_result_path(batch, state["result"], job.seq)
    manifest_path = resolve_local_path(batch, "manifest.json")
    manifest = read_json(manifest_path) if manifest_path.exists() else {}

    def check_identity(result: dict) -> None:
        if type(result.get("seq")) is not int or result["seq"] != job.seq:
            raise ValueError(f"Result sequence does not match sheet {job.seq}")
        if "batch_id" in manifest and result.get("batch_id") != manifest["batch_id"]:
            raise ValueError("Result batch identity does not match manifest")

    old = read_json(result_path)
    check_identity(old)
    ctx = get_context(job.template_dir)
    a = analysis_from_dict(state["analysis"])
    d = decide_sheet(ctx, a, Thresholds(job.mark, job.uncertain))
    exam_value = d.exam_no["value"] if d.exam_no else None
    image_paths = _image_paths(ctx, a)
    targets = {rel: resolve_local_path(batch, rel, base=result_path.parent) for rel in image_paths}
    if any(not path.is_relative_to(result_path.parent) for path in targets.values()):
        raise ValueError(f"Crop path does not belong to sheet {job.seq}")
    missing = {rel for rel, path in targets.items() if not path.exists()}
    raw = [Path(os.path.relpath(p, result_path.parent)).as_posix() for p in raw_paths]

    def rebuild(cur: dict) -> dict:
        # OCR updates share this lock; never trust an identity reread from disk implicitly.
        check_identity(cur)
        input_pages = cur.get("input_pages")
        if not input_pages and state.get("input_pages"):
            input_pages = [
                {key: Path(os.path.relpath(resolve_local_path(batch, value), result_path.parent)).as_posix()
                 if key in ("raw", "document") else value
                 for key, value in origin.items()}
                for origin in state["input_pages"]
            ]
        new = build_result(ctx, a, d, seq=cur["seq"], batch_id=cur["batch_id"], exam=cur["exam"],
                           scanner=cur["scan"]["scanner"], raw_paths=raw, image_paths=image_paths,
                           scan_time=datetime.fromisoformat(cur["scan"]["time"]),
                           input_pages=input_pages)
        return {**cur, **_carry_ocr(new, cur), "manual": cur.get("manual")}

    # Comparing the complete rebuilt schema also upgrades metadata on unchanged answers.
    if rebuild(old) == old and not missing:
        return FinalizeOutcome(job.seq, False, exam_value, recognition_warnings(old["warnings"]))
    if missing:
        imgs = [imread_gray(p) for p in raw_paths]
        if any(im is None for im in imgs):
            raise ValueError("Cannot read current batch raw images")
        images = render_images(ctx, a, d, imgs)
        for rel in missing:
            # Never replace existing evidence, including crops being read by OCR.
            if not targets[rel].exists():
                write_jpg(targets[rel], images[rel])
    new = update_json(result_path, rebuild)
    return FinalizeOutcome(job.seq, True, exam_value, recognition_warnings(new["warnings"]))
