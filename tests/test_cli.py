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
