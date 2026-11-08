# 软件设计与实现一致性审查报告

日期：2026-11-08  
审查基线：`d169ace0a9800c328b45bc03499bc607a36961fc`（本地 `main`）  
审查性质：需求追溯、架构和代码静态审查；不是功能验收或准确率报告。

## 1. 结论与重点

**产品主线没有整体跑偏，主要模块也按原架构落地；但“功能都有入口和代码”不等于“最初设计的完整业务流程已经一致实现”。**

项目仍然是答题卡采集与识别工具：PySide6 桌面界面、Windows TWAIN 扫描、跨平台图片导入、空白卡模板、OMR 和条码、主观题切图、可选云端 OCR，以及本地 JSON 和图片输出。没有擅自扩展成评分、阅卷或考生业务裁决系统，不需要推倒重写。

主要差距集中在三处：

1. **后补功能没有全部接回原架构的统一管理。** 扫描仪增加了独立采集队列，但原来的识别背压没有覆盖它；结果页增加了 OCR 重试，却绕过控制器已有的队列、进度和退出管理。
2. **通用性停留在模型支持，部分编辑流程没有闭合。** 模型能表示跨面主观题、选做组和多种定位方式，但用户从一张新空白卡制作模板时，仍会遇到没有对应操作路径或参数联动的地方。
3. **输出与状态契约不够完整。** 图片命名、条码候选归属、最终阈值重判定、模板版本和 JSON 枚举，都有不能只靠“字段已增加”解决的语义问题。

建议优先处理下文 R01—R08，它们涉及配卡身份、图像证据、后台任务所有权和结果可信度。R09—R16 属于模板通用性、契约、交付和文档一致性的补齐。这里的优先级是设计修正优先级，不表示本次已在真机复现故障。

## 2. 审查范围与依据

本次没有运行应用、功能测试、测试套件、合成数据探针、构建或扫描仪调用；没有修改功能代码。只新增本报告。

依据分四层，不能把最早的草案逐字当作最终需求：

| 层次 | 本次采用的依据 |
|---|---|
| 用户目标和明确决策 | Claude Code 会话 `56363507-3fcc-4b60-919a-81025936d93e`，从 2026-11-06 最初需求到 2026-11-08 的补全指令 |
| 最初总体设计 | [scan-answer 设计文档](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md) |
| 分期实施与后续变更 | [识别核心计划](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-06-recognition-core.md)、[流水线与 OCR 计划](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-pipeline-ocr.md)、[GUI 计划](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-gui-pages.md)、[模板编辑器计划](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md)、[剩余功能设计](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md) |
| 当前实现与接口说明 | `src/scan_answer/` 的主流程及相关实现、`packaging/`、[输出格式](/Users/liuyuhua/github/scan-answer/docs/output-format.md)、[使用说明](/Users/liuyuhua/github/scan-answer/docs/gui.md)、[打包说明](/Users/liuyuhua/github/scan-answer/docs/packaging.md) |

并行审查覆盖模板与编辑器、识别与输出、扫描仪与打包；主审复核需求变更、批次生命周期、OCR 重试与关键证据。以下“静态确定”是指从分支、数据流或状态转换可以确定代码行为，不代表已测得发生概率。

## 3. 从原始需求到当前实现

