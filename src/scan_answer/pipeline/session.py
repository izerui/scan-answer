"""批次会话：批次目录、序号分配、原图保存、阈值、manifest。只在主进程中使用。

转写线程会调用 set_ocr_progress，所以修改 manifest 的方法都在 self.lock（可重入锁）内执行。"""
from __future__ import annotations

import shutil
import threading
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
from ..source.folder import source_name
from .worker import SheetOutcome, clear_outputs, recognition_warnings, state_path

REFIT_MIN = 100  # 成功识别满这么多张后改用拟合阈值
REFIT_EVERY = 50  # 之后每多这么多张重新拟合一次
MANIFEST_INTERVAL = 2.0  # manifest 最短写盘间隔（秒）
SNAPSHOT_DIR = "template"  # 批次内模板快照目录


class BatchSession:
    def __init__(self, batch_dir: Path, template_dir: Path, manifest: dict):
        self.batch_dir = Path(batch_dir)
        self.template_dir = Path(template_dir)
        self.manifest = manifest
        self.manifest.setdefault("ocr_progress", None)
        self.lock = threading.RLock()
        self.sheets: dict[int, dict] = {s["seq"]: s for s in manifest.get("sheets", [])}
        self.fills: dict[int, np.ndarray] = {}
        self.thresholds = Thresholds(**manifest["thresholds"]) if manifest.get("thresholds") else Thresholds()
        self._last_fit_count = 0
        self._last_save = 0.0
        self._disk_max = 0  # 磁盘上出现过的最大序号（open 时扫描）

    # ---------- 创建 / 打开 ----------
    @classmethod
    def create(cls, out_root: str | Path, template_dir: str | Path, *, batch_id: str | None = None,
               exam: dict | None = None, scanner: str = "folder", ocr: bool = False) -> "BatchSession":
        template_dir = Path(template_dir)
        if batch_id is None:
            # 自动生成的批次号精确到秒：同一秒内重复创建时加后缀 _2、_3…（mkdir 不带 exist_ok，原子占位）
            base = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
            Path(out_root).mkdir(parents=True, exist_ok=True)
            batch_id, n = base, 1
            while True:
                batch_dir = Path(out_root) / batch_id
                try:
                    batch_dir.mkdir()
                    break
                except FileExistsError:
                    n += 1
                    batch_id = f"{base}_{n}"
        else:
            batch_dir = Path(out_root) / batch_id
            if batch_dir.exists() and any(batch_dir.iterdir()):
                raise FileExistsError(f"批次目录已存在且非空: {batch_dir}")
            batch_dir.mkdir(parents=True, exist_ok=True)
        tpl = read_json(template_dir / "template.json")
        # 模板快照：template.json 与各面底图复制到批次内，识别一律用快照（原模板之后被修改也不影响本批次）
        snap = batch_dir / SNAPSHOT_DIR
        snap.mkdir()
        shutil.copy(template_dir / "template.json", snap / "template.json")
        for face in tpl["faces"].values():
            dst = snap / face["image"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(template_dir / face["image"], dst)
        shutil.copy(template_dir / "template.json", batch_dir / "template.json")  # 兼容旧版，供人查看
        manifest = {
            "schema_version": "1.0", "batch_id": batch_id, "exam": exam or {},
            "template": {"name": tpl["name"], "version": tpl["version"],
                         "snapshot": f"{SNAPSHOT_DIR}/template.json"},
            "scanner": scanner, "ocr": ocr, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "thresholds": None, "finalized": False, "counts": {}, "ocr_progress": None, "events": [], "sheets": [],
        }
        s = cls(batch_dir, snap, manifest)
        s.save(force=True)
        return s

    @classmethod
    def open(cls, batch_dir: str | Path, template_dir: str | Path | None = None) -> "BatchSession":
        """打开已有批次（程序重启后继续扫描）；涂黑度从 state/ 恢复。

        有模板快照时一律用快照；没有快照的旧批次才用传入的 template_dir。"""
        batch_dir = Path(batch_dir)
        snap = batch_dir / SNAPSHOT_DIR
        if (snap / "template.json").exists():
            template_dir = snap
        elif template_dir is None:
            raise FileNotFoundError(f"批次中没有模板快照，需要指定模板目录: {batch_dir}")
        s = cls(batch_dir, Path(template_dir), read_json(batch_dir / "manifest.json"))
        for seq, item in s.sheets.items():
            sp = state_path(s.batch_dir, seq)
            if item.get("result") and sp.exists():
                s.fills[seq] = analysis_fills(analysis_from_dict(read_json(sp)["analysis"]))
        s._reconcile()
        return s

    def _disk_seqs(self) -> set[int]:
        """raw/、state/、students/、errors/ 中出现的序号（文件/目录名前 4 位数字）。"""
        seqs = set()
        for sub in ("raw", "state", "students", "errors"):
            d = self.batch_dir / sub
            if d.exists():
                seqs.update(int(p.name[:4]) for p in d.iterdir() if p.name[:4].isdigit())
        return seqs

    def _reconcile(self) -> None:
        """崩溃后 manifest 可能落后于磁盘：按磁盘内容恢复 processing 条目与 manifest 中缺失的序号。"""
        disk = self._disk_seqs()
        self._disk_max = max(disk, default=0)
        todo = {seq for seq, item in self.sheets.items() if item.get("status") == "processing"}
        todo |= disk - set(self.sheets)
        for seq in sorted(todo):
            item = self.sheets.get(seq)
            if item is None:
                raws = sorted((self.batch_dir / "raw").glob(f"{seq:04d}_*"))
                files = [p.name[5:] for p in raws]
                item = {"seq": seq, "source": source_name(Path(files[0])) if files else "恢复", "files": files}
                self.sheets[seq] = item
            for k in ("status", "result", "exam_no", "warnings", "physical", "message", "voided"):
                item.pop(k, None)
            if state_path(self.batch_dir, seq).exists() and self._restore_ok(seq, item):
                continue
            errs = sorted((self.batch_dir / "errors").glob(f"{seq:04d}_*/error.json"))
            if errs:
                err = read_json(errs[0])
                item.update({"status": "physical", "physical": err["code"], "message": err["msg"]})
            else:
                item.update({"status": "physical", "physical": "ALIGN_FAILED",
                             "message": "识别中断（程序退出前未完成），请重扫"})
        self.save(force=True)

    def _restore_ok(self, seq: int, item: dict) -> bool:
        """按 state/{seq}.json 与 result.json 恢复成功条目；文件缺失或损坏返回 False。"""
        try:
            st = read_json(state_path(self.batch_dir, seq))
            result = read_json(self.batch_dir / st["result"])
            exam = result["student"].get("exam_no") or {}
            fills = analysis_fills(analysis_from_dict(st["analysis"]))
            warnings = recognition_warnings(result["warnings"])
        except (OSError, KeyError, TypeError, ValueError):
            return False
        item.update({"status": "ok", "result": st["result"], "exam_no": exam.get("value"), "warnings": warnings})
        self.fills[seq] = fills
        return True

    # ---------- 属性 ----------
    @property
    def batch_id(self) -> str:
        return self.manifest["batch_id"]

    @property
    def faces(self) -> int:
        return len(read_json(self.batch_dir / "template.json")["faces"])

    def next_seq(self) -> int:
        return max(max(self.sheets, default=0), self._disk_max) + 1

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
        status = self.sheets[seq].get("status")
        if status not in ("physical", "voided"):
            raise ValueError(f"只能重扫物理异常或已作废的卡: {seq}（当前状态 {status}）")
        clear_outputs(self.batch_dir, seq)
        self.remove_raw(seq)
        self.fills.pop(seq, None)

    # ---------- 记录结果 ----------
    def register(self, seq: int, sheet: SheetImages) -> None:
        with self.lock:
            self.sheets[seq] = {"seq": seq, "source": sheet.source, "files": list(sheet.names), "status": "processing"}
            self.save(force=True)  # 立即写盘：崩溃后重开时据此识别未完成的卡

    def record(self, out: SheetOutcome) -> Thresholds | None:
        """记录一张卡的结果；若因此重新拟合了阈值，返回新阈值。"""
        with self.lock:
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
        with self.lock:
            item = self.sheets[seq]
            if item.get("status") != "physical":
                raise ValueError(f"只能作废物理异常的卡: {seq}")
            item["status"] = "voided"
            item["voided"] = True
            self.save(force=True)

    def add_event(self, kind: str, message: str) -> None:
        with self.lock:
            self.manifest["events"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                            "kind": kind, "message": message, "after_seq": self.next_seq() - 1})
            self.save(force=True)

    # ---------- 结束批次 ----------
    def final_thresholds(self) -> Thresholds:
        if not self.fills:
            return Thresholds()
        return fit_thresholds(np.concatenate(list(self.fills.values())))

    def mark_finalized(self, th: Thresholds) -> None:
        with self.lock:
            self.thresholds = th
            self.manifest["finalized"] = True
            self.save(force=True)

    def set_ocr_progress(self, done: int, failed: int, total: int, force: bool = False) -> None:
        """记录手写转写进度（转写线程调用，节流保存）。"""
        with self.lock:
            self.manifest["ocr_progress"] = {"done": done, "failed": failed, "total": total}
            self.save(force)

    # ---------- manifest ----------
    def counts(self) -> dict:
        items = list(self.sheets.values())
        return {"total": len(items),
                "ok": sum(1 for s in items if s.get("status") == "ok"),
                "physical": sum(1 for s in items if s.get("status") == "physical"),
                "voided": sum(1 for s in items if s.get("status") == "voided"),
                "warnings": sum(s.get("warnings", 0) for s in items if s.get("status") == "ok")}

    def save(self, force: bool = False) -> None:
        with self.lock:
            now = time.monotonic()
            if not force and now - self._last_save < MANIFEST_INTERVAL:
                return
            self.manifest["sheets"] = [self.sheets[k] for k in sorted(self.sheets)]
            self.manifest["counts"] = self.counts()
            self.manifest["thresholds"] = {"mark": round(self.thresholds.mark, 4),
                                           "uncertain": round(self.thresholds.uncertain, 4)}
            write_json_atomic(self.batch_dir / "manifest.json", self.manifest)
            self._last_save = now
