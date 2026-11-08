"""文档输入边界：逐文件配组、页码、纸幅和读取失败。"""
import hashlib

import numpy as np
import pymupdf
import pytest
from PIL import Image


def make_pdf(path, pages=3, size=(72, 36), *, encrypted=False):
    with pymupdf.open() as doc:
        for i in range(pages):
            page = doc.new_page(width=size[0], height=size[1])
            shade = (i + 1) / 10
            page.draw_rect(page.rect, color=None, fill=(shade, shade, shade))
        kwargs = {"encryption": pymupdf.PDF_ENCRYPT_AES_256, "owner_pw": "owner", "user_pw": "secret"} \
            if encrypted else {}
        doc.save(path, **kwargs)
    return path


def test_pdf_pairs_pages_without_crossing_document_boundary(tmp_path):
    from scan_answer.source.documents import DocumentSource
    a = make_pdf(tmp_path / "a.pdf", 3)
    b = make_pdf(tmp_path / "b.pdf", 1)
    sheets = list(DocumentSource([a, b], faces=2).sheets())
    assert [len(s.images) for s in sheets] == [2, 1, 1]
    assert [[o["page"] for o in s.origins] for s in sheets] == [[1, 2], [3], [1]]
    assert [s.origins[0]["file"] for s in sheets] == ["a.pdf", "a.pdf", "b.pdf"]
    assert all(s.dpis == [300.0] * len(s.images) for s in sheets)
    digest = hashlib.sha256(a.read_bytes()).hexdigest()
    assert sheets[0].documents == {digest: a}
    assert sheets[0].origins[0]["document_id"] == digest
    # 所有 PDF 对象已关闭后，返回的像素仍独立有效。
    assert float(sheets[0].images[0].mean()) < float(sheets[0].images[1].mean())
    assert sheets[0].images[0].shape == (150, 300)


@pytest.mark.parametrize("mm, pixels", [
    ((420, 297), (842, 1191)),  # A3 横版
    ((390, 270), (766, 1106)),  # 当前 8K
    ((210, 297), (842, 596)),  # A4
    ((148, 210), (596, 420)),  # A5
    ((100, 80), (227, 284)),   # 自定义
])
def test_pdf_render_preserves_physical_page_size(tmp_path, mm, pixels):
    from scan_answer.source.documents import DocumentSource
    path = make_pdf(tmp_path / "paper.pdf", 1, tuple(v * 72 / 25.4 for v in mm))
    sheet = next(DocumentSource(path, faces=1, dpi=72).sheets())
    assert sheet.images[0].shape == pixels
    assert sheet.dpis == [72.0]


def test_directory_import_includes_images_and_pdf_without_mixing_faces(tmp_path):
    from scan_answer.source.documents import DocumentSource
    for name in ("card_1.jpg", "card_2.jpg"):
        Image.fromarray(np.full((20, 30), 200, np.uint8)).save(tmp_path / name, dpi=(200, 200))
    make_pdf(tmp_path / "written.pdf", 1)
    sheets = list(DocumentSource(tmp_path, faces=2).sheets())
    image_sheet = next(s for s in sheets if s.paths)
    pdf_sheet = next(s for s in sheets if s.images)
    assert image_sheet.names == ["card_1.jpg", "card_2.jpg"]
    assert image_sheet.origins[0] == {"kind": "image", "file": "card_1.jpg", "page": 1}
    assert len(pdf_sheet.images) == 1  # PDF 尾页不能借用文件夹图片补面
    assert pdf_sheet.origins[0]["file"] == "written.pdf"


@pytest.mark.parametrize("encrypted", [False, True])
def test_unreadable_pdf_has_filename_and_clear_error(tmp_path, encrypted):
    from scan_answer.source.documents import DocumentSource
    path = tmp_path / "bad.pdf"
    if encrypted:
        make_pdf(path, 1, encrypted=True)
    else:
        path.write_bytes(b"not a pdf")
    with pytest.raises(ValueError, match="bad.pdf"):
        list(DocumentSource(path, faces=2).sheets())


def test_empty_or_unsupported_input_is_not_an_empty_success(tmp_path):
    from scan_answer.source.documents import DocumentSource
    with pytest.raises(ValueError, match="图片|PDF"):
        list(DocumentSource(tmp_path, faces=2).sheets())
    path = tmp_path / "notes.txt"
    path.write_text("not an answer sheet")
    with pytest.raises(ValueError, match="不支持"):
        list(DocumentSource(path, faces=2).sheets())


def test_parallel_imports_serialize_native_pdf_rendering(tmp_path, monkeypatch):
    """两个入口同时工作时，原生 PDF 调用必须串行，不能依赖 PDFium 自己保护。"""
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    import pypdfium2
    from scan_answer.source.documents import DocumentSource

    guard = threading.Lock()
    active = maximum = 0

    class Bitmap:
        def to_numpy(self):
            return np.full((10, 20), 128, np.uint8)

        def close(self):
            pass

    class Page:
        def get_size(self):
            return 20, 10

        def render(self, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return Bitmap()

        def close(self):
            pass

    class Document:
        def __init__(self, path):
            pass

        def __len__(self):
            return 1

        def __getitem__(self, index):
            return Page()

        def init_forms(self):
            pass

        def close(self):
            pass

    # 原生库不支持并发，用代替实现记录调用重叠，避免让失败测试直接导致进程崩溃。
    monkeypatch.setattr(pypdfium2, "PdfDocument", Document)
    pdf = tmp_path / "parallel.pdf"
    pdf.write_bytes(b"PDF fixture for serialized access")
    with ThreadPoolExecutor(2) as pool:
        jobs = [pool.submit(lambda: list(DocumentSource(pdf, 1).sheets())) for _ in range(2)]
        assert all(job.result()[0].images[0].shape == (10, 20) for job in jobs)
    assert maximum == 1
