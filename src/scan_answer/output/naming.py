"""输出目录命名规则：考试目录与学生目录。

学生目录一律以 {seq:04d} 开头（重扫清理、重开批次都按序号前缀查找），规则只控制后面的部分。
"""
from __future__ import annotations

import re
import string
from datetime import datetime

DEFAULT_EXAM_PATTERN = "{name}_{subject}_{time}"
DEFAULT_STUDENT_PATTERN = "{exam_no}"
EXAM_FIELDS = ("name", "subject", "code", "site", "room", "operator", "date", "time")
STUDENT_FIELDS = ("exam_no", "barcode")
_ILLEGAL = '\\/:*?"<>|'


def unknown_fields(pattern: str, allowed: tuple[str, ...]) -> list[str]:
    """返回规则里不认识的占位符；格式错误（花括号不配对等）时返回 ["格式错误"]。"""
    try:
        names = [f for _, f, _, _ in string.Formatter().parse(pattern) if f is not None]
    except ValueError:
        return ["格式错误"]
    return [n for n in names if n not in allowed]


def clean(text: str) -> str:
    """去掉文件名里不允许的字符和首尾的空白、下划线、点。"""
    s = "".join(ch for ch in text if ch not in _ILLEGAL and ord(ch) >= 32)
    s = re.sub(r"_{2,}", "_", s)
    return s.strip(" _.")


def _render(pattern: str, values: dict[str, str], allowed: tuple[str, ...]) -> str:
    if unknown_fields(pattern, allowed):
        return ""
    return clean(pattern.format(**{k: values.get(k, "") for k in allowed}))


def exam_dir_name(pattern: str, info: dict, now: datetime | None = None) -> str:
    """考试目录名；规则无效或渲染为空时退回默认规则，仍为空时用“考试”。"""
    now = now or datetime.now()
    values = {"name": info.get("name", ""), "subject": info.get("subject", ""), "code": info.get("subject_code", ""),
              "site": info.get("site", ""), "room": info.get("room", ""), "operator": info.get("operator", ""),
              "date": now.strftime("%Y%m%d"), "time": now.strftime("%Y%m%d_%H%M%S")}
    name = _render(pattern or DEFAULT_EXAM_PATTERN, values, EXAM_FIELDS)
    if not name:
        name = _render(DEFAULT_EXAM_PATTERN, values, EXAM_FIELDS)
    return name or "考试"


def student_dir_name(seq: int, pattern: str, exam_no: str | None, barcode: str | None) -> str:
    """学生目录名 {seq:04d}[_后缀]。考号没有完整识别（含 ?）时按空处理，避免目录名带残缺考号；渲染为空时只用序号。"""
    values = {"exam_no": exam_no if exam_no and "?" not in exam_no else "", "barcode": barcode or ""}
    suffix = _render(pattern if pattern is not None else DEFAULT_STUDENT_PATTERN, values, STUDENT_FIELDS)
    return f"{seq:04d}_{suffix}" if suffix else f"{seq:04d}"


def sample_names(exam_pattern: str, student_pattern: str) -> tuple[str, str]:
    """设置页的示例。"""
    info = {"name": "2026年全国硕士研究生招生考试", "subject": "管理类综合能力", "subject_code": "199",
            "site": "第一考点", "room": "001", "operator": "李老师"}
    return (exam_dir_name(exam_pattern, info, datetime(2026, 11, 8, 9, 30, 0)),
            student_dir_name(37, student_pattern, "100015000123456", "100015000123456"))
