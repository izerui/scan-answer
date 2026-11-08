# 文档输入与逐题图片 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 图片与填写 PDF 均能进入识别、试识别，并输出完整逐题图片和可追溯 JSON。

**Architecture:** 新增文档源复用 SheetImages/流水线；统一图片路径生成；来源逐层传递，原 PDF 按内容摘要归档。界面沿用现有入口。

**Tech Stack:** Python 3.14、NumPy、OpenCV、PySide6、pypdfium2、pytest；不新增运行依赖。

**Spec:** `docs/superpowers/specs/2026-11-08-document-input-evidence-design.md`

## Global Constraints

- 保留用户当前工作区与 `docs/reviews/`，不修改已有样卡和模板。
- 坐标为毫米；PDF 默认 300 DPI；A3 及以下纸幅按模板处理。
- JSON 采用兼容性新增字段，旧批次可读。
- 每个 PDF 独立配组，不能用下一文件页面补齐奇数尾页。
- 不做后续工作包、评分、上传或 ZIP。
- 手工修改使用 apply_patch；完成后运行针对性检查和原快速回归。
- 此次仅交付工作区改动；提交和推送不作为隐含步骤。

## Review Focus

1. 正常客观题也必须有题图，重判不得删掉仍被引用的图片。
2. 特殊字段名与小题号不得造成覆盖或目录越界。
3. PDF 原文件与页面可追溯，文档之间不串页。
4. 渲染失败释放资源，输入错误不得悄悄标成已完成。
5. 旧图片输入、旧 result.json 与现有 UI 测试仍兼容。

### Task 1: 完整题图与无冲突身份

**Files:** `output/evidence.py`、`engine/result.py`、`pipeline/worker.py`、`tests/test_result.py`、`tests/test_engine_ext.py`。

**Interfaces:** 图片路径和 `question_id(kind, no, sub="")` 由 evidence 模块提供；result 兼容新增 `sheet_id/question_id/images/regions/input_pages`。

- [x] 写正常题、特殊小题、跨类别重名、缺少题图引用的失败用例。
- [x] 观察失败后，实现路径与全量题图；更新受影响的既有路径断言。
- [x] 重判保留来源并支持新路径；运行结果与 worker 覆盖测试（18 passed）。

### Task 2: PDF 文档源与来源持久化

**Files:** `source/base.py`、`source/documents.py`、`source/pdf.py`、`source/control.py`、`template/auto.py`、`pipeline/session.py`、`pipeline/runner.py`、`pipeline/worker.py`、`batch.py`、`tests/test_document_source.py`、`tests/test_document_batch.py`、`tests/test_control_source.py`。

**Interfaces:** `DocumentSource(inputs, faces, dpi=300).sheets()`；`SheetImages.origins/documents` 默认空；job 与 state 保留来源。

- [x] 写分页、文件边界、尾页、坏文件、纸幅参数化、原文件归档及结果来源用例。
- [x] 观察失败后，实现逐份 PDF 渲染、原 PDF 归档、批量入口接入（文档源 10 passed）。
- [x] 复用 8K 合成样本验证 PDF 识别输出、页码及全量题图。

### Task 3: 正式界面与试识别接入

**Files:** `app/trial.py`、`ui/pages/template_page.py`、`ui/pages/scan_page.py`、`cli.py`、相关 UI/试识别测试和文档。

**Interfaces:** 保留 `trial_recognize(t,bases,pairs)` 和 `start_folder`；新入口接受文件/目录；试识别共用文档源。

- [x] 写 PDF 试识别、文件路径识别、损坏输入反馈用例。
- [x] 观察失败后，接入 GUI、CLI 和试识别。
- [x] 更新输出格式与使用说明，运行快速回归；基线 443 passed，本次最终快测 472 passed，均有 1 项慢测排除。
- [x] 独立代码审查、必要修正，确认只修改本包文件。
- [x] 沿用现有 200 张模拟卡慢速回归：1 passed，472 deselected（270.34s）；结果见实施记录。
