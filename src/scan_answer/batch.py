"""文件批量识别：图片/PDF 文档源 + 流水线 + 结束批次重新判定（+ 可选手写转写）。

命令行 `scan-answer recognize` 与界面的"导入图片"都走这里；扫描仪入口（计划 4）直接使用 BatchPipeline。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .ocr.config import OcrConfig
from .ocr.queue import OcrQueue
from .pipeline.events import OcrProgress
from .pipeline.runner import BatchPipeline
from .pipeline.session import BatchSession
from .output.jsonfile import read_json
from .source.folder import FolderSource, choose_dpi, list_images, pair_images, read_dpi, source_name  # noqa: F401
from .source.documents import DocumentSource

__all__ = ["run_batch", "ocr_progress_listener", "choose_dpi", "list_images", "pair_images", "read_dpi", "source_name"]


def result_ocr_progress(batch_dir: str | Path, sheets: list[dict] | None = None) -> OcrProgress:
    """Final progress describes persisted items, including retries from other queues."""
    root = Path(batch_dir)
    if sheets is None:
        sheets = read_json(root / "manifest.json").get("sheets", [])
    done = failed = total = 0
    for item in sheets:
        if item.get("status") != "ok" or not item.get("result"):
            continue
        try:
            result = read_json(root / item["result"])
            entries = list(result.get("subjective", []))
            entries.extend(result.get("student", {}).get("fields", {}).values())
            for entry in entries:
                status = entry.get("ocr_status", "none")
                if status != "none":
                    total += 1
                    done += status == "done"
                    failed += status == "failed"
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return OcrProgress(done, failed, total)


def ocr_progress_listener(session: BatchSession | None,
                          listener: Callable[[object], None] | None) -> Callable[[object], None]:
    """包一层转写进度监听：收到 OcrProgress 时先写入 manifest，再转给外部 listener。

    全部转完（done + failed == total）时强制写盘，保证后台转完的批次 manifest 里是最终进度。"""
    def on_event(e: object) -> None:
        if session is not None and isinstance(e, OcrProgress):
            finished = e.done + e.failed >= e.total
            if finished:
                with session.lock:
                    e = result_ocr_progress(session.batch_dir, list(session.sheets.values()))
            session.set_ocr_progress(e.done, e.failed, e.total, force=finished)
        if listener:
            listener(e)
    return on_event


def run_batch(template_dir: str | Path, input_dir: str | Path, out_root: str | Path, *,
              exam: dict | None = None, batch_id: str | None = None, scanner: str = "folder",
              workers: int = 0, ocr_config: OcrConfig | None = None,
              listener: Callable[[object], None] | None = None) -> Path:
    session = BatchSession.create(out_root, template_dir, batch_id=batch_id, exam=exam, scanner=scanner,
                                  ocr=ocr_config is not None)
    ocr = OcrQueue(ocr_config, ocr_progress_listener(session, listener)) if ocr_config else None
    try:
        with BatchPipeline(session, workers=workers, listener=listener,
                           on_result=ocr.enqueue if ocr else None) as pipe:
            pipe.run(DocumentSource(input_dir, session.faces))
            pipe.finalize()
        if ocr:
            ocr.wait()
            progress = result_ocr_progress(session.batch_dir, list(session.sheets.values()))
            session.set_ocr_progress(progress.done, progress.failed, progress.total, force=True)
    finally:
        if ocr:
            ocr.close()
    return session.batch_dir
