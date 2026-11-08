# 模板工作流闭环 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans / superpowers:test-driven-development. Independent file scopes may be delegated.

**Goal:** 普通操作员能制作、核对并保存模板，考试使用固定版本。

**Architecture:** TemplateDoc 管理所有内容变更与 revision；模板资源先完成后发布 JSON；考试保存快照；试识别复用正式题图与结果组装。

**Tech Stack:** Python 3.14、PySide6、OpenCV、pytest，沿用现有依赖。

**Spec:** `docs/superpowers/specs/2026-11-08-template-workflow-design.md`

## Global Constraints

- 保留工作包 1 和 docs/reviews 的未提交改动，不自动提交或打包。
- A3 及以下、毫米坐标、300 DPI 底图，保持已有 8K 模板可读取。
- 不做扫描仪/OCR生命周期等后续包，不做评分。
- 手工编辑使用 apply_patch；先观察失败用例，再实现。

## Review Focus

1. 保存 JSON 失败时旧模板及底图仍可读，资源引用不能越界。
2. 题号变化、排序、切面、撤销之后引用与题图顺序正确。
3. 保存后撤销再保存，版本不重复；考试模板不会跟随外部变化。
4. 编辑后、换模板后迟到的试识别结果不得成为当前模板证据。
5. 旧考试与旧模板读取兼容，错误清楚显示，不留半成品。

### Task 1: 校验和资源保存

**Files:** template/validate.py、template/io.py、tests/test_template_validation_workflow.py、tests/test_template_storage.py、相关既有 IO 测试。

- [x] 新增结构校验与存储失败/另存用例并确认失败。
- [x] 完整校验、资源引用约束、内容资源文件与原子 JSON 发布。
- [x] 针对性测试通过（267 项）。

### Task 2: 模板编辑数据操作

**Files:** template/doc.py、tests/test_template_workflow.py。

- [x] revision、单调保存版本、尺寸与定位方式、跨面区域排序、题目引用联动失败用例。
- [x] 实现数据层操作并保证失败修改不留下部分变更。
- [x] 保留撤销重做、现有模板与新建流程测试（数据层 78 项）。

### Task 3: 考试固定模板

**Files:** app/library.py、ui/pages/exam_page.py、ui/controller.py（new_batch）、扫描页/主窗口模板标签；tests/test_exam_snapshot.py、tests/test_library.py。

- [x] 模板选择保留、考试快照、旧考试迁移和冲突拒绝用例。
- [x] 创建快照、加载兼容、新批次固定模板和显示版本。
- [x] 针对性测试通过（结构校验补齐后 39 项及既有 UI/controller 24 项）。

### Task 4: 编辑器交互与试识别预览

**Files:** ui/pages/template_page.py、ui/editor/props.py、ui/editor/new_dialog.py、ui/editor/trial_preview.py、ui/widgets/card_view.py、app/trial.py、对应测试。

- [x] 跨面目标、排序、手动定位、过期回调、题图/JSON预览用例。
- [x] 接入数据层和正式输出预览；同内容保存不误失效。
- [x] 更新文档，832 项快速测试、200 份模拟回归通过；独立审查问题已修复，跨面编辑与题图预览截图已核对。详见 `docs/reviews/2026-11-08-package2-implementation.md`。
