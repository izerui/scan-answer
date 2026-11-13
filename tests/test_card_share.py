"""卡面图片：后台解码不卡界面、连续加载只显示最后一次；同一文件已显示时直接共用，文件变化或缺失时照常加载。"""
import os
import threading
from pathlib import Path

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage, QPixmap

from scan_answer.ui.widgets import card_view
from scan_answer.ui.widgets.card_view import CardView

from conftest import TEMPLATE_DIR, settle


def _png(path: Path, w: int, h: int, value: int = 128) -> Path:
    Image.fromarray(np.full((h, w), value, dtype=np.uint8)).save(path)
    return path


def _count_decodes(monkeypatch) -> list[tuple[str, bool]]:
    """记录 CardView 从文件解码图片：(文件名, 是否在界面线程)。"""
    decoded = []

    def record(args):
        if args and isinstance(args[0], str):
            decoded.append((Path(args[0]).name, threading.current_thread() is threading.main_thread()))

    class CountingPixmap(QPixmap):
        def __init__(self, *args):
            record(args)
            super().__init__(*args)

    class CountingImage(QImage):
        def __init__(self, *args):
            record(args)
            super().__init__(*args)

    monkeypatch.setattr(card_view, "QPixmap", CountingPixmap)
    monkeypatch.setattr(card_view, "QImage", CountingImage)
    return decoded


def _view(qtbot) -> CardView:
    v = CardView()
    qtbot.addWidget(v)
    return v


def test_load_image_decodes_off_the_ui_thread(qtbot, tmp_path, monkeypatch):
    img = _png(tmp_path / "page.png", 40, 30)
    v = _view(qtbot)
    decoded = _count_decodes(monkeypatch)
    results = []
    v.load_image(img, results.append)
    assert v.loading() and v.image_size() == (0, 0)  # 立即返回，界面先显示“加载中…”
    settle()
    assert results == [True] and not v.loading() and v.image_size() == (40, 30)
    assert decoded == [("page.png", False)]  # 在后台线程解码


def test_rapid_loads_show_only_the_last(qtbot, tmp_path):
    paths = [_png(tmp_path / f"p{i}.png", 10 + i, 20) for i in range(6)]
    v = _view(qtbot)
    results = []
    for p in paths:
        v.load_image(p, lambda ok, p=p: results.append((p.name, ok)))
    settle()
    assert results == [("p5.png", True)]  # 被作废的加载不回调
    assert v.image_size() == (15, 20)


def test_clear_and_sync_set_image_cancel_pending_load(qtbot, tmp_path):
    a = _png(tmp_path / "a.png", 40, 30)
    b = _png(tmp_path / "b.png", 20, 10)
    v = _view(qtbot)
    results = []
    v.load_image(a, results.append)
    v.clear()
    settle()
    assert results == [] and v.image_size() == (0, 0) and not v.loading()
    v.load_image(a, results.append)
    assert v.set_image(b)
    settle()
    assert results == [] and v.image_size() == (20, 10)  # 晚到的旧结果不会盖掉同步显示的图


def test_missing_or_corrupt_file_reports_failure(qtbot, tmp_path):
    v = _view(qtbot)
    results = []
    v.load_image(tmp_path / "missing.png", results.append)
    settle()  # 文件是否存在也在后台查
    assert results == [False]
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"broken")
    v.load_image(bad, results.append)
    settle()
    assert results == [False, False] and v.image_size() == (0, 0) and not v.loading()


def test_same_file_is_shared_not_decoded_again(qtbot, tmp_path, monkeypatch):
    img = _png(tmp_path / "page.png", 40, 30)
    a, b, c = _view(qtbot), _view(qtbot), _view(qtbot)
    decoded = _count_decodes(monkeypatch)
    assert a.set_image(img)
    assert b.set_image(img, share=a)
    c.load_image(img, share=a)
    settle()  # 读文件信息在后台：同一文件直接共用，不再解码
    assert len(decoded) == 1 and b.image_size() == c.image_size() == a.image_size() == (40, 30)
    # 一边清空或换图，不影响另一边
    a.clear()
    assert b.image_size() == (40, 30) and b._pix.pixmap().toImage().pixelColor(0, 0).red() == 128


