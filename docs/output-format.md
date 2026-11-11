# 输出格式（schema_version 1.0）

## 目录结构

桌面端先建立考试目录：

```
<输出根目录>/<考试目录>/
  exam.json                           # 考试信息、相对 template_dir、模板名/版本及来源
  template/                           # 考试固定模板快照
    template.json
    <JSON 中声明的底图资源>
  <batch_id>/                         # 各批次结构见下方
```

新考试的 `template_dir` 为相对路径 `template`，整体移动考试目录后仍可读取。批次继续保存自己的快照，已有批次按自己的快照重开。保存后的底图资源可能使用内容摘要文件名，读取者应遵循 `faces.*.image`，不要假设一定叫 `front.png` / `back.png`。

CLI 可直接在输出根目录创建批次；桌面端的批次位于考试目录内：

```
<输出根目录>/<batch_id>/
  template/                           # 本批次使用的模板快照（template.json + 各面底图），识别与重开批次一律用它
  template.json                       # template/template.json 的副本（兼容旧版，供人查看）
  manifest.json                       # 批次汇总（扫描中约每 2 秒更新一次）
  raw/{seq:04d}_{原文件名}             # 原始扫描图副本
  sources/{sha256}.pdf               # 原始填写 PDF（同一文档只保存一次）
  acquisition/{run_id}/             # 扫描回调中落盘的页面，先于识别登记
    run.json                         # 面数、run_id、可选重扫 target_seq
    round-000001/page-000001.png      # PNG 内保存精确 DPI；不同轮次不补面
  attempts/{seq:04d}/{attempt_id}/   # 每次重扫的独立尝试
    journal.json                     # staging/prepared/committed/rejected/rolled_back
    stage/                           # 新原图及识别输出，失败也保留
    previous/                        # 成功提交前备份的旧证据与 manifest
    publish/                         # 提交暂存；由事务日志完成发布或回滚
  state/{seq:04d}.json                # 识别中间量（涂黑度、对齐矩阵），结束批次重新判定时使用
  students/{seq:04d}_{考号}/           # 考号识别不完整时只用 {seq:04d}；目录名在首次识别时确定，之后不改
    result.json
    images/pages/*.jpg
    images/identity/*.jpg
    images/barcodes/*.jpg
    images/barcode_candidates/key-*/candidate-*.jpg # 各条码字段的候选位置证据
    images/fields/*.jpg
    images/omr_fields/*.jpg
    images/objective/*.jpg            # 所有客观题，不仅异常题
    images/subjective/q-*/sub-*/*.jpg  # 按题号、小题号、区域顺序保存
  errors/{seq:04d}_{物理异常代码}/
    error.json                        # {"seq", "code", "msg"}
    face_1.jpg, face_2.jpg
```

恢复时可能将损坏的事务日志隔离为 `journal.corrupt-*.json` 并在 manifest 的事件中记录 `recovery_warning`；受影响且无法确认的当前答卷标为待核对异常，原文件不删除。不要手工移除 `acquisition/` 或 `attempts/`：它们也是页去重、恢复和重扫历史的依据，本阶段不做自动清理。

识别结果的 `content_revision` 是本次图片内容版本，重扫成功生成新值，单纯阈值重判保留。OCR 使用根级 `ocr_result_id` 和逐题/字段的 `ocr_generation`、`ocr_fingerprint` 校验回写权限。这些是可选内部状态字段，旧结果可读取；外部业务不得用它们替代 `sheet_id` / `question_id`，也不应手工复制它们到一份新识别结果。

扫描来源的 `input_pages` 额外记录 `acquisition_id`、`run_id`、`round_id` 和页序，重扫采集还记录 `target_seq`。相对 `raw` / `document` 引用保持原约定。manifest 中重扫成功的答卷可带 `attempt_id`，用于区分当前版本和历史异常。

## 结算版本与交付目录

工作批次 manifest 的 `revision` 是有效答卷集合的修订号。`finalized=true` 仅表示识别结算成功；`finalization` 记录状态、结算修订号、未处置序号和重判失败序号。结算修订号必须对应当前修订，不能用过期任务宣布新数据已经完成。

新生成的 state 使用相对批次的 `raw_paths`，例如 `raw/0001_scan.jpg`。读取旧 POSIX/Windows 绝对原图路径时，只定位当前批次 `raw/` 内相同文件，不回读旧目录；原目录仍存在也不例外。state/result/crop 不能跨答卷或越出批次。

