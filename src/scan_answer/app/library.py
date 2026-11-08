"""考试库与模板库：界面"考试"页的数据来源。

目录约定：
  <输出根目录>/<考试目录>/exam.json      考试元信息 + 所用模板目录
  <输出根目录>/<考试目录>/<批次目录>/      BatchSession 创建的批次（含 manifest.json、模板快照）
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from ..output.local import write_json_atomic
from ..output.protection import ensure_writable_result
from ..output.naming import DEFAULT_EXAM_PATTERN, exam_dir_name
from ..template import load_template, validate_template
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
    template_name: str = ""
    template_version: int | None = None
    template_source: Path | None = None

    @property
    def template_label(self) -> str:
        name, version = self.template_name, self.template_version
        if not name:
            info = _template_info(self.template_dir)
            if info:
                name, version = info.name, info.version
        return f"{name}（v{version}）" if name and version is not None else name or "模板不可用"


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
    ensure_writable_result(root / ".exam")
    name = exam_dir_name(pattern, asdict(info))
    d = root / name
    n = 1
    root.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            d.mkdir()
            break
        except FileExistsError:
            n += 1
            d = root / f"{name}_{n}"
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    source = Path(template_dir).resolve()
    try:
        data = _copy_snapshot(source, d / "template")
        meta = _snapshot_metadata(data, source, source)
        # exam.json 是可列出的考试的提交点：完整快照就绪后才发布。
        write_json_atomic(d / "exam.json", {"info": asdict(info), "template_dir": "template",
                                           "template": meta, "created": created})
    except BaseException:
        shutil.rmtree(d)
        raise
    return Exam(d, info, d / "template", created, data["name"], data["version"], source)


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
    meta = j.get("template")
    meta = meta if isinstance(meta, dict) else {}
    template_dir = Path(j["template_dir"])
    # 新格式相对考试目录；旧格式相对工作目录的来源路径保持兼容。
    if not template_dir.is_absolute() and meta:
        template_dir = d / template_dir
    name, version, source = meta.get("name"), meta.get("version"), meta.get("source")
    return Exam(d, ExamInfo(**kw), template_dir, created if isinstance(created, str) else "",
                name if isinstance(name, str) else "",
                version if isinstance(version, int) and not isinstance(version, bool) else None,
                Path(source) if isinstance(source, str) else None)


def _local_path(directory: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or "\\" in relative or not (directory / path).resolve().is_relative_to(directory.resolve()):
        raise ValueError(f"模板资源路径越出目录：{relative}")
    return directory / path


def _snapshot_data(directory: Path) -> tuple[dict, str]:
    """比较全部声明与底图字节，忽略 JSON 排版和底图相对文件名。"""
    try:
        data = json.loads((directory / "template.json").read_text(encoding="utf-8"))
        normalized = copy.deepcopy(data)
        for face, spec in data["faces"].items():
            image = _local_path(directory, spec["image"])
            with image.open("rb") as stream:
                normalized["faces"][face]["image"] = hashlib.file_digest(stream, "sha256").hexdigest()
        template, _ = load_template(directory)
        problems = validate_template(template)
        if problems:
            raise ValueError(f"模板结构校验失败：{directory}\n" + "\n".join(problems))
        return data, json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    except (KeyError, TypeError, AttributeError, IndexError) as e:
        raise ValueError(f"模板结构不对：{directory}") from e


def _copy_snapshot(source: Path, destination: Path) -> dict:
    data, fingerprint = _snapshot_data(source)
    destination.mkdir()
    shutil.copyfile(source / "template.json", destination / "template.json")
    for face in data["faces"].values():
        relative = face["image"]
        target = _local_path(destination, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_local_path(source, relative), target)
    copied, actual = _snapshot_data(destination)
    if actual != fingerprint:
        raise ValueError("复制期间模板发生变化，请重试")
    return copied


def _snapshot_metadata(data: dict, source: Path, pinned_from: Path) -> dict:
    return {"name": data["name"], "version": data["version"], "source": str(source.resolve()),
            "pinned_from": str(pinned_from.resolve())}


def _historic_template(exam: Exam) -> Path:
    source, fingerprint = None, None
    # 不使用会跳过损坏 manifest 的 list_batches；损坏的历史也是不能忽略的证据。
    for directory in sorted(exam.directory.iterdir()):
        manifest = directory / "manifest.json"
        if not directory.is_dir() or not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            relative = data.get("template", {}).get("snapshot", "template/template.json")
            snapshot = _local_path(directory, relative)
            if snapshot.name != "template.json":
                raise ValueError("历史模板快照文件名无效")
            candidate = snapshot.parent
            _, actual = _snapshot_data(candidate)
        except (OSError, ValueError, TypeError, AttributeError) as e:
            raise ValueError(f"历史批次 {directory.name} 的模板无法确认，请新建考试选择模板版本：{e}") from e
        if fingerprint is not None and actual != fingerprint:
            raise ValueError("历史批次的模板不一致，请新建考试选择模板版本")
        if source is None:
            source, fingerprint = candidate, actual
    return source if source is not None else exam.template_dir


def ensure_exam_snapshot(exam: Exam) -> Path:
    """在新建批次前固定模板；历史有冲突时不猜测，不改写考试或创建批次。"""
    current = load_exam(exam.directory) if (exam.directory / "exam.json").exists() else exam
    snapshot = exam.directory / "template"
    if current.template_dir.resolve() == snapshot.resolve():
        data, _ = _snapshot_data(snapshot)
    else:
        source = _historic_template(current)
        if snapshot.exists():
            raise ValueError("考试存在未绑定的模板快照，请新建考试选择模板版本")
        with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=exam.directory) as temporary:
            staged = Path(temporary) / "template"
            data = _copy_snapshot(source, staged)
            path = exam.directory / "exam.json"
            record = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
                "info": asdict(exam.info), "created": exam.created,
            }
            origin = current.template_source or current.template_dir
            record.update(template_dir="template", template=_snapshot_metadata(data, origin, source))
            staged.rename(snapshot)
            try:
                write_json_atomic(path, record)
            except BaseException:
                shutil.rmtree(snapshot)
                raise
            current.template_source = origin.resolve()
    exam.template_dir = snapshot
    exam.template_name, exam.template_version = data["name"], data["version"]
    exam.template_source = current.template_source
    return snapshot


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
