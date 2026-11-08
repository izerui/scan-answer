"""Confined worker paths, including raw references from relocated legacy state."""
from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath


def resolve_local_path(batch_dir: str | Path, reference: str, *, base: Path | None = None) -> Path:
    """Resolve a relative reference within this batch, checking symlink targets."""
    if not isinstance(reference, str) or not reference or "\0" in reference:
        raise ValueError("Invalid batch path")
    windows = PureWindowsPath(reference)
    relative = PurePosixPath(reference.replace("\\", "/"))
    if relative.is_absolute() or windows.drive or windows.root:
        raise ValueError(f"Expected a relative batch path: {reference}")
    root = Path(batch_dir).resolve()
    parent = root if base is None else base.resolve()
    if not parent.is_relative_to(root):
        raise ValueError(f"Path base escapes batch: {base}")
    target = (parent / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"Path escapes batch: {reference}")
    return target


def resolve_raw_path(batch_dir: str | Path, reference: str, *, seq: int | None = None) -> Path:
    """Legacy absolute paths always map to current raw/<name>, never their origin."""
    if not isinstance(reference, str) or not reference or "\0" in reference:
        raise ValueError("Invalid raw path")
    windows = PureWindowsPath(reference)
    portable = PurePosixPath(reference.replace("\\", "/"))
    if ".." in portable.parts:
        raise ValueError(f"Raw path contains parent traversal: {reference}")
    if windows.drive and not windows.is_absolute():
        raise ValueError(f"Drive-relative raw path is not portable: {reference}")
    if portable.is_absolute() or windows.is_absolute():
        if not portable.name or ":" in portable.name:
            raise ValueError(f"Invalid raw filename: {reference}")
        reference = f"raw/{portable.name}"
    elif windows.root:
        raise ValueError(f"Invalid rooted raw path: {reference}")
    target = resolve_local_path(batch_dir, reference)
    raw = Path(batch_dir).resolve() / "raw"
    if target == raw or not target.is_relative_to(raw):
        raise ValueError(f"Path escapes batch raw directory: {reference}")
    if seq is not None:
        local = PurePosixPath(reference.replace("\\", "/"))
        prefix = f"{seq:04d}_"
        if (len(local.parts) != 2 or local.parts[0] != "raw" or not local.name.startswith(prefix)
                or target.parent != raw or not target.name.startswith(prefix)):
            raise ValueError(f"Raw path does not belong to sheet {seq}: {reference}")
    return target


def resolve_result_path(batch_dir: str | Path, reference: str, seq: int) -> Path:
    """Only the direct, unaliased students/<seq>[_suffix]/result.json may be written."""
    target = resolve_local_path(batch_dir, reference)
    relative = PurePosixPath(reference.replace("\\", "/"))
    parts = relative.parts
    prefix = f"{seq:04d}"
    if (len(parts) != 3 or parts[0] != "students" or parts[2] != "result.json"
            or not (parts[1] == prefix or parts[1].startswith(prefix + "_"))
            or target != Path(batch_dir).resolve() / relative):
        raise ValueError(f"Result path does not belong to sheet {seq}: {reference}")
    return target
