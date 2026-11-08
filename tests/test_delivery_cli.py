import json

from scan_answer.cli import main
from test_delivery_export import batch as batch, delivery_seed as delivery_seed


def test_check_and_deliver_commands_report_real_status(batch, tmp_path, capsys):
    assert main(["check", "--batch", str(batch)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ready"]
    target = tmp_path / "cli-delivery"
    assert main(["deliver", "--batch", str(batch), "--out", str(target)]) == 0
    assert str(target) in capsys.readouterr().out
    assert (target / "delivery.json").exists()


def test_check_command_is_nonzero_for_bad_image(batch, capsys):
    result = next(batch.glob("students/*/result.json"))
    data = json.loads(result.read_text())
    (result.parent / data["objective"][0]["image"]).write_bytes(b"bad image")
    assert main(["check", "--batch", str(batch)]) == 1
    assert not json.loads(capsys.readouterr().out)["ready"]


def test_recognize_keeps_output_but_returns_nonzero_for_unresolved_physical(tmp_path, small_dataset, capsys):
    from conftest import TEMPLATE_DIR
    code = main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(small_dataset),
                 "--out", str(tmp_path), "--workers", "0", "--batch-id", "unresolved"])
    assert code == 1
    manifest = json.loads((tmp_path / "unresolved/manifest.json").read_text())
    assert manifest["counts"]["physical"] > 0 and not manifest["finalized"]
    assert str(tmp_path / "unresolved") in capsys.readouterr().out
