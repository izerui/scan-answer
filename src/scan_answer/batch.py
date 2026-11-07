"""文件夹批量识别：FolderSource + 流水线 + 结束批次重新判定（+ 可选手写转写）。

命令行 `scan-answer recognize` 与界面的"导入图片"都走这里；扫描仪入口（计划 4）直接使用 BatchPipeline。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .ocr.config import OcrConfig
from .ocr.queue import OcrQueue
from .pipeline.runner import BatchPipeline
from .pipeline.session import BatchSession
from .source.folder import FolderSource, choose_dpi, list_images, pair_images, read_dpi, source_name  # noqa: F401

__all__ = ["run_batch", "choose_dpi", "list_images", "pair_images", "read_dpi", "source_name"]


def run_batch(template_dir: str | Path, input_dir: str | Path, out_root: str | Path, *,
              exam: dict | None = None, batch_id: str | None = None, scanner: str = "folder",
              workers: int = 0, ocr_config: OcrConfig | None = None,
              listener: Callable[[object], None] | None = None) -> Path:
    session = BatchSession.create(out_root, template_dir, batch_id=batch_id, exam=exam, scanner=scanner,
                                  ocr=ocr_config is not None)
    ocr = OcrQueue(ocr_config, listener) if ocr_config else None
    try:
        with BatchPipeline(session, workers=workers, listener=listener,
                           on_result=ocr.enqueue if ocr else None) as pipe:
            pipe.run(FolderSource(input_dir, session.faces))
            pipe.finalize()
        if ocr:
            ocr.wait()
    finally:
        if ocr:
            ocr.close()
    return session.batch_dir
