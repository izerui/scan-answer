"""Application-level write protection for published delivery directories."""
from __future__ import annotations

from pathlib import Path

from .jsonfile import read_json


def is_sealed_batch(batch_dir: str | Path) -> bool:
    path = Path(batch_dir) / "manifest.json"
    if not path.is_file():
        return False
    manifest = read_json(path)
    delivery = manifest.get("delivery") if isinstance(manifest, dict) else None
    return isinstance(delivery, dict) and bool(delivery.get("sealed"))


def ensure_writable_batch(batch_dir: str | Path) -> None:
    if is_sealed_batch(batch_dir):
        raise ValueError("交付目录只读，请在原工作批次中修改后重新生成交付目录")


def ensure_writable_result(result_path: str | Path) -> None:
    for parent in Path(result_path).resolve().parents:
        if (parent / "manifest.json").is_file():
            ensure_writable_batch(parent)