| 最初方向 | 当前对应实现 | 一致性判断 |
|---|---|---|
| 只采集和识别，不批改、不阅卷 | `engine/` 输出答案、状态、涂黑度；没有评分模型，`manual` 保持预留 | 主线一致 |
| Windows 扫描仪；Mac 可用图片开发与使用 | `TwainSource` 与 `FolderSource` 共用 `SheetImages / DeviceEvent` | 接口方向一致；设备交接和配对保护需补齐 |
| A3/8K、双面、300 DPI 默认、最高 600 DPI | 纸张尺寸进入模板，采集参数与工作图 DPI 分离 | 配置与模型已覆盖；未据此认定硬件支持或吞吐量达标 |
| 不同考试可自行制作模板 | `TemplateDoc.new`、PDF/图片导入、区域模型、画布与属性编辑 | 通用入口真实存在；跨面追加、定位切换等操作仍有缺口 |
| 单选、多选、判断、考号和自定义填涂 | `engine/omr.py`、`recognize.py` 与相应模板模型 | 主路径一致 |
| 填空、问答、作文按区域保留图片，可选转写 | 有序 `regions`、`images[]`、OCR 多图输入 | 引擎表达一致；编辑器跨面制作路径及图片唯一性需补齐 |
| 条码与填涂考号同时保留，冲突交给业务系统 | 两种身份独立输出，并给 `consistent`/警告 | 常规流程一致；全页条码关联存在候选信息损失 |
| 物理异常现场重扫/作废，数据异常只标记 | 批次状态、错误目录、重扫保持序号、结果警告 | 图像识别异常流程基本一致；设备中断恢复还缺身份闭环 |
| 扫描、识别、OCR 解耦 | 专用采集线程、流水线线程、`spawn` 进程池、OCR 线程池 | 分层存在；背压和新 OCR 入口未统一 |
| 本地 JSON 和相对路径图片，方便业务系统处理 | `LocalExporter`、`result.json`、`manifest.json`、模板快照 | 基础输出一致；正式契约与可替换导出接口未完整落地 |
| 五个工作页面和浅色区域色码 | 考试、扫描、模板、结果、设置均存在 | 页面结构一致；本次未做视觉验收 |
| Windows 安装包、Mac 应用包 | PyInstaller onedir 与 macOS BUNDLE 配置 | Mac 产物形式一致；Windows 当前是便携目录包，不是安装器 |

原架构的模块关系仍成立：

```text
UI / 应用控制器
    -> 图像源（文件 / TWAIN）
    -> 批次流水线（原图落盘、序号、任务调度）
    -> 识别引擎（定位、OMR、条码、切图、结果）
    -> 本地输出
    -> 可选 OCR 队列与回写

模板数据层为识别与编辑器共用。
```

需要修正的是模块之间的契约和少数绕行路径，不是重选技术栈或重建全部模块。

## 4. 有依据的设计演进，不应误判为遗漏

| 变更 | 依据与判断 |
|---|---|
| HTTP 推送等移到后期 | 用户最初讨论后明确先做本地输出。没有推送不是一期漏项。 |
| GUI 与模板编辑器拆成两份计划，扫描仪顺延为计划 5 | 会话中用户选择拆分；旧文档的“计划 4 扫描仪”属于历史编号。 |
| 扫描中默认阈值，满 100 张拟合，结束批次统一重判 | 用户于 11 月 7 日 07:50（北京时间）明确选择；`session.py` 和 `runner.py` 已有实现。 |
| 旧批次 OCR 在后台继续转完 | 用户于 11 月 7 日 18:34 明确选择；控制器已有后台队列管理。R06 指的是新重试入口没有复用这套管理。 |
| 同步头只测偏移，不参与逐行校正 | 用户于 11 月 7 日 19:24 明确选择；[模板计划第 22 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:22)记录原因。不是漏做校正。 |
| 底图相关性替代 `fingerprint` | 在[计划偏差表](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:553)中记录；职责仍是判定方向和正反面。 |
| 单个选项格不独立拖动 | 在[计划偏差表](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:545)中明确收缩为整块调整。应作为已披露限制，不当作隐藏遗漏。 |
| 主观题输出有序多图，不合成长图 | 最终总体设计的 JSON 示例本身使用 `images[]`，OCR 也按顺序传多图。早期聊天提过拼成一张，后来数据契约已具体化；需要统一措辞，不必强行增加长图。 |
| 密集括号格拒绝不可靠吸附 | 是修复误吸附后的保守限制；199 原考号格已恢复正确坐标。它会限制模板制作便利性，但不能为了成功率重新放宽为错误成功。 |
| 原图键为 `raw_1/raw_2`、DPI 放在逐面信息、快照放在批次内 | [识别核心偏差表](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-06-recognition-core.md:2938)已有记录；可通过 `source` 映射到正反面。 |
| 后期停止新增测试和完整测试运行 | 是原会话中的用户阶段指令。本报告不把未运行测试本身归类为实现跑偏，也不据此认定功能正确。 |

