# 剩余功能详细设计：扫描仪接入、Windows 打包与界面补全

- 日期：2026-11-07
- 上位文档：`docs/superpowers/specs/2026-11-06-scan-answer-design.md`（第 2、5、8 节）
- 范围：原计划 5（TWAIN 扫描仪、Windows 打包），以及计划 3 明确延后的界面功能。按用户要求，本阶段只写功能代码，不写测试，测试由他人负责。

---

## 1. 扫描仪接入（TWAIN，仅 Windows）

### 1.1 依赖与可用性
- `pyproject.toml` 运行依赖增加 `pytwain>=2.3; sys_platform == 'win32'`。pytwain 是纯 Python + ctypes 实现，需要系统里装有 TWAIN DSM（TWAINDSM.dll，扫描仪驱动一般会带）。
- `source/twain_source.py` 中的 `twain_available() -> tuple[bool, str]`：
  - 非 Windows 返回 `(False, "扫描仪仅支持 Windows")`；
  - `import twain` 失败返回 `(False, "未安装 pytwain")`；
  - 加载 DSM 失败返回 `(False, "未找到 TWAIN 驱动管理器（TWAINDSM.dll）")`；
  - 都成功返回 `(True, "")`。
- 界面上“扫描仪”按钮是否可用，由这个函数决定；不可用时，提示文字就是上面的原因。

### 1.2 线程模型
TWAIN 的数据源只能在打开它的那个线程里工作，而且这个线程必须运行 Windows 消息循环（pytwain 的 `_modal_loop` 用 `GetMessage(0)` 取的是当前线程的消息）。流水线则在 `ScanController` 的后台线程里通过迭代器取卡。因此：

- `TwainSource` 内部启动一个专用的**扫描线程**（`_ScannerThread`）。由它创建隐藏的消息窗口（`CreateWindowExW`，父窗口为 `HWND_MESSAGE`），把 HWND 交给 `twain.SourceManager(parent_window=hwnd)`，打开数据源，设置能力项，执行采集。
- 扫描线程与流水线之间用 `queue.Queue` 传递数据。扫描线程把每一面放进队列：`_Page(image, dpi)`；一轮采集结束放 `_RoundEnd(reason)`；出错放 `_Failure(kind, message)`。
- `TwainSource.sheets()` 运行在流水线线程，从队列里取页面，按模板的面数组装成 `SheetImages(images=..., dpis=..., names=["{n:06d}_1.jpg", ...])` 后 yield 出去。
- **一轮采集**：一次 `acquire_natively(show_ui=False)`，扫描仪把进纸器里的纸全部扫完后返回，或者因卡纸、双张而中止。
- 每一轮结束后，`sheets()` yield 一个 `DeviceEvent`：
  - 进纸器空了：`kind="no_paper"`，消息“进纸器已空。放入下一叠后点“继续”。”；
  - 双张：`kind="double_feed"`；
  - 卡纸：`kind="paper_jam"`；
  - 其他错误：`kind="error"`。

  控制器收到 `no_paper` / `double_feed` / `paper_jam` 后**自动暂停**（见 1.5）。暂停期间 `ControlledSource` 阻塞在下一次取值之前，生成器停在 yield 处；用户点“继续”后，生成器向扫描线程发出“开始下一轮”的命令。
- 一轮里的页数是奇数时（双面模板），最后不成对的那一面单独组成一张不完整的卡交给流水线，流水线会按 `MISSING_FACE` 列入物理异常。
- 停止：`ControlledSource.stop()` 之后，流水线不再取值，生成器被关闭（`GeneratorExit`）。这时在 `finally` 中通知扫描线程退出：正在采集时抛 `CancelAll` 结束本轮，然后关闭数据源和 DSM、销毁隐藏窗口。

### 1.3 采集参数（能力项）
都在打开数据源后、开始采集之前设置。逐项设置，单项失败只记入 `warnings` 列表（扫描页显示“扫描仪不支持：…”），不中断采集。