通过交付检查后生成的新目录保留当前载荷：

```
<新交付目录>/
  manifest.json                   # delivery.sealed=true，附独立 revision
  delivery.json                   # 检查摘要、警告/作废清单、逐文件 SHA256
  template.json
  template/                       # 模板快照与底图
  raw/                            # 当前答卷引用的原图
  sources/                        # 当前答卷引用的原 PDF（如有）
  students/.../result.json
  students/.../images/...
  errors/...                      # 已作废答卷存在的异常证据（如有）
```

不复制 `state/`、`acquisition/`、`attempts/`。这个固定目录用于交付和读取，不能作为继续采集的工作目录。原工作批次仍保留全部恢复材料。

`manifest.delivery` 含 `sealed`、`revision`、`created`、`source_batch_id`。`delivery.json` 含相同 `revision`、检查 `summary`、`issues`、`files`（相对路径→SHA256）和源清单指纹。`delivery.json` 不参与自身摘要；`files` 覆盖实际声明的载荷，包括封存后的 manifest。

检查状态为 `ready`、`warnings` 或 `blocked`，同时提供 `ready` 布尔值。数据/转写警告不代表已人工确认正确；`pending` 或缺失 OCR 状态、未处置物理异常、坏文件、缺失/重复题目、区域图片不匹配等会阻止交付。每题、每区域及学生字段必须绑定规范证据路径，不能只靠相互一致但错误的引用蒙混通过。

生成时先复制到同级临时目录，对照源摘要并复查副本，再使用系统提供的“不替换已有目标”发布。目标存在、复制失败、源文件变化或文件系统不支持所需发布语义时，不发布半成品，也不退回可覆盖目标的方式。封存摘要用于发现意外修改，不是数字签名；不承诺阻止拥有文件修改权限的人主动篡改清单。

## 状态取值

| 字段 | 取值 |
|---|---|
| `status`（客观题、考号、考号逐位） | `ok` / `blank` / `multiple` / `uncertain` / `partial` / `failed` |
| `status`（条码） | `ok` / `failed` / `ambiguous`（候选或归属有歧义） |
| `ocr_status` | `none`（未开启转写，或已放弃转写，见 `ocr_skipped`）/ `pending`（等待转写）/ `done` / `failed`（原因见 `ocr_error`） |
| `scan.faces.*.align_method` | `anchors` / `frames` / `features` / `derived` / `manual`（操作员手动点击参照点定位） |

## 采样与图片证据

新识别的 `scan.processing.evidence_version=2`。`resampling=perspective_linear`
说明整页和区域图经过几何纠偏、线性插值；`back_whitening=false` 表示背面
题图不再把浅灰像素自动抹白。图片仍输出灰度 JPEG，并非无损彩色原件。
`raw_stage=as_received` 指识别入口收到的图像：扫描仪驱动及已配置的软件
颜色滤除可能已处理过它，不能据此声称保留了滤除前的原稿。PDF 原文另存于
`sources/`。没有新证据版本的旧 state 按版本 1 处理，保持原来的裁图方式，
并记录 `back_whitening=true`、`back_whitening_level=185`；阈值重判不改写已有图片。

客观题、考号、自定义填涂字段、缺考和选做组可带 `sampling`：

```json
{"face": "front", "shift_mm": [0.127, -0.254],
 "reasons": [[null, "source_uncovered", null, null]]}
```

`shift_mm` 是相对模板的实际采样平移：整块网格与空白底图做局部匹配，最多平移 1.5 mm 且不超过最小格距的
40%（考号这类一行行重复的图案，错开半行左右也能“匹配上”），最佳位置落在搜索边界上时不平移（记为 0）；
`reasons` 按位/题、选项顺序保存，
`null` 表示有效，字符串说明无法使用的原因，例如 `source_uncovered`、
`base_out_of_bounds`、`work_out_of_bounds`、`derived_alignment`。
无效采样的 `fill` 为 JSON `null`，不能当作 0；受影响的题/字段为 `failed`。
缺考无法采样时 `value=null`，界面显示“未确定”。这些采样不进入阈值拟合。
客观题 `regions[].template_rect` 保留模板矩形，`rect` 是加上实际平移后的
裁图矩形；界面叠层使用同一平移。旧结果无采样元数据时按原模板位置读取。