以上变更中，“用户明确选择”和“计划已披露的范围收缩”证据强度不同，报告保留这种区别。

## 5. 需要处理的不一致

### R01｜高优先：双面采集与配卡身份没有形成强约束

- **原要求：** 一名学生一张纸，正反面属于同一份结果；卡纸、双张后可按原顺序继续。见[主设计第 177 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:177)、[第 202 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:202)。
- **实现：** 双面开关和模板面数独立；`CAP_DUPLEXENABLED` 设置失败仅加警告，后面仍按模板 `faces` 数量分组。见 [twain_source.py:274](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:274)、[twain_source.py:408](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:408)。
- **缺口：** 双面模板配合单面进纸或双面设置失败时，采集层仍可能把相邻两张纸作为一份输入。后面的正反面匹配可能拒绝其中一些情况，但不能替代采集身份约束。中断留下的半张卡会生成异常记录，放回整纸后“继续”又分配新序号，没有显式关联旧异常。
- **分类：** 关键业务约束遗漏；补充设计的“顺序配对”没有完整覆盖上位恢复流程。
- **建议：** 开始前校验模板面数与设备实际模式，关键能力无法满足时阻止采集；定义待恢复卡的序号、已收页面、替换/作废关系，区分“补纸继续”和“重扫某一份”。

### R02｜高优先：高速扫描的背压只覆盖识别端

- **原要求：** 采集和识别并行，600 DPI 要控制并发及内存。见[主设计第 78 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:78)。
- **实现：** 流水线限制在途识别任务为 `workers * 2`，但 TWAIN 使用无界 `queue.Queue()` 传整页数组；暂停只阻塞消费者，扫描线程没有对应的暂停协议。见 [runner.py:136](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/runner.py:136)、[twain_source.py:392](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:392)、[control.py:24](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/control.py:24)。
- **缺口：** “采集比识别快”或暂停时，采集队列没有内存上限；原图尚未到流水线保存前仍仅在内存中。代码中未见按纸幅、色彩和 DPI 计算采集预算。这是静态可见的无界路径，不是本次已测得内存耗尽。
- **分类：** 非功能设计遗漏。
- **建议：** 明确采集缓冲预算和高水位行为，优先在采集后独立落盘、队列传路径；设计设备暂停与缓冲保留策略，而不是仅在驱动回调中无限等待。

### R03｜高优先：停止请求与设备真正释放没有分开

- **原要求：** 停止后释放数据源、DSM 和隐藏窗口，同一设备不能并发开启；结束本轮后允许重扫。见[补充设计第 35 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:35)、[第 76 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:76)。
- **实现：** `ControlledSource.sheets()` 停止后直接返回，没有显式关闭内部迭代器；TWAIN 的 `finally` 只发送 `shutdown`，没有释放完成确认。`ScanController.scanning` 根据停止标志就返回假。见 [control.py:47](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/control.py:47)、[twain_source.py:436](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:436)、[controller.py:276](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/controller.py:276)。
- **缺口：** 命令队列串行不等于外部设备线程已关闭。重扫流程缺少“旧设备会话已释放”的前置条件；实际驱动是否出现冲突需另行验证。
- **分类：** 生命周期设计未闭合。
- **建议：** 引入停止中、已排空、设备已释放、释放失败等状态或完成信号；在消费线程结束迭代器，确认设备释放后再接受下一次采集。

### R04｜高优先：跨面主观题有模型，没有完整的新建操作路径

- **原要求：** 一题可跨面、多区域，且能指定区域顺序。见[主设计第 104 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:104)。
- **实现：** 画框只会追加到当前选中的主观题；切面重建画布并清空选择，区域树只列当前面的区域，属性列表主要提供删除。见 [template_page.py:537](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/pages/template_page.py:537)、[canvas.py:312](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/editor/canvas.py:312)、[props.py:415](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/editor/props.py:415)。
- **缺口：** 内置 199 模板已经带有跨面区域，能读取它不等于用户能从零创建同类模板。只有正面区域的题，切到背面后没有明确入口选它作为追加目标；也没有区域顺序调整操作。
- **分类：** 用户工作流遗漏。
- **建议：** 新画区域后可选择全模板中的题目；跨面保持目标题，或提供统一题目列表；为区域列表提供顺序调整。

