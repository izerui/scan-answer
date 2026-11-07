import json

import pytest

from scan_answer.ocr.client import OcrClient, OcrError, _parse
from scan_answer.ocr.config import OcrConfig, load_ocr_config, save_ocr_config

from ocr_mock import MockOcrServer


def cfg(url, **kw):
    return OcrConfig(base_url=url, api_key="sk-test", model="mock-vl", **kw)


def test_parse_json_and_plain():
    assert _parse('```json\n{"text": "你好", "confidence": 0.8}\n```') == ("你好", 0.8)
    assert _parse('{"text": "a", "confidence": 7}') == ("a", None)  # 越界置信度丢弃
    assert _parse("  直接文本 ") == ("直接文本", None)


def test_client_success_retry_and_auth():
    with MockOcrServer("ok") as srv:
        t = OcrClient(cfg(srv.url)).transcribe([b"x", b"y"], "提示")
        assert (t.text, t.confidence, t.model) == ("转写了 2 张图", 0.9, "mock-vl")
        assert srv.requests[0]["auth"] == "Bearer sk-test"
        assert srv.requests[0]["body"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with MockOcrServer("fail_first", fail_times=2) as srv:
        t = OcrClient(cfg(srv.url, max_retries=2), sleep=lambda s: None).transcribe([b"x"], "提示")
        assert t.text == "转写了 1 张图" and srv.calls == 3
    with MockOcrServer("auth") as srv:
        with pytest.raises(OcrError):
            OcrClient(cfg(srv.url, max_retries=3), sleep=lambda s: None).transcribe([b"x"], "提示")
        assert srv.calls == 1  # 401 不重试
    with MockOcrServer("slow", delay=1.0) as srv:
        with pytest.raises(OcrError):
            OcrClient(cfg(srv.url, timeout_s=0.2, max_retries=0)).transcribe([b"x"], "提示")


def test_config_roundtrip_and_validation(tmp_path):
    c = cfg("https://example.com/v1", concurrency=2)
    save_ocr_config(c, tmp_path / "c.json")
    assert load_ocr_config(tmp_path / "c.json") == c
    (tmp_path / "bad.json").write_text(json.dumps({"base_url": "ftp://x", "api_key": "", "model": "m"}))
    with pytest.raises(ValueError):
        load_ocr_config(tmp_path / "bad.json")