def test_different_changed_or_missing_file_is_loaded_normally(qtbot, tmp_path, monkeypatch):
    front = _png(tmp_path / "front.png", 40, 30)
    back = _png(tmp_path / "back.png", 20, 10)
    a, b = _view(qtbot), _view(qtbot)
    assert a.set_image(front)
    decoded = _count_decodes(monkeypatch)
    # 不同文件（如卡面看背面、证据预览是正面）：自己解码
    assert b.set_image(back, share=a) and b.image_size() == (20, 10) and len(decoded) == 1
    # 同名文件被覆盖（如重新识别）：不共用旧图
    _png(front, 50, 60, value=200)
    st = front.stat()
    os.utime(front, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    assert b.set_image(front, share=a) and b.image_size() == (50, 60) and len(decoded) == 2
    # 文件缺失：和不共用时一样返回 False
    assert not b.set_image(tmp_path / "missing.png", share=a) and b.image_size() == (0, 0)
    # 共用方没有图片：自己解码
    a.clear()
    assert b.set_image(back, share=a) and len(decoded) == 3
    # 共用方显示的是内存数组（模板编辑器试识别）：没有文件来源，不共用
    a.set_array(np.zeros((30, 40), dtype=np.uint8))
    assert b.set_image(back, share=a) and len(decoded) == 4


def test_results_page_rapid_selection_ends_on_last_student(qtbot, small_dataset, tmp_path, monkeypatch):
    from scan_answer.batch import run_batch
    from scan_answer.ui.pages.results_page import ResultsPage
    bd = run_batch(TEMPLATE_DIR, small_dataset, tmp_path, batch_id="b1")
    rp = ResultsPage()
    qtbot.addWidget(rp)
    assert rp.open_batch(bd)
    settle()  # 批次在后台读取
    decoded = _count_decodes(monkeypatch)
    for seq in (1, 2, 4, 1, 2):  # 快速翻页：中途不等图片
        assert rp.select_seq(seq)
    settle()
    page = (rp.current.result.parent / rp.result["pages"]["front"]).resolve()
    assert rp.current.seq == 2
    assert rp.card._source[0] == rp.evidence.view._source[0] == str(page)  # 最终是最后一个学生的图
    assert rp.card.image_size()[0] > 3000 and rp.card.scene().items() != [rp.card._pix]  # 识别框已画上
    assert decoded and not any(main for _name, main in decoded)  # 都不在界面线程解码
    # 切到背面再切回正面：正面与证据预览共用，不再解码
    rp.face_seg.buttons["back"].click()
    settle()
    back = (rp.current.result.parent / rp.result["pages"]["back"]).resolve()
    assert rp.card._source[0] == str(back)
    count = len(decoded)
    rp.face_seg.buttons["front"].click()
    settle()
    assert rp.card._source[0] == str(page) and len(decoded) == count


def test_first_selection_decodes_page_once(qtbot, small_dataset, tmp_path, monkeypatch):
    """首次选中：证据预览和卡面同时在后台加载同一张整页图，只解码一次。"""
    from scan_answer.batch import run_batch
    from scan_answer.ui.pages.results_page import ResultsPage
    bd = run_batch(TEMPLATE_DIR, small_dataset, tmp_path, batch_id="b1")
    rp = ResultsPage()
    qtbot.addWidget(rp)
    assert rp.open_batch(bd)
    settle()
    decoded = []
    gate = threading.Event()

    class SlowImage(QImage):  # 第一个解码停住，保证卡面提交加载时证据预览的解码还在进行
        def __init__(self, *args):
            if args and isinstance(args[0], str):
                decoded.append(Path(args[0]).name)
                gate.wait(5)
            super().__init__(*args)

    monkeypatch.setattr(card_view, "QImage", SlowImage)
    assert rp.select_seq(2)
    settle()  # 结果文件在后台读
    gate.set()
    settle()
    front = Path(rp.result["pages"]["front"]).name
    assert decoded == [front]
    assert rp.card.image_size() == rp.evidence.view.image_size() and rp.card.image_size()[0] > 3000


def test_concurrent_loads_of_same_file_share_one_decode(qtbot, tmp_path, monkeypatch):
    img = _png(tmp_path / "page.png", 40, 30)
    views = [_view(qtbot) for _ in range(3)]
    decoded = []
    gate = threading.Event()

    class SlowImage(QImage):  # 第一个解码停住，保证其余加载提交时它还在进行
        def __init__(self, *args):
            if args and isinstance(args[0], str):
                decoded.append(args[0])
                gate.wait(5)
            super().__init__(*args)

    monkeypatch.setattr(card_view, "QImage", SlowImage)
    for v in views:
        v.load_image(img)
    gate.set()
    settle()
    assert len(decoded) == 1 and all(v.image_size() == (40, 30) for v in views)
    assert not card_view._decoding  # 解码完成后登记清除，之后同一文件变化时会重新解码