### R05｜高优先：模板标识与图片路径没有统一的唯一性约束

- **原要求：** 每题图片对应正确，字段可自定义，缺陷应在保存前拦下；小题号采用完整唯一键。见[模板计划第 45 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:45)、[补充设计第 201 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:201)。
- **实现：** 各类别内检查键重复，但图片共用 `images/`；信息栏键直接成为文件名，主观题键经替换字符后成为文件名。见 [validate.py:168](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/validate.py:168)、[model.py:90](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/model.py:90)、[result.py:39](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/result.py:39)。
- **静态例子：** 信息栏键 `page_front` 会与整页图片使用同一字典键；主观题键 `56.(1)` 与 `56.[1]` 均转换为 `56._1_`。字典后写入项覆盖先写入项，之后两个字段可以引用同一张错误图片。这是代码规则推演，未执行样例。
- **另一个校验缺口：** 定位矩形未纳入统一矩形检查，完整网格结构检查只覆盖自定义填涂字段；客观题还可能在检查标签数量前访问 `labels[j]`。见 [validate.py:49](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/validate.py:49)、[validate.py:75](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/validate.py:75)、[validate.py:182](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/validate.py:182)。
- **分类：** 数据契约和保存校验遗漏，影响作答证据完整性。
- **建议：** 区分用户显示名、逻辑 ID 和存储路径；各类图片分命名空间，采用无碰撞编码；先完成全部结构校验，再生成路径与提示文字。空键、非法名称及路径冲突在保存时统一拒绝。

### R06｜高优先：结果页 OCR 重试绕开统一任务生命周期

- **原要求与变更：** OCR 后台排队、并发受控、进度可追踪；用户明确要求旧批次转写继续完成，关闭时由控制器统一处理。见[主设计第 275 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:275)、[controller.py:8](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/controller.py:8)。
- **实现：** 结果页单项重试自行启动线程，批量重试自行创建没有批次进度监听器的 `OcrQueue`，都没有注册到控制器的当前/后台队列。见 [results_page.py:338](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/pages/results_page.py:338)、[results_page.py:398](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/pages/results_page.py:398)、[controller.py:209](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/controller.py:209)。
- **缺口：** 控制器的退出提示、取消、批次重开隔离和进度统计不包含这些任务；单项入口没有同项在途去重。“重试全部失败项”还调用 `enable_ocr`，会启用此前未转写的项，行为范围超过按钮名称。
- **分类：** 后补功能造成的架构偏离；部分行为符合后续补充设计，却与统一生命周期原则不一致。
- **建议：** 把重试交给应用层 OCR 任务服务，统一批次、题目、代次、并发、进度和关闭管理；将“重试失败项”与“补做未开启项”明确分开。

### R07｜高优先：`finalized` 尚不能完整表达最终判定一致性

- **原要求：** 结束批次时按整批最终阈值重新判定，临界结果以最终值为准。见[输出文档第 88 行](/Users/liuyuhua/github/scan-answer/docs/output-format.md:88)。
- **实现缺口一：** `_decision_key` 比较整体答案、值和状态，不比较考号及自定义字段的 `digits`；某位从空白变存疑，但整体仍是同一个含 `?` 的值及 `partial` 时，可直接返回而不更新逐位状态。见 [worker.py:128](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/worker.py:128)、[worker.py:170](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/worker.py:170)。
- **实现缺口二：** 单张最终重判异常时记录事件后继续，最后仍设 `finalized=True`，没有批次级的部分失败状态或失败序号汇总。见 [runner.py:183](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/runner.py:183)。
- **分类：** “最终结果”的状态契约未完整细化。
- **建议：** 定义完整的判定字段更新范围；区分采集结束、判定完成、部分重判失败、OCR 完成。对失败序号保留可重试状态，不能只让消费者看到一个无条件的最终标志。

