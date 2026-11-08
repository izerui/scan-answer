"""Confined, portable references used by delivery readers and exporters."""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


def resolve_path(root: Path, base: Path, reference: str) -> Path:
    """Resolve a relative reference, retaining its name but checking symlink targets.

    Parent components are legal inside the batch (for example ../../raw/a.png).
    Returning the lexical path lets exports materialize internal symlink aliases.
    """
    if not isinstance(reference, str) or not reference.strip() or "\x00" in reference:
        raise ValueError(f"Invalid relative path: {reference!r}")
    windows = PureWindowsPath(reference)
    if Path(reference).is_absolute() or windows.drive or windows.root or ":" in reference:
        raise ValueError(f"Absolute or drive-qualified path is forbidden: {reference!r}")
    root = Path(root).resolve()
    base = Path(base)
    if not base.is_absolute():
        base = root / base
    try:
        if not base.resolve().is_relative_to(root):
            raise ValueError("Reference base is outside the batch")
        candidate = base / reference.replace("\\", "/")
        path = Path(os.path.abspath(candidate))
        real = candidate.resolve()
        if path == root or real == root or not path.is_relative_to(root) or not real.is_relative_to(root):
            raise ValueError(f"Path escapes the batch: {reference!r}")
        if path.resolve() != real:
            raise ValueError(f"Parent traversal through a symlink is ambiguous: {reference!r}")
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"Cannot resolve path: {reference!r}") from exc
    return path
