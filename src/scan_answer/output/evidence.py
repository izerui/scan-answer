"""证据图片路径和题目身份；用户键不会成为未经编码的路径。"""
from __future__ import annotations

import hashlib

_PLAIN = frozenset(b"abcdefghijklmnopqrstuvwxyz0123456789-_")


def _component(value: str) -> str:
    """编码大写和标点，避免 Windows/macOS 的大小写折叠造成路径碰撞。"""
    raw = str(value).encode("utf-8")
    encoded = "".join(chr(byte) if byte in _PLAIN else f"%{byte:02x}" for byte in raw)
    # 长组件用完整摘要；~ 不出现在普通编码中，原始题号/字段键仍保存在 JSON。
    return encoded if len(encoded) <= 80 else "~" + hashlib.sha256(raw).hexdigest()


def image_path(kind: str, key: str = "", *, sub: str = "", region: int = 1) -> str:
    encoded = _component(key)
    if kind == "page":
        return f"images/pages/{encoded}.jpg"
    if kind == "exam_no":
        return "images/identity/exam_no.jpg"
    if kind in ("barcode", "field", "omr_field"):
        category = {"barcode": "barcodes", "field": "fields", "omr_field": "omr_fields"}[kind]
        return f"images/{category}/key-{encoded}.jpg"
    if kind == "barcode_candidate":
        return f"images/barcode_candidates/key-{encoded}/candidate-{region}.jpg"
    if kind == "objective":
        return f"images/objective/q-{encoded}.jpg"
    if kind == "subjective":
        return f"images/subjective/q-{encoded}/sub-{_component(sub)}/region-{region}.jpg"
    raise ValueError(f"未知图片类型：{kind}")


def question_id(kind: str, no: str, sub: str = "") -> str:
    """在同一模板内唯一；题号与小题号分段编码，保留原值的区别。"""
    key = f"{kind}/{_component(no)}"
    return f"{key}/{_component(sub)}" if kind == "subjective" else key
