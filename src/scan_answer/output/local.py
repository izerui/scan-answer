"""本地目录导出器：students/{seq}_{考号}/result.json + images/，errors/，manifest.json。

所有 JSON 先写临时文件再 os.replace，保证读者不会看到写了一半的文件。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

from .naming import DEFAULT_STUDENT_PATTERN, student_dir_name


REPLACE_RETRIES = 10  # os.replace 遇到 PermissionError 的最多重试次数
REPLACE_DELAY = 0.05  # 每次重试前等待（秒）


def write_json_atomic(path: Path, data: dict) -> None:
    """写临时文件后 os.replace 原子替换。

    临时文件名唯一（多个写者互不覆盖）；Windows 上目标文件正被其他进程读取时 os.replace 会抛
    PermissionError，退避重试，仍失败则删除临时文件并抛出原异常。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o644)  # mkstemp 默认 0600，与普通文件保持一致
        for attempt in range(REPLACE_RETRIES + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == REPLACE_RETRIES:
                    raise
                time.sleep(REPLACE_DELAY)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


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

    def student_dir(self, seq: int, exam_no: str | None, barcode: str | None = None,
                    pattern: str = DEFAULT_STUDENT_PATTERN) -> Path:
        return self.batch_dir / "students" / student_dir_name(seq, pattern, exam_no, barcode)

    def write_student(self, seq: int, exam_no: str | None, result: dict, images: dict[str, np.ndarray],
                      barcode: str | None = None, pattern: str = DEFAULT_STUDENT_PATTERN) -> Path:
        """images 的键为相对 student 目录的路径（如 images/q56_1.jpg）。返回 result.json 路径。"""
        d = self.student_dir(seq, exam_no, barcode, pattern)
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