条码 `image` 仍是模板原区域。新结果还提供 `candidates`、`selected_id`、
`hit_image`、`reason`。每个候选含 `id`、`value`、`symbology`、`face`、
`polygon`、`rect`、`located`、`distance_mm`、`image`、`image_rect`：
坐标为纠偏后的模板毫米，候选图片矩形在候选外接框四边各外扩 2 mm。
`id` 区分候选的物理位置及可能冲突的解码值。
同文本出现在不同位置时保留为不同候选。`selected_id` 和 `hit_image`
只在关联无歧义时填写。先分配原区域候选，再处理外扩、全页候选；弱位置证据
不能抢占强位置证据。同一层级多候选、距离接近或多个字段争用同一码时不自动
确定身份。`candidate_reserved` 表示候选已由位置更明确的字段占用，本字段不采用。
候选图进入交付清单并检查可读性、归属、位置和采用结果的一致性。
旧结果没有这些字段时，不能把原区域图片当成实际命中位置的证明。

识别警告允许随结果交付，但不等于已经人工核对。整批浅涂诊断不再要求先有
五个深涂样本；自适应阈值的保守拟合条件保持不变。少于 50 个有效选项或
不足 5 个达到诊断分组阈值的选项时，不作整批浅涂结论。

## 物理异常代码（不生成 result.json，写入 errors/）

| 代码 | 含义 | 现场处理 |
|---|---|---|
| `ALIGN_FAILED` | 所有面都无法定位、图片文件无法读取，或识别时发生内部错误（说明以"内部错误："开头） | 检查折角、放反，重扫 |
| `FACE_MISMATCH` | 能定位但与模板正反面对不上（扫错卡、混入其他纸） | 核对后重扫或作废 |
| `BLANK_PAGE` | 有一面几乎全白 | 检查是否漏进纸、双张 |
| `MISSING_FACE` | 图像数量不足一张卡的面数 | 按原序号整份重扫 |
| `SKEW_TOO_LARGE` | 倾斜超过 8° | 摆正后重扫 |
| `WRONG_CARD` | 混卡：定位成功，但模板标识区（`identity`，印有科目 / 卷别名称的印刷文字）与底图对不上，混入了其他科目或版本的答题卡 | 取出该卡，重扫正确的卡或作废；核对原图确是本科目的卡（例如学生在标识区写画）时，“确认无误”或手动定位后跳过标识检查重新识别，结果带 `IDENTITY_CONFIRMED` 警告 |

混卡检测由模板 `template.json` 的可选字段 `identity` 配置：列表，每项 `{"face": "front" | "back", "rect": [x, y, w, h]}`（mm，纠偏后的模板坐标），
框住印有科目 / 卷别名称、不会被书写的印刷文字。识别时把对齐后的卡面与空白底图在每个标识区内做局部相关比较
（100 DPI、轻度模糊、±1.5 mm 内搜索平移），可比较的标识区全部低于 0.75 才为 `WRONG_CARD`，只有部分低于时照常识别并加 `IDENTITY_PARTIAL` 警告；只由另一面推算定位的面不检查。
没有 `identity` 的旧模板照常读取，不做混卡检查（`schema_version` 仍为 `1.0`）。

模板的 `ignore`（忽略区，格式同 `identity`）不产生任何输出：模板编辑器自动检测定位块 / 外框 / 同步头时当作白纸，
识别时整页比对与特征匹配兜底定位不看这些区域；不影响填涂识别、切图和混卡检测。

## 数据警告代码（result.json 的 `warnings[].code`）

