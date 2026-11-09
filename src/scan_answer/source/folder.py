"""文件夹图像源：按文件名配对正反面（macOS 上的主要输入方式，Windows 上作兜底）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from PIL import Image

from .base import SheetImages

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
_SUFFIX = re.compile(r"_(\d+)$")


def list_images(input_dir: str | Path) -> list[Path]:
    return sorted(p for p in Path(input_dir).iterdir() if p.suffix.lower() in IMAGE_EXT)


def read_dpi(path: Path) -> float | None:
    """读取图片文件记录的 DPI（扫描仪输出的 JPEG/TIFF 通常都有）；没有或文件损坏返回 None。"""
    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi")
    except (Image.UnidentifiedImageError, OSError):
        return None
    if dpi and dpi[0] and float(dpi[0]) > 50:
        return float(dpi[0])
    return None


def choose_dpi(meta: float | None, estimated: float) -> float:
    """元数据 DPI 与按纸张尺寸估算的值相差超过 20% 时使用估算值，否则用元数据值。"""
    if meta is None or estimated <= 0:
        return meta or estimated
    if abs(meta / estimated - 1) > 0.2:
        return estimated
    return meta


def source_name(path: Path) -> str:
    stem = path.stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def _face_suffix_groups(paths: list[Path], faces: int) -> list[list[Path]] | None:
    """所有文件名都带 _N 后缀、且按前缀分组后每组都像“一张卡的各面”时返回分组，否则返回 None。

    像面后缀：组内张数不超过 faces，后缀是 1..faces 中互不相同的编号（缺一面允许）。
    IMG_0001..IMG_0200 这类流水号虽然也带 _N，但同一前缀下有很多张、编号超出面数，不按前缀配对。"""
    if not paths or not all(_SUFFIX.search(p.stem) for p in paths):
        return None
    groups: dict[str, list[Path]] = {}
    for p in paths:
        groups.setdefault(source_name(p), []).append(p)
    out = []
    for group in groups.values():
        nums = [int(_SUFFIX.search(p.stem).group(1)) for p in group]
        if len(group) > faces or len(set(nums)) != len(nums) or not all(1 <= n <= faces for n in nums):
            return None
        out.append(sorted(group, key=lambda p: int(_SUFFIX.search(p.stem).group(1))))
    return out


def pair_images(paths: list[Path], faces: int) -> list[list[Path]]:
    """配对：文件名都是“前缀_面号”（_1.._faces）时按前缀分组（组内按面号排序），否则按排序顺序每 faces 张一组。"""
    groups = _face_suffix_groups(paths, faces)
    if groups is not None:
        return groups
    return [paths[i:i + faces] for i in range(0, len(paths), faces)]


def pairing_note(paths: list[Path], faces: int) -> str | None:
    """说明图片实际按哪种规则配对；可能配错（命名混用、流水号、张数不成对）时返回提示，正常时返回 None。"""
    if not paths or faces < 2:
        return None
    if _face_suffix_groups(paths, faces) is not None:
        return None
    suffixed = sum(1 for p in paths if _SUFFIX.search(p.stem))
    reasons = []
    if 0 < suffixed < len(paths):
        reasons.append(f"{suffixed} 个文件名带 _1/_2 面号、{len(paths) - suffixed} 个不带，命名不统一")
    elif suffixed == len(paths):
        reasons.append("文件名末尾的 _数字 不是 1..%d 的面号（像流水号）" % faces)
    if len(paths) % faces:
        reasons.append(f"共 {len(paths)} 张图片，不是 {faces} 的整数倍，最后一份会缺面")
    if not reasons:
        return None
    return ("图片按文件名排序每 %d 张配成一份答卷：" % faces + "；".join(reasons)
            + "。若有缺页会导致之后的答卷串卷，请核对结果页的“正反面顺序”警告。")


class FolderSource:
    def __init__(self, input_dir: str | Path, faces: int):
        self.input_dir = Path(input_dir)
        self.faces = faces

    def sheets(self) -> Iterator[SheetImages]:
        for group in pair_images(list_images(self.input_dir), self.faces):
            yield SheetImages(source=source_name(group[0]), names=[p.name for p in group],
                              dpis=[read_dpi(p) for p in group], paths=list(group))