### R08｜高优先：条码全页搜索完成了，但歧义和证据被压缩

- **原原则：** 能识别的内容尽量全部输出，冲突标记、不静默裁决。见[主设计第 17 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:17)。
- **后续设计与实现：** 按最近距离将全页候选分配给条码字段；同值条码按文本去重，输出只保留选中的一个值并标为 `ok`。见[补充设计第 185 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:185)、[barcode.py:30](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/barcode.py:30)、[recognize.py:350](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/recognize.py:350)。
- **缺口：** 空间匹配可以是合理识别方法，但多候选、同值不同位置或竞争归属的证据不应丢失。全页命中后图片仍按原模板条码框切，不能保证包含实际命中条码，见 [result.py:43](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/result.py:43)。
- **分类：** 后续细化设计与上位“不静默出错、数据尽量全”原则存在张力。
- **建议：** 保留候选坐标、码制、归属评分和歧义；最近邻作为建议关联，歧义给明确状态；同时输出实际命中位置切图，保留原框切图用于追溯。

### R09｜中优先：模板参数修改还没有统一维护模型约束

- **定位方式：** 自动检测失败时默认 `anchors`；手动画外框只增加 `frames`，不切换 `align.method`，区域枚举又只看当前方式。因此手动画外框不能自然接替检测失败的定位块。见 [doc.py:150](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:150)、[doc.py:93](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:93)、[doc.py:413](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:413)。原依据是[计划第 544 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:544)的手动定位修正。
- **选做组：** 题号数改变只更新 `questions`，未按计划重建 `cells`；默认选题取 `s.no`，没有使用新增的 `s.key`，带小题号时可能生成无效引用。见 [doc.py:489](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:489)、[doc.py:716](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:716)；对应[计划第 449 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:449)。
- **分类：** 实现遗漏，不是已经声明的功能收缩。
- **建议：** 在 `TemplateDoc` 中提供明确的定位方式切换、题目引用和网格重建操作，界面只发送意图，避免参数间依赖散落在控件中。

### R10｜中优先：模板保存的资源引用和版本身份不够稳定

- **原要求：** 模板可保存、另存、带递增版本，不损坏已有模板。见[模板计划第 46 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:46)、[第 547 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/plans/2026-11-07-template-editor.md:547)。
- **资源引用：** 读取按 `face.image`，保存底图却固定为 `{face}.png`，未同步修改模型引用；非默认文件名的合法模板另存会产生引用与文件不一致。见 [io.py:71](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/io.py:71)、[io.py:85](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/io.py:85)。
- **版本身份：** 撤销快照包含版本号；版本 2 编辑后保存为 3，再撤销恢复版本 2、保存仍为 3，是当前状态转换允许的路径。见 [doc.py:864](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:864)、[doc.py:876](/Users/liuyuhua/github/scan-answer/src/scan_answer/template/doc.py:876)。
- **分类：** 持久化契约偏离。
- **建议：** 明确资源路径规范，保存时同步处理文件和引用；版本号作为持久化元数据独立于撤销内容。现有 `os.replace` 是单文件原子替换，不应宣称整套底图与 JSON 已具备事务回滚。

### R11｜中优先：整批偏浅的提示条件存在需求盲区

- **原要求：** 整批铅笔偏浅或扫描偏亮时应提示。见[主设计第 191 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:191)。
- **实现：** 提示函数沿用阈值拟合的前置条件：至少五个涂黑度达到 0.5 的样本，否则直接返回无问题。见 [omr.py:122](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/omr.py:122)。
- **缺口：** 即使总样本很多，若已涂项普遍只有约 0.4，也会走“样本不足”的空列表分支。“无法可靠拟合”被表达成了“没有批次提示”。
- **分类：** 需求场景定义遗漏。实现遵循了补充设计的简化条件，需先修正设计条件。
- **建议：** 将样本数量不足、没有深涂样本、两组无法区分和正常分布分开；不能用判断拟合是否可用的门槛替代质量诊断。

