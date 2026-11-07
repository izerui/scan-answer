"""手写转写配置（兼容 OpenAI 格式的接口）。界面的"设置"页（计划 3）读写同一个 JSON 文件。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class OcrConfig:
    base_url: str  # 如 https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: str
    model: str  # 如 qwen-vl-max
    concurrency: int = 4
    timeout_s: float = 60.0
    max_retries: int = 2

    def validate(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not self.model:
            raise ValueError("model 不能为空")
        if self.concurrency < 1:
            raise ValueError("concurrency 至少为 1")


def load_ocr_config(path: str | Path) -> OcrConfig:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {k: d[k] for k in OcrConfig.__dataclass_fields__ if k in d}
    cfg = OcrConfig(**known)
    cfg.validate()
    return cfg


def save_ocr_config(cfg: OcrConfig, path: str | Path) -> None:
    Path(path).write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=1), encoding="utf-8")