| 参数 | 能力项 | 值 |
|---|---|---|
| 进纸器 | `CAP_FEEDERENABLED` (BOOL) | True |
| 自动进纸 | `CAP_AUTOFEED` (BOOL) | True |
| 张数 | `CAP_XFERCOUNT` (INT16) | -1（扫完为止）；重扫单张时为模板面数 |
| 双面 | `CAP_DUPLEXENABLED` (BOOL) | 来自扫描参数 |
| 分辨率 | `ICAP_XRESOLUTION` / `ICAP_YRESOLUTION` (FIX32) | 200/300/400/600 |
| 色彩 | `ICAP_PIXELTYPE` (UINT16) | 灰度 `TWPT_GRAY`，彩色 `TWPT_RGB` |
| 双张检测 | `CAP_DOUBLEFEEDDETECTION` (0x1132, UINT16 数组/单值) | 开启时设为 `TWDF_ULTRASONIC`(0)；关闭时尝试 `CAP_DOUBLEFEEDDETECTION` 置空，失败忽略 |
| 不显示驱动界面 | `CAP_INDICATORS` (BOOL) | False |

### 1.4 图像转换
- `acquire_natively(after=cb)` 的回调拿到的是内存中的 DIB 句柄。用 `GlobalLock` 和 `GlobalSize` 取出字节，通过 `twain.windows.convert_dib_to_bmp` 转成 BMP 字节，再用 Pillow 打开。
  - 灰度和黑白图转为 `L` 模式的 numpy 数组。
  - 彩色图保持 `RGB` 模式。保存原图时直接用 Pillow 写 JPEG，识别时 worker 以灰度读取，所以不需要做 BGR 转换。
- DPI 优先取 `image_info["XResolution"]`，取不到时用设置的 DPI。
- 句柄由 `_Image.close()` 释放；回调结束前必须释放，否则大批量扫描会泄漏内存。

### 1.5 控制器与扫描页的配合
- `ScanController` 处理 `DeviceNotice` 时，对 `no_paper` / `double_feed` / `paper_jam` 调用 `self.pause()`（在后台线程中调用，`pause()` 本身是线程安全的），然后照常发出 `deviceNotice` 信号。
- 扫描页订阅 `deviceNotice` 信号，在中栏计数区上方显示通知条：
  - 双张、卡纸用红色，`no_paper` 用中性色；
  - 文案包含事件说明，以及“处理后点“继续”。双张或卡纸的那张纸放回进纸口最前面。”；
  - 用户点“继续”或开始新一轮后，通知条隐藏。
- 输入源“扫描仪”可用时：
  - 显示扫描仪下拉框（列出 `list_scanners()` 的结果）和“刷新”按钮；
  - 进纸、DPI、色彩、双张检测这几个控件变为可用，初始值取设置中的扫描默认参数；
  - 文件夹输入行隐藏。
- “开始识别”时：
  1. 按当前控件的值构造 `TwainConfig(scanner, duplex, dpi, color, double_feed)`；
  2. `new_batch(..., scanner=<扫描仪名>)`（manifest 和 result.json 的 `scan.scanner` 字段记录扫描仪名）；
  3. `ctrl.start(TwainSource(cfg, faces))`。
- 设置的扫描默认参数增加 `scanner: str = ""`，记住上次使用的扫描仪。
- 物理异常的“重扫”：
  - 输入源为扫描仪时，弹出提示“把 #0005 放进进纸器后点“确定””，然后调用 `ctrl.rescan_scanner(seq, cfg)`。后台线程用单张模式（`XFERCOUNT` 为面数）的 `TwainSource` 取一张卡，交给 `pipeline.rescan`。没取到纸时，`errorOccurred` 报“未扫描到纸张”。
  - 输入源为导入图片时，保持现在的选择文件方式。
  - 同一台扫描仪不能同时开两轮采集，而暂停时数据源仍被占用，所以扫描仪重扫只在没有扫描源在运行时进行（`ctrl.scanning` 为 False），否则提示“请先点“结束本轮”，再重扫”。扫描仪模式下，主按钮旁增加“结束本轮”按钮（调用 `ctrl.stop()`），用来结束采集但不结束批次。