### R12｜中优先：统一 JSON 契约和溯源信息尚未完整定稿

- **原要求：** 尽量完整输出，JSON 结构可被业务系统稳定消费；主设计明确提出 JSON Schema 校验。见[主设计第 328 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:328)。
- **实现与文档：** 有项目自有字段及 `schema_version="1.0"`，但未找到正式输出 JSON Schema。输出表中的定位方式、填涂字段和选做组状态，没有完整覆盖新增的 `derived` / `failed`。见 [output-format.md:26](/Users/liuyuhua/github/scan-answer/docs/output-format.md:26)、[output-format.md:76](/Users/liuyuhua/github/scan-answer/docs/output-format.md:76)、[recognize.py:448](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/recognize.py:448)。
- **溯源缺口：** 文档称信息栏和主观题都回写 OCR 模型及置信度，信息栏分支实际只保存文字和状态。见 [output-format.md:110](/Users/liuyuhua/github/scan-answer/docs/output-format.md:110)、[queue.py:33](/Users/liuyuhua/github/scan-answer/src/scan_answer/ocr/queue.py:33)、[worker.py:149](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/worker.py:149)。
- **分类：** 接口契约遗漏和文档漂移，不是 JSON 语法非法。
- **建议：** 明确这是项目对外数据协议，提供与实现一致的 Schema、枚举、空值和兼容规则；补齐信息栏转写来源。不要仅凭字段名或版本字符串把“标准 JSON”理解为已符合某个外部业务标准。

### R13｜中优先：UI 与后台处理边界仍有同步阻塞路径

- **原要求：** UI 主要处理预览和交互，耗时处理在后台。见[主设计第 78 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:78)、[第 295 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:295)。
- **实现：** 扫描仪探测内部新建线程，但调用方随后 `join(timeout=20)`；页面构建和刷新仍同步调用。结果页批量重试还在界面线程遍历结果、改 JSON 并投递全部任务。见 [twain_source.py:94](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:94)、[scan_page.py:254](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/pages/scan_page.py:254)、[results_page.py:386](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/pages/results_page.py:386)。
- **缺口：** “内部用了线程”不等于调用方不阻塞。慢驱动或大批次时存在可静态识别的界面等待路径，实际时长未测。
- **分类：** 架构边界逐步弱化。
- **建议：** 将完整的探测、枚举和批量准备操作交给应用服务异步执行；用任务 ID、信号和取消状态回传结果，避免 UI 直接等待或批量读改持久化数据。

### R14｜中优先：设备设置还缺请求值、实际值与能力失败的区分

- **原要求：** 扫描参数可配置，驱动不支持时明确提示。见[主设计第 171 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:171)、[补充设计第 223 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:223)。
- **实现缺口：** 关闭双张检测没有对应的设置动作；滤色“无”设置失败被忽略；模板裁切换算成英寸但未设置或确认设备单位，也未关闭之前的自动裁边能力。见 [twain_source.py:278](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:278)、[twain_source.py:293](/Users/liuyuhua/github/scan-answer/src/scan_answer/source/twain_source.py:293)。
- **分类：** 能力协商设计不完整。缺少调用可从代码确定，实际设备后果取决于驱动，不能在本报告中声称真机已失效。
- **建议：** 每轮配置明确单位和互斥状态，记录请求值与确认值；区分影响配卡正确性的必需能力和可降级能力，复位失败也给出提示。

### R15｜较低优先：扩展接口与交付物存在承诺差距

- **导出器：** 主设计称已预留统一导出接口，但 worker 直接实例化 `LocalExporter`，最终重判又绕过它写图。当前是本地输出模块，不是可直接替换的导出接入点。见[主设计第 74 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:74)、[worker.py:83](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/worker.py:83)、[worker.py:172](/Users/liuyuhua/github/scan-answer/src/scan_answer/pipeline/worker.py:172)。
- **安装包：** 主设计要求 Windows 安装包，后续设计和实现提供 onedir 目录包，没有安装器生成步骤。见[主设计第 46 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:46)、[scan-answer.spec:93](/Users/liuyuhua/github/scan-answer/packaging/scan-answer.spec:93)。
- **分类：** 架构预留未完全实现、后续交付范围收缩未统一说明。不是要求现在实现第二期上传功能，也不是构建失败结论。
- **建议：** 首次写出与更新至少共用一个输出服务；正式决定 Windows 交付便携目录包还是安装包，更新交付清单。

