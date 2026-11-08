"""Atomic directory publication that cannot replace an existing destination."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import sys


def publish_directory(source: Path, target: Path) -> None:
    if sys.platform == "win32":
        os.rename(source, target)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = getattr(libc, "renamex_np", None)
        if function is not None:
            function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            # Darwin sys/stdio.h: RENAME_EXCL.
            result = function(os.fsencode(source), os.fsencode(target), 0x00000004)
    elif sys.platform.startswith("linux"):
        function = getattr(libc, "renameat2", None)
        if function is not None:
            function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            function.restype = ctypes.c_int
            # Linux AT_FDCWD and RENAME_NOREPLACE.
            result = function(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    else:
        function = None
    if function is None:
        raise OSError(errno.ENOTSUP, "当前系统不支持无覆盖发布交付目录", str(target))
    if result != 0:
        error = ctypes.get_errno()
        message = ("此文件系统不支持无覆盖发布，请选择受支持的本地目录"
                   if error in (errno.ENOTSUP, errno.ENOSYS, errno.EINVAL) else os.strerror(error))
        raise OSError(error, message, str(target))
