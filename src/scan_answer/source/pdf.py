"""PDF 逐页读取；模板导入和答卷导入共用同一原生调用锁。"""
from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np

_PDF_LOCK = threading.RLock()
MAX_PAGE_PIXELS = 160_000_000


def pdf_page_count(source: str | Path | bytes) -> int:
    """Read actual page count under the shared PDFium lock, without rendering.

    Bytes let an auditor inspect the exact content whose digest it recorded.
    """
    try:
        import pypdfium2

        with _PDF_LOCK:
            with pypdfium2.PdfDocument(source if isinstance(source, bytes) else str(source)) as document:
                count = len(document)
                if count < 1:
                    raise ValueError("PDF has no pages")
                return count
    except Exception as exc:
        raise ValueError(f"Cannot read PDF page count: {exc}") from exc


def iter_pdf_pages(path: str | Path, dpi: float = 300,
                   skip: Callable[[int], bool] | None = None) -> Iterator[tuple[int, np.ndarray | None]]:
    """产出 (1 起始页码, 独立灰度数组)。释放锁后才 yield，避免占住其他入口。

    skip(页码) 为真的页不渲染，产出 (页码, None)。"""
    try:
        import pypdfium2
    except ImportError as e:
        raise ValueError(f"无法读取 PDF {Path(path).name}：缺少 pypdfium2") from e

    path = Path(path)
    document = None
    try:
        with _PDF_LOCK:
            document = pypdfium2.PdfDocument(str(path))
            count = len(document)
            if not count:
                raise ValueError("没有页面")
            document.init_forms()
    except Exception as e:
        if document is not None:
            with _PDF_LOCK:
                document.close()
        raise ValueError(f"无法读取 PDF {path.name}：{e}") from e
    try:
        for index in range(count):
            if skip is not None and skip(index + 1):
                yield index + 1, None
                continue
            try:
                with _PDF_LOCK:
                    page = document[index]
                    try:
                        width, height = page.get_size()
                        scale = dpi / 72
                        if math.ceil(width * scale) * math.ceil(height * scale) > MAX_PAGE_PIXELS:
                            raise ValueError("页面过大，请降低 PDF 分辨率或拆分文件")
                        bitmap = page.render(scale=scale, grayscale=True)
                        try:
                            image = bitmap.to_numpy().copy()
                            if image.ndim == 3:
                                image = image[:, :, 0].copy()
                        finally:
                            bitmap.close()
                    finally:
                        page.close()
            except Exception as e:
                raise ValueError(f"无法读取 PDF {path.name} 第 {index + 1} 页：{e}") from e
            yield index + 1, image
    finally:
        with _PDF_LOCK:
            document.close()