### 1.6 扫描仪列表
`list_scanners() -> list[str]`：在一个临时线程里创建隐藏窗口，打开 DSM，读取 `source_list`，然后关闭。出错时返回空列表，并把原因记在 `last_error`。

---

## 2. Windows 兼容与打包

### 2.1 中文路径读写
OpenCV 的 `imread` / `imwrite` 在 Windows 上不支持非 ASCII 路径。新增 `src/scan_answer/imageio.py`：
- `imread_gray(path) -> np.ndarray | None`：`np.fromfile` + `cv2.imdecode(..., IMREAD_GRAYSCALE)`，失败返回 None；
- `imwrite(path, img, params=None) -> bool`：`cv2.imencode(扩展名)` + `tofile`。

`pipeline/worker.py`、`app/trial.py`、`template/io.py` 里的 `cv2.imread` / `cv2.imwrite` 全部改用这两个函数。`template/io.py` 保存底图时检查返回值，写失败抛 `OSError`（顺带修掉计划 4 Task 1 延后的那个小问题）。`output/` 中写 JPEG 的地方如果用了 cv2，也一并改。

### 2.2 打包后的资源路径
`bundled_templates_dir()`：程序被打包时（`getattr(sys, "frozen", False)`），返回 `Path(sys._MEIPASS) / "templates"`；否则保持现状。PyInstaller 的 onedir 模式下，`_MEIPASS` 指向 `_internal` 目录。

### 2.3 PyInstaller
- `packaging/scan-answer.spec`：
  - 入口为 `packaging/entry.py`，内容是调用 `scan_answer.ui.app.main()`，启动前执行 `multiprocessing.freeze_support()`；
  - onedir 模式，`console=False`，名称 `答题卡扫描识别`，Windows 图标用 `packaging/icon.ico`（如果存在）；
  - `datas` 包含 `templates/`；
  - `collect_all("pypdfium2")`、`collect_all("zxingcpp")`、`collect_dynamic_libs("cv2")`；Windows 上再加 `collect_all("twain")`；
  - `hiddenimports` 包含 `scan_answer.pipeline.worker`（spawn 子进程需要）。
- `packaging/build_windows.ps1`：创建 venv，安装 `.[dev]` 和 `pyinstaller`，运行 `pyinstaller packaging/scan-answer.spec --noconfirm`，输出到 `dist/答题卡扫描识别/`。
- `packaging/build_macos.sh`：同样的流程，生成 `.app`（macOS 只能导入图片）。
- `docs/packaging.md`：写明打包步骤、TWAIN 驱动要求（需要 64 位 TWAIN 驱动）、首次运行的设置目录位置。
- 进程池是 spawn 模式，打包后能否正常启动子进程，取决于 `freeze_support()` 是否在入口最前面执行。`run_app` 中已经调用，入口文件里再调用一次，保证在任何导入之前执行。

---

## 3. 界面补全（计划 3 延后项）

### 3.1 结果页：单项重试转写
- `ResultsPage(settings=None)` 新增可选参数，由 `MainWindow` 传入设置。
- 主观题区块中，`ocr_status` 为 `failed` 的题显示“重试转写”按钮；`none` 或 `pending` 且已配置转写时，显示“转写”按钮。
- 点击后，在后台线程中执行 `retry_item(result_path, kind, key, cfg)`。这是 `ocr/queue.py` 中新增的函数，复用 `OcrQueue._run` 的单项逻辑：读取切图、调用 `OcrClient.transcribe`、用 `update_json` 回写。完成后经 Qt 信号刷新当前学生。
- 没有配置转写时，按钮不可用，提示“请先在设置中配置大模型转写”。
- 结果页顶部增加“重试本批全部失败项”按钮：
  1. 对批次内每个 result.json 调用 `reset_failed`；
  2. 用一个临时 `OcrQueue` 把它们重新投递；
  3. 完成后刷新。

  正在扫描的批次（控制器当前批次）不允许这样操作，提示“请在扫描页结束批次后再重试”。

