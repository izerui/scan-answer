from scan_answer.cli import main

from conftest import TEMPLATE_DIR


def test_cli_synth_recognize_eval(tmp_path):
    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "4", "--seed", "5"]) == 0
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                 "--out", str(tmp_path / "out"), "--batch-id", "b"]) == 0
    assert main(["eval", "--truth", str(tmp_path / "ds"), "--batch", str(tmp_path / "out" / "b")]) == 0


def test_cli_recognize_bad_exam(tmp_path):
    # 非法 JSON 字符串
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path),
                 "--out", str(tmp_path / "o"), "--exam", "{bad"]) == 2
    # 合法 JSON 但不是对象
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path),
                 "--out", str(tmp_path / "o"), "--exam", "[1]"]) == 2


def test_cli_recognize_with_workers_and_ocr(tmp_path):
    import json as _json

    from ocr_mock import MockOcrServer

    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "3", "--seed", "6"]) == 0
    with MockOcrServer("ok", fail_marker="栏的图片") as srv:
        cfg = tmp_path / "ocr.json"
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m", "max_retries": 0}))
        assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                     "--out", str(tmp_path / "out"), "--batch-id", "b", "--workers", "2", "--ocr-config", str(cfg)]) == 0
        # 识别时信息栏转写全部失败；不加 --retry-failed 时没有待转写项，返回 0
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg)]) == 0  # 无待转写项
    with MockOcrServer("ok") as srv:
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m"}))
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg), "--retry-failed"]) == 0
    assert main(["eval", "--truth", str(tmp_path / "ds"), "--batch", str(tmp_path / "out" / "b")]) == 0


def test_cli_ocr_failure_exit_code(tmp_path):
    import json as _json

    from ocr_mock import MockOcrServer

    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "1", "--seed", "7"]) == 0
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                 "--out", str(tmp_path / "out"), "--batch-id", "b", "--workers", "0"]) == 0
    with MockOcrServer("error") as srv:
        cfg = tmp_path / "ocr.json"
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m", "max_retries": 0}))
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg), "--enable"]) == 1