### R16｜中优先：缺少统一的当前设计基线

- **可确认的冲突：** 总设计仍为“待用户审阅”；补充设计前文把颜色滤除排除，第 6 节又实现它；推算定位门槛文档写 0.15，代码为 0.30，且多出 ±40 mm 平移校正；主窗口仍显示“扫描仪接入在后续版本”。
- **证据：** [主设计第 4 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-06-scan-answer-design.md:4)、[补充设计第 141 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:141)、[第 176 行](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-07-remaining-features-design.md:176)、[recognize.py:24](/Users/liuyuhua/github/scan-answer/src/scan_answer/engine/recognize.py:24)、[main_window.py:236](/Users/liuyuhua/github/scan-answer/src/scan_answer/ui/main_window.py:236)。
- **另外需要统一的语义：** 彩色软件滤色在最新设计中明确保存处理后的灰度图为原图；这符合后续实现方案，但“原始采集证据”与“处理后识别图”应分开定义。推算定位输出 `residual_mm=0` 也不等于测得零误差，应明确“未测量”与实际残差。
- **分类：** 设计变更未集中同步。
- **建议：** 保留历史文档，但建立一个权威的当前版本设计；每个需求标注“保留、替代、收缩、未完成、后期”，并链接代码位置和决策记录。避免后续继续凭“7 项已提交”推导“一期与原设计完全一致”。

## 6. 建议先完成的设计整理

这里是报告建议，不是本轮已经实施的改动：

| 顺序 | 需要定清的内容 | 涉及问题 |
|---|---|---|
| 1 | 采集事务：一张纸的身份、实际双面模式、半张卡恢复、暂停/停止/设备释放、缓冲落盘与内存预算 | R01—R03、R14 |
| 2 | 模板契约：区域逻辑 ID、跨面题目标识、图片路径唯一性、完整保存校验、版本与资源引用 | R04、R05、R09、R10 |
| 3 | 后台任务契约：OCR 首次执行和重试共用所有权、并发、批次进度、退出处理 | R06、R13 |
| 4 | 结果契约：最终判定状态、条码候选与证据、诊断提示、正式 Schema 和来源信息 | R07、R08、R11、R12 |
| 5 | 当前范围与交付：安装包形式、导出扩展点、保留限制、文档更新 | R15、R16 |

建议保持现有技术路线，不为解决这些问题引入新的前端框架、服务器或大型消息系统。先补清上述契约，再在现有模块边界内修实现，避免一边补按钮一边产生第二套后台管理。

## 7. 保留限制与后期范围

不建议把下面事项混入本次一期一致性缺陷清单：

- macOS 扫描仪接入、考生名单比对、人工修改识别结果、一个学生多张纸、从制卡系统直接导入坐标。
- ZIP 导出、HTTP 推送、对象存储上传和业务系统对接。
- 已在计划中披露的单格拖动不做、密集考号格不能自动吸附、同步头只测量不校正。

但是，“为后期预留接口”如果尚未真正形成，应像 R15 一样准确描述，不能把模型存在或模块命名当成扩展点已完成。

## 8. 本报告没有证明的事情

本次没有验证扫描仪实际行为、识别正确率、不同卡版式适配率、OCR 模型效果、UI 实际交互和视觉表现、600 DPI 内存及吞吐量、安装包启动和跨平台兼容性。

历史会话中的局部探查与四张合成卡跑通只能作为已有活动记录，没有作为本次静态审查的通过依据。也没有将历史“测试未跑”直接列成代码问题。

**最终判断：方向正确、主体架构可保留；当前需要补的是采集与后台任务状态设计、通用模板操作闭环、数据输出契约和设计变更记录。尚不适合仅凭功能清单把一期标为“与最初设计完全一致”。**
