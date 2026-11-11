# 考试 / 扫描 / 结果 / 设置四页按功能区重排 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把考试页、扫描页、结果页、设置页按用户选定的原型（考试 B、扫描 C、结果 B、设置 A）重排，功能一个不丢。

**Architecture:** 每页只改自己的 `src/scan_answer/ui/pages/<page>.py` 的界面构建部分，业务方法、信号、对外属性保持；四页互不依赖，Task 1–4 可并行；共享文案和文档在 Task 5 统一更新。

**Tech Stack:** Python 3、PySide6、pytest-qt（`uv run --extra dev pytest`）。

**Spec:** `docs/superpowers/specs/2026-11-11-pages-layout-design.md`

## Global Constraints

- 反目标：任何现有功能丢失或不可达（按钮、输入、分段、开关、“?”、悬停说明、快捷键、启用/禁用原因、空状态、双击、离开设置页的未保存询问）。
- 原型 HTML 为布局的准绳：`design-demos/pages/{exam-B,scan-C,results-B,settings-A}-*.html` 与 `design-demos/pages/shots/` 对应截图。
- 保留被其它模块/测试引用的属性、方法、信号名；必须改名时同步改调用方和测试。
- 颜色只用 theme token（`theme.set_style` 的 `$X`），深浅主题都对；1280×800 不截断。
- **文件所有权（并行防冲突）**：Task 1–4 只能改自己那页的 `pages/<page>.py`、新建的 `ui/widgets/<page>_*.py`、以及测试文件；**不得改** `help_text.py`、`docs/gui.md`、`theme.py`、`widgets/common.py`、`widgets/guide.py`、`main_window.py`、其它页面、`template_page.py`。需要改这些的，把确切改动写进报告，由 Task 5 统一做。
- 原型里新增的展示只用现有数据计算，算不出的不造，写进报告。

## Review Focus

- 状态切换后控件的启用/禁用与原逻辑一致（扫描页：未开始 / 识别中 / 暂停 / 本轮结束 / 已结算；结果页：未打开批次 / 有阻断 / 可交付）。→ 各页测试按状态逐一断言。
- 被折叠 / 收进浮层 / 抽屉 / 另一模式的控件仍可达且信号连着（点开后能点、点了有效果）。→ 各页测试点开再触发。
- 快捷键在新布局下仍生效（结果页 PgUp/PgDn 在卡面上、扫描页空格）。→ 真实按键测试。
- 空状态：没有考试 / 没有批次 / 未打开批次 / 设置首次使用。→ 各页测试。
- 主题切换后新控件颜色跟着变（`test_theme_pages.py` 风格）。→ 各页测试或 Task 5 截图核对。

---

### Task 1: 考试页 → B 行内展开 + 新建抽屉
**Files:** `src/scan_answer/ui/pages/exam_page.py`；测试 `tests/test_exam_*.py`、`tests/test_ui_pages.py` 中考试页部分、新增 `tests/test_exam_page_layout.py`。
- [ ] 读原型与 spec，列出现有控件 → 新位置对照表（写进报告）；`rg` 出所有外部引用的 exam_page 属性/方法。
- [ ] 先写失败测试：展开行显示详情和批次子表；行尾按钮触发原动作（继续扫描、查看结果、打开目录、打开考试目录、扫描新批次）；抽屉打开/关闭、“创建并进入扫描”走原创建流程；无考试时“新建考试”为主按钮；双击行为保留；所有可见交互控件有悬停说明。
- [ ] 实现；跑 `uv run --extra dev pytest tests/test_exam_page_layout.py tests/test_exam_flow_fixes.py tests/test_exam_settings_guidance.py tests/test_exam_snapshot.py tests/test_ui_pages.py -q` 全过。
- [ ] 1440×900 深色截图到 `design-demos/pages/impl/exam.png` 与原型对照。

### Task 2: 扫描页 → C 左管批次、右管本轮
**Files:** `src/scan_answer/ui/pages/scan_page.py`；测试 `tests/test_scan_page_guidance.py` 及所有引用 scan_page 控件的测试，新增 `tests/test_scan_page_layout.py`。
- [ ] 对照表 + 外部引用清单（同 Task 1）。
- [ ] 失败测试：新批次在左栏头；结束批次在左栏底并显示前置检查清单（异常数、正在识别、警告不拦截），启用条件与原逻辑一致；已作废 / 数据警告折叠行可展开且条目操作可用；“修改…”打开参数面板，每个参数改动生效到原设置；开始/暂停/继续/结束本轮在右栏底；空格键暂停/继续仍有效；区域色码在卡面下方。
- [ ] 实现；跑全部扫描相关测试全过。
- [ ] 截图 `design-demos/pages/impl/scan.png`（尽量有数据的状态）。

### Task 3: 结果页 → B 核对 / 交付两种模式
**Files:** `src/scan_answer/ui/pages/results_page.py`、`src/scan_answer/ui/widgets/delivery_panel.py`（结果页专属，可改）；测试 `tests/test_results_*.py`、`tests/test_delivery_ui.py`，新增 `tests/test_results_page_layout.py`。
- [ ] 对照表 + 外部引用清单。
- [ ] 失败测试：模式切换（徽标显示阻断数）；核对模式悬浮条的上一份/下一份/第 k/n 份/正背面生效，PgUp/PgDn 仍生效；交付模式检查交付、结论、阻断项/说明项列表、阻断项的处理按钮、生成交付目录的禁用原因；整批转写两按钮与 ? 在交付模式右栏且行为不变；单项重试转写仍在主观题面板；“下一步”两模式都可见；未打开批次的空状态。
- [ ] 实现；跑全部结果/交付相关测试全过。
- [ ] 截图 `design-demos/pages/impl/results-check.png`、`results-deliver.png`。

### Task 4: 设置页 → A 分节目录 + 顶部保存条
**Files:** `src/scan_answer/ui/pages/settings_page.py`；测试 `tests/test_app_settings.py`、`tests/test_exam_settings_guidance.py`（设置部分）及引用 settings_page 的测试，新增 `tests/test_settings_page_layout.py`。
- [ ] 对照表 + 外部引用清单。
- [ ] 失败测试：左栏四节切换右侧内容，默认“输出”；节摘要随取值变化；改字段后顶部条列出改了哪些项、对应节标点、字段显示原值；保存后清除；离开页面的未保存询问保留；接口地址/密钥中文标签（原英文作副标签）；显示密钥、浏览、测试连接、命名示例都可用。
- [ ] 实现；跑全部设置相关测试全过。
- [ ] 截图 `design-demos/pages/impl/settings.png`。

### Task 5: 集成
- [ ] 按 Task 1–4 报告更新 `help_text.py`（PAGE_GUIDES、CONCEPTS 中提到位置的文案）、`docs/gui.md`、及报告中列出的其它共享改动。
- [ ] 全量 `uv run --extra dev pytest -q` 通过。
- [ ] 真实程序深浅两主题、1440×900 与 1280×800 截图逐页核对；汇总四页“现有控件 → 新位置”对照表交用户。
