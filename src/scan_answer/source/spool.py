"""Durable scanner pages, shared by live acquisition and session recovery.

Each run has run.json: {version: 1, run_id: str, faces: 1|2,
target_seq: int|null}. Pages live at round-NNNNNN/page-NNNNNN.png.
The PNG and its exact DPI are published in one atomic rename after fsync.
Recovery returns all published pages; sessions deduplicate acquisition_id.
"""
from __future__ import annotations

import json
import math
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from uuid import uuid4

import numpy as np
from PIL import Image, PngImagePlugin

from .base import SheetImages


def _sync_dir(path: Path) -> None:
    # Windows does not expose directory handles through os.open.
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _origin(run_id: str, round_id: int, page: int, target_seq: int | None) -> dict:
    origin = {
        "kind": "scanner",
        "acquisition_id": f"{run_id}:{round_id:06d}:{page:06d}",
        "run_id": run_id,
        "round_id": round_id,
        "page": page,
    }
    if target_seq is not None:
        origin["target_seq"] = target_seq
    return origin


@dataclass(frozen=True)
class SpooledPage:
    path: Path
    dpi: float | None
    origin: dict


def sheet_from_pages(pages: list[SpooledPage]) -> SheetImages:
    first = pages[0].origin
    return SheetImages(
        source=f"扫描 {first['run_id']} / {first['round_id']} / {first['page']}",
        names=[page.path.name for page in pages],
        dpis=[page.dpi for page in pages],
        paths=[page.path for page in pages],
        origins=[dict(page.origin) for page in pages],
    )


class AcquisitionSpool:
    def __init__(self, batch_dir: Path, faces: int, target_seq: int | None = None):
        if faces not in (1, 2):
            raise ValueError("Scanner faces must be 1 or 2")
        if target_seq is not None and (type(target_seq) is not int or target_seq < 1):
            raise ValueError("Rescan target must be a positive sequence")
        self.run_id = uuid4().hex
        self.run_dir = Path(batch_dir).resolve() / "acquisition" / self.run_id
        self.faces = faces
        self.target_seq = target_seq
        self._counts: dict[int, int] = {}
        self.run_dir.mkdir(parents=True)
        metadata = {
            "version": 1, "run_id": self.run_id, "faces": faces, "target_seq": target_seq,
        }
        pending = self.run_dir / "run.json.tmp"
        with pending.open("w", encoding="utf-8") as stream:
            json.dump(metadata, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, self.run_dir / "run.json")
        _sync_dir(self.run_dir)
        _sync_dir(self.run_dir.parent)
        _sync_dir(self.run_dir.parent.parent)

    def append(self, round_id: int, pixels: np.ndarray, dpi: float) -> SpooledPage:
        if round_id < 1 or not math.isfinite(dpi) or dpi <= 0:
            raise ValueError("Invalid scanner round or DPI")
        page = self._counts.get(round_id, 0) + 1
        folder = self.run_dir / f"round-{round_id:06d}"
        if not folder.exists():
            folder.mkdir()
            _sync_dir(self.run_dir)
        path = folder / f"page-{page:06d}.png"
        pending = path.with_suffix(".png.tmp")
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("scan_answer_dpi", str(float(dpi)))
        try:
            with pending.open("wb") as stream:
                Image.fromarray(pixels).save(
                    stream, format="PNG", pnginfo=metadata, dpi=(dpi, dpi),
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
            _sync_dir(folder)
        finally:
            pending.unlink(missing_ok=True)
        self._counts[round_id] = page
        return SpooledPage(path, float(dpi), _origin(self.run_id, round_id, page, self.target_seq))


def recover_spooled(batch_dir: Path) -> Iterator[SheetImages]:
    """Yield complete/partial groups without crossing runs, rounds or pair slots.

Does not consume/delete pages or consult session state. A corrupt published
image still yields its path so session recovery can record a rescan exception.
Unpublished *.tmp files are ignored. target_seq exists only for rescan origins.
"""
    for run_dir in sorted((Path(batch_dir).resolve() / "acquisition").glob("*")):
        if not run_dir.is_dir():
            continue
        try:
            metadata = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            run_id, faces = metadata["run_id"], metadata["faces"]
            target_seq = metadata["target_seq"]
            if (metadata["version"] != 1 or run_id != run_dir.name or faces not in (1, 2)
                    or (target_seq is not None and (type(target_seq) is not int or target_seq < 1))):
                raise ValueError("Invalid acquisition metadata")
        except (OSError, ValueError, KeyError, TypeError) as error:
            warnings.warn(f"Cannot recover acquisition run {run_dir}: {error}", stacklevel=2)
            continue
        rounds = sorted(
            (int(path.name[6:]), path) for path in run_dir.glob("round-*")
            if path.is_dir() and path.name[6:].isdigit()
        )
        for round_id, folder in rounds:
            groups: dict[int, list[SpooledPage]] = {}
            pages = sorted(
                (int(path.stem[5:]), path) for path in folder.glob("page-*.png")
                if path.stem[5:].isdigit() and int(path.stem[5:]) > 0
            )
            for number, path in pages:
                dpi = None
                try:
                    with Image.open(path) as image:
                        dpi = float(image.info["scan_answer_dpi"])
                        if not math.isfinite(dpi) or dpi <= 0:
                            dpi = None
                except (OSError, ValueError, KeyError):
                    pass
                origin = _origin(run_id, round_id, number, target_seq)
                groups.setdefault((number - 1) // faces, []).append(SpooledPage(path, dpi, origin))
            for pages_in_sheet in groups.values():
                yield sheet_from_pages(pages_in_sheet)
