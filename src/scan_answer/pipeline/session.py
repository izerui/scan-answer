"""批次会话：批次目录、序号分配、原图保存、阈值、manifest。只在主进程中使用。

转写线程会调用 set_ocr_progress，所以修改 manifest 的方法都在 self.lock（可重入锁）内执行。"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from ..engine.omr import Thresholds, fit_thresholds, threshold_issues
from ..engine.state import analysis_fills, analysis_from_dict
from ..output.jsonfile import read_json
from ..output.local import write_json_atomic
from ..output.protection import ensure_writable_batch, ensure_writable_result
from ..source.base import SheetImages
from ..source.folder import source_name
from .worker import SheetOutcome, recognition_warnings, state_path
from .paths import resolve_local_path, resolve_result_path

REFIT_MIN = 100  # 成功识别满这么多张后改用拟合阈值
REFIT_EVERY = 50  # 之后每多这么多张重新拟合一次
MANIFEST_INTERVAL = 2.0  # manifest 最短写盘间隔（秒）
SNAPSHOT_DIR = "template"  # 批次内模板快照目录


def _file_digest(path: Path) -> str:
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def input_keys(sheet: SheetImages) -> list[str] | None:
    """一份输入答卷各页的内容标识，用于同一批次内识别“重复导入”；扫描仪采集的页没有稳定标识，返回 None。

    图片按文件内容 SHA-256（换目录、改名仍能认出）；PDF 页按文档摘要 + 页码。"""
    keys = []
    for i, name in enumerate(sheet.names):
        origin = sheet.origins[i] if i < len(sheet.origins) else {}
        if origin.get("kind") == "pdf" and origin.get("document_id"):
            keys.append(f"pdf:{origin['document_id']}:{origin.get('page', i + 1)}")
        elif sheet.paths and origin.get("kind", "image") == "image":
            try:
                keys.append(f"sha256:{_file_digest(sheet.paths[i])}")
            except (OSError, IndexError):
                return None
        else:
            return None
    return keys or None


class BatchSession:
    def __init__(self, batch_dir: Path, template_dir: Path, manifest: dict):
        self.batch_dir = Path(batch_dir)
        self.template_dir = Path(template_dir)
        self.manifest = manifest
        self.manifest.setdefault("ocr_progress", None)
        self.manifest.setdefault("revision", 0)
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
               exam: dict | None = None, scanner: str = "folder", ocr: bool = False,
               student_dir_pattern: str = "{exam_no}") -> "BatchSession":
        ensure_writable_result(Path(out_root) / ".batch")
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
            "scanner": scanner, "ocr": ocr, "student_dir_pattern": student_dir_pattern, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "thresholds": None, "finalized": False, "counts": {}, "ocr_progress": None, "events": [], "sheets": [],
        }
        s = cls(batch_dir, snap, manifest)
        s.save(force=True)
        return s

    @classmethod
    def open(cls, batch_dir: str | Path, template_dir: str | Path | None = None) -> "BatchSession":
        """打开已有批次（程序重启后继续扫描）；涂黑度从 state/ 恢复。

        有模板快照时一律用快照；没有快照的旧批次才用传入的 template_dir。"""
        from .attempts import recover_attempts

        batch_dir = Path(batch_dir)
        try:
            ensure_writable_batch(batch_dir)
        except (OSError, ValueError) as error:
            # Invalid JSON may be recoverable from an interrupted transaction;
            # an intact sealed manifest must never enter recovery.
            from json import JSONDecodeError
            if not isinstance(error, (OSError, JSONDecodeError)):
                raise
        recovery_issues = recover_attempts(batch_dir)
        snap = batch_dir / SNAPSHOT_DIR
        if (snap / "template.json").exists():
            template_dir = snap
        elif template_dir is None:
            raise FileNotFoundError(f"批次中没有模板快照，需要指定模板目录: {batch_dir}")
        s = cls(batch_dir, Path(template_dir), read_json(batch_dir / "manifest.json"))
        for seq, item in s.sheets.items():
            sp = state_path(s.batch_dir, seq)
            if item.get("result"):
                try:
                    resolve_result_path(s.batch_dir, item["result"], seq)
                    sp = resolve_local_path(s.batch_dir, f"state/{seq:04d}.json")
                    s.fills[seq] = analysis_fills(analysis_from_dict(read_json(sp)["analysis"]))
                except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
                    if item.get("status") == "ok":
                        item["status"] = "processing"
        s._reconcile()
        s._recover_acquisitions()
        for issue in recovery_issues:
            item = s.sheets.get(issue["seq"])
            if item and item.get("attempt_id") in (None, issue["attempt_id"]):
                s.invalidate_finalization("恢复发现损坏的重扫记录")
                item.update(status="physical", physical="ALIGN_FAILED",
                            message=issue["message"], recovery_review=issue["path"])
                s.fills.pop(issue["seq"], None)
            if not any(e.get("recovery_path") == issue["path"] for e in s.manifest["events"]):
                s.manifest["events"].append({
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "kind": "recovery_warning", "message": issue["message"],
                    "after_seq": issue["seq"], "recovery_path": issue["path"],
                })
        if recovery_issues:
            s.save(force=True)
        return s

    def _recover_acquisitions(self) -> None:
        if not (self.batch_dir / "acquisition").exists():
            return
        from ..source.spool import recover_spooled
        from .attempts import retain_spooled

        known: dict[tuple[str, int], list[tuple[Path, int, dict]]] = {}

        def remember(root: Path, sheets) -> None:
            for item in sheets:
                for i, origin in enumerate(item.get("input_pages", []), 1):
                    acquisition = origin.get("acquisition_id")
                    if acquisition:
                        key = (acquisition, origin.get("page", i))
                        known.setdefault(key, []).append((root, item["seq"], origin))

        remember(self.batch_dir, self.sheets.values())
        for path in (self.batch_dir / "attempts").glob("*/*/stage/manifest.json"):
            try:
                remember(path.parent, read_json(path).get("sheets", []))
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        for path in (self.batch_dir / "attempts").glob("*/*/previous/manifest.json"):
            try:
                seq = int(path.parents[2].name)
                remember(path.parent, [item for item in read_json(path).get("sheets", [])
                                       if item["seq"] == seq])
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                continue
        for sheet in recover_spooled(self.batch_dir):
            keys = [(o.get("acquisition_id"), o.get("page", i))
                    for i, o in enumerate(sheet.origins, 1)]
            if not keys or any(not key[0] for key in keys):
                continue
            known_seqs = set()
            for i, key in enumerate(keys):
                for root, seq, origin in known.get(key, []):
                    known_seqs.add(seq)
                    raw = root / origin["raw"]
                    if raw.exists():
                        continue
                    # Only the missing page is restored; other pages may already be active.
                    one = SheetImages(sheet.source, [raw.name.split("_", 1)[1]], [sheet.dpis[i]],
                                      paths=[sheet.paths[i]] if sheet.paths else [],
                                      images=[sheet.images[i]] if sheet.images else [])
                    dest = self if root == self.batch_dir else BatchSession(
                        root, self.template_dir, read_json(root / "manifest.json"))
                    dest.save_raw(seq, one)
            unseen = [i for i, key in enumerate(keys) if key not in known]
            if not unseen:
                continue
            sheet = SheetImages(
                sheet.source, [sheet.names[i] for i in unseen], [sheet.dpis[i] for i in unseen],
                paths=[sheet.paths[i] for i in unseen] if sheet.paths else [],
                images=[sheet.images[i] for i in unseen] if sheet.images else [],
                origins=[sheet.origins[i] for i in unseen], documents=sheet.documents,
            )
            targets = {o["target_seq"] for o in sheet.origins if o.get("target_seq") is not None}
            if targets or known_seqs:
                # Partial groups are audit evidence, not automatic missing-face repair.
                # Sequence zero is an unassigned history when ownership is ambiguous.
                candidates = targets or known_seqs
                target = int(next(iter(candidates))) if len(candidates) == 1 else 0
                retained = retain_spooled(self, target, sheet)
                remember(retained.batch_dir, retained.sheets.values())
                continue
            seq = self.next_seq()
            self.register(seq, sheet)
            self.save_raw(seq, sheet)
            self.record(SheetOutcome(seq, False, physical="ALIGN_FAILED",
                                     message="采集后识别中断，原图已恢复，可点“重新识别”或重扫"))
            self.save(force=True)
            remember(self.batch_dir, [self.sheets[seq]])

    def _disk_seqs(self) -> set[int]:
        """Read the full numeric prefix; sequence 10000 must not become 1000."""
        seqs = set()
        for sub in ("raw", "state", "students", "errors"):
            d = self.batch_dir / sub
            if d.exists():
                for path in d.iterdir():
                    match = re.match(r"^(\d+)(?:_|\.json$|$)", path.name)
                    if match:
                        seqs.add(int(match[1]))
        return seqs

    def _reconcile(self) -> None:
        """崩溃后 manifest 可能落后于磁盘：按磁盘内容恢复 processing 条目与 manifest 中缺失的序号。"""
        disk = self._disk_seqs()
        self._disk_max = max(disk, default=0)
        todo = {seq for seq, item in self.sheets.items() if item.get("status") == "processing"}
        todo |= disk - set(self.sheets)
        if todo or (self.manifest.get("finalized") and
                    any(item.get("status") not in ("ok", "voided") for item in self.sheets.values())):
            self.invalidate_finalization("存在未结算或恢复的答卷")
        for seq in sorted(todo):
            item = self.sheets.get(seq)
            if item is None:
                raws = sorted((self.batch_dir / "raw").glob(f"{seq:04d}_*"))
                files = [p.name.split("_", 1)[1] for p in raws]
                item = {"seq": seq, "source": source_name(Path(files[0])) if files else "恢复", "files": files}
                if files:
                    # 按 raw/ 里的原图补齐来源，崩溃恢复的卡也能“重新识别”或手动定位
                    item["input_pages"] = [self._recovered_origin(name, i) for i, name in enumerate(files, 1)]
                    item["input_pages"] = [dict(o, raw=f"raw/{seq:04d}_{name}")
                                           for o, name in zip(item["input_pages"], files)]
                self.sheets[seq] = item
            for k in ("status", "result", "exam_no", "warnings", "physical", "message", "voided"):
                item.pop(k, None)
            if state_path(self.batch_dir, seq).exists() and self._restore_ok(seq, item):
                continue
            errs = sorted((self.batch_dir / "errors").glob(f"{seq:04d}_*/error.json"))
            if errs:
                try:
                    err = read_json(errs[0])
                    item.update({"status": "physical", "physical": err["code"], "message": err["msg"]})
                    continue
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            item.update({"status": "physical", "physical": "ALIGN_FAILED",
                         "message": "识别中断（程序退出前未完成），原图完整时可点“重新识别”，否则请重扫"})
        self.save(force=True)

    def _recovered_origin(self, name: str, page: int) -> dict:
        """对账时由原图文件名推出来源：PDF 渲染页能对上 sources/ 里唯一的原 PDF 时还原页码与文档。"""
        m = re.fullmatch(r"pdf_([0-9a-f]{16})_(\d{6})\.png", name)
        if m:
            docs = sorted((self.batch_dir / "sources").glob(f"{m[1]}*.pdf"))
            if len(docs) == 1:
                return {"kind": "pdf", "file": docs[0].name, "page": int(m[2]), "document_id": docs[0].stem,
                        "document": f"sources/{docs[0].name}"}
        kind = "image" if self.manifest.get("scanner") == "folder" else "scanner"
        return {"kind": kind, "file": name, "page": page}

    def _restore_ok(self, seq: int, item: dict) -> bool:
        """按 state/{seq}.json 与 result.json 恢复成功条目；文件缺失或损坏返回 False。"""
        try:
            st = read_json(resolve_local_path(self.batch_dir, f"state/{seq:04d}.json"))
            result = read_json(resolve_result_path(self.batch_dir, st["result"], seq))
            exam = result["student"].get("exam_no") or {}
            fills = analysis_fills(analysis_from_dict(st["analysis"]))
            warnings = recognition_warnings(result["warnings"])
        except (OSError, KeyError, TypeError, ValueError, AttributeError, IndexError, OverflowError):
            return False
        item.update({"status": "ok", "result": st["result"], "exam_no": exam.get("value"), "warnings": warnings})
        if isinstance(st.get("input_pages"), list):
            item["input_pages"] = st["input_pages"]
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
    def _save_documents(self, sheet: SheetImages) -> None:
        """原 PDF 按内容归档；验证复制内容与读取时的摘要一致。"""
        for digest, source in sheet.documents.items():
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("原始文档摘要无效")
            folder = self.batch_dir / "sources"
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{digest}.pdf"
            if target.exists():
                continue
            fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
            try:
                actual = hashlib.sha256()
                with os.fdopen(fd, "wb") as writer:
                    with Path(source).open("rb") as reader:
                        while chunk := reader.read(1024 * 1024):
                            actual.update(chunk)
                            writer.write(chunk)
                if actual.hexdigest() != digest:
                    raise ValueError(f"输入 PDF 已改变，请重新导入：{Path(source).name}")
                os.replace(tmp, target)
            finally:
                Path(tmp).unlink(missing_ok=True)

    def save_raw(self, seq: int, sheet: SheetImages) -> list[Path]:
        """把一张卡的各面原图存到 raw/，文件名 {seq:04d}_{原文件名}。"""
        self.assert_writable()
        self._save_documents(sheet)
        raw = self.batch_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        out = []
        for i, name in enumerate(sheet.names):
            dst = raw / f"{seq:04d}_{name}"
            fd, tmp_name = tempfile.mkstemp(dir=raw, prefix=f".{seq:04d}_", suffix=dst.suffix)
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                if sheet.paths:
                    shutil.copy(sheet.paths[i], tmp)
                else:
                    dpi = sheet.dpis[i] or 300
                    Image.fromarray(sheet.images[i]).save(tmp, quality=95, dpi=(dpi, dpi))
                with tmp.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.replace(tmp, dst)
            finally:
                tmp.unlink(missing_ok=True)
            out.append(dst)
        return out

    def remove_raw(self, seq: int) -> None:
        self.assert_writable()
        for p in (self.batch_dir / "raw").glob(f"{seq:04d}_*"):
            p.unlink()

    def prepare_rescan(self, seq: int) -> None:
        """Validate eligibility; RescanAttempt owns all replacement and cleanup."""
        self.assert_writable()
        if seq not in self.sheets:
            raise KeyError(f"序号不存在: {seq}")
        status = self.sheets[seq].get("status")
        if status not in ("physical", "voided"):
            raise ValueError(f"只能重扫物理异常或已作废的卡: {seq}（当前状态 {status}）")
        for path in (self.batch_dir / "attempts" / f"{seq:04d}").glob("*/journal.json"):
            try:
                phase = read_json(path)["phase"]
            except (OSError, ValueError, KeyError, TypeError):
                raise RuntimeError("重扫日志未确认，请重开批次恢复后再重扫") from None
            if phase == "prepared":
                raise RuntimeError("上次重扫尚未完成恢复，请重开批次后再重扫")

    # ---------- 记录结果 ----------
    def register(self, seq: int, sheet: SheetImages, keys: list[str] | None = None) -> None:
        """登记一份答卷；keys 为 input_keys(sheet) 的结果（调用方已算过时传入，免得重复读文件）。"""
        self.assert_writable()
        if keys is None:
            keys = input_keys(sheet)
        if sheet.origins and len(sheet.origins) != len(sheet.names):
            raise ValueError("输入页面来源与页面数量不一致")
        origins = []
        for i, name in enumerate(sheet.names):
            origin = dict(sheet.origins[i]) if sheet.origins else {
                "kind": "image" if sheet.paths else "scanner", "file": name, "page": i + 1,
            }
            origin["raw"] = f"raw/{seq:04d}_{name}"
            if origin.get("document_id") in sheet.documents:
                origin["document"] = f"sources/{origin['document_id']}.pdf"
            origins.append(origin)
        with self.lock:
            self.invalidate_finalization("新增答卷")
            self.sheets[seq] = {"seq": seq, "source": sheet.source, "files": list(sheet.names),
                                "input_pages": origins, "status": "processing"}
            if keys:
                self.sheets[seq]["input_keys"] = list(keys)
            self.save(force=True)  # 立即写盘：崩溃后重开时据此识别未完成的卡

    def record(self, out: SheetOutcome) -> Thresholds | None:
        """记录一张卡的结果；若因此重新拟合了阈值，返回新阈值。"""
        self.assert_writable()
        with self.lock:
            self.invalidate_finalization("识别结果更新")
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
                all_fills = np.concatenate(list(self.fills.values()))
                self.thresholds = fit_thresholds(all_fills)
                self.manifest["threshold_issues"] = threshold_issues(all_fills)
                self._last_fit_count = n
                refit = self.thresholds
            self.save()
            return refit

    def void(self, seq: int) -> None:
        self.assert_writable()
        with self.lock:
            item = self.sheets[seq]
            if item.get("status") != "physical":
                raise ValueError(f"只能作废物理异常的卡: {seq}")
            item["status"] = "voided"
            item["voided"] = True
            item["void_reason"] = item.get("void_reason") or "操作员作废"
            item["voided_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.invalidate_finalization("作废答卷")
            self.save(force=True)

    def unvoid(self, seq: int) -> None:
        """撤销作废：恢复为物理异常（之后可重扫、手动定位或再作废），已有结算随之失效。"""
        self.assert_writable()
        with self.lock:
            item = self.sheets[seq]
            if item.get("status") != "voided":
                raise ValueError(f"只能恢复已作废的卡: {seq}")
            for k in ("voided", "void_reason", "voided_at"):
                item.pop(k, None)
            item["status"] = "physical"
            item.setdefault("physical", "ALIGN_FAILED")
            item.setdefault("message", "已从作废恢复，请重扫或作废")
            self.invalidate_finalization("恢复作废答卷")
            self.save(force=True)

    def registered_inputs(self) -> dict[str, int]:
        """已登记答卷的输入内容标识 -> 序号（用于跳过重复导入）。

        旧批次没有 input_keys 时，PDF 页按 input_pages 的文档摘要 + 页码补出；旧图片无法低成本补出，不参与去重。"""
        out: dict[str, int] = {}
        with self.lock:
            for seq, item in sorted(self.sheets.items()):
                keys = item.get("input_keys")
                if not keys:
                    keys = [f"pdf:{o['document_id']}:{o.get('page')}" for o in item.get("input_pages") or []
                            if o.get("kind") == "pdf" and o.get("document_id")]
                for key in keys or []:
                    out.setdefault(key, seq)
        return out

    def registered_sheets(self) -> dict[tuple[str, ...], int]:
        """已登记答卷的整份输入标识（各页标识按顺序）-> 序号：页面及配组完全相同才算重复导入。"""
        out: dict[tuple[str, ...], int] = {}
        with self.lock:
            for seq, item in sorted(self.sheets.items()):
                keys = item.get("input_keys")
                if not keys:
                    keys = [f"pdf:{o['document_id']}:{o.get('page')}" for o in item.get("input_pages") or []
                            if o.get("kind") == "pdf" and o.get("document_id")]
                if keys:
                    out.setdefault(tuple(keys), seq)
        return out

    def add_event(self, kind: str, message: str) -> None:
        self.assert_writable()
        with self.lock:
            self.manifest["events"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                            "kind": kind, "message": message, "after_seq": self.next_seq() - 1})
            self.save(force=True)

    # ---------- 结束批次 ----------
    def assert_writable(self) -> None:
        ensure_writable_batch(self.batch_dir)

    def invalidate_finalization(self, reason: str) -> None:
        with self.lock:
            self.manifest["revision"] += 1
            self.manifest["finalized"] = False
            self.manifest["finalization"] = {"status": "dirty", "reason": reason}

    def final_thresholds(self) -> Thresholds:
        if not self.fills:
            return Thresholds()
        return fit_thresholds(np.concatenate(list(self.fills.values())))

    @property
    def threshold_issues(self) -> list[dict]:
        return list(self.manifest.get("threshold_issues") or [])

    def mark_finalized(self, th: Thresholds, *, expected_revision: int | None = None) -> None:
        self.assert_writable()
        with self.lock:
            if expected_revision is not None and self.manifest["revision"] != expected_revision:
                raise ValueError("批次内容已改变，请重新结算")
            if not self.sheets or any(item.get("status") not in ("ok", "voided")
                                      or item.get("finalize_error") for item in self.sheets.values()):
                raise ValueError("仍有未处理答卷或重判错误，不能结束批次")
            self.thresholds = th
            if self.fills:
                self.manifest["threshold_issues"] = threshold_issues(np.concatenate(list(self.fills.values())))
            self.manifest["finalized"] = True
            self.manifest["finalization"] = {
                "status": "complete", "blocked_seqs": [], "failed_seqs": [],
                "revision": self.manifest["revision"],
            }
            self.save(force=True)

    def set_ocr_progress(self, done: int, failed: int, total: int, force: bool = False) -> None:
        """记录手写转写进度（转写线程调用，节流保存）。"""
        self.assert_writable()
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
        self.assert_writable()
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
