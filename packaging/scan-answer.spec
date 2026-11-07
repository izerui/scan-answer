# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 答题卡扫描识别."""

import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_dynamic_libs,
    collect_submodules,
)

block_cipher = None

# ── Paths ──────────────────────────────────────────────────────────
PROJECT_ROOT = Path(SPECPATH).parent  # noqa: F821  (SPECPATH injected by PyInstaller)
ENTRY = str(PROJECT_ROOT / "packaging" / "entry.py")
ICON_PATH = PROJECT_ROOT / "packaging" / "icon.ico"
TEMPLATES_SRC = str(PROJECT_ROOT / "templates")

# ── Data & binaries ───────────────────────────────────────────────
datas = [(TEMPLATES_SRC, "templates")]
binaries = []
hiddenimports = ["scan_answer.pipeline.worker"]

# collect all scan_answer submodules so spawn workers can import them
hiddenimports += collect_submodules("scan_answer")

# pypdfium2 – ships platform-specific .so/.dll/.dylib
_pp_datas, _pp_bins, _pp_hi = collect_all("pypdfium2")
datas += _pp_datas
binaries += _pp_bins
hiddenimports += _pp_hi

# zxingcpp – native extension
_zx_datas, _zx_bins, _zx_hi = collect_all("zxingcpp")
datas += _zx_datas
binaries += _zx_bins
hiddenimports += _zx_hi

# OpenCV dynamic libs
binaries += collect_dynamic_libs("cv2")

# Windows: bundle pytwain if available
if sys.platform == "win32":
    try:
        import twain  # noqa: F401

        _tw_datas, _tw_bins, _tw_hi = collect_all("twain")
        datas += _tw_datas
        binaries += _tw_bins
        hiddenimports += _tw_hi
    except ImportError:
        pass

# ── Icon (only if the file exists) ────────────────────────────────
icon = str(ICON_PATH) if ICON_PATH.exists() else None

# ── Analysis ──────────────────────────────────────────────────────
a = Analysis(
    [ENTRY],
    pathex=[str(PROJECT_ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="答题卡扫描识别",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="答题卡扫描识别",
)

# ── macOS: create .app bundle ─────────────────────────────────────
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="答题卡扫描识别.app",
        icon=icon,
        bundle_identifier="com.scananswer.app",
    )
