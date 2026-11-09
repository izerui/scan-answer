import json
from pathlib import Path

from scan_answer.app.results import load_rows
from scan_answer.pipeline.session import BatchSession
from test_delivery_export import batch as batch, delivery_seed as delivery_seed


def test_result_list_does_not_read_json_outside_its_batch(batch, tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_text('{"subjective":[{"ocr_status":"failed"}]}')
    manifest = json.loads((batch / "manifest.json").read_text())
    manifest["sheets"][0]["result"] = "../outside.json"
    (batch / "manifest.json").write_text(json.dumps(manifest))
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path.resolve() != outside.resolve(), "must not read outside the chosen batch"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    row = load_rows(batch)[0]
    assert row.result is None and row.status == "broken"
    assert row.ocr_failed == 0


def test_recovery_cannot_restore_an_external_result_reference(batch, tmp_path, monkeypatch):
    outside = tmp_path / "outside.json"
    outside.write_text('{"student":{},"warnings":[]}')
    manifest = json.loads((batch / "manifest.json").read_text())
    manifest["sheets"][0].update(result="../outside.json", status="processing")
    (batch / "manifest.json").write_text(json.dumps(manifest))
    state_path = batch / "state/0001.json"
    state = json.loads(state_path.read_text())
    state["result"] = "../outside.json"
    state_path.write_text(json.dumps(state))
    read_text = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert path.resolve() != outside.resolve(), "recovery must remain inside its batch"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    restored = BatchSession.open(batch)
    assert restored.sheets[1]["status"] == "physical"
    assert not restored.sheets[1].get("result")
