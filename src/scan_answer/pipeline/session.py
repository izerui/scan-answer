"""批次会话：批次目录、序号分配、原图保存、阈值、manifest。只在主进程中使用。"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from ..engine.omr import Thresholds, fit_thresholds
from ..engine.state import analysis_fills, analysis_from_dict
from ..output.jsonfile import read_json
from ..output.local import write_json_atomic
from ..source.base import SheetImages
from .worker import SheetOutcome, clear_outputs, state_path

REFIT_MIN = 100  # 成功识别满这么多张后改用拟合阈值
REFIT_EVERY = 50  # 之后每多这么多张重新拟合一次
MANIFEST_INTERVAL = 2.0  # manifest 最短写盘间隔（秒）


class BatchSession:
    def __init__(self, batch_dir: Path, template_dir: Path, manifest: dict):
        self.batch_dir = Path(batch_dir)
        self.template_dir = Path(template_dir)
        self.manifest = manifest
        self.sheets: dict[int, dict] = {s["seq"]: s for s in manifest.get("sheets", [])}
        self.fills: dict[int, np.ndarray] = {}
        self.thresholds = Thresholds(**manifest["thresholds"]) if manifest.get("thresholds") else Thresholds()
        self._last_fit_count = 0
        self._last_save = 0.0

    # ---------- 创建 / 打开 ----------
    @classmethod
    def create(cls, out_root: str | Path, template_dir: str | Path, *, batch_id: str | None = None,
               exam: dict | None = None, scanner: str = "folder", ocr: bool = False) -> "BatchSession":
        template_dir = Path(template_dir)
        batch_id = batch_id or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        batch_dir = Path(out_root) / batch_id
        if batch_dir.exists() and any(batch_dir.iterdir()):
            raise FileExistsError(f"批次目录已存在且非空: {batch_dir}")
        batch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(template_dir / "template.json", batch_dir / "template.json")
        tpl = read_json(template_dir / "template.json")
        manifest = {
            "schema_version": "1.0", "batch_id": batch_id, "exam": exam or {},
            "template": {"name": tpl["name"], "version": tpl["version"], "snapshot": "template.json"},
            "scanner": scanner, "ocr": ocr, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "thresholds": None, "finalized": False, "counts": {}, "events": [], "sheets": [],
        }
        s = cls(batch_dir, template_dir, manifest)
        s.save(force=True)
        return s

    @classmethod
    def open(cls, batch_dir: str | Path, template_dir: str | Path) -> "BatchSession":
        """打开已有批次（程序重启后继续扫描）；涂黑度从 state/ 恢复。"""
        s = cls(Path(batch_dir), Path(template_dir), read_json(Path(batch_dir) / "manifest.json"))
        for seq, item in s.sheets.items():
            sp = state_path(s.batch_dir, seq)
            if item.get("result") and sp.exists():
                s.fills[seq] = analysis_fills(analysis_from_dict(read_json(sp)["analysis"]))
        return s

    # ---------- 属性 ----------
    @property
    def batch_id(self) -> str:
        return self.manifest["batch_id"]

    @property
    def faces(self) -> int:
        return len(read_json(self.batch_dir / "template.json")["faces"])

    def next_seq(self) -> int:
        return max(self.sheets, default=0) + 1

    # ---------- 原图 ----------
    def save_raw(self, seq: int, sheet: SheetImages) -> list[Path]:
        """把一张卡的各面原图存到 raw/，文件名 {seq:04d}_{原文件名}。"""
        raw = self.batch_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        out = []
        for i, name in enumerate(sheet.names):
            dst = raw / f"{seq:04d}_{name}"
            if sheet.paths:
                shutil.copy(sheet.paths[i], dst)
            else:
                dpi = sheet.dpis[i] or 300
                Image.fromarray(sheet.images[i]).save(dst, quality=95, dpi=(dpi, dpi))
            out.append(dst)
        return out

    def remove_raw(self, seq: int) -> None:
        for p in (self.batch_dir / "raw").glob(f"{seq:04d}_*"):
            p.unlink()

    def prepare_rescan(self, seq: int) -> None:
        if seq not in self.sheets:
            raise KeyError(f"序号不存在: {seq}")
        clear_outputs(self.batch_dir, seq)
        self.remove_raw(seq)
        self.fills.pop(seq, None)

    # ---------- 记录结果 ----------
    def register(self, seq: int, sheet: SheetImages) -> None:
        self.sheets[seq] = {"seq": seq, "source": sheet.source, "files": list(sheet.names), "status": "processing"}

    def record(self, out: SheetOutcome) -> Thresholds | None:
        """记录一张卡的结果；若因此重新拟合了阈值，返回新阈值。"""
        item = self.sheets[out.seq]
        for k in ("result", "exam_no", "warnings", "physical", "message", "voided"):
            item.pop(k, None)
        if out.ok:
            item.update({"status": "ok", "result": out.result, "exam_no": out.exam_no, "warnings": out.warnings})
            self.fills[out.seq] = np.asarray(out.fills, dtype=np.float32)
        else:
            item.update({"status": "physical", "physical": out.physical, "message": out.message})
            self.fills.pop(out.seq, None)
        refit = None
        n = len(self.fills)
        if not self.manifest["finalized"] and n >= REFIT_MIN and n - self._last_fit_count >= (REFIT_EVERY if self._last_fit_count else 0):
            self.thresholds = fit_thresholds(np.concatenate(list(self.fills.values())))
            self._last_fit_count = n
            refit = self.thresholds
        self.save()
        return refit

    def void(self, seq: int) -> None:
        item = self.sheets[seq]
        if item.get("status") != "physical":
            raise ValueError(f"只能作废物理异常的卡: {seq}")
        item["status"] = "voided"
        item["voided"] = True
        self.save(force=True)

    def add_event(self, kind: str, message: str) -> None:
        self.manifest["events"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                        "kind": kind, "message": message, "after_seq": self.next_seq() - 1})
        self.save(force=True)

    # ---------- 结束批次 ----------
    def final_thresholds(self) -> Thresholds:
        if not self.fills:
            return Thresholds()
        return fit_thresholds(np.concatenate(list(self.fills.values())))

    def mark_finalized(self, th: Thresholds) -> None:
        self.thresholds = th
        self.manifest["finalized"] = True
        self.save(force=True)

    # ---------- manifest ----------
    def counts(self) -> dict:
        items = list(self.sheets.values())
        return {"total": len(items),
                "ok": sum(1 for s in items if s.get("status") == "ok"),
                "physical": sum(1 for s in items if s.get("status") == "physical"),
                "voided": sum(1 for s in items if s.get("status") == "voided"),
                "warnings": sum(s.get("warnings", 0) for s in items if s.get("status") == "ok")}

    def save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save < MANIFEST_INTERVAL:
            return
        self.manifest["sheets"] = [self.sheets[k] for k in sorted(self.sheets)]
        self.manifest["counts"] = self.counts()
        self.manifest["thresholds"] = {"mark": round(self.thresholds.mark, 4),
                                       "uncertain": round(self.thresholds.uncertain, 4)}
        write_json_atomic(self.batch_dir / "manifest.json", self.manifest)
        self._last_save = now
