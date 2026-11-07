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