### 3.2 批次拼图“展开”按钮
- 扫描页拼图标题行右侧放一个“展开全部”按钮，仅在 `mosaic.total_slots() > 1000` 时显示。
- 点击后设置 `mosaic.expanded = True`，按钮文字变为“只看最近 10 行”；再次点击恢复。

### 3.3 考试页构成条
- 考试表增加“构成”列，用自绘委托 `CompositionDelegate` 画一条水平比例条：成功（无警告）`OK_FILL`、有数据警告 `WARN_FILL`、物理异常 `ERR`、作废灰色。
- 数据来自 `exam_totals(exam_dir)`，没有数据时显示“—”。悬停时显示各项数量。

### 3.4 设置页“区域色码”
- 设置页末尾新增“区域色码”一节，只读：用现有的 `RegionLegend` 显示六种区域色，再加上物理异常和数据警告的颜色说明，便于现场人员对照。

### 3.5 扫描页设备事件通知
见 1.5。导入图片时没有设备事件，通知条一直隐藏。

---

## 4. 不在本阶段范围
- macOS 扫描仪接入（ImageCaptureCore）。
- 颜色滤除（红/绿/蓝），各驱动的能力项差异很大，留到真机测试时再加。
- 一个学生多张纸。
- 推送、打包上传。

## 5. 文件清单
```
src/scan_answer/source/twain_source.py   新增：twain_available、list_scanners、TwainConfig、TwainSource
src/scan_answer/imageio.py               新增：imread_gray、imwrite
src/scan_answer/app/settings.py          修改：ScanDefaults.scanner；bundled_templates_dir 打包路径
src/scan_answer/ui/controller.py         修改：设备事件自动暂停；rescan_scanner
src/scan_answer/ui/pages/scan_page.py    修改：扫描仪输入、参数控件、通知条、结束本轮、扫描仪重扫、拼图展开
src/scan_answer/ui/pages/results_page.py 修改：单项重试、本批失败项重试
src/scan_answer/ui/pages/exam_page.py    修改：构成条
src/scan_answer/ui/pages/settings_page.py 修改：区域色码一节
src/scan_answer/ui/main_window.py        修改：ResultsPage 传入设置
src/scan_answer/ocr/queue.py             修改：retry_item
src/scan_answer/pipeline/worker.py app/trial.py template/io.py   修改：改用 imageio
packaging/entry.py packaging/scan-answer.spec packaging/build_windows.ps1 packaging/build_macos.sh   新增
docs/packaging.md docs/gui.md            新增 / 修改
pyproject.toml                           修改：pytwain（Windows）
```

---

## 6. 第二批补全（2026-11-08）

对照上位设计文档，第一期范围内还有 7 项没做，本节逐项给出设计。同样只写功能代码，不写测试。

### 6.1 背面定位全部失败时，借用正面位置推算（上位 5.2③）
- **触发条件**：模板有两面，某一面（记为 A）用主定位方式或特征匹配成功、得分 ≥ `MIN_SCORE`，另一面（B）在所有输入图上都定位失败或得分不够。原来这种情况会判为 `ALIGN_FAILED` / `FACE_MISMATCH` 物理异常。
- **推算方法**：双面扫描仪同时拍正反面，同一张纸在两张图里的位置互为镜像。设 A 面的单应矩阵 H_A（A 图像素 → A 面 mm），B 图宽 W、高 H，纸宽 pw、纸高 ph。
  - 左右翻页（书本式）：`H_B = F_mm(x) · H_A · F_px(x)`，其中 `F_px(x)` 把像素 x 换成 `W-1-x`，`F_mm(x)` 把 x 换成 `pw-x`；
  - 上下翻页：同理换 y 坐标。

  对 B 图分别尝试 0°、180° 和左右、上下两种翻转，共 4 种组合，用 `_score`（与空白底图的相关性）选得分最高的一种。
