"""跨进程安全的 JSON 读-改-写。

结束批次时的重新判定（在识别进程里）和手写转写（在主进程线程里）都会改写同一个 result.json，
用一个 `<文件>.lock` 锁文件（O_CREAT | O_EXCL 原子创建）串行化，Windows / macOS 通用。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .local import write_json_atomic

STALE_SECONDS = 60.0  # 锁文件超过该时长视为持有者已崩溃


@contextmanager
def file_lock(path: str | Path, timeout: float = 30.0, poll: float = 0.02) -> Iterator[None]:
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待文件锁超时: {lock}")
            time.sleep(poll)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def update_json(path: str | Path, fn: Callable[[dict], dict | None]) -> dict:
    """加锁读取 JSON，交给 fn 修改（可原地改并返回 None，或返回新 dict），再原子写回。"""
    path = Path(path)
    with file_lock(path):
        data = read_json(path)
        new = fn(data)
        if new is not None:
            data = new
        write_json_atomic(path, data)
    return data