| 代码 | 含义 | `field` |
|---|---|---|
| `EXAM_NO_PARTIAL` | 考号部分位未识别（未涂/多涂/涂浅），对应位输出 `?` | `student.exam_no` |
| `EXAM_NO_BLANK` | 考号整体未填涂 | `student.exam_no` |
| `ABSENT_MARKED` | 缺考标记已填涂 | `student.absent` |
| `OBJ_BLANK` | 客观题未作答 | `objective.{题号}` |
| `OBJ_MULTIPLE` | 单选题多涂 | `objective.{题号}` |
| `OBJ_UNCERTAIN` | 涂得浅、擦除不净或与其他选项区分度不足 | `objective.{题号}` |
| `OBJ_FAILED` | 采样无效，答案不能确定 | `objective.{题号}` |
| `SAMPLE_INVALID` | 采样区域缺失、越界或只能推算定位；详见 sampling.reasons | 内部采样项键 |
| `ID_MISMATCH` | 填涂考号与考生条码不一致 | — |
| `BARCODE_MISSING` | 考生条码未识别（未贴或损坏） | `student.barcodes.student_barcode` |
| `ALIGN_FALLBACK` | 该面主定位失败，使用特征匹配兜底 | `scan.faces.{面}` |
| `ALIGN_MANUAL` | 该面由操作员在原图上手动点击参照点定位（自动定位失败后的补救），请核对识别结果 | `scan.faces.{面}` |
| `ANCHOR_MISSING` | 模板的定位块在扫描图上缺了一个（污损、涂写或印出纸边），已用其余 3 个按仿射定位；缺块的角附近精度略低，`scan.faces.{面}.missing_anchors` 列出缺失序号 | `scan.faces.{面}` |
| `ALIGN_DERIVED` | 该面所有定位方式都失败，按另一面的位置（双面扫描镜像关系）推算，只用于切图；该面的填涂项 `status` 为 `failed`，`scan.faces.{面}.align_method` 为 `derived` | `scan.faces.{面}` |
| `BARCODE_RELOCATED` | 条码区域内读不到，在外扩或整页搜索中找到；`located` 为 `expanded` 或 `page` | `student.barcodes.{键}` |
| `BARCODE_AMBIGUOUS` | 存在多个候选或位置归属冲突；保留候选图片，不确定单个值 | `student.barcodes.{键}` |
| `OCR_FAILED` | 手写转写失败（可用 `scan-answer ocr --retry-failed` 重试）；msg 为“手写转写失败：”加原因，原因与该项的 `ocr_error` 相同，可能以"内部错误："开头表示非预期异常。交付检查只以 `ocr_failed` 报告一次，不再重复列为识别警告 | `subjective.{题号}`（有小题号时为 `subjective.{题号}.{小题号}`）或 `student.fields.{键}` |
| `OMR_FIELD_PARTIAL` | 自定义填涂字段部分位未识别，msg 为"{label}有位未识别" | `student.omr_fields.{key}` |
| `OMR_FIELD_BLANK` | 自定义填涂字段整体未填涂，msg 为"{label}未填涂" | `student.omr_fields.{key}` |
| `CHOICE_BLANK` | 选做组未填涂 | `choose_one.{key}` |
| `CHOICE_MULTIPLE` | 选做组多涂 | `choose_one.{key}` |
| `CHOICE_UNCERTAIN` | 选做组涂得浅或擦除不净 | `choose_one.{key}` |

## result.json 字段

| 字段 | 说明 |
|---|---|
| `seq`、`batch_id` | 扫描序号、批次号 |
| `sheet_id` | `{batch_id}:{seq:04d}`，同一批次内稳定的答卷身份；重扫仍沿用该序号，不代替考生身份 |
| `input_pages[]` | 按输入顺序的页面来源：`kind`、原文件名 `file`、页码 `page`、原图路径 `raw`；PDF 另有 `document_id`（SHA-256）和原文件路径 `document`。路径相对 result.json 所在目录 |
| `template` | `{"name", "version"}` |
| `exam` | 扫描前填写的考试元信息（原样写入） |
| `scan` | `time`、`scanner`、`swapped`（正反面是否互换）、`faces.{面}`：`source`（第几张输入图）、`rotated`、`align_method`、`skew_deg`、`residual_mm`、`dpi`、`missing_anchors`（仅当模板的定位块在扫描图上缺了一个、改用其余 3 个按仿射定位时出现：缺失定位块的序号列表，从 1 开始）、`sync`（仅配置了同步头时出现：`found`、`total`、`max_offset_mm` 保留 2 位小数，全部未找到时为 `null`） |
| `pages` | `front`/`back` 为对齐后的整页图；`raw_1`/`raw_2` 为原图相对路径 |
| `student.exam_no` | `value`（未识别位为 `?`）、`status`、`digits`（只列出非 ok 的位）、`image` |
| `student.barcodes.{key}` | `value`、`symbology`、`status`、`image` |
| `student.absent` | `value`、`fill` |
| `student.fields.{key}` | `label`、`image`、`text`、`ocr_status`；转写失败时有 `ocr_error`（失败原因）；放弃转写时有 `ocr_skipped` |
| `student.consistent` | 考号与条码都识别成功时比较结果，否则 `null` |
| `objective[]` | `no`、`type`、`answer`（选中选项按顺序拼接）、`status`、`fill`（每个选项的涂黑度 0~1）、`question_id`、`image`、`images`、`regions`。所有状态都有题图 |
| `subjective[]` | `no`、`sub`、`type`、`content`、`question_id`、`images`（每个区域一张，按模板顺序）、`regions`、`text`、`ocr_status`、`ocr_model`、`ocr_confidence`；转写失败时有 `ocr_error`；放弃转写时有 `ocr_skipped` |
| `student.omr_fields.{key}` | `label`、`value`（未识别位为 `?`）、`status`（ok/blank/partial/failed）、`digits`（只列出非 ok 的位）、`image` |
| `choose_one[]` | `key`、`label`、`questions`、`chosen`（选中的题号列表）、`status`（ok/blank/multiple/uncertain/failed）、`fill`（每个题号的涂黑度） |
| `manual` | 预留人工校对，本期 `null` |
| `warnings[]` | `code`、可选 `field`、可选 `msg` |

