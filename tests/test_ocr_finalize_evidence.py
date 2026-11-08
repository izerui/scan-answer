from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json, update_json
from scan_answer.pipeline.worker import FinalizeJob, finalize_sheet, process_sheet
from conftest import TEMPLATE_DIR
from test_worker import _job, _raw


def test_redecision_preserves_ocr_generation_and_does_not_rewrite_ocr_images(ctx, tmp_path):
    raw, _ = _raw(ctx, tmp_path, 103)
    outcome = process_sheet(_job(tmp_path, 1, raw, Thresholds(0.97, 0.5), ocr=True))
    path = tmp_path / outcome.result

    def claimed(result):
        result["ocr_result_id"] = "same-result"
        for entry in result["subjective"] + list(result["student"]["fields"].values()):
            entry.update(ocr_generation="active-request", ocr_fingerprint="same-evidence")

    update_json(path, claimed)
    before = read_json(path)
    image_names = [p for q in before["subjective"] for p in q["images"]]
    image_names += [f["image"] for f in before["student"]["fields"].values()]
    stamps = {p: (path.parent / p).stat().st_mtime_ns for p in image_names}
    result = finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(tmp_path), str(TEMPLATE_DIR)))
    assert result.changed
    after = read_json(path)
    assert after["ocr_result_id"] == "same-result"
    assert after["content_revision"] == before["content_revision"]
    for entry in after["subjective"] + list(after["student"]["fields"].values()):
        assert entry["ocr_generation"] == "active-request"
        assert entry["ocr_fingerprint"] == "same-evidence"
    assert {p: (path.parent / p).stat().st_mtime_ns for p in image_names} == stamps
