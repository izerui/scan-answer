"""Publish a checked, immutable-by-application batch payload directory."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from .audit import AuditReport, audit_batch
from .jsonfile import read_json
from .local import write_json_atomic
from .paths import resolve_path
from .protection import ensure_writable_result
from .publication import publish_directory


class DeliveryError(ValueError):
    def __init__(self, report: AuditReport):
        self.report = report
        reasons = [issue["message"] for issue in report.issues if issue["severity"] == "error"]
        super().__init__("当前批次不能交付：" + "；".join(reasons[:8]))


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export_delivery(batch_dir: str | Path, target: str | Path) -> Path:
    """Copy only validated payload, recheck it, then publish a unique directory."""
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"目标已经存在，不会覆盖：{target}")
    source, target = Path(batch_dir).resolve(), target.resolve()
    ensure_writable_result(target)
    if target.is_relative_to(source):
        raise ValueError("交付目录不能放在源批次内部")
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"目标已经存在，不会覆盖：{target}")
    original = audit_batch(source)
    if not original.ready:
        raise DeliveryError(original)
    target.parent.mkdir(parents=True, exist_ok=True)
    reservation = target.parent / f".{target.name}.delivery.lock"
    fd = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with tempfile.TemporaryDirectory(prefix=".delivery-", dir=target.parent) as temporary:
            stage = Path(temporary)
            for relative, expected in sorted(original.files.items()):
                origin = resolve_path(source, source, relative)
                destination = resolve_path(stage, stage, relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, destination)
                if _digest(destination) != expected:
                    raise ValueError("复制期间源文件发生变化，未生成交付目录")
            current = audit_batch(source)
            if not current.ready or current.files != original.files:
                raise ValueError("源批次在生成期间发生变化，请重新检查")

            revision = uuid4().hex
            created = datetime.now().astimezone().isoformat(timespec="seconds")
            manifest = read_json(stage / "manifest.json")
            manifest["delivery"] = {"sealed": True, "revision": revision, "created": created,
                                    "source_batch_id": manifest["batch_id"]}
            write_json_atomic(stage / "manifest.json", manifest)
            files = dict(original.files)
            files["manifest.json"] = _digest(stage / "manifest.json")
            write_json_atomic(stage / "delivery.json", {
                "schema_version": "1.0", "revision": revision, "created": created,
                "files": files, "summary": original.summary, "issues": original.issues,
                "source_fingerprint": hashlib.sha256(
                    json.dumps(original.files, sort_keys=True).encode("utf-8")).hexdigest(),
            })
            checked = audit_batch(stage)
            if not checked.ready:
                raise DeliveryError(checked)
            if checked.files != files:
                raise ValueError("交付副本与检查清单不一致")
            if target.exists() or target.is_symlink():
                raise FileExistsError(f"目标已经存在，不会覆盖：{target}")
            publish_directory(stage, target)
    finally:
        reservation.unlink(missing_ok=True)
    return target
