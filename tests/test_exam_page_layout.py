"""考试页布局（行内展开 + 新建抽屉）：展开行的详情与批次子表、行尾操作、抽屉、空状态、双击、悬停说明。"""
import json

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QLabel, QWidget

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.pipeline.session import BatchSession
from scan_answer.ui.pages import exam_page as exam_page_module
from scan_answer.ui.pages.exam_page import CONTINUE_TIP, FINALIZED_TIP, ExamPage
from scan_answer.ui.widgets.guide import HelpButton

from conftest import TEMPLATE_DIR, settle
from test_exam_settings_guidance import missing_tooltips


def make_settings(tmp_path) -> AppSettings:
    return AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(bundled_templates_dir())],
                       workers=0, operator="李老师")


def show(qtbot, page, w=1440, h=900):
    qtbot.addWidget(page)
    page.resize(w, h)
    page.show()
    qtbot.waitExposed(page)
    settle()  # 考试页在后台读取考试与批次
    return page


def two_exams(s):
    """两场考试：第一场（较新，排第一行）有两批，第二场没有批次。"""
    old = create_exam(s.output_root, ExamInfo("旧考试"), TEMPLATE_DIR)
    e = create_exam(s.output_root, ExamInfo("期中", "管综", "199", "一中", "第 03 考场", "王敏"), TEMPLATE_DIR)
    for bid in ("b1", "b2"):
        BatchSession.create(e.directory, e.template_dir, batch_id=bid)
    # 保证排序：新考试排第一行
    j = json.loads((old.directory / "exam.json").read_text(encoding="utf-8"))
    j["created"] = "2000-01-01T00:00:00+08:00"
    (old.directory / "exam.json").write_text(json.dumps(j), encoding="utf-8")
    return e, old


def texts(w: QWidget) -> str:
    return " ".join(lab.text() for lab in w.findChildren(QLabel) if lab.isVisible())


def row_point(table, row, col):
    return table.visualRect(table.model().index(row, col)).topLeft() + QPoint(12, 12)


def test_expanded_row_shows_details_and_batch_subtable(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    e, _old = two_exams(s)
    page = show(qtbot, ExamPage(s))
    assert page.selected().directory == e.directory
    assert page.exam_table.expanded_row() == 0
    assert page.detail.isVisible()
    # 详情：科目代码、考场、操作员、创建日期、所用模板
    t = texts(page.detail)
    for part in ("科目代码", "199", "第 03 考场", "王敏", e.created[:10], "所用模板", "2 批"):
        assert part in t, part
    # 批次子表在展开区里
    assert page.detail.isAncestorOf(page.batch_table) and page.batch_table.isVisible()
    assert page.batch_table.rowCount() == 2
    concepts = {b.text_html for b in page.detail.findChildren(HelpButton)}
    assert any("批次" in c for c in concepts) and any("所用模板" in c for c in concepts)
    # 展开区位于该考试行的正下方，不盖住下一行
    rect0 = page.exam_table.visualRect(page.exam_table.model().index(0, 1))
    rect1 = page.exam_table.visualRect(page.exam_table.model().index(1, 1))
    assert page.detail.geometry().top() >= rect0.top() + 30
    assert page.detail.geometry().bottom() <= rect1.top()
    # 选另一场考试：展开它，批次子表换成它的（没有批次时说明怎么开始）
    page.exam_table.selectRow(1)
    assert page.exam_table.expanded_row() == 1
    assert page.batch_table.rowCount() == 0 and "0 批" in texts(page.detail)
    assert "扫描新批次" in page.batch_table._empty_hint.label.text()


def test_collapse_and_expand(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    table = page.exam_table
    # 点当前行的三角：收起；再点：展开
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_point(table, 0, 0))
    assert table.expanded_row() == -1 and not page.detail.isVisible()
    assert page.selected() is not None  # 收起不取消选中
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_point(table, 0, 0))
    assert table.expanded_row() == 0 and page.detail.isVisible()
    # 键盘：← 收起，→ 展开
    table.setFocus()
    qtbot.keyClick(table, Qt.Key.Key_Left)
    assert table.expanded_row() == -1
    qtbot.keyClick(table, Qt.Key.Key_Right)
    assert table.expanded_row() == 0
    # 收起后刷新（如回到考试页）：保持收起
    qtbot.keyClick(table, Qt.Key.Key_Left)
    page.refresh()
    settle()  # 考试页在后台刷新
    assert table.expanded_row() == -1
    # 点另一行：展开那一行
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_point(table, 1, 2))
    assert table.currentRow() == 1 and table.expanded_row() == 1


