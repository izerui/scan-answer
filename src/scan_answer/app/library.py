"""考试库与模板库：界面"考试"页的数据来源。

目录约定：
  <输出根目录>/<考试目录>/exam.json      考试元信息 + 所用模板目录
  <输出根目录>/<考试目录>/<批次目录>/      BatchSession 创建的批次（含 manifest.json、模板快照）
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..output.local import write_json_atomic


# ---------- 模板库 ----------
@dataclass
class TemplateInfo:
    name: str
    version: int
    directory: Path
    paper: tuple[float, float]
    faces: int
    questions: int  # 客观题 + 主观题数量


def _template_info(d: Path) -> TemplateInfo | None:
    p = d / "template.json"
    if not p.exists():
        return None
    try:
        t = json.loads(p.read_text(encoding="utf-8"))
        n_obj = sum(len(b["grid"]["cells"]) for b in t.get("objective", []))
        return TemplateInfo(t["name"], int(t["version"]), d, tuple(t["paper"]), len(t["faces"]),
                            n_obj + len(t.get("subjective", [])))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def list_templates(dirs: list[str]) -> list[TemplateInfo]:
    """每个目录本身是模板，或其子目录是模板；按名称排序、去重。"""
    seen, out = set(), []
    for raw in dirs:
        d = Path(raw)
        candidates = [d] + (sorted(x for x in d.iterdir() if x.is_dir()) if d.is_dir() else [])
        for c in candidates:
            info = _template_info(c)
            if info and info.directory.resolve() not in seen:
                seen.add(info.directory.resolve())
                out.append(info)
    return sorted(out, key=lambda t: (t.name, t.version))


# ---------- 考试与批次 ----------
@dataclass
class ExamInfo:
    name: str
    subject: str = ""
    subject_code: str = ""
    site: str = ""
    room: str = ""
    operator: str = ""
    extra: dict = field(default_factory=dict)

    def meta(self) -> dict:
        """写进每张卡 result.json 的 exam 字段。"""
        d = asdict(self)
        return {k: v for k, v in d.items() if v not in ("", {}, None)}


@dataclass
class Exam:
    directory: Path
    info: ExamInfo
    template_dir: Path
    created: str


@dataclass
class BatchSummary:
    batch_id: str
    directory: Path
    created: str
    counts: dict
    finalized: bool
    ocr: bool
    ocr_progress: dict | None

    @property
    def state(self) -> str:
        """界面显示的状态：识别中 / 已结束 / 待结束。"""
        if self.finalized:
            return "已结束"
        return "未结束"


def create_exam(output_root: str | Path, info: ExamInfo, template_dir: str | Path) -> Exam:
    root = Path(output_root)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(ch for ch in f"{info.name}_{info.subject}" if ch not in '\\/:*?"<>|').strip("_ ") or "考试"
    d = root / f"{safe}_{stamp}"
    n = 1
    while d.exists():
        n += 1
        d = root / f"{safe}_{stamp}_{n}"
    d.mkdir(parents=True)
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json_atomic(d / "exam.json", {"info": asdict(info), "template_dir": str(Path(template_dir)),
                                        "created": created})
    return Exam(d, info, Path(template_dir), created)


def load_exam(directory: str | Path) -> Exam:
    d = Path(directory)
    j = json.loads((d / "exam.json").read_text(encoding="utf-8"))
    info = ExamInfo(**{k: v for k, v in j["info"].items() if k in ExamInfo.__dataclass_fields__})
    return Exam(d, info, Path(j["template_dir"]), j.get("created", ""))


def list_exams(output_root: str | Path) -> list[Exam]:
    root = Path(output_root)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if (d / "exam.json").exists():
            try:
                out.append(load_exam(d))
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue
    return sorted(out, key=lambda e: e.created, reverse=True)


def list_batches(exam_dir: str | Path) -> list[BatchSummary]:
    root = Path(exam_dir)
    if not root.is_dir():
        return []
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append(BatchSummary(m.get("batch_id", d.name), d, m.get("created", ""), m.get("counts") or {},
                                bool(m.get("finalized")), bool(m.get("ocr")), m.get("ocr_progress")))
    return sorted(out, key=lambda b: b.created, reverse=True)


def exam_totals(exam_dir: str | Path) -> dict:
    """考试下所有批次的计数汇总。"""
    tot = {"total": 0, "ok": 0, "physical": 0, "voided": 0, "warnings": 0}
    for b in list_batches(exam_dir):
        for k in tot:
            tot[k] += int(b.counts.get(k, 0))
    return tot
