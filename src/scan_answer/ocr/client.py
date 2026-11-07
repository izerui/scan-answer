"""兼容 OpenAI Chat Completions 格式的视觉模型客户端（只用标准库，不增加依赖）。"""
from __future__ import annotations

import base64
import http.client
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


_CONF = re.compile(r"^置信度\s*[:：]\s*([01](?:\.\d+)?)\s*$")


def _parse(content: str) -> tuple[str, float | None]:
    """最后一个非空行是"置信度: x"时取出置信度，其余为转写内容；否则全文为转写内容。

    不做任何转义解析，LaTeX 中的反斜杠原样保留。"""
    lines = content.rstrip().split("\n")
    m = _CONF.match(lines[-1].strip()) if lines else None
    if not m:
        return content.strip(), None
    conf = float(m.group(1))
    return "\n".join(lines[:-1]).strip(), conf if 0 <= conf <= 1 else None


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
            except (OSError, http.client.HTTPException, KeyError, IndexError, ValueError) as e:
                # 网络瞬时错误（连接被重置、超时、对方断开等，URLError 也是 OSError）与回复格式错误都重试
                last = e
            if attempt < self.cfg.max_retries:
                self._sleep(0.5 * (2 ** attempt))
        raise OcrError(f"转写失败: {last}")

    def ping(self) -> str:
        """"测试连接"：发一条纯文本请求，返回模型名。"""
        data = self._post({"model": self.cfg.model, "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]})
        return data.get("model") or self.cfg.model
