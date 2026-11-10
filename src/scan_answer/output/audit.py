"""Read-only batch delivery validation and content-addressed payload inventory."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path

from PIL import Image

from ..engine.result import _objective_rect, _pad, sampling_rect
from ..source.pdf import pdf_page_count
from ..template.io import template_from_dict
from ..template.model import subjective_item_key
from ..template.validate import validate_template
from .evidence import image_path, question_id
from .paths import resolve_path

_RUNTIME = {"state", "acquisition", "attempts"}

ISSUE_LABELS: dict[str, str] = {
    "invalid_structure": "数据结构不正确",
    "invalid_path": "文件路径不安全",
    "file_unreadable": "文件缺失或无法读取",
    "payload_changed": "检查期间文件发生变化",
    "image_invalid": "图片损坏或无法完整解码",
    "json_invalid": "JSON 文件损坏",
    "image_reused": "不同证据共用了图片",
    "evidence_identity": "图片路径与证据身份不一致",
    "template_invalid": "模板快照不合法",
    "template_mismatch": "模板名称、版本或快照不一致",
    "ocr_pending": "文字转写尚未完成",
    "ocr_failed": "文字转写失败",
    "ocr_skipped": "文字转写已放弃（标为未启用）",
    "ocr_status_invalid": "文字转写状态不正确",
    "question_invalid": "题目编号不合法",
    "question_identity": "题目身份不一致",
    "question_regions": "题目区域或图片顺序不一致",
    "question_coverage": "题目缺失、重复或顺序不一致",
    "student_fields": "学生字段或条码与模板不一致",
    "sampling_invalid": "采样位置或有效性记录不一致",
    "barcode_evidence": "条码候选、采用结果或证据不一致",
    "source_invalid": "页面来源记录不合法",
    "source_duplicate": "同一原始页被重复使用",
    "source_mapping": "原图与来源记录不一致",
    "pdf_digest": "原始 PDF 内容摘要不一致",
    "pdf_page_bounds": "页码超出原始 PDF 范围",
    "pdf_invalid": "原始 PDF 无法读取",
    "page_mapping": "整页图片与原始页映射不一致",
    "result_identity": "结果的批次、序号或目录不一致",
    "result_reused": "不同答卷共用了结果文件",
    "schema_version": "不支持的数据格式版本",
    "recognition_warning": "识别结果有警告",
    "sheet_voided": "已作废答卷明确排除",
    "void_evidence_missing": "旧作废答卷的部分证据缺失",
    "void_invalid": "作废答卷缺少明确处置标记",
    "delivery_invalid": "交付封存记录不合法",
    "delivery_revision": "交付封存版本不一致",
    "delivery_inventory": "交付文件摘要清单不一致",
    "batch_identity": "批次标识不合法",
    "not_finalized": "批次尚未完成结算",
    "rejudge_failed": "最终重新判定失败",
    "empty_batch": "空批次不能交付",
    "sequence_invalid": "答卷序号不合法",
    "sequence_duplicate": "答卷序号重复",
    "sheet_unresolved": "答卷仍有未处理异常",
    "exam_no_duplicate": "多份答卷识别出同一考号",
}


@dataclass
class AuditReport:
    ready: bool
    status: str
    issues: list[dict]
    files: dict[str, str]
    summary: dict
    checked_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def duplicate_exam_numbers(sheets: list) -> dict[str, list[int]]:
    """有效答卷（status 为 ok）中出现不止一次的完整考号 -> 序号列表；含 ? 的残缺考号不参与比较。"""
    owners: dict[str, list[int]] = {}
    for sheet in sheets:
        if not isinstance(sheet, dict) or sheet.get("status") != "ok" or type(sheet.get("seq")) is not int:
            continue
        exam_no = sheet.get("exam_no")
        if isinstance(exam_no, str) and exam_no and "?" not in exam_no:
            owners.setdefault(exam_no, []).append(sheet["seq"])
    return {k: sorted(v) for k, v in owners.items() if len(v) > 1}


class _Invalid(ValueError):
    """An issue has already been recorded for this malformed branch."""


def _reject_constant(value: str):
    raise ValueError(f"Non-finite JSON value: {value}")


class _Audit:
    def __init__(self, root: Path):
        self.root = root
        self.issues: list[dict] = []
        self.files: dict[str, str] = {}
        self.summary = {"total": 0, "ok": 0, "voided": 0, "ocr_pending": 0,
                        "ocr_failed": 0, "ocr_skipped": 0, "excluded": []}
        self.seq = None
        self.location = "manifest.json"
        self.optional = False
        self.image_owners: dict[Path, tuple[int, str]] = {}
        self.result_owners: dict[Path, int] = {}
        self.pdf_metadata: dict[Path, tuple[str, int | None, str | None]] = {}
        self.require_evidence = False
        self.ocr_reasons: dict[str, str] = {}  # 旧结果只在 OCR_FAILED 警告里记录失败原因

    def duplicate_exam_numbers(self, sheets: list) -> None:
        """同一批次里多份有效答卷识别出同一个完整考号：至少有一份对应错人（重复扫描或考号识别/填涂错误）。

        作为说明项列出而不阻断：重复扫描的作废其中一份即可；确属不同考生时需人工核对原图。"""
        owners = duplicate_exam_numbers(sheets)
        for exam_no, seqs in owners.items():
            self.issue("exam_no_duplicate",
                       f"考号 {exam_no} 出现在 {len(seqs)} 份答卷中（" + "、".join(f"#{s:04d}" for s in seqs)
                       + "）。重复扫描的请作废多余的一份；不同考生的请核对原图", warning=True)

    def issue(self, code: str, message: str, path=None, *, warning=False):
        label = ISSUE_LABELS.get(code, code)
        if not any("\u4e00" <= char <= "\u9fff" for char in message):
            message = f"{label}：{message}"
        self.issues.append({"severity": "warning" if warning or self.optional else "error",
                            "code": code, "message": message, "seq": self.seq,
                            "path": str(path) if path is not None else self.location})

    def obj(self, value, label: str) -> dict:
        if not isinstance(value, dict):
            self.issue("invalid_structure", f"{label} must be an object")
            raise _Invalid(label)
        return value

    def array(self, value, label: str) -> list:
        if not isinstance(value, list):
            self.issue("invalid_structure", f"{label} must be a list")
            raise _Invalid(label)
        return value

    def path(self, base: Path, reference, *, scope: Path | None = None) -> Path | None:
        try:
            path = resolve_path(self.root, base, reference)
            real = path.resolve()
            if (path.relative_to(self.root).parts[0] in _RUNTIME
                    or real.relative_to(self.root).parts[0] in _RUNTIME
                    or path == self.root / "delivery.json"):
                raise ValueError("Runtime state and delivery.json are not payload references")
            if scope is not None and (not path.is_relative_to(scope)
                                      or not real.is_relative_to(scope.resolve())):
                raise ValueError("Evidence belongs to a different student or payload directory")
            return path
        except (ValueError, OSError, RuntimeError) as exc:
            # An unsafe path is never downgraded, even for an excluded sheet.
            optional, self.optional = self.optional, False
            self.issue("invalid_path", str(exc), repr(reference))
            self.optional = optional
            return None

    def data(self, path: Path | None, *, image=False) -> bytes | None:
        if path is None:
            return None
        rel = path.relative_to(self.root).as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.issue("void_evidence_missing" if self.optional else "file_unreadable",
                       f"Cannot read payload: {exc}", rel)
            return None
        digest = hashlib.sha256(data).hexdigest()
        previous = self.files.get(rel)
        if previous is not None and previous != digest:
            self.issue("payload_changed", "Payload changed during audit", rel)
        self.files[rel] = digest
        if image:
            try:
                with Image.open(BytesIO(data)) as picture:
                    picture.verify()
                with Image.open(BytesIO(data)) as picture:
                    for frame in range(getattr(picture, "n_frames", 1)):
                        picture.seek(frame)
                        picture.load()
            except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
                self.issue("image_invalid", f"Image cannot be fully decoded: {exc}", rel)
        return data

    def json(self, path: Path | None) -> dict | None:
        data = self.data(path)
        if data is None:
            return None
        try:
            value = json.loads(data, parse_constant=_reject_constant)
            return self.obj(value, path.name)
        except _Invalid:
            return None
        except (ValueError, UnicodeError, RecursionError) as exc:
            self.issue("json_invalid", f"Cannot read JSON: {exc}", path.relative_to(self.root))
            return None

    def pdf(self, path: Path | None) -> tuple[str, int | None, str | None] | None:
        if path is None:
            return None
        if path not in self.pdf_metadata:
            data = self.data(path)
            if data is None:
                return None
            digest = self.files[path.relative_to(self.root).as_posix()]
            metadata: tuple[str, int | None, str | None]
            try:
                metadata = (digest, pdf_page_count(data), None)
            except ValueError as exc:
                metadata = (digest, None, str(exc))
            # Keep only metadata; run() rereads the payload to detect concurrent changes.
            self.pdf_metadata[path] = metadata
        return self.pdf_metadata[path]

    def image(self, base: Path, reference, label: str, *, scope: Path | None = None,
              expected: str | None = None) -> Path | None:
        path = self.path(base, reference, scope=scope)
        if path is not None and expected is not None:
            canonical = self.path(base, expected, scope=scope)
            if canonical is not None and path.resolve() != canonical.resolve():
                self.issue("evidence_identity",
                           f"Image does not match {label}; expected {expected}", reference)
        self.data(path, image=True)
        if path is not None and self.seq is not None:
            key = path.resolve()
            owner = (self.seq, label)
            if key in self.image_owners and self.image_owners[key] != owner:
                self.issue("image_reused", f"Image is shared by different evidence items: {label}", reference)
            self.image_owners[key] = owner
        return path

    def template(self, manifest: dict):
        identity = self.obj(manifest.get("template"), "manifest.template")
        snapshot = self.path(self.root, identity.get("snapshot"), scope=self.root / "template")
        data = self.json(snapshot)
        if data is None:
            return None
        try:
            template = template_from_dict(data)
            problems = validate_template(template)
            for message in problems:
                self.issue("template_invalid", message, snapshot.relative_to(self.root))
            if problems:
                return None
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
            self.issue("template_invalid", f"Invalid template structure: {exc}")
            return None
        if (type(data.get("version")) is not int or type(identity.get("version")) is not int
                or identity.get("name") != template.name or identity.get("version") != template.version):
            self.issue("template_mismatch", "Manifest template identity differs from snapshot")
        for face in template.faces.values():
            path = self.path(snapshot.parent, face.image, scope=snapshot.parent)
            self.data(path, image=True)
        # The root copy is compatibility metadata; its asset references belong to the snapshot.
        legacy = self.root / "template.json"
        if legacy.exists() or legacy.is_symlink():
            copied = self.json(self.path(self.root, "template.json"))
            if copied is not None and copied != data:
                self.issue("template_mismatch", "Template compatibility copy differs from snapshot", "template.json")
        return template

    def ocr(self, item: dict, label: str, *, required=False, field: str | None = None):
        status = item.get("ocr_status", None if required else "none")
        if status == "pending":
            self.summary["ocr_pending"] += 1
            self.issue("ocr_pending", f"文字转写尚未完成：{label}")
        elif status == "failed":
            self.summary["ocr_failed"] += 1
            # 原因以条目的 ocr_error 为准；旧结果没有该字段时取同一 field 的 OCR_FAILED 警告
            reason = item.get("ocr_error") or self.ocr_reasons.get(field or "", "")
            self.issue("ocr_failed", f"文字转写失败：{label}" + (f"；{reason}" if reason else ""), warning=True)
        elif status == "none" and isinstance(item.get("ocr_skipped"), dict):
            self.summary["ocr_skipped"] += 1
            info = item["ocr_skipped"]
            detail = "；".join(str(v) for v in (info.get("reason"), info.get("time")) if v)
            self.issue("ocr_skipped", f"文字转写已放弃（标为未启用）：{label}" + (f"；{detail}" if detail else ""),
                       warning=True)
        elif status not in ("none", "done"):
            self.issue("ocr_status_invalid", f"Unknown OCR status for {label}: {status!r}")

    def sampling(self, item: dict, face: str, shape: list[int]) -> dict:
        if "sampling" not in item:
            if self.require_evidence:
                self.issue("sampling_invalid", "Version 2 result is missing sampling evidence")
            return {}
        metadata = self.obj(item["sampling"], "sampling")
        shift = metadata.get("shift_mm")
        reasons = metadata.get("reasons")
        valid_shift = (isinstance(shift, list) and len(shift) == 2
                       and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 1.6 for v in shift))
        valid_reasons = (isinstance(reasons, list) and len(reasons) == len(shape)
                         and all(isinstance(row, list) and len(row) == count
                                 and all(v is None or isinstance(v, str) and bool(v) for v in row)
                                 for row, count in zip(reasons, shape)))
        if metadata.get("face") != face or not valid_shift or not valid_reasons:
            self.issue("sampling_invalid", "Invalid sampling face, displacement or cell reasons")
            return {}
        if any(reason is not None for row in reasons for reason in row) and item.get("status") != "failed":
            self.issue("sampling_invalid", "Invalid samples must not be reported as readable")
        return metadata

    def barcode_candidates(self, entry: dict, spec, base: Path, selected_hits: set):
        if "candidates" not in entry:
            if self.require_evidence or entry.get("selected_id") is not None or entry.get("hit_image") is not None:
                self.issue("barcode_evidence", "Candidate evidence is missing")
            return
        candidates = self.array(entry["candidates"], "barcode.candidates")
        ids = set()
        selected = []
        for index, value in enumerate(candidates, 1):
            candidate = self.obj(value, "barcode.candidate")
            identity = candidate.get("id")
            if not isinstance(identity, str) or not identity or identity in ids:
                self.issue("barcode_evidence", "Missing or duplicate candidate identity")
                continue
            else:
                ids.add(identity)
            path = image_path("barcode_candidate", spec.key, region=index)
            self.image(base, candidate.get("image"), f"barcode_candidates/{spec.key}/{index}",
                       scope=base, expected=path)
            rect, polygon = candidate.get("rect"), candidate.get("polygon")
            valid_rect = (isinstance(rect, list) and len(rect) == 4
                          and all(type(v) in (int, float) and math.isfinite(v) for v in rect)
                          and rect[2] > 0 and rect[3] > 0)
            valid_polygon = (isinstance(polygon, list) and len(polygon) == 4
                             and all(isinstance(p, list) and len(p) == 2
                                     and all(type(v) in (int, float) and math.isfinite(v) for v in p)
                                     for p in polygon))
            if valid_rect and valid_polygon:
                xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
                bounds = [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]
                if any(abs(x - y) > 1e-6 for x, y in zip(bounds, rect)):
                    valid_rect = False
            if (candidate.get("face") != spec.face or not valid_rect or not valid_polygon
                    or candidate.get("image_rect") != list(_pad(rect))
                    or not isinstance(candidate.get("value"), str) or not candidate["value"]
                    or not isinstance(candidate.get("symbology"), str)
                    or candidate.get("located") not in ("region", "expanded", "page")):
                self.issue("barcode_evidence", "Invalid candidate position or decoding evidence")
            if identity is not None and identity == entry.get("selected_id"):
                selected.append(candidate)
        if entry.get("status") == "ok":
            if len(selected) != 1:
                self.issue("barcode_evidence", "Recognized barcode must select exactly one candidate")
            else:
                hit = selected[0]
                owner = (spec.face, hit["id"])
                if owner in selected_hits:
                    self.issue("barcode_evidence", "One physical barcode is assigned to several fields")
                selected_hits.add(owner)
                if (entry.get("value") != hit.get("value") or entry.get("symbology") != hit.get("symbology")
                        or entry.get("located") != hit.get("located") or entry.get("hit_image") != hit.get("image")):
                    self.issue("barcode_evidence", "Selected barcode differs from its evidence")
        elif (entry.get("status") not in ("failed", "ambiguous") or entry.get("selected_id") is not None
              or entry.get("hit_image") is not None or entry.get("value") is not None):
            self.issue("barcode_evidence", "Unresolved barcode must not claim a selected value or image")

    def questions(self, result: dict, template, base: Path):
        expected = {"objective": [], "subjective": []}
        for block in template.objective:
            for offset, cells in enumerate(block.grid.cells):
                expected["objective"].append({
                    "no": str(block.start + offset), "sub": "", "type": block.qtype,
                    "regions": [(block.grid.face, list(_objective_rect(cells)))],
                    "sample_shape": [len(cells)],
                })
        for question in template.subjective:
            expected["subjective"].append({
                "no": question.no, "sub": question.sub, "type": question.qtype,
                "content": question.content,
                "regions": [(r.face, list(r.rect)) for r in question.regions],
            })
        for category, wanted in expected.items():
            entries = self.array(result.get(category), category)
            actual_keys = []
            for index, entry in enumerate(entries):
                try:
                    item = self.obj(entry, f"{category}[{index}]")
                    no, sub = item.get("no"), item.get("sub", "")
                    if not isinstance(no, str) or not isinstance(sub, str):
                        self.issue("question_invalid", "Question number and sub-number must be strings")
                        continue
                    actual_keys.append((no, sub))
                    label = question_id(category, no, sub)
                    if item.get("question_id") != label:
                        self.issue("question_identity", f"Question identity does not match: {label}")
                    spec = next((q for q in wanted if (q["no"], q["sub"]) == (no, sub)), None)
                    metadata = (self.sampling(item, spec["regions"][0][0], spec["sample_shape"])
                                if category == "objective" and spec is not None else {})
                    images = self.array(item.get("images"), f"{label}.images")
                    regions = self.array(item.get("regions"), f"{label}.regions")
                    if not images or len(images) != len(regions):
                        self.issue("question_regions", f"Missing images or inconsistent region count: {label}")
                    if category == "objective":
                        if not isinstance(item.get("answer"), str):
                            self.issue("question_invalid", f"Objective answer must be a string: {label}")
                        if item.get("status") not in ("ok", "blank", "multiple", "uncertain", "partial", "failed"):
                            self.issue("question_invalid", f"Objective status is missing or invalid: {label}")
                        if len(images) != 1:
                            self.issue("question_regions", f"Objective requires exactly one image: {label}")
                        self.image(base, item.get("image"), f"{label}/1", scope=base,
                                   expected=image_path(category, no))
                    for order, reference in enumerate(images, 1):
                        self.image(base, reference, f"{label}/{order}", scope=base,
                                   expected=image_path(category, no, sub=sub, region=order))
                    for order, region in enumerate(regions, 1):
                        region = self.obj(region, f"{label}.regions[{order}]")
                        if region.get("order") != order or order > len(images):
                            self.issue("question_regions", f"Region order/image differs: {label}/{order}")
                        self.image(base, region.get("image"), f"{label}/{order}", scope=base,
                                   expected=image_path(category, no, sub=sub, region=order))
                        if spec is not None and order <= len(spec["regions"]):
                            face, rect = spec["regions"][order - 1]
                            effective = list(sampling_rect(rect, metadata))
                            if (region.get("face") != face or region.get("rect") != effective
                                    or metadata and region.get("template_rect") != rect):
                                self.issue("question_regions", f"Region geometry differs: {label}/{order}")
                    if spec is not None:
                        if len(regions) != len(spec["regions"]) or item.get("type") != spec["type"]:
                            self.issue("question_regions", f"Question type/region count differs: {label}")
                        if category == "subjective" and item.get("content") != spec["content"]:
                            self.issue("question_identity", f"Question content type differs: {label}")
                    self.ocr(item, label, required=category == "subjective",
                             field=f"subjective.{subjective_item_key(item)}" if category == "subjective" else None)
                except _Invalid:
                    continue
            wanted_keys = [(q["no"], q["sub"]) for q in wanted]
            if actual_keys != wanted_keys or len(set(actual_keys)) != len(actual_keys):
                self.issue("question_coverage", f"Question identities/order/uniqueness differ: {category}")

    def student(self, result: dict, template, base: Path):
        student = self.obj(result.get("student"), "student")
        exam = student.get("exam_no")
        if template.exam_no is not None:
            exam = self.obj(exam, "student.exam_no")
            self.image(base, exam.get("image"), "exam_no", scope=base,
                       expected=image_path("exam_no"))
            grid = template.exam_no.grid
            self.sampling(exam, grid.face, [len(row) for row in grid.cells])
        elif exam is not None:
            self.issue("student_fields", "Unexpected exam number evidence")
        selected_hits = set()
        for category, kind in (("fields", "field"), ("barcodes", "barcode"), ("omr_fields", "omr_field")):
            fields = self.obj(student.get(category), f"student.{category}")
            expected = {field.key for field in getattr(template, category)}
            if set(fields) != expected:
                self.issue("student_fields", f"Student {category} keys differ from template")
            for key, value in fields.items():
                entry = self.obj(value, f"student.{category}.{key}")
                self.image(base, entry.get("image"), f"{category}/{key}", scope=base,
                           expected=image_path(kind, key))
                self.ocr(entry, f"{category}/{key}", required=category == "fields",
                         field=f"student.fields.{key}" if category == "fields" else None)
                spec = next((s for s in getattr(template, category) if s.key == key), None)
                if spec is not None and category == "barcodes":
                    self.barcode_candidates(entry, spec, base, selected_hits)
                elif spec is not None and category == "omr_fields":
                    self.sampling(entry, spec.grid.face, [len(row) for row in spec.grid.cells])
        if template.absent and isinstance(student.get("absent"), dict):
            self.sampling(student["absent"], template.absent.face, [1])
        elif template.absent and self.require_evidence:
            self.issue("sampling_invalid", "Absent measurement evidence is missing")
        choices = result.get("choose_one", [])
        if self.require_evidence:
            choices = self.array(choices, "choose_one")
            keys = [self.obj(item, "choose_one").get("key") for item in choices]
            if keys != [spec.key for spec in template.choose_one]:
                self.issue("sampling_invalid", "Choice group evidence differs from template")
        for item in choices or []:
            item = self.obj(item, "choose_one")
            spec = next((s for s in template.choose_one if s.key == item.get("key")), None)
            if spec:
                self.sampling(item, spec.face, [len(spec.cells)])

    def origins(self, entries, base: Path) -> list[dict]:
        normalized = []
        raw_pages = set()
        pdf_pages = set()
        for index, entry in enumerate(self.array(entries, "input_pages"), 1):
            try:
                origin = self.obj(entry, f"input_pages[{index}]")
                item = dict(origin)
                if origin.get("kind") not in ("image", "scanner", "pdf"):
                    self.issue("source_invalid", "Input page kind is unknown")
                number = origin.get("page")
                if type(number) is not int or number < 1:
                    self.issue("source_invalid", "Input page number must be a positive integer")
                raw = self.path(base, origin.get("raw"), scope=self.root / "raw")
                self.data(raw, image=True)
                if raw is not None:
                    item["raw"] = raw.relative_to(self.root).as_posix()
                    if raw.resolve() in raw_pages:
                        self.issue("source_duplicate", "The same raw page is used more than once")
                    raw_pages.add(raw.resolve())
                if origin.get("kind") == "pdf" or "document" in origin:
                    document = self.path(base, origin.get("document"), scope=self.root / "sources")
                    metadata = self.pdf(document)
                    if document is not None:
                        item["document"] = document.relative_to(self.root).as_posix()
                    if metadata is not None:
                        digest, count, error = metadata
                        if origin.get("document_id") != digest:
                            self.issue("pdf_digest", "PDF digest differs from document_id", item.get("document"))
                        if count is not None:
                            if type(number) is not int or not 1 <= number <= count:
                                self.issue("pdf_page_bounds", f"Page {number!r} is outside PDF's {count} pages")
                            else:
                                page_key = (digest, number)
                                if page_key in pdf_pages:
                                    self.issue("source_duplicate", "The same PDF page supplies multiple faces")
                                pdf_pages.add(page_key)
                        else:
                            self.issue("pdf_invalid", error, item.get("document"))
                normalized.append(item)
            except _Invalid:
                continue
        return normalized

    def pages(self, result: dict, sheet: dict, template, base: Path):
        recorded = self.origins(sheet.get("input_pages"), self.root)
        actual = self.origins(result.get("input_pages"), base)
        if recorded != actual or len(recorded) != len(template.faces):
            self.issue("source_mapping", "Result input pages do not match manifest/template")
        files = self.array(sheet.get("files"), "sheet.files")
        expected_raw = [f"raw/{self.seq:04d}_{name}" for name in files if isinstance(name, str)]
        if len(expected_raw) != len(files) or [o.get("raw") for o in recorded] != expected_raw:
            self.issue("source_mapping", "Raw pages do not match sheet sequence/files")
        pages = self.obj(result.get("pages"), "pages")
        if set(pages) != set(template.faces) | {f"raw_{i}" for i in range(1, len(recorded) + 1)}:
            self.issue("page_mapping", "Aligned/raw page keys differ from template and source pages")
        for face in template.faces:
            self.image(base, pages.get(face), f"page/{face}", scope=base)
        for index, origin in enumerate(recorded, 1):
            path = self.path(base, pages.get(f"raw_{index}"), scope=self.root / "raw")
            if path is not None and path.relative_to(self.root).as_posix() != origin.get("raw"):
                self.issue("page_mapping", f"Raw page alias differs for page {index}")
        scan = self.obj(result.get("scan"), "scan")
        faces = self.obj(scan.get("faces"), "scan.faces")
        if set(faces) != set(template.faces):
            self.issue("page_mapping", "Scan face mapping differs from template")
        sources = [self.obj(value, f"scan.faces.{face}").get("source") for face, value in faces.items()]
        if any(type(value) is not int for value in sources) or sorted(sources) != list(range(1, len(recorded) + 1)):
            self.issue("page_mapping", "Scan faces must map each raw page exactly once")

    def result(self, sheet: dict, manifest: dict, template):
        path = self.path(self.root, sheet.get("result"), scope=self.root / "students")
        if path is None:
            return
        self.location = path.relative_to(self.root).as_posix()
        prefix = f"{self.seq:04d}"
        if (path.parent.parent != self.root / "students" or path.name != "result.json"
                or not (path.parent.name == prefix or path.parent.name.startswith(prefix + "_"))):
            self.issue("result_identity", "Result directory does not match its sheet sequence")
        if path.resolve() in self.result_owners:
            self.issue("result_reused", "Students share the same result file")
        self.result_owners[path.resolve()] = self.seq
        result = self.json(path)
        if result is None:
            return
        processing = self.obj(result.get("scan"), "scan").get("processing")
        self.require_evidence = False
        if processing is not None:
            processing = self.obj(processing, "scan.processing")
            version = processing.get("evidence_version")
            if type(version) is not int or version not in (1, 2):
                self.issue("schema_version", "Unsupported evidence version")
            self.require_evidence = version == 2
        if result.get("schema_version") != "1.0":
            self.issue("schema_version", "Unsupported result schema version")
        identity = self.obj(result.get("template"), "result.template")
        if (type(result.get("seq")) is not int or result.get("seq") != self.seq
                or result.get("batch_id") != manifest.get("batch_id")
                or result.get("sheet_id") != f"{manifest.get('batch_id')}:{self.seq:04d}"):
            self.issue("result_identity", "Result sequence/batch/sheet identity differs from manifest")
        if (type(identity.get("version")) is not int
                or identity.get("name") != template.name or identity.get("version") != template.version):
            self.issue("template_mismatch", "Result template identity differs from snapshot")
        self.ocr_reasons = {}
        for warning in self.array(result.get("warnings", []), "warnings"):
            if isinstance(warning, dict) and warning.get("code") == "OCR_FAILED":
                # 转写失败由 ocr_failed 逐项报告（含原因），这里不再以识别警告重复一次
                msg = str(warning.get("msg") or "")
                self.ocr_reasons[str(warning.get("field", ""))] = msg.removeprefix("手写转写失败：")
                continue
            if isinstance(warning, dict):
                code, field, msg = (str(warning.get(k) or "") for k in ("code", "field", "msg"))
                text = "，".join(v for v in (msg or code, field) if v)
                self.issue("recognition_warning", f"识别结果有警告：{text}", warning=True)
            else:
                self.issue("recognition_warning", str(warning), warning=True)
        if sheet.get("warnings") and not result.get("warnings"):
            self.issue("recognition_warning", "Manifest records recognition warnings", warning=True)
        # Each branch can fail structurally without concealing unrelated evidence issues.
        for check in (lambda: self.questions(result, template, path.parent),
                      lambda: self.student(result, template, path.parent),
                      lambda: self.pages(result, sheet, template, path.parent)):
            try:
                check()
            except _Invalid:
                continue

    def void_evidence(self, sheet: dict):
        self.optional = True
        self.summary["voided"] += 1
        reason = sheet.get("void_reason") or sheet.get("message") or sheet.get("physical") or "操作员已作废"
        self.summary["excluded"].append({"seq": self.seq, "reason": reason})
        self.issue("sheet_voided", f"已明确排除此作废答卷：{reason}", warning=True)
        if sheet.get("input_pages"):
            self.origins(sheet["input_pages"], self.root)
        else:
            self.issue("void_evidence_missing", "Legacy void has no input page provenance")
            for reference in self.array(sheet.get("files", []), "sheet.files"):
                path = self.path(self.root, f"raw/{self.seq:04d}_{reference}", scope=self.root / "raw")
                self.data(path, image=True)
            for entry in sorted((self.root / "raw").glob(f"{self.seq:04d}_*")):
                path = self.path(self.root, entry.relative_to(self.root).as_posix(), scope=self.root / "raw")
                self.data(path, image=True)
        directories = sorted((self.root / "errors").glob(f"{self.seq:04d}_*"))
        if not directories:
            self.issue("void_evidence_missing", "Legacy void has no retained error evidence")
        for directory in directories:
            folder = self.path(self.root, directory.relative_to(self.root).as_posix(), scope=self.root / "errors")
            if folder is None:
                continue
            for entry in sorted(folder.rglob("*")):
                if entry.is_dir() or entry.name.endswith((".lock", ".tmp")):
                    continue
                path = self.path(self.root, entry.relative_to(self.root).as_posix(), scope=folder)
                if entry.suffix.lower() == ".json":
                    self.json(path)
                else:
                    self.data(path, image=entry.suffix.lower() in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"})
        if sheet.get("result"):
            path = self.path(self.root, sheet["result"], scope=self.root / "students")
            result = self.json(path)
            if result is not None:
                self.retained_references(result, path.parent)

    def retained_references(self, value, base: Path):
        """Keep available legacy void evidence without pretending it is a valid result."""
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("image", "raw", "document"):
                    self.data(self.path(base, child), image=key != "document")
                elif key == "images" and isinstance(child, list):
                    for reference in child:
                        self.data(self.path(base, reference), image=True)
                elif key == "pages" and isinstance(child, dict):
                    for reference in child.values():
                        self.data(self.path(base, reference), image=True)
                else:
                    self.retained_references(child, base)
        elif isinstance(value, list):
            for child in value:
                self.retained_references(child, base)

    def sealed(self, manifest: dict):
        delivery = manifest.get("delivery")
        if delivery is None:
            return
        delivery = self.obj(delivery, "manifest.delivery")
        if delivery.get("sealed") is not True:
            self.issue("delivery_invalid", "Delivery metadata must explicitly mark the batch sealed")
            return
        try:
            path = resolve_path(self.root, self.root, "delivery.json")
        except (ValueError, OSError, RuntimeError) as exc:
            self.issue("invalid_path", str(exc), "delivery.json")
            return
        try:
            info = json.loads(path.read_bytes(), parse_constant=_reject_constant)
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            self.issue("delivery_invalid", f"Cannot read sealed delivery metadata: {exc}", "delivery.json")
            return
        info = self.obj(info, "delivery.json")
        revision = delivery.get("revision")
        if not isinstance(revision, str) or not revision or revision != info.get("revision"):
            self.issue("delivery_revision", "批次清单与交付记录的封存版本不一致", "delivery.json")
        inventory = self.obj(info.get("files"), "delivery.files")
        if inventory != self.files:
            self.issue("delivery_inventory", "当前文件内容或文件集合与封存摘要清单不一致", "delivery.json")

    def run(self) -> AuditReport:
        manifest = self.json(self.path(self.root, "manifest.json"))
        if manifest is not None:
            try:
                if manifest.get("schema_version") != "1.0":
                    self.issue("schema_version", "Unsupported manifest schema version")
                if not isinstance(manifest.get("batch_id"), str) or not manifest["batch_id"].strip():
                    self.issue("batch_identity", "Manifest batch_id must be a nonempty string")
                if manifest.get("finalized") is not True:
                    self.issue("not_finalized", "批次尚未按最终阈值完成重新判定，请先结束批次")
                if "finalization" in manifest:
                    finalization = self.obj(manifest["finalization"], "manifest.finalization")
                    if finalization.get("status") != "complete" or finalization.get("blocked_seqs"):
                        self.issue("not_finalized", "结算尚未成功完成，或仍有阻断答卷")
                    if "revision" in finalization and (
                            type(finalization["revision"]) is not int or type(manifest.get("revision")) is not int
                            or finalization["revision"] != manifest["revision"]):
                        self.issue("not_finalized", "结算版本与当前批次不一致，请重新结算")
                    if finalization.get("failed_seqs"):
                        self.issue("rejudge_failed", f"最终重新判定失败的序号：{finalization['failed_seqs']}")
                if manifest.get("rejudge_failures") or manifest.get("finalize_failures"):
                    self.issue("rejudge_failed", "批次仍有最终重新判定失败的答卷")
                template = self.template(manifest)
                sheets = self.array(manifest.get("sheets"), "manifest.sheets")
                self.summary["total"] = len(sheets)
                if not sheets:
                    self.issue("empty_batch", "空批次不能生成交付目录")
                seen = set()
                for value in sheets:
                    self.seq, self.location, self.optional = None, "manifest.json", False
                    try:
                        sheet = self.obj(value, "sheet")
                        seq = sheet.get("seq")
                        if type(seq) is not int or seq < 1:
                            self.issue("sequence_invalid", "Sheet sequence must be a positive integer")
                            continue
                        self.seq = seq
                        if seq in seen:
                            self.issue("sequence_duplicate", "Sheet sequence is duplicated")
                        seen.add(seq)
                        status = sheet.get("status")
                        if sheet.get("rejudge_failed") or sheet.get("rejudge_error") or sheet.get("finalize_error"):
                            self.issue("rejudge_failed", f"此答卷最终重新判定失败：{sheet.get('finalize_error', '')}")
                        if status == "voided":
                            if sheet.get("voided") is not True:
                                self.issue("void_invalid", "Voided sheet lacks explicit disposition")
                            self.void_evidence(sheet)
                        elif status == "ok":
                            self.summary["ok"] += 1
                            if sheet.get("physical") or sheet.get("voided"):
                                self.issue("sheet_unresolved", "Successful sheet has unresolved disposition")
                            if template is not None:
                                self.result(sheet, manifest, template)
                        else:
                            self.issue("sheet_unresolved", f"Sheet is not settled: {status!r}")
                    except _Invalid:
                        continue
                    finally:
                        self.optional = False
                self.seq, self.location = None, "manifest.json"
                self.duplicate_exam_numbers(sheets)
                self.sealed(manifest)
            except _Invalid:
                pass
        # A second content read detects changes during validation, including same-stat rewrites.
        self.seq, self.optional = None, False
        for relative, digest in list(self.files.items()):
            try:
                path = resolve_path(self.root, self.root, relative)
                with path.open("rb") as stream:
                    current = hashlib.file_digest(stream, "sha256").hexdigest()
                if current != digest:
                    self.issue("payload_changed", "Payload changed during audit", relative)
            except (OSError, ValueError, RuntimeError) as exc:
                self.issue("payload_changed", f"Payload became unreadable: {exc}", relative)
        errors = sum(i["severity"] == "error" for i in self.issues)
        warnings = len(self.issues) - errors
        self.summary.update(errors=errors, warnings=warnings)
        return AuditReport(not errors, "blocked" if errors else "warnings" if warnings else "ready",
                           self.issues, dict(sorted(self.files.items())), self.summary,
                           datetime.now().astimezone().isoformat(timespec="seconds"))


def audit_batch(batch_dir: str | Path) -> AuditReport:
    """Inspect current payload bytes without recovery, writes, locks or OCR side effects."""
    return _Audit(Path(batch_dir).resolve()).run()