## manifest.json

| 字段 | 说明 |
|---|---|
| `batch_id`、`exam`、`template`、`scanner`、`created` | 批次信息；`template.snapshot` 为快照路径 `template/template.json`（旧批次为 `template.json`） |
| `ocr` | 本批次是否开启手写转写 |
| `thresholds` | 当前判定阈值 `{"mark", "uncertain"}`：扫描中先用默认值，成功识别满 100 张后改用已扫部分的拟合值（之后每 50 张更新）；结束批次后为整批拟合的最终值 |
| `finalized` | 是否已结束批次。结束批次时用最终阈值逐张重新判定，判定有变化的卡会重写 `result.json`（保留已有转写结果）；未结束的批次中个别临界题的状态以后可能变化 |
| `counts` | `total`、`ok`、`physical`、`voided`、`warnings`（成功卡识别警告数之和，口径同 `sheets[].warnings`） |
| `threshold_issues` | 批次涂黑度分布的异常提示（拟合阈值和结束批次时更新）：`[{"code", "msg"}]`，code 为 `LIGHT_MARKS`（整批偏浅）、`LOW_SEPARATION`（已涂与未涂区分度低）、`THRESHOLD_CLAMPED`（阈值偏高，超过 0.60 被截断）；没有问题或样本不足时为空列表或缺省 |
| `ocr_progress` | 手写转写进度 `{"done", "failed", "total"}`，转写中约每 2 秒更新、转写结束时写入最终值；未开启转写的批次为 `null`。`scan-answer ocr` 命令运行后记录的是该次运行的进度 |
| `events[]` | 扫描仪事件：`time`、`kind`（`double_feed` / `paper_jam` / `no_paper` / `error`）、`message`、`after_seq`（事件发生时已分配的最大序号） |
| `sheets[]` | 每张卡：`seq`、`source`、`files`、`input_pages`、`status`（`processing` / `ok` / `physical` / `voided`）；这里 input_pages 的 raw/document 路径相对批次目录。成功时有 `result`、`exam_no`、`warnings`（识别警告数，不含 `OCR_FAILED`）；物理异常时有 `physical`、`message`；作废时 `voided: true` |

### 重扫与作废

- 只能重扫 `status` 为 `physical`（物理异常）或 `voided`（已作废）的卡，序号不变；重扫成功的卡不能再重扫。
- 只能作废 `physical` 的卡。

### 程序崩溃后重开批次

每张卡开始识别前先把 `processing` 条目写入 manifest。重开批次时按磁盘内容对账：

- 新序号从 manifest 与 `raw/`、`state/`、`students/`、`errors/` 中出现过的最大序号之后继续，不会复用。
- manifest 中为 `processing` 的条目、以及磁盘上有但 manifest 中没有的序号：有 `state/{seq}.json` 则恢复为 `ok`（`exam_no`、`warnings` 取自 `result.json`）；否则有 `errors/{seq}_{代码}/error.json` 则恢复为对应的物理异常；都没有则记为 `ALIGN_FAILED`，`message` 为“识别中断（程序退出前未完成），请重扫”。
- manifest 中原本没有的序号，`source` 与 `files` 取自 `raw/` 中的原图文件名（去掉序号前缀），没有原图时 `source` 为“恢复”。

## 手写转写

