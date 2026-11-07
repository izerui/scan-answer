"""应用设置：保存在用户配置目录的 settings.json（测试时用环境变量 SCAN_ANSWER_HOME 指定目录）。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..ocr.config import OcrConfig
from ..output.local import write_json_atomic

APP_DIR_NAME = "scan-answer"


def config_dir() -> Path:
    env = os.environ.get("SCAN_ANSWER_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_DIR_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_DIR_NAME


def default_output_root() -> Path:
    return Path.home() / "扫描输出"


def bundled_templates_dir() -> Path:
    """随程序发布的模板目录（开发时为仓库根目录下的 templates/）。"""
    return Path(__file__).resolve().parents[3] / "templates"


@dataclass
class ScanDefaults:
    duplex: bool = True
    dpi: int = 300
    color: str = "gray"  # gray / color
    double_feed: bool = True


@dataclass
class AppSettings:
    output_root: str = field(default_factory=lambda: str(default_output_root()))
    template_dirs: list[str] = field(default_factory=lambda: [str(bundled_templates_dir())])
    workers: int = 2
    scan: ScanDefaults = field(default_factory=ScanDefaults)
    ocr: dict | None = None  # OcrConfig 的字段；None 表示未配置转写
    operator: str = ""

    def ocr_config(self) -> OcrConfig | None:
        if not self.ocr:
            return None
        cfg = OcrConfig(**{k: self.ocr[k] for k in OcrConfig.__dataclass_fields__ if k in self.ocr})
        cfg.validate()
        return cfg

    def set_ocr_config(self, cfg: OcrConfig | None) -> None:
        self.ocr = asdict(cfg) if cfg else None


def settings_path() -> Path:
    return config_dir() / "settings.json"


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _clean_ocr(o: object) -> dict | None:
    """校验 ocr 字段：类型不对的字段丢弃；必填字段缺失或类型不对时视为未配置转写。"""
    if not isinstance(o, dict):
        return None
    out: dict = {}
    for k in ("base_url", "api_key", "model"):
        if not isinstance(o.get(k), str):
            return None
        out[k] = o[k]
    if _is_int(o.get("concurrency")) and o["concurrency"] >= 1:
        out["concurrency"] = o["concurrency"]
    if (_is_int(o.get("timeout_s")) or isinstance(o.get("timeout_s"), float)) and o["timeout_s"] > 0:
        out["timeout_s"] = float(o["timeout_s"])
    if _is_int(o.get("max_retries")) and o["max_retries"] >= 0:
        out["max_retries"] = o["max_retries"]
    return out


def load_settings() -> AppSettings:
    p = settings_path()
    if not p.exists():
        return AppSettings()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()  # 配置损坏时用默认值，不阻止启动
    if not isinstance(d, dict):
        return AppSettings()  # 顶层不是对象（如 []）：同样回退默认值
    s = AppSettings()
    # 逐字段校验类型，错误类型保留默认值
    if isinstance(d.get("output_root"), str):
        s.output_root = d["output_root"]
    if isinstance(d.get("template_dirs"), list):
        cleaned = [x for x in d["template_dirs"] if isinstance(x, str)]
        if cleaned:
            s.template_dirs = cleaned
    v = d.get("workers")
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 64:
        s.workers = v
    s.ocr = _clean_ocr(d.get("ocr"))
    if isinstance(d.get("operator"), str):
        s.operator = d["operator"]
    if isinstance(d.get("scan"), dict):
        sd = d["scan"]
        kw = {}
        if isinstance(sd.get("duplex"), bool):
            kw["duplex"] = sd["duplex"]
        if isinstance(sd.get("double_feed"), bool):
            kw["double_feed"] = sd["double_feed"]
        if isinstance(sd.get("dpi"), int) and not isinstance(sd.get("dpi"), bool) and sd["dpi"] in (200, 300, 400, 600):
            kw["dpi"] = sd["dpi"]
        if isinstance(sd.get("color"), str) and sd["color"] in ("gray", "color"):
            kw["color"] = sd["color"]
        s.scan = ScanDefaults(**kw)
    return s


def save_settings(s: AppSettings) -> None:
    write_json_atomic(settings_path(), asdict(s))
