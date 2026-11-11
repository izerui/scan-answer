# 新建模板自动识别版面 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建模板后自动识别考号、客观题块、主观题作答区，人只核对。

**Architecture:** 新模块 `template/detect_layout.py` 纯函数：底图 → 选项框 → 网格 → 分类编号 → `LayoutResult`。`TemplateDoc.auto_layout()` 把结果写进模板；`TemplateDoc.new()` 创建时调用一次；编辑器加“识别版面”按钮走现有 `_mutate` 撤销机制。

**Tech Stack:** Python、numpy、OpenCV、PySide6、pytest。

**Spec:** `docs/superpowers/specs/2026-11-10-auto-layout-design.md`

## Global Constraints

- 不新增依赖；不联网。
- 坐标单位 mm，底图 300 DPI，与 `template/model.py` 一致。
- 单面识别 < 2 秒。
- 题型一律 `single`；主观题 `qtype="essay"`, `content="zh"`。
- 宁缺勿错：网格不规则（拟合残差大、格子数不像题块）的不输出。
- 界面文案中文。

## Review Focus

- 全白 / 无选项框的底图 → 返回空结果和“未识别到”提示，不抛异常。
- 只有正面的单面卡 → 只识别正面，编号正常。
- 定位块、同步头、条码这些实心块不能被当成选项框。
- “正确涂卡 / 错误涂卡 / 缺考标记”示例小框不能变成一道题（单个、不成网格的框丢弃）。
- 已有手工内容时重新识别 → 先确认，可撤销。

---

### Task 1: 识别模块

**Files:**
- Create: `src/scan_answer/template/detect_layout.py`
- Test: `tests/test_detect_layout.py`

**Interfaces:**
- Produces:
  - `detect_boxes(base: np.ndarray, dpi: float) -> list[Rect]`：印刷选项框（mm）。
  - `@dataclass LayoutResult(exam_no: ExamNoSpec | None, objective: list[ObjectiveBlock], subjective: list[SubjectiveSpec], notes: list[str])`
  - `detect_layout(bases: dict[str, np.ndarray], dpi: int, paper: tuple[float, float], occupied: dict[str, list[Rect]] | None = None) -> LayoutResult`；`occupied` 为各面定位外框等，主观题区要排除。

- [x] **Step 1: 写失败测试**（对 6 套 `templates/*` 用 `TemplateDoc.load` 取底图与标准答案）
  - `test_exam_no_matches_bundled[tpl]`：位数相同，对应格子中心误差 ≤ 1 mm。
  - `test_objective_matches_bundled[tpl]`：题号集合相同，选项数相同，≥ 95% 格子中心误差 ≤ 1 mm。
  - `test_blank_page_gives_empty_result`：全白图 → 三项为空，notes 非空。
  - `test_front_only`：只传 front → 结果全部在 front。
- [x] **Step 2: 运行确认失败**：`.venv/bin/python -m pytest tests/test_detect_layout.py -q` → ImportError。
- [x] **Step 3: 实现**
  - 选项框：自适应阈值；方框式用轮廓外接矩形（2–6 mm、空心）；括号式用成对竖笔画（参考 `tools/build_templates_kaoyan.py:bracket_boxes`）。去重（中心距 < 1 mm 合并）。
  - 网格：按尺寸相近 + 近邻间距把框连成行（同 y、等距 x）与列，再合成规则二维块；块需 ≥ 2×2 且残差小。
  - 分类：10×N / N×10 → 考号（取最大一块）；其余选项数 2–7 → 客观题块，选项方向 = 组内框排列方向；一块内按每题一组。
  - 列排列中竖向分段间隙（每 5 题）合并成同一列连续题号；跨栏按栏从左到右，正面先于背面。
  - 主观题：大闭合外框（宽高均 > 40 mm），排除与考号/客观题/occupied 重叠者，内缩 1.5 mm。
  - 每块用 `snap_grid_search` 精修，通过则采用。
- [x] **Step 4: 运行测试至通过**（调参以 6 套模板为准）。
- [x] **Step 5: 提交**（用户要求时）。

### Task 2: 接入 TemplateDoc

**Files:**
- Modify: `src/scan_answer/template/doc.py`（`new()`，新增 `auto_layout()`）
- Test: `tests/test_detect_layout.py`

**Interfaces:**
- Consumes: `detect_layout`。
- Produces: `TemplateDoc.auto_layout(self) -> str`：替换 exam_no / objective / subjective，有改动时 `_changed()`，返回中文说明（如“识别到：考号 15 位；客观题 1–55（请核对单选/多选）；主观题 3 个”）。`TemplateDoc.new()` 末尾调用并把说明存在 `doc.layout_message`。

- [x] **Step 1: 测试** `test_new_doc_auto_layout`：用 guanzong199 底图构造 `ImportedBase` 调 `TemplateDoc.new` → objective 题号 1..55、exam_no 15 位、`layout_message` 含“考号”。
- [x] **Step 2–4:** 实现、跑 `tests/` 全量，修正因 `new()` 现在会带出区域而变化的旧测试（只改预期，不改语义）。

### Task 3: 编辑器按钮与提示

**Files:**
- Modify: `src/scan_answer/ui/pages/template_page.py`（`_build_editor` row2 加按钮；新增 `auto_layout()`；`_new_dialog` / `new_template` 把 `doc.layout_message` 拼进状态行）
- Test: `tests/test_template_flow_fixes.py`

- [x] **Step 1: 测试** `test_auto_layout_button_undoable`：打开新建的 199 文档，清空客观题后点“识别版面” → 客观题恢复；undo 后回到清空状态。已有内容时弹确认（monkeypatch `QMessageBox.question` 返回 Yes）。
- [x] **Step 2–4:** 实现：按钮文案“识别版面”，tooltip“自动识别考号、选择题和作答区，替换当前这三类区域（可撤销）”；已有这三类内容时 `QMessageBox.question` 确认；用 `self._mutate("识别版面", self.doc.auto_layout)`。
- [x] **Step 5:** `lint_files` 改动文件；全量测试。