- **兜底的下限**：最高得分 ≥ `DERIVED_MIN_SCORE = 0.15` 才采用，否则仍按原来的物理异常处理（可能根本不是这套卡）。
- **采用后的处理**：
  - `FacePlacement.align.method = "derived"`，`scan.faces.<面>.align_method` 输出 `"derived"`；
  - B 面只用于切图（主观题、信息栏、整页图），B 面上的 OMR（客观题、考号、填涂字段、选做组、缺考）一律判为 `status: "failed"`，不给答案；
  - 新增数据警告 `ALIGN_DERIVED`（`field` 为 `scan.faces.<面>`，`msg` 为“背面定位失败，按正面位置推算切图；该面的填涂项未识别”）；
  - B 面的条码仍按区域读取，并且会走 6.2 的全页搜索。

### 6.2 条码识别失败后的全页搜索（上位 5.2⑥）
- 现有的读取顺序是：区域原图 → 区域二值化 → 区域放大 2 倍 → 外扩 10 mm。外扩 10 mm 仍然读不出来时，对该面整页原图执行一次 `read_barcodes`（找出所有码），再用该面的单应矩阵把每个码的中心换算到模板 mm 坐标。
- 选择距离该条码区域中心最近、并且没有被同一张卡的其他条码键用掉的那个码。距离要小于 `max(区域宽, 区域高) + 30 mm`，否则视为没找到。
- 一张卡上全页搜索最多执行一次，读到的码按条码键依次分配。
- 输出 JSON：`student.barcodes.<键>` 新增 `"located"` 字段，取值 `"region"` / `"expanded"` / `"page"`。通过全页搜索找到时，另加数据警告 `BARCODE_RELOCATED`（`field` 为该条码），提示条码贴歪或贴出了框。

### 6.3 整批填涂偏浅等阈值异常提示（上位 5.3）
- `engine/omr.py` 新增 `threshold_issues(all_fills) -> list[dict]`，基于与 `fit_thresholds` 相同的 Otsu 分组，返回问题列表，每项为 `{"code", "msg"}`：
  - `LIGHT_MARKS`：已涂组（≥ 阈值）的涂黑度中位数 < 0.55，提示“本批填涂整体偏浅（已涂选项涂黑度中位数 x），可能是铅笔偏软或扫描偏亮；浅涂会被标为存疑”；
  - `LOW_SEPARATION`：已涂组均值与未涂组均值的差 < 0.35，提示“已涂与未涂区分度低，存疑题可能较多，建议检查扫描参数（亮度、颜色滤除）”；
  - `THRESHOLD_CLAMPED`：Otsu 原始阈值高于 0.60 被截断时提示（低于 0.30 是未涂选项占多数时的常态，不提示）；
  - 样本不足时（与 `fit_thresholds` 的条件相同）返回空列表。
- `BatchSession` 每次拟合阈值（扫描中重新拟合、结束批次）时计算问题列表，写进 `manifest["threshold_issues"]`。
- `ThresholdEvent` 和 `FinalizedEvent` 增加 `issues: list[dict]` 字段。
- 扫描页收到带问题的阈值事件时，在设备通知条显示（琥珀色，标题“阈值提示”）。结束批次时有问题，同样显示。

### 6.4 主观题小题号（上位 4.2）
- 模型：`SubjectiveSpec.sub: str = ""`，可以省略，旧模板照常读取。
- 主观题的唯一键为 `qkey = no`（没有小题号时）或 `f"{no}.{sub}"`。校验改为按 `qkey` 判重，同时仍不能和客观题题号重复（只比较没有小题号的 `no`）。
- result.json 的主观题项增加 `"sub"` 字段。图片名改为 `q{安全名(qkey)}_{i}.jpg`；没有小题号时与原来一样，是 `q56_1.jpg`。
- 转写队列（`_tasks` / `_apply` / `retry_item`）、`OCR_FAILED` 的 `field`（`subjective.{qkey}`）都改用 `qkey`。
- 编辑器属性面板的主观题表单增加“小题号”输入框。区域树和画布标签显示为“56 (1)”。结果页标题显示为“第 56 题（1）”。