def test_exam_row_actions(qtbot, app_home, tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    e, old = two_exams(s)
    opened = []
    monkeypatch.setattr(exam_page_module, "open_in_file_manager", opened.append)
    page = show(qtbot, ExamPage(s))
    # 只有展开行的“扫描新批次”是主按钮；其它行的操作也一直可见（淡色）
    ops0, ops1 = page.exam_ops(0), page.exam_ops(1)
    assert ops0.scan.objectName() == "primary" and ops1.scan.objectName() != "primary"
    assert ops1.scan.isVisible() and ops1.open_dir.isVisible()
    assert page.scan_btn is ops0.scan and page.open_dir_btn is ops0.open_dir
    assert page.scan_btn.text() == "扫描新批次"
    with qtbot.waitSignal(page.startScan) as sig:
        ops0.scan.click()
    assert sig.args[0].directory == e.directory
    ops0.open_dir.click()
    assert opened == [e.directory]
    # 其它行的按钮作用于那一行（并选中、展开它）
    with qtbot.waitSignal(page.startScan) as sig:
        page.exam_ops(1).scan.click()
    assert sig.args[0].directory == old.directory and page.exam_table.currentRow() == 1
    assert page.exam_table.expanded_row() == 1 and page.exam_ops(1).scan.objectName() == "primary"
    assert page.exam_ops(0).scan.objectName() != "primary"
    page.exam_ops(0).open_dir.click()
    assert opened[-1] == e.directory and page.exam_table.currentRow() == 0


def test_batch_row_actions(qtbot, app_home, tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    e, _old = two_exams(s)
    opened = []
    monkeypatch.setattr(exam_page_module, "open_in_file_manager", opened.append)
    page = show(qtbot, ExamPage(s))
    b0, b1 = page.batches[0].directory, page.batches[1].directory
    ops = page.batch_ops(1)
    assert ops.cont.isVisible() and ops.res.isVisible() and ops.bdir.isVisible()
    with qtbot.waitSignal(page.continueBatch) as sig:
        ops.cont.click()
    assert sig.args[0].directory == e.directory and sig.args[1] == str(b1)
    assert page.batch_table.currentRow() == 1 and page.cont_btn is ops.cont
    with qtbot.waitSignal(page.showResults) as sig:
        page.batch_ops(0).res.click()
    assert sig.args[1] == str(b0) and page.batch_table.currentRow() == 0
    page.batch_ops(1).bdir.click()
    assert opened == [b1]
    # 已结算的批次：“继续扫描”的提示说明可选追加或新建
    m = json.loads((b1 / "manifest.json").read_text(encoding="utf-8"))
    m["finalized"] = True
    (b1 / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    page.refresh()
    settle()  # 考试页在后台刷新
    i = next(i for i, b in enumerate(page.batches) if b.directory == b1)
    assert page.batch_ops(i).cont.toolTip() == FINALIZED_TIP
    assert page.batch_ops(1 - i).cont.toolTip() == CONTINUE_TIP


def dclick(qtbot, view, pos):
    """真实双击：先按下再双击（视图只认按下与双击落在同一格）。"""
    qtbot.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)


def test_double_click_behaviour_kept(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    e, _old = two_exams(s)
    page = show(qtbot, ExamPage(s))
    table = page.exam_table
    with qtbot.waitSignal(page.startScan) as sig:
        dclick(qtbot, table, row_point(table, 0, 2))
    assert sig.args[0].directory == e.directory
    bt = page.batch_table
    with qtbot.waitSignal(page.showResults) as sig:
        dclick(qtbot, bt, row_point(bt, 1, 1))
    assert sig.args[1] == str(page.batches[1].directory)


def test_new_exam_drawer(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    assert page.new_btn.isVisible() and page.new_btn.objectName() != "primary"  # 有考试时展开行才是主按钮
    assert not page.drawer.isVisible() and not page.create_btn.isVisible()
    page.new_btn.click()
    assert page.drawer.isVisible() and page.create_btn.isVisible()
    assert page.create_btn.objectName() == "primary"
    for w in (page.f_name, page.f_subject, page.f_code, page.f_site, page.f_room, page.f_operator, page.f_template):
        assert w.isVisible() and page.drawer.isAncestorOf(w)
    assert page.drawer.isAncestorOf(page.out_hint) and str(s.output_root) in page.out_hint.text()
    assert any("所用模板" in b.text_html for b in page.drawer.findChildren(HelpButton))
    assert missing_tooltips(page) == []
    # 关闭、取消、Esc 都收起抽屉
    page.drawer_close_btn.click()
    assert not page.drawer.isVisible()
    page.new_btn.click()
    page.drawer_cancel_btn.click()
    assert not page.drawer.isVisible()
    page.new_btn.click()
    qtbot.keyClick(page.f_name, Qt.Key.Key_Escape)
    assert not page.drawer.isVisible()
    # 点遮罩也收起
    page.new_btn.click()
    qtbot.mouseClick(page.drawer_scrim, Qt.MouseButton.LeftButton, pos=QPoint(20, 20))
    assert not page.drawer.isVisible()


def test_drawer_fits_minimum_window(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    page = show(qtbot, ExamPage(s), 1216, 764)  # 1280×800 窗口去掉图标栏、顶栏、状态栏
    page.new_btn.click()
    btn = page.create_btn
    bottom_right = btn.mapTo(page, QPoint(btn.width(), btn.height()))
    assert page.rect().contains(bottom_right - QPoint(1, 1))


def test_drawer_create_runs_original_flow(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    page = show(qtbot, ExamPage(s))
    page.new_btn.click()
    page.f_template.setCurrentIndex(page.f_template.findData(str(TEMPLATE_DIR)))
    page.f_name.setText("期末")
    page.f_subject.setText("管综")
    with qtbot.waitSignal(page.startScan, timeout=10000) as sig:
        page.create_btn.click()
        settle()
    assert sig.args[0].info.name == "期末"
    assert not page.drawer.isVisible()  # 创建成功：抽屉收起，表单清空
    assert page.f_name.text() == "" and page.selected().info.name == "期末"


def test_settings_link_in_drawer(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    page = show(qtbot, ExamPage(s))
    page.new_btn.click()
    assert not page.settings_link.isVisible()  # 没人接收跳转：不显示链接
    page.close_drawer()
    got = []
    page.openSettings.connect(lambda: got.append(1))
    page.new_btn.click()
    assert page.settings_link.isVisible() and page.settings_link.toolTip()
    page.settings_link.click()
    assert got == [1] and not page.drawer.isVisible()


def test_empty_page_new_exam_is_primary(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    (tmp_path / "out").mkdir()
    page = show(qtbot, ExamPage(s))
    assert page.new_btn.objectName() == "primary"
    assert "新建考试" in page.exam_table._empty_hint.label.text()
    assert not page.legend.isVisible() and not page.detail.isVisible()
    assert page.scan_btn is None and page.cont_btn is None  # 没有行：没有行尾操作
    assert missing_tooltips(page) == []
    create_exam(s.output_root, ExamInfo("期中"), TEMPLATE_DIR)
    page.refresh()
    settle()  # 考试页在后台刷新
    assert page.new_btn.objectName() != "primary" and page.legend.isVisible()


def test_all_visible_controls_have_tooltips(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    assert missing_tooltips(page) == []
    for table in (page.exam_table, page.batch_table):
        for c in range(table.columnCount()):
            assert table.horizontalHeaderItem(c).toolTip()
    # Tab 能离开表格走到行尾按钮（不被表格当作单元格间移动吃掉）
    assert not page.exam_table.tabKeyNavigation() and not page.batch_table.tabKeyNavigation()
    for b in (page.scan_btn, page.open_dir_btn, page.cont_btn, page.res_btn, page.bdir_btn):
        assert b.focusPolicy() & Qt.FocusPolicy.TabFocus


def test_clicks_inside_detail_do_not_act_on_exam_row(qtbot, app_home, tmp_path):
    """展开区里的空白、文字上按下、双击：不会冒泡成“双击考试 → 扫描新批次”，也不会收起。"""
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    emitted = []
    page.startScan.connect(emitted.append)
    label = page.d_values["operator"]
    pos = QPoint(3, label.height() // 2)
    qtbot.mousePress(label, Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseRelease(label, Qt.MouseButton.LeftButton, pos=pos)
    qtbot.mouseDClick(label, Qt.MouseButton.LeftButton, pos=pos)
    blank = QPoint(page.detail.width() - 5, 5)
    qtbot.mousePress(page.detail, Qt.MouseButton.LeftButton, pos=blank)
    qtbot.mouseRelease(page.detail, Qt.MouseButton.LeftButton, pos=blank)
    qtbot.mouseDClick(page.detail, Qt.MouseButton.LeftButton, pos=blank)
    assert emitted == [] and page.exam_table.expanded_row() == 0
    # 考试行本身双击仍进入扫描
    dclick(qtbot, page.exam_table, row_point(page.exam_table, 0, 2))
    assert len(emitted) == 1


def test_click_collapsed_current_row_expands(qtbot, app_home, tmp_path):
    """点已选中但收起的行的任意位置：展开；已展开时点行（非三角）保持展开。"""
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    table = page.exam_table
    table.toggleRequested.emit(0)
    assert table.expanded_row() == -1
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_point(table, 0, 2))
    assert table.expanded_row() == 0
    qtbot.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=row_point(table, 0, 2))
    assert table.expanded_row() == 0


def test_preparing_explains_other_rows(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    page.set_preparing(True)
    other = page.exam_ops(1).scan
    assert not other.isEnabled() and "请稍候" in other.toolTip()
    assert page.scan_btn.text() == "正在检查模板…"
    page.set_preparing(False)
    assert other.isEnabled() and other.toolTip() == exam_page_module.SCAN_TIP


def test_drawer_is_modal(qtbot, app_home, tmp_path):
    """抽屉打开时，遮罩后面的考试表和行尾按钮拿不到焦点、点不动；关闭后恢复。"""
    s = make_settings(tmp_path)
    two_exams(s)
    page = show(qtbot, ExamPage(s))
    behind = (page.exam_table, page.scan_btn, page.open_dir_btn, page.cont_btn, page.new_btn)
    page.new_btn.click()
    for w in behind:
        assert not w.isEnabled()
    for w in (page.f_name, page.f_template, page.create_btn, page.drawer_cancel_btn):
        assert w.isEnabled()
    page.close_drawer()
    for w in behind:
        assert w.isEnabled()


def _paint_composition(qtbot, data, cell_w=110):
    """把构成列的一个单元格画到比它宽的画布上，返回画布和单元格矩形。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QStyleOptionViewItem, QTableWidgetItem

    from scan_answer.ui.widgets.exam_table import ROW_H, CompositionDelegate, ExamTable
    table = ExamTable(1, 1)
    qtbot.addWidget(table)
    delegate = CompositionDelegate(table)
    item = QTableWidgetItem("")
    item.setData(Qt.ItemDataRole.UserRole, data)
    table.setItem(0, 0, item)
    img = QImage(cell_w * 4, ROW_H, QImage.Format.Format_ARGB32)
    img.fill(QColor(255, 255, 255))
    cell = QRect(0, 0, cell_w, ROW_H)
    opt = QStyleOptionViewItem()
    opt.rect = cell
    opt.widget = None
    painter = QPainter(img)
    delegate.paint(painter, opt, table.model().index(0, 0))
    painter.end()
    return img, cell


def test_composition_bar_stays_in_cell_when_warnings_exceed_sheets(qtbot, app_home):
    """数据警告是条数（一张卡可能有多条）：25 条警告 / 7 张卡时构成条也不能超出单元格、盖住后面的列。"""
    from PySide6.QtGui import QColor
    data = {"total": 7, "ok": 7, "physical": 0, "voided": 0, "warnings": 25}
    img, cell = _paint_composition(qtbot, data)
    white = QColor(255, 255, 255).rgb()
    outside = [x for x in range(cell.right() + 1, img.width())
               for y in range(img.height()) if img.pixel(x, y) != white]
    assert not outside, f"画到了单元格外：x={min(outside)}…{max(outside)}"
    # 条本身画出来了（单元格里有颜色）
    assert any(img.pixel(x, cell.center().y()) != white for x in range(cell.left(), cell.right()))
    from scan_answer.ui.widgets.exam_table import CompositionDelegate
    segs = CompositionDelegate.segments(data, 8, 94)
    assert segs and sum(w for _x, w, _c in segs) <= 94 + 1e-6
    assert all(8 <= x and x + w <= 102 + 1e-6 for x, w, _c in segs)


def test_warning_column_tips_say_count_of_warnings():
    """“数据警告”列显示的是警告条数，表头提示不能说成张数。"""
    from scan_answer.ui.pages.exam_page import BATCH_COLUMN_TIPS, EXAM_COLUMN_TIPS
    for tips, col in ((EXAM_COLUMN_TIPS, 8), (BATCH_COLUMN_TIPS, 6)):
        assert "条数" in tips[col] and "张数" not in tips[col], tips[col]
