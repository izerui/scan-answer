"""手动定位对话框：点击、跳过、撤销、校验与定位块吸附。"""
import cv2
import numpy as np

from scan_answer.engine.recognize import analyze_sheet
from scan_answer.ui.pages.scan_page import ExceptionItem
from scan_answer.ui.widgets.manual_align import ManualAlignDialog, snap_to_blob

from test_manual_align import _clicks, _tilted


def _dialog(qtbot, ctx, tmp_path, seed=41, deg=12.0):
    images, transforms, truth = _tilted(ctx, seed, deg)
    paths = []
    for i, img in enumerate(images):
        p = tmp_path / f"raw_{i + 1}.png"
        cv2.imwrite(str(p), img)
        paths.append(p)
    dlg = ManualAlignDialog(5, ctx.tpl, ctx.bases, paths)
    qtbot.addWidget(dlg)
    return dlg, images, transforms, truth


def test_snap_moves_click_to_anchor_center():
    img = np.full((400, 400), 250, np.uint8)
    img[200:230, 100:140] = 0  # 中心 (119.5, 214.5)
    x, y = snap_to_blob(img, 108, 205, 40)
    assert abs(x - 119.5) < 1 and abs(y - 214.5) < 1
    assert snap_to_blob(img, 330, 330, 40) == (330, 330)  # 附近没有块：不动


def test_clicks_skip_undo_and_result(qtbot, ctx, tmp_path):
    dlg, images, transforms, truth = _dialog(qtbot, ctx, tmp_path)
    assert not dlg.ok_btn.isEnabled()
    front = _clicks(ctx, "front", transforms[0])
    for x, y in front[:2]:
        dlg._place(0, x + 3, y - 2)  # 点得稍偏，应吸附回定位块中心
    dlg._skip()
    assert dlg.current_index() == 3
    dlg._undo()  # 撤销跳过：回到第 3 个点
    assert dlg.current_index() == 2
    dlg._place(0, *front[2])
    dlg._place(0, *front[3])
    assert dlg.current_index() is None
    dlg.tabs.setCurrentIndex(1)
    back = _clicks(ctx, "back", transforms[1])
    for pt in back[:4]:
        dlg._place(1, *pt)
    assert dlg.ok_btn.isEnabled(), dlg.status.text()
    manual = dlg.result_value()
    assert manual["front"]["image"] == 0 and len(manual["front"]["points"]) == 4
    assert np.allclose(manual["front"]["points"][0], front[0], atol=1.5)
    assert manual["back"]["points"][4:] == [None] * (len(back) - 4)
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert {p.align.method for p in a.placements.values()} == {"manual"}


def test_validation_messages(qtbot, ctx, tmp_path):
    dlg, _images, transforms, _ = _dialog(qtbot, ctx, tmp_path, seed=42)
    dlg.image_tabs[1].face_combo.setCurrentIndex(dlg.image_tabs[1].face_combo.findData("front"))
    assert "两张图不能选同一面" in dlg.status.text()
    for tab in dlg.image_tabs:
        tab.face_combo.setCurrentIndex(tab.face_combo.findData(None))
    assert "至少要手动定位一张图" in dlg.status.text() and not dlg.ok_btn.isEnabled()
    dlg.image_tabs[0].face_combo.setCurrentIndex(dlg.image_tabs[0].face_combo.findData("front"))
    dlg.tabs.setCurrentIndex(0)
    for pt in _clicks(ctx, "front", transforms[0])[:2]:
        dlg._place(0, *pt)
    assert "还需要点满 3 个点" in dlg.status.text()
    dlg._clear()
    assert dlg.current_index() == 0


def test_relocate_button_only_for_alignment_failures(qtbot):
    from PySide6.QtWidgets import QPushButton

    def buttons(code):
        item = ExceptionItem(1, code, "", None)
        qtbot.addWidget(item)
        return [b.text() for b in item.findChildren(QPushButton)]

    assert buttons("SKEW_TOO_LARGE") == ["重扫", "作废", "重新识别", "手动定位"]  # 第二行：重新识别 / 手动定位
    assert buttons("ALIGN_FAILED") == ["重扫", "作废", "重新识别", "手动定位"]  # 第二行：重新识别 / 手动定位
    assert buttons("BLANK_PAGE") == ["重扫", "作废"]


def test_undo_after_replacing_a_point_restores_previous_position(qtbot, ctx, tmp_path):
    dlg, _images, transforms, _ = _dialog(qtbot, ctx, tmp_path, seed=43)
    front = _clicks(ctx, "front", transforms[0])
    for pt in front[:3]:
        dlg._place(0, *pt)
    first = list(dlg.points[0]["front"][0])
    dlg.ref_list.setCurrentRow(0)  # 选中第 1 个点重新放
    dlg._place(0, front[0][0] + 400, front[0][1] + 300)
    assert dlg.points[0]["front"][0] != first
    dlg._undo()
    assert dlg.points[0]["front"][0] == first  # 恢复第一次的位置，而不是删掉
    dlg.ref_list.setCurrentRow(1)  # 把已放的第 2 个点改成跳过，再撤销：恢复坐标
    second = list(dlg.points[0]["front"][1])
    dlg._skip()
    assert dlg.points[0]["front"][1] is None
    dlg._undo()
    assert dlg.points[0]["front"][1] == second
    dlg._undo()  # 再撤销：第 3 个点是首次放置，撤销后回到未处理
    assert 2 not in dlg.points[0]["front"]


def test_double_click_does_not_add_extra_point_or_refit(qtbot, ctx, tmp_path):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    dlg, _images, _transforms, _ = _dialog(qtbot, ctx, tmp_path, seed=44)
    dlg.show()
    view = dlg.image_tabs[0].view
    qtbot.waitExposed(view)
    view.scale(2, 2)  # 先放大：双击不应把视图恢复成全图
    zoomed = view.transform().m11()
    hits = []
    view.clicked.connect(lambda x, y: hits.append((x, y)))
    # 真实双击的事件序列：按下、松开、双击、松开（QTest.mouseDClick 只发双击事件，不能代表实际操作）
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    vp, pos = view.viewport(), QPoint(200, 200)
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=pos)
    dbl = QMouseEvent(QEvent.Type.MouseButtonDblClick, QPointF(pos), QPointF(vp.mapToGlobal(pos)),
                      Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(vp, dbl)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=pos)
    assert len(hits) == 1  # 双击只相当于一次单击
    assert view.transform().m11() == zoomed
    dlg.image_tabs[0].fit_btn.click()  # 看全图改用独立按钮
    assert view.transform().m11() < zoomed


def test_clicks_only_update_preview_marks(qtbot, ctx, tmp_path):
    """每点一个参照点只更新右侧预览的红框：同一面的底图不重新转换，用户放大的视图也不被打回全图。"""
    dlg, _images, transforms, _truth = _dialog(qtbot, ctx, tmp_path)
    pix = dlg.preview._pix.pixmap().cacheKey()
    x, y = _clicks(ctx, "front", transforms[0])[0]
    dlg._place(0, x, y)
    assert dlg.preview._pix.pixmap().cacheKey() == pix
    dlg._skip()
    assert dlg.preview._pix.pixmap().cacheKey() == pix

