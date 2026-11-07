"""兼容 OpenAI Chat Completions 格式的视觉模型客户端（只用标准库，不增加依赖）。"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import OcrConfig
from .prompts import SYSTEM


class OcrError(Exception):
    pass


@dataclass
class Transcription:
    text: str
    confidence: float | None
    model: str


def _parse(content: str) -> tuple[str, float | None]:
    """优先解析 {"text": ..., "confidence": ...}；模型没按格式输出时把全文当作转写结果。"""
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and isinstance(d.get("text"), str):
                c = d.get("confidence")
                conf = float(c) if isinstance(c, (int, float)) and 0 <= float(c) <= 1 else None
                return d["text"], conf
        except json.JSONDecodeError:
            pass
    return content.strip(), None


class OcrClient:
    def __init__(self, cfg: OcrConfig, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep

    def _post(self, payload: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.cfg.api_key}"})
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def transcribe(self, jpegs: list[bytes], instruction: str) -> Transcription:
        content = [{"type": "text", "text": instruction}]
        content += [{"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")}}
                    for b in jpegs]
        payload = {"model": self.cfg.model, "temperature": 0,
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]}
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                data = self._post(payload)
                text, conf = _parse(data["choices"][0]["message"]["content"])
                return Transcription(text, conf, data.get("model") or self.cfg.model)
            except urllib.error.HTTPError as e:
                last = e
                # 4xx（限流 429 除外）是请求或鉴权问题，重试无意义
                if 400 <= e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
                last = e
            if attempt < self.cfg.max_retries:
                self._sleep(0.5 * (2 ** attempt))
        raise OcrError(f"转写失败: {last}")

    def ping(self) -> str:
        """"测试连接"：发一条纯文本请求，返回模型名。"""
        data = self._post({"model": self.cfg.model, "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]})
        return data.get("model") or self.cfg.model
