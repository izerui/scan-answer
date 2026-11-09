"""填写答卷输入：图片按既有规则配组，PDF 在各自文件内逐页配组。"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator, Sequence
from contextlib import closing
from pathlib import Path

from .base import SheetImages
from .folder import IMAGE_EXT, pair_images, pairing_note, read_dpi, source_name
from .pdf import iter_pdf_pages, pdf_page_count

DOCUMENT_EXT = IMAGE_EXT | {".pdf"}
DOCUMENT_FILTER = "答卷文件 (*.pdf *.jpg *.jpeg *.png *.tif *.tiff *.bmp)"


class DocumentSource:
    def __init__(self, inputs: str | Path | Sequence[str | Path], faces: int, dpi: float = 300):
        if faces not in (1, 2):
            raise ValueError("每份答卷应为单面或双面")
        if not math.isfinite(dpi) or not 0 < dpi <= 600:
            raise ValueError("PDF 渲染分辨率必须在 0 到 600 DPI 之间")
        self.faces = faces
        self.dpi = float(dpi)
        entries = [inputs] if isinstance(inputs, (str, Path)) else list(inputs)
        paths: list[Path] = []
        for entry in entries:
            path = Path(entry)
            if path.is_dir():
                paths.extend(p for p in sorted(path.iterdir()) if p.is_file() and p.suffix.lower() in DOCUMENT_EXT)
            elif not path.is_file():
                raise ValueError(f"输入文件不存在：{path}")
            elif path.suffix.lower() not in DOCUMENT_EXT:
                raise ValueError(f"不支持的答卷文件：{path.name}")
            else:
                paths.append(path)
        self.paths = sorted(dict.fromkeys(paths))
        if not self.paths:
            raise ValueError("没有找到可识别的图片或 PDF")
        images = [p for p in self.paths if p.suffix.lower() in IMAGE_EXT]
        # 图片配对规则的提示（命名混用、流水号、张数不成对时可能配错），界面在开始识别时显示
        self.pairing_note = pairing_note(images, faces)

    def sheet_count(self) -> int | None:
        """本次输入一共会产出几份答卷（PDF 只读页数不渲染）；读不出时返回 None。"""
        images = [p for p in self.paths if p.suffix.lower() in IMAGE_EXT]
        total = len(pair_images(images, self.faces))
        for path in self.paths:
            if path.suffix.lower() == ".pdf":
                try:
                    total += math.ceil(pdf_page_count(path) / self.faces)
                except ValueError:
                    return None
        return total

    def sheets(self) -> Iterator[SheetImages]:
        # 图片继续共用前缀配组规则；PDF 不参与图片配组，也不跨 PDF 补尾页。
        images = [p for p in self.paths if p.suffix.lower() in IMAGE_EXT]
        groups = {group[0]: group for group in pair_images(images, self.faces)}
        for path in self.paths:
            if path.suffix.lower() == ".pdf":
                yield from self._pdf(path)
            elif path in groups:
                group = groups[path]
                names = [p.name for p in group]
                if len(names) != len(set(names)):
                    names = [f"input{i}_{p.name}" for i, p in enumerate(group, start=1)]
                yield SheetImages(
                    source=source_name(group[0]), names=names,
                    dpis=[read_dpi(p) for p in group], paths=group,
                    origins=[{"kind": "image", "file": p.name, "page": 1} for p in group],
                )

    def _pdf(self, path: Path) -> Iterator[SheetImages]:
        try:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as e:
            raise ValueError(f"无法读取 PDF {path.name}：{e}") from e
        with closing(iter_pdf_pages(path, self.dpi)) as pages:
            images, origins, names = [], [], []
            for number, image in pages:
                images.append(image)
                origins.append({"kind": "pdf", "file": path.name, "page": number, "document_id": digest})
                names.append(f"pdf_{digest[:16]}_{number:06d}.png")
                if len(images) == self.faces:
                    yield SheetImages(
                        source=f"{path.name} [{origins[0]['page']}-{origins[-1]['page']}]",
                        names=names, dpis=[self.dpi] * len(images), images=images,
                        origins=origins, documents={digest: path},
                    )
                    images, origins, names = [], [], []
            if images:
                yield SheetImages(
                    source=f"{path.name} [{origins[0]['page']}-{origins[-1]['page']}]",
                    names=names, dpis=[self.dpi] * len(images), images=images,
                    origins=origins, documents={digest: path},
                )
