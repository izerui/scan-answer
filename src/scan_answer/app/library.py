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
from ..output.naming import DEFAULT_EXAM_PATTERN, exam_dir_name
from .settings import bundled_templates_dir, config_dir


# ---------- 用户模板目录 ----------

def user_templates_dir() -> Path:
    """返回用户模板目录（config_dir() / "templates"），并确保目录存在。"""
    d = config_dir() / "templates"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    try:
        if not p.exists():
            return None
        t = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(t, dict):
            return None
        paper = tuple(float(x) for x in t["paper"])
        if len(paper) != 2:
            return None
        n_obj = sum(len(b["grid"]["cells"]) for b in t.get("objective", []))
        return TemplateInfo(str(t["name"]), int(t["version"]), d, paper, len(t["faces"]),
                            n_obj + len(t.get("subjective", [])))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
        return None  # 结构不对的模板跳过，不影响其他模板


def list_templates(dirs: list[str]) -> list[TemplateInfo]:
    """每个目录本身是模板，或其子目录是模板；按名称排序、去重。不可读的目录跳过。

    总是额外扫描随程序提供的模板目录和 user_templates_dir()：设置里记的路径可能来自旧版本或开发环境。"""
    seen, out = set(), []
    # 合并随程序提供的模板目录与用户模板目录
    all_dirs = list(dirs)
    bundled = str(bundled_templates_dir())
    if bundled not in all_dirs:
        all_dirs.append(bundled)
    try:
        utd = str(user_templates_dir())
        if utd not in all_dirs:
            all_dirs.append(utd)
    except OSError:
        pass
    for raw in all_dirs:
        d = Path(raw)
        try:
            candidates = [d] + (sorted(x for x in d.iterdir() if x.is_dir()) if d.is_dir() else [])
        except OSError:
            candidates = [d]
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


def create_exam(output_root: str | Path, info: ExamInfo, template_dir: str | Path,
                pattern: str = DEFAULT_EXAM_PATTERN) -> Exam:
    root = Path(output_root)
    name = exam_dir_name(pattern, asdict(info))
    d = root / name
    n = 1
    while d.exists():
        n += 1
        d = root / f"{name}_{n}"
    d.mkdir(parents=True)
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    write_json_atomic(d / "exam.json", {"info": asdict(info), "template_dir": str(Path(template_dir)),
                                        "created": created})
    return Exam(d, info, Path(template_dir), created)


def load_exam(directory: str | Path) -> Exam:
    """读 exam.json；结构不对时抛 ValueError（列表里跳过该考试）。"""
    d = Path(directory)
    j = json.loads((d / "exam.json").read_text(encoding="utf-8"))
    if not isinstance(j, dict) or not isinstance(j.get("info"), dict) or not isinstance(j.get("template_dir"), str):
        raise ValueError(f"exam.json 结构不对: {d}")
    raw = j["info"]
    if not isinstance(raw.get("name"), str):
        raise ValueError(f"exam.json 缺少考试名称: {d}")
    kw = {k: v for k, v in raw.items()
          if k in ExamInfo.__dataclass_fields__ and isinstance(v, dict if k == "extra" else str)}
    created = j.get("created")
    return Exam(d, ExamInfo(**kw), Path(j["template_dir"]), created if isinstance(created, str) else "")


def _subdirs(root: Path) -> list[Path]:
    try:
        return list(root.iterdir())
    except OSError:
        return []  # 目录不可读：当作空


def list_exams(output_root: str | Path) -> list[Exam]:
    root = Path(output_root)
    if not root.is_dir():
        return []
    out = []
    for d in _subdirs(root):
        try:
            if (d / "exam.json").exists():
                out.append(load_exam(d))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
            continue
    return sorted(out, key=lambda e: e.created, reverse=True)


def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _clean_progress(p: object) -> dict | None:
    if not isinstance(p, dict):
        return None
    return {k: _int(p.get(k)) for k in ("done", "failed", "total")}


def _summary(d: Path, m: object) -> BatchSummary | None:
    """manifest → 批次摘要；字段类型不对时按默认值兜底，顶层不是对象则跳过。"""
    if not isinstance(m, dict):
        return None
    counts = m.get("counts")
    counts = {k: _int(v) for k, v in counts.items()} if isinstance(counts, dict) else {}
    bid, created = m.get("batch_id"), m.get("created")
    return BatchSummary(bid if isinstance(bid, str) else d.name, d, created if isinstance(created, str) else "",
                        counts, m.get("finalized") is True, m.get("ocr") is True, _clean_progress(m.get("ocr_progress")))


def list_batches(exam_dir: str | Path) -> list[BatchSummary]:
    root = Path(exam_dir)
    if not root.is_dir():
        return []
    out = []
    for d in _subdirs(root):
        try:
            mf = d / "manifest.json"
            if not d.is_dir() or not mf.exists():
                continue
            b = _summary(d, json.loads(mf.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
        if b:
            out.append(b)
    return sorted(out, key=lambda b: b.created, reverse=True)


def exam_totals(exam_dir: str | Path) -> dict:
    """考试下所有批次的计数汇总。"""
    tot = {"total": 0, "ok": 0, "physical": 0, "voided": 0, "warnings": 0}
    for b in list_batches(exam_dir):
        for k in tot:
            tot[k] += _int(b.counts.get(k, 0))
    return tot
