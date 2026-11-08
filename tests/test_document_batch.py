"""复用既有 8K 模拟样卡验证填写 PDF 到逐题 JSON/图片的链路。"""
import hashlib
import shutil
import pymupdf
import pytest
from PIL import Image

from scan_answer.batch import run_batch
from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.session import BatchSession

from conftest import TEMPLATE_DIR


def scans_to_pdf(path, images, dpi=200):
    with pymupdf.open() as doc:
        for image in images:
            with Image.open(image) as im:
                width, height = im.size
            page = doc.new_page(width=width / dpi * 72, height=height / dpi * 72)
            page.insert_image(page.rect, filename=str(image))
        doc.save(path)
    return path


def test_pdf_batch_keeps_pages_original_document_and_all_question_images(small_dataset, tmp_path):
    images = sorted(small_dataset.glob("*.jpg"))
    pdf = scans_to_pdf(tmp_path / "answers.pdf", images[:4] + images[:1])
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    batch = run_batch(TEMPLATE_DIR, pdf, tmp_path / "out", batch_id="pdf")
    manifest = read_json(batch / "manifest.json")
    assert manifest["counts"]["total"] == 3
    assert manifest["counts"]["ok"] == 2
    assert manifest["sheets"][2]["physical"] == "MISSING_FACE"
    assert [p["page"] for p in manifest["sheets"][1]["input_pages"]] == [3, 4]
    assert (batch / "sources" / f"{digest}.pdf").read_bytes() == pdf.read_bytes()

    moved = tmp_path / "moved"
    shutil.copytree(batch, moved)
    pdf.unlink()
    for item in manifest["sheets"][:2]:
        rp = moved / item["result"]
        result = read_json(rp)
        assert result["sheet_id"] == f"pdf:{item['seq']:04d}"
        assert len(result["objective"]) == 55
        questions = result["objective"] + result["subjective"]
        assert len({q["question_id"] for q in questions}) == len(questions)
        for q in questions:
            assert q["images"]
            assert all((rp.parent / rel).is_file() for rel in q["images"])
        for page in result["input_pages"]:
            assert page["kind"] == "pdf"
            assert page["file"] == "answers.pdf"
            assert (rp.parent / page["document"]).is_file()
            assert (rp.parent / page["raw"]).is_file()
    assert read_json(moved / manifest["sheets"][1]["result"])["input_pages"][0]["page"] == 3
    expected_origins = manifest["sheets"][0]["input_pages"]
    manifest["sheets"] = manifest["sheets"][1:]  # 模拟 manifest 落后于已写出的结果
    write_json_atomic(moved / "manifest.json", manifest)
    recovered = BatchSession.open(moved)
    assert recovered.sheets[1]["input_pages"] == expected_origins


@pytest.mark.parametrize("workers", [0, 2])
def test_bad_pdf_does_not_finalize_partial_import(small_dataset, tmp_path, workers):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    scans_to_pdf(inputs / "a.pdf", sorted(small_dataset.glob("*.jpg"))[:2])
    (inputs / "z.pdf").write_bytes(b"broken")
    with pytest.raises(ValueError, match="z.pdf"):
        run_batch(TEMPLATE_DIR, inputs, tmp_path / "out", batch_id="partial", workers=workers)
    manifest = read_json(tmp_path / "out" / "partial" / "manifest.json")
    assert not manifest["finalized"]
    assert manifest["counts"]["ok"] == 1  # 后续文件失败不能使已经提交的好卡停留在 processing
