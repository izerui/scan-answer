"""试识别：在编辑器中快速验证模板是否能正确识别。"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..engine.recognize import (PhysicalError, SheetDecision, TemplateContext,
                                 Thresholds, analyze_sheet, decide_sheet,
                                 warp_to_template)
from ..template.model import Template


@dataclass
class TrialSheet:
    sources: list[str]
    ok: bool
    message: str
    decision: SheetDecision | None
    warped: dict[str, np.ndarray]


def trial_recognize(
    t: Template,
    bases: dict[str, np.ndarray],
    pairs: list[list[str]],
) -> list[TrialSheet]:
    """对每组图片进行试识别，返回结果列表。

    不写任何文件；PhysicalError 或读图失败时返回 ok=False。
    """
    ctx = TemplateContext(t, bases)
    th = Thresholds()
    results: list[TrialSheet] = []

    for group in pairs:
        # 读取图片
        images: list[np.ndarray] = []
        dpis: list[float] = []
        read_ok = True
        bad_file = ""
        for path in group:
            try:
                img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
                if img is None:
                    read_ok = False
                    bad_file = path
                    break
                images.append(img)
                # 尝试读取 DPI
                try:
                    from PIL import Image
                    with Image.open(path) as pil:
                        dpi_info = pil.info.get("dpi")
                        dpi = float(dpi_info[0]) if dpi_info and dpi_info[0] and float(dpi_info[0]) > 50 else None
                except Exception:
                    dpi = None
                if dpi is None:
                    # 根据纸张估算
                    dpi = max(img.shape) / max(t.paper) * 25.4
                dpis.append(dpi)
            except Exception:
                read_ok = False
                bad_file = path
                break

        if not read_ok:
            from pathlib import Path
            results.append(TrialSheet(
                sources=group, ok=False,
                message=f"无法读取 {Path(bad_file).name}",
                decision=None, warped={},
            ))
            continue

        try:
            a = analyze_sheet(ctx, images, dpis)
            decision = decide_sheet(ctx, a, th)
            # 生成 warped 图（按模板 DPI 对齐）
            warped: dict[str, np.ndarray] = {}
            from ..engine.recognize import oriented
            for face, placement in a.placements.items():
                g = oriented(images[placement.source], placement.rotated)
                warped[face] = warp_to_template(g, placement.align.H, t.paper, t.dpi)
            results.append(TrialSheet(
                sources=group, ok=True, message="",
                decision=decision, warped=warped,
            ))
        except PhysicalError as e:
            results.append(TrialSheet(
                sources=group, ok=False,
                message=f"{e.code}: {e.msg}",
                decision=None, warped={},
            ))
        except Exception as e:
            results.append(TrialSheet(
                sources=group, ok=False,
                message=str(e),
                decision=None, warped={},
            ))

    return results
