import json

import pytest

from scan_answer.ocr.client import OcrClient, OcrError, _parse
from scan_answer.ocr.config import OcrConfig, load_ocr_config, save_ocr_config

from ocr_mock import MockOcrServer


def cfg(url, **kw):
    return OcrConfig(base_url=url, api_key="sk-test", model="mock-vl", **kw)


def test_parse_confidence_line_and_plain():
    assert _parse("你好\n置信度: 0.8") == ("你好", 0.8)
    assert _parse("  第一行\n\n第二行 \n置信度：0.75\n\n") == ("第一行\n\n第二行", 0.75)  # 全角冒号
    assert _parse("a\n置信度: 1.5") == ("a", None)  # 越界置信度丢弃
    assert _parse("  直接文本 ") == ("直接文本", None)
    assert _parse("置信度很高的回答") == ("置信度很高的回答", None)


def test_parse_keeps_latex_backslashes():
    latex = "$\\frac{1}{2} \\times x \\sqrt{2} \\neq y$"
    assert _parse(latex + "\n置信度: 0.8") == (latex, 0.8)
    assert "\\frac" in _parse(latex + "\n置信度: 0.8")[0]


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


def test_transient_network_error_is_retried():
    c = OcrClient(cfg("http://127.0.0.1:1/v1", max_retries=2), sleep=lambda s: None)
    calls = []

    def post(payload):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionResetError("连接被重置")
        return {"model": "m", "choices": [{"message": {"content": "好\n置信度: 0.5"}}]}

    c._post = post
    assert (c.transcribe([b"x"], "提示").text, len(calls)) == ("好", 2)

    import http.client
    calls.clear()

    def post2(payload):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("断开")
        return {"model": "m", "choices": [{"message": {"content": "好"}}]}

    c._post = post2
    assert c.transcribe([b"x"], "提示").text == "好" and len(calls) == 2
