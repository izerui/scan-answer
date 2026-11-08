"""试识别：在编辑器中快速验证模板是否能正确识别。"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..engine.recognize import (PhysicalError, SheetDecision, TemplateContext,
                                 Thresholds, analyze_sheet, decide_sheet,
                                 warp_to_template)
from ..imageio import imread_gray
from ..engine.result import build_result, render_images
from ..output.evidence import image_path
from ..source.base import SheetImages
from ..source.documents import DocumentSource
from ..source.folder import choose_dpi, read_dpi
from ..template.model import Template


@dataclass
class TrialSheet:
    sources: list[str]
    ok: bool
    message: str
    decision: SheetDecision | None
    warped: dict[str, np.ndarray]
    input_pages: list[dict] = field(default_factory=list)
    result: dict | None = None
    images: dict[str, np.ndarray] = field(default_factory=dict)


def trial_recognize(
    t: Template,
    bases: dict[str, np.ndarray],
    pairs: list[list[str]],
) -> list[TrialSheet]:
    """对每组图片进行试识别，返回结果列表。

    不写任何文件；PhysicalError 或读图失败时返回 ok=False。
    """
    ctx = TemplateContext(t, bases)
    return [_trial_sheet(ctx, SheetImages(
        source="试识别", names=[Path(p).name for p in group],
        dpis=[read_dpi(Path(p)) for p in group], paths=[Path(p) for p in group],
    ), seq=i) for i, group in enumerate(pairs, 1)]


def trial_documents(t: Template, bases: dict[str, np.ndarray], files: list[str]) -> list[TrialSheet]:
    """正式识别与试识别共用 PDF 分页和文件配组规则。输入失败在结果中明确显示。"""
    ctx = TemplateContext(t, bases)
    results: list[TrialSheet] = []
    try:
        source = DocumentSource(files, len(t.faces))
        with closing(source.sheets()) as sheets:
            for seq, sheet in enumerate(sheets, 1):
                results.append(_trial_sheet(ctx, sheet, seq=seq))
    except (OSError, ValueError) as e:
        results.append(TrialSheet([str(p) for p in files], False, str(e), None, {}))
    return results


def _trial_sheet(ctx: TemplateContext, sheet: SheetImages, *, seq: int = 1) -> TrialSheet:
    sources = [str(p) for p in sheet.paths] if sheet.paths else [
        f"{p['file']} 第 {p['page']} 页" for p in sheet.origins
    ]
    try:
        images = [imread_gray(p) for p in sheet.paths] if sheet.paths else sheet.images
        for i, image in enumerate(images):
            if image is None:
                raise ValueError(f"无法读取 {Path(sources[i]).name}")
        dpis = [choose_dpi(meta, max(image.shape) / max(ctx.tpl.paper) * 25.4)
                for image, meta in zip(images, sheet.dpis)]
        analysis = analyze_sheet(ctx, images, dpis)
        decision = decide_sheet(ctx, analysis, Thresholds())
        evidence = render_images(ctx, analysis, decision, images)
        result = build_result(
            ctx, analysis, decision, seq=seq, batch_id="trial", exam={}, scanner="folder",
            raw_paths=[], image_paths=set(evidence), input_pages=sheet.origins,
        )
        from ..engine.recognize import oriented
        warped = {
            face: (evidence[image_path("page", face)] if p.dpi == ctx.tpl.dpi else
                   warp_to_template(oriented(images[p.source], p.rotated),
                                    p.align.H, ctx.tpl.paper, ctx.tpl.dpi))
            for face, p in analysis.placements.items()
        }
        return TrialSheet(sources, True, "", decision, warped, sheet.origins, result, evidence)
    except PhysicalError as e:
        return TrialSheet(sources, False, f"{e.code}: {e.msg}", None, {}, sheet.origins)
    except Exception as e:
        return TrialSheet(sources, False, str(e), None, {}, sheet.origins)
