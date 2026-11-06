"""本地目录导出器：students/{seq}_{考号}/result.json + images/，errors/，manifest.json。

所有 JSON 先写临时文件再 os.replace，保证读者不会看到写了一半的文件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def write_jpg(path: Path, img: np.ndarray, quality: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError(f"JPEG 编码失败: {path}")
    path.write_bytes(enc.tobytes())


class LocalExporter:
    def __init__(self, batch_dir: str | Path):
        self.batch_dir = Path(batch_dir)
        self.batch_dir.mkdir(parents=True, exist_ok=True)

    def student_dir(self, seq: int, exam_no: str | None) -> Path:
        safe = exam_no if exam_no and "?" not in exam_no else ""
        return self.batch_dir / "students" / (f"{seq:04d}_{safe}" if safe else f"{seq:04d}")

    def write_student(self, seq: int, exam_no: str | None, result: dict, images: dict[str, np.ndarray]) -> Path:
        """images 的键为相对 student 目录的路径（如 images/q56_1.jpg）。返回 result.json 路径。"""
        d = self.student_dir(seq, exam_no)
        for rel, img in images.items():
            write_jpg(d / rel, img)
        write_json_atomic(d / "result.json", result)
        return d / "result.json"

    def write_error(self, seq: int, code: str, msg: str, images: list[np.ndarray]) -> Path:
        d = self.batch_dir / "errors" / f"{seq:04d}_{code}"
        for i, img in enumerate(images, start=1):
            write_jpg(d / f"face_{i}.jpg", img)
        write_json_atomic(d / "error.json", {"seq": seq, "code": code, "msg": msg})
        return d / "error.json"

    def write_manifest(self, manifest: dict) -> None:
        write_json_atomic(self.batch_dir / "manifest.json", manifest)