### 6.5 可配置的输出目录命名（上位第 8 节“输出目录与命名”）
- 设置项：
  - `AppSettings.exam_dir_pattern = "{name}_{subject}_{time}"`；
  - `AppSettings.student_dir_pattern = "{exam_no}"`。
- **考试目录**可用的占位符：`{name}` `{subject}` `{code}` `{site}` `{room}` `{operator}` `{date}`（YYYYMMDD）`{time}`（YYYYMMDD_HHMMSS）。渲染后去掉非法字符，结果为空时用“考试”，重名时加 `_2`、`_3`。
- **学生目录**一律以 `{seq:04d}` 开头，因为重扫清理和重开批次都依赖这个前缀，命名规则只控制后面的部分。可用的占位符：`{exam_no}`（填涂考号，未识别的位去掉）、`{barcode}`（考生条码）。渲染结果为空时，只用序号；否则为 `{seq:04d}_{渲染结果}`。
- 学生目录规则在新建批次时写进 `manifest["student_dir_pattern"]`，同一批次始终沿用。识别进程从 `SheetJob.student_dir_pattern` 取得，交给 `LocalExporter.student_dir`。
- 设置页“输出”一节增加两个输入框，旁边实时显示示例名称；占位符写错（花括号里不是已知名称）时，保存会提示。

### 6.6 扫描仪颜色滤除（上位 5.1）
- 扫描参数增加 `dropout`：`none` / `red` / `green` / `blue`，默认 `none`。扫描页增加一组分段按钮“颜色滤除：无 / 红 / 绿 / 蓝”，设置页的扫描默认参数里也增加这一项。
- TWAIN：`ICAP_FILTER`（UINT16），取值 `TWFT_RED=0`、`TWFT_GREEN=1`、`TWFT_BLUE=2`，选“无”时为 `TWFT_NONE=3`。多数扫描仪只在灰度或黑白模式下生效。设置失败时记入“扫描仪不支持”提示。
- **软件兜底**：彩色扫描并且选了滤除时，`TwainSource` 把彩色图的对应通道当作灰度图交给流水线（例如滤红就取 R 通道，红色印刷会变浅）。原图另存为这个灰度图。界面上说明“彩色扫描时由软件滤色”。

### 6.7 扫描仪自动裁边与纸张尺寸（上位 5.1）
- 扫描参数增加 `crop`：
  - `auto`（默认）：让扫描仪自动检测纸张边缘，`ICAP_AUTOMATICBORDERDETECTION=True`，`ICAP_UNDEFINEDIMAGESIZE=True`；
  - `template`：按模板纸张尺寸，`ICAP_SUPPORTEDSIZES=TWSS_NONE` 后 `set_image_layout((0, 0, 纸宽/25.4, 纸高/25.4))`，单位英寸；
  - `driver`：不设置，用驱动默认。

  扫描页和设置页都提供这一项。
- `TwainConfig` 增加 `dropout`、`crop`、`paper_mm`（取自当前批次模板的纸张尺寸）。
- 设置失败时记入“扫描仪不支持”提示，不中断扫描。识别本身不依赖裁边（有定位兜底），这一项只影响原图的大小和存储。

### 6.8 新增数据警告代码汇总
`ALIGN_DERIVED`、`BARCODE_RELOCATED`，写进 `docs/output-format.md` 和结果页 `WARNING_TEXT`：
- `ALIGN_DERIVED`：“背面定位失败，按正面推算切图”；
- `BARCODE_RELOCATED`：“条码在区域外找到（贴歪或贴出框）”。

阈值异常属于批次层面的提示，只写进 manifest，不写进每张卡的 warnings。
