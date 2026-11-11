# 模板编辑页按功能区重排（方向 C）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把模板编辑页的操作按钮按功能区重排为方向 C：单行顶栏、竖排画框工具列、画布底部悬浮视图条、右栏“自动处理”卡片。

**Architecture:** 只改 `src/scan_answer/ui/pages/template_page.py` 的界面构建部分（`_build_editor` / `_build_bar` 及新拆出的构建函数）和引用新位置的说明文案；功能方法（`auto_align`、`auto_layout`、`auto_snap`、`rotate_base`、`set_tool`、试识别）保持不变。

**Tech Stack:** Python 3、PySide6、pytest-qt。

**Spec:** `docs/superpowers/specs/2026-11-11-template-editor-layout-design.md`；原型 `design-demos/toolbar/C-完整版.html`。

## Global Constraints

- 原有功能、快捷键、悬停说明、“?”说明（regions / snap / trial / subjective_target / anchors）一个不少。
- 保留的控件属性名：`back_btn rename_btn title_label summary_label layout_btn face_seg zoom_btn save_btn auto_btns snap_btn trial_btn rotate_btn rotate_reset undo_btn redo_btn select_btn props_stack exit_trial_btn`；对象名 `editorBar`、`editorStatus`、`propsSide` 保留。
- 新增 `tool_btns: dict[str | None, QPushButton]`（`None` = 选择），删除 `tool_combo`。
- 颜色只用 theme token（`theme.set_style` 的 `$X` 写法），深浅两套主题都要正确。

## Review Focus

- 试识别中：右栏只显示试识别面板，自动处理卡片不可见；退出后卡片回来。→ Task 3 测试。
- 画完一个框 / 按 Esc：工具列回到“选择”且只有它被勾选。→ Task 1 测试。
- 单面模板：悬浮条“背面”禁用且有说明（原逻辑，位置变了仍要成立）。→ 现有 `test_ui_editor` 覆盖。
- 切换正反面 / 撤销 / 自动定位后，“自动定位”卡片的状态文字随之更新。→ Task 3 测试。
- 窗口最小尺寸下工具列和卡片不被裁掉（工具列不可缩、卡片说明换行）。→ 手动截图检查。

---

### Task 1: 单行顶栏 + 竖排画框工具列

**Files:**
- Modify: `src/scan_answer/ui/pages/template_page.py`（`_build_editor`、`_build_bar`、`set_tool`）
- Test: `tests/test_template_page_guidance.py`、`tests/test_ui_editor.py:624`、`tests/test_mixed_card.py:277-278`

**Interfaces:**
- Produces: `self.tool_btns: dict[str | None, QPushButton]`；`_build_tool_dock() -> QWidget`（objectName `toolDock`）。

- [ ] **Step 1: 改测试**：`test_editor_controls_all_have_tooltips` 里把下拉框检查换成
  `assert set(page.tool_btns) == {None, *TOOL_KINDS}` 和 `all(b.toolTip() for b in page.tool_btns.values())`；新增
  `test_tool_dock_exclusive`：点 `tool_btns["objective"]` → `canvas._tool == "objective"` 且只有它勾选；`page.set_tool(None)` → 只有 `tool_btns[None]` 勾选。`test_ui_editor` / `test_mixed_card` 改为 `page.tool_btns[kind].click()`。
- [ ] **Step 2: 跑测试确认失败**：`pytest tests/test_template_page_guidance.py -q` → `AttributeError: tool_btns`。
- [ ] **Step 3: 实现**：顶栏一行：back │ 标题 rename 摘要 stretch undo redo │ trial_btn + HelpButton("trial") │ save_btn。工具列放在左栏和画布之间，顺序：选择；标题“画框”+HelpButton("regions")；`exam_no objective subjective barcode field absent` │ `omr_field choose_one identity ignore` │ `anchor frame`；按钮带 `_swatch_icon`、`TOOL_TIPS` 说明、checkable、互斥 QButtonGroup，`clicked` → `set_tool(kind)`。`set_tool` 改为勾选对应按钮。
- [ ] **Step 4: 跑测试确认通过**：`pytest tests/test_template_page_guidance.py tests/test_ui_editor.py tests/test_mixed_card.py -q`。
- [ ] **Step 5: Commit**

### Task 2: 画布底部悬浮视图条

**Files:** Modify `template_page.py`（`_build_editor`）；Test `tests/test_template_page_guidance.py`

**Interfaces:** Produces `_build_view_bar() -> QWidget`（objectName `viewBar`），内含 `face_seg`、缩放（`zoom_btn`）、`rotate_btn`。

- [ ] **Step 1: 测试** `test_view_bar_floats_on_canvas`：`page.face_seg`、`page.zoom_btn`、`page.rotate_btn` 的祖先里有 `viewBar`，且 `viewBar` 与 `canvas` 有同一父控件。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现**：画布与悬浮条放进同一个 QGridLayout 的同一格，悬浮条 `Qt.AlignBottom | Qt.AlignHCenter`，底边距 14px；样式 `$WORK` 底、`$LINE` 边、圆角 8px。
- [ ] **Step 4: 跑测试确认通过。**
- [ ] **Step 5: Commit**

### Task 3: 右栏“自动处理”卡片 + 试识别切换 + 文案

**Files:** Modify `template_page.py`（`_build_editor`、`_update_header`）、`src/scan_answer/ui/help_text.py`；Test `tests/test_template_page_guidance.py`、`tests/test_ui_editor.py:657`

**Interfaces:** Produces `_build_auto_panel() -> QWidget`（objectName `autoPanel`）；`self.align_status: QLabel`（自动定位卡片说明行）。`props_stack` 第 0 页 = 卡片 + `props` 的容器。

- [ ] **Step 1: 测试** `test_auto_cards_hide_during_trial`：打开 B199，`autoPanel` 可见；`props_stack.setCurrentIndex(1)` 后不可见；`exit_trial()` 后可见。`test_auto_locate_status`：`align_status.text()` 含“定位点 4”（B199 正面）。`auto_btns` 三项在 `layout_btn` 同一卡片外的菜单里：`{a.text() for a in <重找按钮>.menu().actions()} == {"自动找定位点","自动检测外框","自动识别同步头"}`。`test_ui_editor:657` 改为 `currentIndex() == 0`。
- [ ] **Step 2: 跑测试确认失败。**
- [ ] **Step 3: 实现**：三张卡（识别版面→`layout_btn` “运行”；自动定位→“重找 ▾”按钮 + 菜单，三个 QAction 连 `auto_align(what)`，`auto_btns` 存这三个 action（保留 `.toolTip()`），HelpButton("anchors")；自动吸附→`snap_btn` “运行” + HelpButton("snap")）。`_update_header` 里把 `align_label` 的文字同步给 `align_status`。`help_text.py` 与 `template_page.py` 中“工具栏‘画框’下拉”“工具栏‘试识别…’”等位置描述改为新位置。
- [ ] **Step 4: 全量测试**：`pytest -q`，全部通过。
- [ ] **Step 5: 截图核对**（深浅两套主题、最小窗口），Commit。
