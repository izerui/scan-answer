# 工作包 1：文档输入与逐题图片

用户已确认主线并于 2026-11-08 授权开始。本设计落实已确认的第一个工作包；采用当前 Python/PySide6 工程和既有 8K 模拟体系。

## 范围

- 正式识别与试识别接受图片、PDF 或包含它们的目录。
- 所有客观题和主观题都有作答区域图片与题目身份。
- 原始 PDF、页码、页面图和答卷结果可追溯，批次移动后对外相对路径仍可使用。
- 模板物理尺寸保持参数化，适用 A3 及以下，不将 390×270 mm 的现有 8K 样卡写死为通用尺寸。
- 保留既有 JSON 主体字段和旧结果读取能力；新增字段，不实现评分、业务推送或 ZIP。

本包不重做扫描仪生命周期、OCR 管理、跨面模板编辑等后续工作包。

## 实现选择

继续使用 `SheetImages` 和现有流水线，新建 `DocumentSource` 作为图片/PDF 统一输入。选择直接逐份渲染 PDF，而不预先将整份文档全量转换到临时目录：保留分页来源，控制持有图像数量，也避免用户手工转换。

保留 `FolderSource` 的既有图片接口，供旧调用方兼容。GUI 和批量入口使用新的输入源。图片仍按现有文件名规则配组；PDF 按文件内页序、模板面数配组，两个 PDF 不互相补页。

## 输入与来源

- `DocumentSource(inputs, faces, dpi=300)` 接受文件、目录或文件列表，独立处理每个 PDF。
- PDF 使用现有运行依赖 pypdfium2；逐页渲染灰度图，及时关闭 page/bitmap/document，输出数组自行持有像素。
- 不完整末组照常输出给引擎判定 `MISSING_FACE`。坏 PDF、密码保护或渲染失败给出文件/页码错误，停止本次输入；已写出的答卷保留，不能将该次输入误标为正常完成。
- `SheetImages` 新增可选 `origins` 和 `documents`，不影响现有图像源。origins 与页面一一对应，包含类型、原文件名、PDF 页码、文档内容摘要。documents 将摘要映射到原 PDF 文件。
- 批次把原 PDF 按 SHA-256 摘要存入 `sources/`，同内容仅存一份；渲染页保存到 `raw/`。来源写入 manifest、state 和 result，result 中路径相对学生目录。
- 答卷身份继续以 `(batch_id, seq)` 为作用域，新增易于引用的 `sheet_id`。不宣称它是跨任意独立数据集的全局 UUID。

## 逐题图片和身份

统一由 `output/evidence.py` 生成路径和题目 ID。对路径组件使用可逆百分号编码，并按类别隔离：

```text
images/pages/front.jpg
images/identity/exam_no.jpg
images/barcodes/key-student_barcode.jpg
images/fields/key-name.jpg
images/omr_fields/key-paper_type.jpg
images/objective/q-1.jpg
images/subjective/q-56/sub-1/region-1.jpg
```

主观题的题号、小题号分成独立路径组件，防止 `56.(1)` 和 `56.[1]` 被下划线替换后碰撞。所有图片写入统一检查重复路径。

组件的大写字母也编码，避免大小写不敏感文件系统合并不同键；编码后超过 80 字符时用 `~` 和完整 SHA-256 作为稳定短名，原始逻辑身份保留在 JSON。

- 客观题无论正常、空白、多涂或存疑均裁图。
- `objective[]` 保留 `image`，增加 `question_id` 和有序 `images`。
- `subjective[]` 保留 `no/sub/images`，增加 `question_id`。
- 两类题增加 `regions`，记录每张图片对应的面和模板毫米矩形。
- `result.input_pages[]` 保存来源与原图对应关系，原有 `pages.raw_1/raw_2` 保留。
- 最终阈值重判保留来源信息并刷新全量题图；旧结果引用旧文件仍可读取。

## 交互

扫描页图片输入改为“文件导入”，保留目录选择并增加文件选择；可以输入单个 PDF/图片路径。沿用原 `start_folder` 入口以避免破坏旧调用方。

试识别文件选择接受 PDF，经相同输入源分页后调用相同识别核心。保留现有结果列表与对齐叠层，显示 PDF 页码。完整的逐题预览与模板修订确认归下一工作包。

## 验证边界

先固定新增行为的失败用例，再实现：正常题裁图、路径碰撞、来源保留、PDF 跨文件不配组、奇数末页、损坏输入、GUI 文件入口、PDF 试识别。复用已有 8K 数据生成器做小批量端到端检查，并运行原快速测试。A3/A4/A5 和 8K 的 PDF 页面尺寸通过小型参数化输入用例检查，不将纸幅检查冒充全版式识别验收。完整真机与性能验收留待后续。
