# 输出格式（schema_version 1.0）

## 目录结构

```
<输出根目录>/<batch_id>/
  template.json                       # 本批次使用的模板快照
  manifest.json                       # 批次汇总
  raw/{seq:04d}_{原文件名}             # 原始扫描图副本
  students/{seq:04d}_{考号}/           # 考号识别不完整时只用 {seq:04d}
    result.json
    images/*.jpg
  errors/{seq:04d}_{物理异常代码}/
    error.json                        # {"seq", "code", "msg"}
    face_1.jpg, face_2.jpg
```

## 状态取值

| 字段 | 取值 |
|---|---|
| `status`（客观题、考号、考号逐位、条码） | `ok` / `blank` / `multiple` / `uncertain` / `partial` / `failed` |
| `ocr_status` | `none` / `pending` / `done` / `failed`（本期固定 `none`） |
| `scan.faces.*.align_method` | `anchors` / `frames` / `features` |

## 物理异常代码（不生成 result.json，写入 errors/）

| 代码 | 含义 | 现场处理 |
|---|---|---|
| `ALIGN_FAILED` | 所有面都无法定位，或图片文件无法读取 | 检查折角、放反，重扫 |
| `FACE_MISMATCH` | 能定位但与模板正反面对不上（扫错卡、混入其他纸） | 核对后重扫或作废 |
| `BLANK_PAGE` | 有一面几乎全白 | 检查是否漏进纸、双张 |
| `MISSING_FACE` | 图像数量不足一张卡的面数 | 补扫 |
| `SKEW_TOO_LARGE` | 倾斜超过 8° | 摆正后重扫 |

## 数据警告代码（result.json 的 `warnings[].code`）

| 代码 | 含义 | `field` |
|---|---|---|
| `EXAM_NO_PARTIAL` | 考号部分位未识别（未涂/多涂/涂浅），对应位输出 `?` | `student.exam_no` |
| `EXAM_NO_BLANK` | 考号整体未填涂 | `student.exam_no` |
| `ABSENT_MARKED` | 缺考标记已填涂 | `student.absent` |
| `OBJ_BLANK` | 客观题未作答 | `objective.{题号}` |
| `OBJ_MULTIPLE` | 单选题多涂 | `objective.{题号}` |
| `OBJ_UNCERTAIN` | 涂得浅、擦除不净或与其他选项区分度不足 | `objective.{题号}` |
| `ID_MISMATCH` | 填涂考号与考生条码不一致 | — |
| `BARCODE_MISSING` | 考生条码未识别（未贴或损坏） | `student.barcodes.student_barcode` |
| `ALIGN_FALLBACK` | 该面主定位失败，使用特征匹配兜底 | `scan.faces.{面}` |

## result.json 字段

| 字段 | 说明 |
|---|---|
| `seq`、`batch_id` | 扫描序号、批次号 |
| `template` | `{"name", "version"}` |
| `exam` | 扫描前填写的考试元信息（原样写入） |
| `scan` | `time`、`scanner`、`swapped`（正反面是否互换）、`faces.{面}`：`source`（第几张输入图）、`rotated`、`align_method`、`skew_deg`、`residual_mm`、`dpi` |
| `pages` | `front`/`back` 为对齐后的整页图；`raw_1`/`raw_2` 为原图相对路径 |
| `student.exam_no` | `value`（未识别位为 `?`）、`status`、`digits`（只列出非 ok 的位）、`image` |
| `student.barcodes.{key}` | `value`、`symbology`、`status`、`image` |
| `student.absent` | `value`、`fill` |
| `student.fields.{key}` | `label`、`image`、`text`、`ocr_status` |
| `student.consistent` | 考号与条码都识别成功时比较结果，否则 `null` |
| `objective[]` | `no`、`type`、`answer`（选中选项按顺序拼接）、`status`、`fill`（每个选项的涂黑度 0~1）、`image`（仅非 ok 时） |
| `subjective[]` | `no`、`type`、`content`、`images`（每个区域一张，按模板顺序）、`text`、`ocr_status`、`ocr_model`、`ocr_confidence` |
| `omr_fields`、`choose_one` | 本期为 `{}`、`[]` |
| `manual` | 预留人工校对，本期 `null` |
| `warnings[]` | `code`、可选 `field`、可选 `msg` |
