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
    """随程序发布的模板目录：打包后在 PyInstaller 的资源目录下，开发时为仓库根目录下的 templates/。"""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "templates"
    return Path(__file__).resolve().parents[3] / "templates"


def is_bundled_templates_dir(path: str | Path) -> bool:
    """是否为某个版本随程序提供的模板目录：当前运行的、开发仓库的、macOS .app 内的或 Windows 安装目录 _internal 下的。

    旧版本会把这类绝对路径写进 settings.json；开发版与安装版共用配置目录，读到对方的路径会让模板重复出现。"""
    p = Path(path)
    if p.name != "templates":
        return False
    try:
        if p.resolve() == bundled_templates_dir().resolve():
            return True
    except OSError:
        pass
    parent = p.parent
    return ((parent / "src" / "scan_answer").is_dir()
            or parent.name == "_internal"
            or (parent.parent.name == "Contents" and parent.parent.parent.suffix == ".app"))


@dataclass
class ScanDefaults:
    duplex: bool = True
    dpi: int = 300
    color: str = "gray"  # gray / color
    double_feed: bool = True
    scanner: str = ""  # 上次使用的扫描仪（TWAIN 数据源名称）
    dropout: str = "none"  # 颜色滤除：none / red / green / blue
    crop: str = "auto"  # 裁边：auto（扫描仪自动检测纸边）/ template（按模板纸张尺寸）/ driver（驱动默认）


@dataclass
class AppSettings:
    output_root: str = field(default_factory=lambda: str(default_output_root()))
    # 用户添加的模板目录；随程序提供的目录和用户模板目录总会扫描，不记在这里（见 library.list_templates）
    template_dirs: list[str] = field(default_factory=list)
    workers: int = 2
    scan: ScanDefaults = field(default_factory=ScanDefaults)
    ocr: dict | None = None  # OcrConfig 的字段；None 表示未启用转写（读取方只看这个字段）
    ocr_saved: dict | None = None  # 关闭转写时保留的连接配置，重新启用时回填表单；不参与转写
    operator: str = ""
    exam_dir_pattern: str = "{name}_{subject}_{time}"  # 考试目录命名规则（见 output/naming.py）
    student_dir_pattern: str = "{exam_no}"  # 学生目录名序号之后的部分

    def ocr_config(self) -> OcrConfig | None:
        if not self.ocr:
            return None
        cfg = OcrConfig(**{k: self.ocr[k] for k in OcrConfig.__dataclass_fields__ if k in self.ocr})
        cfg.validate()
        return cfg

    def set_ocr_config(self, cfg: OcrConfig | None, keep: OcrConfig | dict | None = None) -> None:
        """启用时传 cfg；关闭时 cfg 为 None，keep 为要保留的连接配置（base_url、api_key、模型等）。"""
        self.ocr = asdict(cfg) if cfg else None
        if cfg:
            self.ocr_saved = None
        elif keep is not None:
            self.ocr_saved = asdict(keep) if isinstance(keep, OcrConfig) else dict(keep)

    def ocr_form_values(self) -> dict:
        """设置页表单显示的转写配置：已启用的配置，否则为关闭时保留的配置。"""
        return dict(self.ocr or self.ocr_saved or {})


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


def _clean_saved_ocr(o: object) -> dict | None:
    """关闭转写时保留的配置：字段可以为空（用户只填了一部分），类型不对的字段丢弃。"""
    if not isinstance(o, dict):
        return None
    out = {k: o[k] for k in ("base_url", "api_key", "model") if isinstance(o.get(k), str)}
    if _is_int(o.get("concurrency")) and o["concurrency"] >= 1:
        out["concurrency"] = o["concurrency"]
    return out or None


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
        s.template_dirs = [x for x in d["template_dirs"] if isinstance(x, str) and not is_bundled_templates_dir(x)]
    v = d.get("workers")
    if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 64:
        s.workers = v
    s.ocr = _clean_ocr(d.get("ocr"))
    s.ocr_saved = None if s.ocr else _clean_saved_ocr(d.get("ocr_saved"))  # 旧版 settings.json 没有该字段
    if isinstance(d.get("operator"), str):
        s.operator = d["operator"]
    for key in ("exam_dir_pattern", "student_dir_pattern"):
        if isinstance(d.get(key), str):
            setattr(s, key, d[key])
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
        if isinstance(sd.get("scanner"), str):
            kw["scanner"] = sd["scanner"]
        if sd.get("dropout") in ("none", "red", "green", "blue"):
            kw["dropout"] = sd["dropout"]
        if sd.get("crop") in ("auto", "template", "driver"):
            kw["crop"] = sd["crop"]
        s.scan = ScanDefaults(**kw)
    return s


def save_settings(s: AppSettings) -> None:
    write_json_atomic(settings_path(), asdict(s))
