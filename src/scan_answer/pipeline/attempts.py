"""Isolated rescan attempts with a write-ahead publication journal."""
from __future__ import annotations

import copy
import hashlib
import os
import shutil
from dataclasses import asdict
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from ..output.jsonfile import read_json
from ..output.local import write_json_atomic
from .worker import SheetOutcome, state_path


def _sync(path: Path) -> None:
    if path.is_dir():
        if os.name == "nt":
            return
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    else:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())


def _journal(path: Path, data: dict) -> None:
    write_json_atomic(path, data)
    _sync(path)
    _sync(path.parent)


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
        for path in target.rglob("*"):
            _sync(path)
        _sync(target)
    else:
        shutil.copy2(source, target)
        _sync(target)
    _sync(target.parent)


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _outputs(root: Path, seq: int) -> list[str]:
    paths = []
    for name in ("raw", "students", "errors"):
        folder = root / name
        if folder.exists():
            paths.extend(p.relative_to(root).as_posix() for p in folder.iterdir()
                         if p.name == f"{seq:04d}" or p.name.startswith(f"{seq:04d}_"))
    state = state_path(root, seq)
    if state.exists():
        paths.append(state.relative_to(root).as_posix())
    return sorted(paths)


def _rollback(root: Path, folder: Path, journal: dict) -> None:
    """Consumed backups mean already restored, making interrupted rollback repeatable."""
    previous = folder / "previous"
    for rel in set(journal["new_paths"]) - set(journal["old_paths"]):
        _remove(root / rel)
    for rel in journal["old_paths"] + ["manifest.json"]:
        backup = previous / rel
        if backup.exists():
            target = root / rel
            if target.is_file() and backup.is_file():
                with target.open("rb") as current, backup.open("rb") as saved:
                    if hashlib.file_digest(current, "sha256").digest() == hashlib.file_digest(saved, "sha256").digest():
                        continue
            # A directory may exist after an interrupted rmtree with only some
            # files left. Restore its complete backup, not just its missing name.
            if target.is_dir():
                _remove(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(backup, target)
            _sync(target.parent)
    journal["phase"] = "rolled_back"
    _journal(folder / "journal.json", journal)


def _validate_journal(root: Path, path: Path, journal: dict) -> None:
    if (not isinstance(journal, dict) or type(journal.get("seq")) is not int
            or journal["seq"] < 0 or path.parent.parent.name != f"{journal['seq']:04d}"
            or journal.get("phase") not in {"staging", "prepared", "committed", "rejected", "rolled_back"}):
        raise ValueError("无效的重扫事务日志")
    if journal["phase"] != "prepared":
        return
    for key in ("old_paths", "new_paths"):
        paths = journal.get(key)
        if not isinstance(paths, list):
            raise ValueError("重扫日志缺少文件清单")
        normalized = []
        for relative in paths:
            if not isinstance(relative, str) or "\x00" in relative or ":" in relative:
                raise ValueError("重扫日志路径无效")
            # Older Windows journals used native separators; validate before rollback.
            windows = PureWindowsPath(relative)
            p = Path(relative.replace("\\", "/"))
            if (windows.drive or windows.root or p.is_absolute() or ".." in p.parts or len(p.parts) < 2
                    or p.parts[0] not in {"raw", "students", "errors", "state", "sources"}
                    or not (root / p).resolve().is_relative_to(root.resolve())):
                raise ValueError("重扫日志路径越界")
            prefix = f"{journal['seq']:04d}"
            if p.parts[0] != "sources" and not (
                    p.parts[1] == prefix or p.parts[1] == f"{prefix}.json"
                    or p.parts[1].startswith(prefix + "_")):
                raise ValueError("重扫日志引用了其他答卷")
            normalized.append(p.as_posix())
        journal[key] = normalized


def recover_attempts(batch_dir: Path) -> list[dict]:
    """Called before reading the active manifest; only a committed journal wins."""
    for path in sorted((batch_dir / "attempts").glob("*/*/journal.json")):
        try:
            journal = read_json(path)
            _validate_journal(batch_dir, path, journal)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            os.replace(path, path.with_name(f"journal.corrupt-{uuid4().hex}.json"))
            _sync(path.parent)
            continue
        if journal.get("phase") == "prepared":
            _rollback(batch_dir, path.parent, journal)
        elif journal.get("phase") == "staging":
            journal.update(phase="rejected", message="Rescan interrupted before publication")
            _journal(path, journal)
    issues = []
    for path in sorted((batch_dir / "attempts").glob("*/*/journal.corrupt*.json")):
        folder = path.parent
        seq = int(folder.parent.name) if folder.parent.name.isdigit() else None
        issues.append({"seq": seq, "attempt_id": folder.name,
                       "path": str(path.relative_to(batch_dir)),
                       "message": "重扫历史日志损坏，文件已保留，请核对对应答卷"})
    return issues


def _new_stage(parent, folder: Path):
    from .session import BatchSession

    manifest = copy.deepcopy(parent.manifest)
    manifest.update(sheets=[], counts={}, events=[], ocr_progress=None)
    stage = BatchSession(folder / "stage", parent.template_dir, manifest)
    stage.save(force=True)
    shutil.copy2(parent.batch_dir / "template.json", stage.batch_dir / "template.json")
    return stage


def retain_spooled(session, seq: int, sheet):
    """Audit a recovered rescan even when its original target no longer exists."""
    folder = session.batch_dir / "attempts" / f"{seq:04d}" / uuid4().hex
    journal = {"seq": seq, "phase": "staging", "attempt_id": folder.name,
               "message": "Recovered rescan acquisition"}
    _journal(folder / "journal.json", journal)
    stage = _new_stage(session, folder)
    stage.register(seq, sheet)
    stage.save_raw(seq, sheet)
    outcome = SheetOutcome(seq, False, physical="ALIGN_FAILED",
                           message="Recovered unfinished rescan; active sheet preserved")
    stage.record(outcome)
    stage.save(force=True)
    journal.update(phase="rejected", outcome=asdict(outcome))
    _journal(folder / "journal.json", journal)
    return stage


class RescanAttempt:
    """Process using ``session``, then explicitly commit or reject the outcome.

    The parent must not register or collect a failed outcome into its active
    session. Both terminal methods return the supplied SheetOutcome.
    """

    def __init__(self, session, seq: int):
        session.prepare_rescan(seq)
        self.parent = session
        self.seq = seq
        self.folder = session.batch_dir / "attempts" / f"{seq:04d}" / uuid4().hex
        self.folder.mkdir(parents=True)
        self._journal = {"seq": seq, "phase": "staging", "attempt_id": self.folder.name}
        _journal(self.folder / "journal.json", self._journal)
        self.session = _new_stage(session, self.folder)
        self._original = copy.deepcopy(session.sheets[seq])
        self._memory = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._journal["phase"] == "prepared":
            with self.parent.lock:
                if self._memory is not None:
                    (self.parent.manifest, self.parent.sheets, self.parent.fills, self.parent.thresholds,
                     self.parent._last_fit_count, self.parent._last_save) = self._memory
                _rollback(self.parent.batch_dir, self.folder, self._journal)
        elif self._journal["phase"] == "staging":
            self.reject(SheetOutcome(self.seq, False, physical="ALIGN_FAILED",
                                     message=str(exc) if exc else "Rescan ended without commit"))
        return False

    def reject(self, outcome: SheetOutcome) -> SheetOutcome:
        if outcome.seq != self.seq or outcome.ok:
            raise ValueError("Rejected outcome must be a failure for this sequence")
        if self._journal["phase"] != "staging":
            raise RuntimeError("Rescan attempt already completed")
        self._journal.update(phase="rejected", outcome=asdict(outcome), message=outcome.message)
        _journal(self.folder / "journal.json", self._journal)
        if self.seq in self.session.sheets:
            self.session.record(outcome)
            self.session.save(force=True)
        return outcome

    def commit(self, outcome: SheetOutcome) -> SheetOutcome:
        if outcome.seq != self.seq or not outcome.ok or not outcome.result:
            raise ValueError("Committed outcome must be a success for this sequence")
        if self._journal["phase"] != "staging":
            raise RuntimeError("Rescan attempt already completed")
        parent, stage = self.parent, self.session
        with parent.lock:
            parent.prepare_rescan(self.seq)
            if parent.sheets[self.seq] != self._original:
                raise RuntimeError("Active sheet changed during rescan")
            item = stage.sheets[self.seq]
            item["attempt_id"] = self.folder.name
            for origin in item["input_pages"]:
                if not (stage.batch_dir / origin["raw"]).is_file():
                    raise FileNotFoundError(origin["raw"])
            state = read_json(state_path(stage.batch_dir, self.seq))
            state["raw_paths"] = [
                f"raw/{Path(path).name}"
                for path in state["raw_paths"]
            ]
            write_json_atomic(state_path(stage.batch_dir, self.seq), state)
            result = read_json(stage.batch_dir / outcome.result)
            result.setdefault("content_revision", uuid4().hex)
            write_json_atomic(stage.batch_dir / outcome.result, result)
            stage.record(outcome)
            stage.save(force=True)
            old_paths = _outputs(parent.batch_dir, self.seq)
            new_paths = _outputs(stage.batch_dir, self.seq)
            # Document archives are immutable and shared across sequences.
            new_paths += [p.relative_to(stage.batch_dir).as_posix()
                          for p in (stage.batch_dir / "sources").glob("*")
                          if not (parent.batch_dir / p.relative_to(stage.batch_dir)).exists()]
            for rel in old_paths + ["manifest.json"]:
                _copy(parent.batch_dir / rel, self.folder / "previous" / rel)
            for rel in new_paths:
                _copy(stage.batch_dir / rel, self.folder / "publish" / rel)
            self._journal.update(phase="prepared", old_paths=old_paths, new_paths=new_paths,
                                 outcome=asdict(outcome))
            _journal(self.folder / "journal.json", self._journal)
            memory = (copy.deepcopy(parent.manifest), copy.deepcopy(parent.sheets),
                      dict(parent.fills), parent.thresholds, parent._last_fit_count, parent._last_save)
            self._memory = memory
            try:
                for rel in new_paths:
                    target = parent.batch_dir / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.is_dir():
                        _remove(target)
                    os.replace(self.folder / "publish" / rel, target)
                    _sync(target.parent)
                for rel in set(old_paths) - set(new_paths):
                    _remove(parent.batch_dir / rel)
                    _sync((parent.batch_dir / rel).parent)
                parent.sheets[self.seq] = copy.deepcopy(stage.sheets[self.seq])
                parent.record(outcome)
                parent.save(force=True)
                _sync(parent.batch_dir / "manifest.json")
                _sync(parent.batch_dir)
                self._journal["phase"] = "committed"
                _journal(self.folder / "journal.json", self._journal)
            except Exception:
                (parent.manifest, parent.sheets, parent.fills, parent.thresholds,
                 parent._last_fit_count, parent._last_save) = memory
                # Keep the prepared journal when rollback itself fails; open() retries it.
                self._journal["phase"] = "prepared"
                _rollback(parent.batch_dir, self.folder, self._journal)
                raise
        return outcome