- 识别时提供转写配置（`recognize --ocr-config`）则主观题与信息栏的 `ocr_status` 为 `pending`，后台转写完成后回写 `text`、`ocr_model`、`ocr_confidence`。
- 模型直接输出转写内容（数学公式为 LaTeX，反斜杠原样保留），最后单独一行写“置信度: 0.xx”；`ocr_confidence` 即取自该行（0~1，没有该行或数值越界时为 `null`），该行不计入 `text`。
- 配置文件（JSON）：`{"base_url": "https://.../v1", "api_key": "...", "model": "...", "concurrency": 4, "timeout_s": 60, "max_retries": 2}`，接口为 OpenAI Chat Completions 兼容格式。
- 事后补做或重试：`scan-answer ocr --batch <批次目录> --config <配置> [--enable] [--retry-failed]`；有失败项时退出码为 1。
- 失败原因：`ocr_status` 为 `failed` 的项带 `ocr_error`（字符串），同时在 `warnings` 中有一条同文的 `OCR_FAILED`；转写成功或改回等待时两者一起清除。旧结果只有 `OCR_FAILED` 警告时，读取方（包括交付检查）以该警告的 msg 作为原因。
- 放弃转写：结果页“放弃未完成转写…”或 `scan-answer ocr-skip --batch <批次目录>` 把 `pending` 项改为 `none`，并写 `ocr_skipped: {"time", "reason"}`；交付检查不再阻断，以说明项 `ocr_skipped` 逐项列出（`summary.ocr_skipped` 为项数）。之后用 `--enable` 或“补做和重试转写”补做时去掉该标记。
- `scan-answer ocr` 只能对**已停止扫描**的批次运行：它会打开批次并做一次磁盘对账，若同时有程序在扫描同一批次，正在识别的卡会被误标为“识别中断”。

## 主观题小题号

`subjective[]` 每项含 `sub`（小题号，没有时为空字符串）。OCR 兼容原有的 `题号.小题号` 键。

新结果用独立的 `question_id` 标识题目：客观题为 `objective/{编码题号}`，主观题为 `subjective/{编码题号}/{编码小题号}`。题号和小题号分段，不再用替换下划线的方式构造图片名。

## 图片与来源的关联

- 图片按页面、身份、条码、信息栏、填涂字段、客观题、主观题分目录，避免自定义键覆盖整页图。
- 路径组件按 UTF-8 字节作百分号编码，只保留小写字母、数字、短横线和下划线。大写也编码，避免大小写不敏感文件系统把 `A` 与 `a` 当成同一文件。编码后超过 80 字符的组件使用 `~` 加完整 SHA-256 摘要，原始题号、小题号和字段键仍保存在 JSON。读取方应直接使用 JSON 提供的路径，不自行拼接文件名。
- 主观题路径示例：`images/subjective/q-56/sub-1/region-1.jpg`；没有小题号时目录为 `sub-`。
- 客观题路径示例：`images/objective/q-1.jpg`。正常、未答、多涂、存疑、推算面识别失败等状态都保留对应作答区域图。
- 每题的 `regions[]` 与 `images[]` 同序，包含 `face`、毫米矩形 `rect`、1 起始 `order` 和 `image`。由 `scan.faces.<面>.source`（1 起始输入索引）可定位 `input_pages[]` 中对应的原始页。
- 旧结果仍按其 JSON 中的旧路径读取；新增字段缺省时不影响查看，既有文件不自动迁移。重新判定会在需要更新结果时生成新路径。

PDF 来源示例（路径相对该学生的 result.json）：

```json
{
  "sheet_id": "batch_20261108_120000:0001",
  "input_pages": [
    {
      "kind": "pdf",
      "file": "answers.pdf",
      "page": 1,
      "document_id": "<sha256>",
      "document": "../../sources/<sha256>.pdf",
      "raw": "../../raw/0001_pdf_<摘要前16位>_000001.png"
    }
  ]
}
```

复制交付目录时保留整个批次目录，包括 `sources/` 和 `raw/`；只复制学生子目录不能保留这些来源引用。

## 目录命名

- 考试目录名按设置中的“考试目录命名”规则生成（默认 `{考试名}_{科目}_{时间}`）。
- 学生目录名为 `{序号4位}` 或 `{序号4位}_{后缀}`，后缀按批次 manifest 中的 `student_dir_pattern` 生成（默认 `{exam_no}`，考号没有完整识别时为空）。业务系统应以 `result.json` 内容为准，不要依赖目录名解析考号。
