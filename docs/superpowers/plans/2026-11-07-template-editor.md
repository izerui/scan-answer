# 模板编辑器与新区域类型（第 1 期 · 计划 4/5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在软件里可视化制作模板：导入空白卡（图片或 PDF）→ 自动纠偏、找定位点/外框/同步头 → 框选区域、填参数、自动吸附到印刷框 → 试识别 → 保存。同时补齐设计文档 4.2 中尚未实现的区域类型：同步头、自定义填涂字段、选做组，以及判断题标签。

**Architecture:** 三层。
- `template/`：模型扩展。新增 `auto.py`，放与界面无关的纯函数：自动检测、吸附、按矩形生成网格、底图导入。新增 `doc.py`，提供可编辑的模板文档和快照式撤销栈。
- `engine/`：识别新字段，写进 result.json。
- `ui/editor/`：画布、区域树、属性面板。由模板页组合成编辑器。

编辑操作都通过 `TemplateDoc` 完成，界面只负责显示和转发。试识别在后台线程中运行。

**Tech Stack:** Python 3.14、PySide6（`QGraphicsView`）、OpenCV、NumPy；PDF 渲染新增运行依赖 `pypdfium2>=5.0`（Apache-2.0/BSD，纯 Python 接口 + 预编译 pdfium，Windows/macOS 均有 wheel；开发依赖 PyMuPDF 是 AGPL，不能进运行依赖）。

**Spec:** `docs/superpowers/specs/2026-11-06-scan-answer-design.md`（4.1 制作流程、4.2 区域类型、4.3 定位方式、6.2 result.json、8 界面“模板”行）；界面正式稿 `design-demos/答题卡扫描识别-正式稿.html`，截图 `design-demos/shots/final-template.png`。

**前置:** 计划 1–3 已完成。

## 已确认的决策与原型结论

- **同步头**（用户已确认）：模板里可以标注，编辑器能自动识别。识别时在对齐后的工作图上测量每个同步头的实际偏移，把找到几个、最大偏移多少写进 `scan.faces.<面>.sync`，**不参与校正**。原型实验依据：波浪形变 1.5 mm 时，经过全局对齐和分块微调，残余偏移不超过 0.4 mm；加上逐行校正后，考号准确率从 368/369 降到 365/369。
- **199 模板**：版本号升为 2，加入左侧 12 个同步头，客观题块记录 `group=5`。（更正：起草时曾以为考号格列距有误，执行中经实测印刷框确认原考号格正确——中心 115.63、列距 5.098 mm；括号式选项框会让吸附锁到“] | [”竖线上，因此吸附必须能识别窗口边缘和双峰并报失败。考号格保持不变。）
- **原型实测**（脚本思路已写进各任务的算法说明，以下数值是验收依据）：
  - 自动定位块：199 正面 4 个全部找到，误差不超过 0.3 mm；背面不误报。
  - 自动外框：199 背面找到 2 个。
  - 自动同步头：199 正面一列 12 个。
  - 吸附：人为加入 ±1.2 mm 平移和 ±3% 缩放后，客观题块都能回到 0.3 mm 以内，残差不超过 0.25 mm。
  - 底图导入纠偏：199 背面原本倾斜 1.45°，纠偏后残余 0.000°。
- **用户模板存放位置**：`config_dir()/templates/<模板名>/`，自动加入模板列表。随程序提供的模板（`bundled_templates_dir()` 下）不能就地覆盖；在编辑器里保存时，另存到用户模板目录。

## Global Constraints

- 沿用计划 1–3 的全部全局约束：原子 JSON 写入；中文注释和提示，英文标识符；中文提示里用弯引号“”，不要改成 ASCII 引号；`spawn` 进程池；界面线程不直接调用 `BatchSession` / `BatchPipeline`；区域色码；QSS 用对象名选择器；无显示环境运行测试并使用 `no_dialogs` 夹具。
- 运行依赖只新增 `pypdfium2>=5.0`。
- 坐标单位 mm，原点为纠偏后底图的左上角；模板底图 300 DPI。
- 模板 JSON 的 `schema_version` 仍为 `"1.0"`。新增的键都可以省略：旧模板和旧批次快照照常读取，缺省为空。
- 新增数据警告代码：`OMR_FIELD_PARTIAL`、`OMR_FIELD_BLANK`（`field` 为 `student.omr_fields.{key}`）；`CHOICE_BLANK`、`CHOICE_MULTIPLE`、`CHOICE_UNCERTAIN`（`field` 为 `choose_one.{key}`）。
- 区域色码：同步头用“定位” `#15181c`；自定义填涂字段用“考号” `#1d5bbf`；选做组用“主观题” `#b45309`（与正式稿一致）；忽略区用“信息栏” `#4e5a66` 虚线。
- 测试命令：`QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q`（快速）；`.venv/bin/python -m pytest -m slow -q`（200 张回归）。

## Review Focus

1. **旧模板、旧批次快照**：没有 `sync`、`omr_fields`、`choose_one`、`group` 键的 template.json，以及没有新字段的 `state/*.json`，都要能正常读取、识别、结束批次。（Task 1、3 的测试）
2. **编辑中产生的非法模板**：宽或高 ≤ 0、超出纸面、题号重复、键重复、选做组引用不存在的题号、定位块不足 4 个、模板在自己的底图上对齐不上。这些都必须在保存时拦下并逐条列出原因，不能留到扫描时才崩溃。（Task 1、4 的测试）
3. **保存失败或保存位置不可写**：随程序提供的模板目录、只读目录、磁盘写失败时，程序不崩溃；已有模板不能被写坏（底图和 template.json 都原子替换）；未保存的修改不会悄悄丢失（切换页面或关窗时提示）。（Task 4、7 的测试）
4. **试识别的样卡有问题**：物理异常、图片损坏、用错模板时，逐张显示失败原因；试识别在后台线程运行，界面不卡。（Task 4、7 的测试）
5. **导入底图的输入有问题**：PDF 页数不等于 1 或 2、文件损坏、图片没有 DPI 信息、找不到定位块或外框时，给出明确提示，不崩溃；只有正面的单面模板能完整走通“保存 → 识别”。（Task 2、7 的测试）

## 文件结构

```
src/scan_answer/
  template/model.py        # 修改：SyncSpec、OmrFieldSpec、ChooseOneSpec；ObjectiveBlock.group；Template 新增 sync/omr_fields/choose_one
  template/io.py           # 修改：新键可省略；template_to_dict 不变（asdict）；save_template 增加底图写出参数
  template/validate.py     # 新增：validate_template、check_alignment
  template/auto.py         # 新增：auto_anchors、auto_frames、auto_sync、grid_in_rect、snap_grid、load_pages、import_base
  template/doc.py          # 新增：RegionRef、TemplateDoc、UndoStack
  engine/omr.py            # 修改：decide_idx（decide 改为调用它）
  engine/recognize.py      # 修改：SheetAnalysis 新字段；analyze/decide 处理 omr_fields、choose_one、sync
  engine/state.py          # 修改：新字段序列化（缺省兼容）；analysis_fills 包含新涂黑度
  engine/result.py         # 修改：omr_fields、choose_one、scan.faces.*.sync、omr 字段切图
  synth/generate.py        # 修改：填涂 omr_fields 与 choose_one，写进 truth
  app/results.py           # 修改：face_overlays 新 kind；WARNING_TEXT 新代码
  app/library.py           # 修改：user_templates_dir()；list_templates 自动包含它
  app/trial.py             # 新增：trial_recognize（试识别，纯函数）
  ui/editor/__init__.py
  ui/editor/canvas.py      # 新增：TemplateCanvas、RegionItem
  ui/editor/tree.py        # 新增：RegionTree
  ui/editor/props.py       # 新增：PropsPanel
  ui/editor/new_dialog.py  # 新增：NewTemplateDialog（导入底图）
  ui/pages/template_page.py   # 重写：模板列表 + 编辑器
  ui/pages/results_page.py    # 修改：信息行显示自定义填涂字段，答案表追加选做组
  ui/main_window.py           # 修改：离开模板页、关窗时检查未保存修改
tools/build_template_199.py   # 修改：吸附考号格、加入同步头、version=2
templates/guanzong199/        # 重新生成
docs/output-format.md docs/gui.md   # 修改
pyproject.toml                # 修改：pypdfium2
tests/test_template_ext.py test_template_auto.py test_engine_ext.py test_template_doc.py
tests/test_trial.py test_ui_editor.py   # 新增
```

---

### Task 1: 模板模型扩展与校验

**Files:**
- Modify: `src/scan_answer/template/model.py`、`src/scan_answer/template/io.py`、`src/scan_answer/template/__init__.py`（导出新类和函数）
- Create: `src/scan_answer/template/validate.py`
- Test: `tests/test_template_ext.py`

**Interfaces:**
- Produces（model.py，追加在现有类之后；`Template` 的新字段放在 `ignore` 之后，都有默认值）:
  ```python
  @dataclass
  class SyncSpec:
      face: FaceName
      direction: Literal["vertical", "horizontal"]  # vertical = 同步头沿纸张上下排成一列
      marks: list[Rect]

  @dataclass
  class OmrFieldSpec:
      key: str            # 如 "room"、"paper_type"
      label: str          # 如 "考场号"、"AB 卷"
      grid: OmrGrid       # 每位一组；labels 为候选值

  @dataclass
  class ChooseOneSpec:
      key: str
      label: str
      face: FaceName
      questions: list[str]   # 题号，与 cells 一一对应
      cells: list[Rect]

  # ObjectiveBlock 追加：group: int = 0   （每组题数，仅编辑器重新生成网格时用）
  # Template 追加：sync: list[SyncSpec] = []、omr_fields: list[OmrFieldSpec] = []、choose_one: list[ChooseOneSpec] = []
  ```
- Produces（io.py）：`template_from_dict` 能读取新键，缺省为空或 0；`save_template(t, directory, bases: dict[str, np.ndarray] | None = None)`：给了 `bases` 时，先把各面底图写成 PNG（临时文件再 `os.replace`），最后原子写 template.json。
- Produces（validate.py）:
  - `validate_template(t: Template) -> list[str]`：返回中文问题列表，空列表表示通过。
  - `check_alignment(t: Template, bases: dict[str, np.ndarray]) -> list[str]`：在每面自己的底图上运行 `align_primary(base, face.align, t.dpi)`。失败或残差 > 0.5 mm 时，返回 `"{面中文名}：用当前定位方式在底图上对齐失败（{原因}）"` 或 `"…残差 {x:.2f} mm 过大"`。面中文名：front→正面，back→背面。

`validate_template` 必须检查的项目（每项对应一条消息，消息里要包含区域名或题号）：
1. 所有矩形 `w > 0`、`h > 0`，并且完全落在 `[0, paper_w] × [0, paper_h]` 内。
2. 区域引用的面在 `t.faces` 中存在。
3. `anchors` 方式至少 3 个定位块（3 个时按仿射变换定位，2026-11-09 放宽）；`frames` 方式至少 1 个外框。
4. 客观题题号（`start + k`）不重复；主观题 `no` 不重复，并且不和客观题题号重复。
5. `barcodes`、`fields`、`omr_fields`、`choose_one` 的 `key` 在各自类别内不重复；`student_barcode` 之外的条码键随意。
6. OMR 网格每组的格数等于 `len(labels)`，并且至少 1 组。
7. 选做组的 `questions` 与 `cells` 等长，至少 2 项，每个题号都能在主观题或客观题中找到。
8. 同步头每组至少 2 个。

- [ ] **Step 1: 写失败测试** `tests/test_template_ext.py`
  - `test_old_template_loads_with_defaults`：读取 `templates/guanzong199/template.json`，删掉 `sync`、`omr_fields`、`choose_one` 和 objective 里的 `group` 键后，`template_from_dict` 得到的 `t.sync == []`、`t.omr_fields == []`、`t.choose_one == []`、`t.objective[0].group == 0`。
  - `test_roundtrip_new_fields`：构造含 `SyncSpec("front","vertical",[(18,50,4,2.2),(18,54,4,2.2)])`、一个 `OmrFieldSpec`（labels `["A","B"]`，1 组）、一个 `ChooseOneSpec`（questions `["56","57"]`）的模板，经过 `template_to_dict` → `json` → `template_from_dict` 后与原对象相等。
  - `test_validate_ok_on_bundled`：`validate_template(load_template(templates/guanzong199)[0]) == []`。
  - `test_validate_catches_each_problem`：参数化 8 种破坏方式（逐条对应上面的检查项：把客观题块第一个格的宽改成 0；把 barcode 的 x 改成 389；字段 face 改成 `"middle"`；定位块删到 3 个；把第二个客观题块的 start 改成 1；复制一个 field；选做组题号改成 `"99"`；同步头只留 1 个），每种都断言返回列表非空，且消息包含预期的关键字（依次为 `"宽或高"`、`"超出纸面"`、`"不存在的面"`、`"至少 4 个定位块"`、`"题号重复"`、`"键重复"`、`"99"`、`"同步头"`）。
  - `test_check_alignment_bundled_ok_and_broken`：199 模板的 `check_alignment == []`；把正面定位块整体平移 30 mm 后，返回包含 `"正面"` 的消息。
  - `test_save_template_with_bases_atomic`：`save_template(t, d, bases)` 后，目录中有 `front.png`、`back.png`、`template.json`，没有 `*.tmp` 残留；重新 `load_template(d)` 得到的底图尺寸不变。
- [ ] **Step 2: 运行确认失败** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_template_ext.py -q` → ImportError（`SyncSpec`、`validate_template`）。
- [ ] **Step 3: 实现** model.py / io.py / validate.py，按上面的接口。io 读取新键时用 `d.get(key, [])`；`group` 用 `int(o.get("group", 0))`。
- [ ] **Step 4: 运行确认通过** 同上命令 → 全部通过；再跑全部快速测试，确认没有回归。
- [ ] **Step 5: Commit** `feat(template): 同步头、自定义填涂字段、选做组模型；模板校验与自对齐检查`

---

### Task 2: 自动检测、吸附、底图导入；重建 199 模板

**Files:**
- Create: `src/scan_answer/template/auto.py`
- Modify: `pyproject.toml`（dependencies 增加 `"pypdfium2>=5.0"`，然后运行 `uv pip install --python .venv -e ".[dev]"`）、`tools/build_template_199.py`、`templates/guanzong199/*`（重新生成）
- Test: `tests/test_template_auto.py`

**Interfaces (auto.py):**
```python
@dataclass
class SnapResult:
    cells: list[list[Rect]]   # 吸附后的格子（未通过时为原格子）
    ok: bool
    inliers: int
    total: int
    max_residual_mm: float
    message: str              # 中文，如 "已吸附 75/75 格，最大偏差 0.15 mm" 或失败原因

@dataclass
class ImportedBase:
    image: np.ndarray         # 灰度，300 DPI，已纠偏
    paper: tuple[float, float]  # mm，保留 1 位小数
    skew_deg: float           # 纠正掉的角度
    method: str               # "anchors" / "frames" / "none"

def auto_anchors(base: np.ndarray, dpi: float, paper: tuple[float, float]) -> list[Rect] | None
def auto_frames(base: np.ndarray, dpi: float, paper: tuple[float, float], max_n: int = 4) -> list[Rect]
def auto_sync(base: np.ndarray, dpi: float, min_count: int = 5) -> list[SyncSpec]   # face 字段填 "front"，由调用方改
def grid_in_rect(rect: Rect, n_groups: int, n_options: int, options_horizontal: bool,
                 block_every: int = 0, gap_ratio: float = 0.5, cell_ratio: float = 0.62) -> list[list[Rect]]
def snap_grid(base: np.ndarray, cells: list[list[Rect]], dpi: float, radius_mm: float = 1.5,
              max_residual_mm: float = 0.4) -> SnapResult
def load_pages(path: str | Path) -> list[tuple[np.ndarray, float]]   # [(灰度图, dpi)]
def import_base(gray: np.ndarray, dpi: float, target_dpi: int = 300) -> ImportedBase
```

算法要点：这些参数都是原型在 199 底图上验证过的，照用即可。
- **候选黑块**：Otsu 反二值化，取连通域；宽和高都在给定的 mm 范围内，且实心度 `面积/(w·h)` 不低于下限。
- **auto_anchors**：候选尺寸 2.5–12 mm，实心度 ≥ 0.75，宽高比 0.5–2。四个角各取一个：中心离该纸角最近，且 x 距离 < 纸宽 20%、y 距离 < 纸高 20%。有任何一个角没找到就返回 `None`。返回顺序为左上、右上、右下、左下。
- **auto_frames**：自适应阈值（`ADAPTIVE_THRESH_MEAN_C`，block 31，C 15）后找轮廓，用 `approxPolyDP(0.02·周长)` 近似，保留 4 个顶点的轮廓。外接矩形面积须在纸面的 5%–90% 之间。去掉被其他框包含的（容差 2 mm），按面积从大到小取前 `max_n` 个。
- **auto_sync**：候选尺寸 1–8 mm，实心度 ≥ 0.7，长边 / 短边 > 1.3。中心 x 相差 < 1 mm、宽高相差 < 1 mm 的归为一列（vertical）；同理按中心 y 归为一行（horizontal）。保留至少 `min_count` 个的组，每个块只归入较大的那组。列内按 y 排序，行内按 x 排序。
- **grid_in_rect**：网格铺满矩形，即第一格和最后一格的外沿贴着矩形边。组间额外间距 = `gap_ratio × 组距`，每 `block_every` 组插入一次。格子尺寸 = `cell_ratio × 对应方向的格距`。格距由矩形尺寸、格数和插入的间距反算。
- **snap_grid**：
  1. 墨迹图 = `GaussianBlur(255 - base, σ=1)`。
  2. 对每个格，在 ±`radius_mm` 窗口内用 `boxFilter`（格子大小）找格内墨迹均值最大的位置；最大值比窗口中位数高不到 8 的，记为没找到。
  3. 用全部找到的格中心，拟合“原中心 → 新中心”的仿射映射：最小二乘迭代 3 轮，每轮剔除残差 ≥ `max(0.25, 2.5 × 中位残差)` 的点。
  4. 把所有格（包括没找到的）按这个映射移动。
  5. 判定通过的条件：内点数 ≥ 60% 且最大内点残差 ≤ `max_residual_mm`。不通过时 `ok=False`，`cells` 保持原样，`message` 写明原因（“可吸附的格子太少”或“偏差 x mm 超过上限”）。
- **load_pages**：
  - `.pdf` 用 pypdfium2 以 300 DPI 灰度渲染每一页。
  - 图片用 Pillow 打开后转灰度，dpi 取 `info["dpi"][0]`，没有时按 300。
  - 读不了的文件抛 `ValueError("无法读取 {文件名}：{原因}")`。
- **import_base**：
  1. 先缩放到 `target_dpi`。
  2. 纠偏角：能找到 4 个定位块时，取上边一对、下边一对连线角度的平均；否则取最大轮廓 `minAreaRect` 的角度，归一化到 `((a + 45) % 90) - 45`；轮廓面积不足纸面 5% 时角度为 0，`method="none"`。
  3. 绕图像中心旋转，空白处填白（255）。
  4. `paper` = 图像宽高（像素）/ target_dpi × 25.4。

**199 模板重建（tools/build_template_199.py）**：
- 改用 `load_pages` 加 pypdfium2 渲染，不再依赖 PyMuPDF。
- 考号格保持原来的 `grid_cells` 参数，不吸附。
- `sync = auto_sync(front_base, 300)`，face 设为 `"front"`，要求恰好 1 组、12 个。
- 每个客观题块设 `group=5`；`version=2`。
- 重新生成 `templates/guanzong199/` 后，`git diff --stat` 应该只看到 template.json 和两张底图的变化。

- [ ] **Step 1: 写失败测试** `tests/test_template_auto.py`（都用 `templates/guanzong199` 的底图）
  - `test_auto_anchors_199`：正面结果有 4 个，每个中心与 `t.faces["front"].align.anchors` 对应中心的距离 < 0.5 mm；背面返回 `None`。
  - `test_auto_frames_199_back`：返回 2 个框，排序后与模板的 `frames` 逐个比较，x、y、w、h 的误差都 < 1.5 mm。
  - `test_auto_sync_199`：正面恰好 1 组 vertical，12 个；第一个中心 y ≈ 51.3 ± 0.5 mm，最后一个中心 y ≈ 132.0 ± 0.5 mm；背面返回 `[]`。
  - `test_snap_recovers_perturbed_block`：对 `t.objective[0].grid.cells` 做平移 (+1.0, −0.8) mm、缩放 1.02（以第一个格为原点）后，调用 `snap_grid`：`ok`；吸附结果与原格子逐格中心误差的最大值 < 0.35 mm；`inliers / total ≥ 0.9`。
  - `test_snap_rejects_blank_area`：对纸面空白区域内的一个 3×3 网格（如 x 300–320、y 250–260，必要时先看底图确认是空白），返回 `ok=False`，`cells` 与输入相同。
  - `test_grid_in_rect_shape_and_bounds`：`grid_in_rect((10,20,30,60), 15, 5, True, block_every=5)` 得到 15×5；所有格都在矩形内；第 5 题与第 6 题的间距比第 1 题与第 2 题的间距大 50%（±1%）。
  - `test_load_pages_pdf_and_image(tmp_path)`：`samples/raw/8k-199.pdf` 返回 2 页，dpi 都是 300，形状约 (3188, 4606)；一张写入了 `dpi=(200,200)` 的 PNG 返回 dpi 200；一张没有 dpi 信息的 PNG 返回 300；内容是乱码的 `.jpg` 抛 `ValueError`，消息含文件名。
  - `test_import_base_deskews_back`：对 199 PDF 的第 2 页调用 `import_base`：`method == "frames"`，`abs(skew_deg - 1.45) < 0.2`；对结果图再算一次纠偏角，`abs(...) < 0.1`；`paper ≈ (390.0, 269.9) ± 0.5`。
  - `test_rebuilt_199_template`：`templates/guanzong199` 的 `version == 2`；`len(t.sync) == 1` 且有 12 个同步头；考号最后一列（第 15 位）"0" 格中心 x 在 187.0 ± 0.3 mm（与印刷框实测一致）；`check_alignment(t, bases) == []`。
- [ ] **Step 2: 运行确认失败** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_template_auto.py -q`。
- [ ] **Step 3: 实现** `auto.py`；修改 pyproject.toml 并安装依赖。
- [ ] **Step 4: 重建 199 模板** `.venv/bin/python tools/build_template_199.py samples/raw/8k-199.pdf templates/guanzong199`。输出要显示正反面对齐成功、考号吸附通过、同步头 12 个。
- [ ] **Step 5: 运行确认通过**：先跑 `tests/test_template_auto.py`，再跑全部快速测试，最后跑 `-m slow`。考号格换成真实位置之后，回归指标仍须达标（模拟数据按新模板坐标涂卡）。
- [ ] **Step 6: Commit** `feat(template): 自动找定位点/外框/同步头、选项框吸附、底图导入；199 模板修正考号格并加入同步头（v2）`

---

### Task 3: 识别引擎与输出支持新区域

**Files:**
- Modify: `src/scan_answer/engine/omr.py`、`engine/recognize.py`、`engine/state.py`、`engine/result.py`、`synth/generate.py`、`app/results.py`、`docs/output-format.md`
- Test: `tests/test_engine_ext.py`；`tests/conftest.py` 新增夹具 `ext_template`

**Interfaces:**
- `omr.decide_idx(fills: np.ndarray, qtype: str, th: Thresholds) -> tuple[list[int], str]`：返回选中的下标（按下标升序）和状态，判定规则与现有 `decide` 完全相同。`decide` 改为调用它，再用 labels 拼出答案（行为不变，现有测试照常通过）。
- `SheetAnalysis` 追加字段：
  - `omr_field_fills: dict[str, np.ndarray]`，默认空；
  - `choose_fills: dict[str, np.ndarray]`，默认空；
  - `sync: dict[str, dict]`，默认空，结构为 `{面: {"found": int, "total": int, "max_offset_mm": float | None}}`。
- `SheetDecision` 追加字段：`omr_fields: dict[str, dict]`、`choose_one: list[dict]`。
- `engine.recognize.measure_sync(work: np.ndarray, marks: list[Rect], dpi: float, radius_mm: float = 1.5) -> list[tuple[float, float] | None]`：对每个同步头，在 ±radius 窗口内取 `(min + max) / 2` 作阈值；窗口内 `max - min < 60`，或暗像素面积小于同步头面积的 40% 时，返回 `None`；否则返回暗像素质心相对模板中心的偏移 (dx, dy)，单位 mm。
- result.json 新增内容（同时写进 `docs/output-format.md`，并补充 5 个新警告代码）:
  - `student.omr_fields[key] = {"label", "value", "status", "digits": [不是 ok 的位], "image": "images/omr_{key}.jpg"}`。判定方式和考号相同，逐位判定：全部 ok 为 `ok`；全部空为 `blank`；其余为 `partial`，没识别出的位用 `?` 占位。`partial` 发 `OMR_FIELD_PARTIAL`，`blank` 发 `OMR_FIELD_BLANK`。
  - `choose_one = [{"key", "label", "questions": [...], "chosen": [选中的题号], "status", "fill": {题号: 涂黑度}}]`。用 `decide_idx(fills, "single", th)` 判定。状态为 `blank`、`multiple`、`uncertain` 时，分别发 `CHOICE_BLANK`、`CHOICE_MULTIPLE`、`CHOICE_UNCERTAIN`。
  - `scan.faces[面].sync`：只在该面配置了同步头时出现，`max_offset_mm` 保留 2 位小数，一个都没找到时为 `null`。不发警告。
- `state.py`：新字段写入 state；读取时用 `.get(..., {})` 兼容旧 state。`analysis_fills` 把 `omr_field_fills` 和 `choose_fills` 也纳入批次阈值的拟合。
- `synth.generate.make_sheet`：
  - 每个 `omr_field`：每位随机填一个值，10% 的样本漏涂一位，truth 记 `truth["omr_fields"][key] = {"value", "status"}`；
  - 每个选做组：按 0.90 选一题、0.05 空、0.05 多涂，truth 记 `truth["choose_one"][key] = {"chosen": [...], "status"}`；
  - 模板里没有这些区域时，不调用随机数，保证现有数据集的随机序列不变。
- `app/results.py`：
  - `face_overlays` 新增两种 kind：`omr`（蓝，取 omr_field 选中的格）、`choice`（橙，取选做组选中的格）。状态不是 ok 的，沿用 `exam_bad` / `bad`。
  - `WARNING_TEXT` 增加 5 个新代码的中文：
    - `OMR_FIELD_PARTIAL` → “{label}有位未识别”
    - `OMR_FIELD_BLANK` → “{label}未填涂”
    - `CHOICE_BLANK` → “选做题未填涂”
    - `CHOICE_MULTIPLE` → “选做题多涂”
    - `CHOICE_UNCERTAIN` → “选做题涂得浅或擦除不净”

    其中 `{label}` 取警告的 `msg` 字段：引擎生成这两个警告时，`msg` 写成上面的中文。

**夹具 `ext_template`**（会话级，放在 `tests/conftest.py`）：以 `templates/guanzong199` 为基础，复制到临时目录后修改并保存：
- 加入 `OmrFieldSpec("paper_type", "AB 卷", OmrGrid("front", ["A","B"], grid_cells((40.0, 236.0), 1, 2, 6.0, 0, (3.8, 2.4))))`；
- 加入 `ChooseOneSpec("opt1", "选做", "front", ["56","57"], [(120.0, 234.8, 3.8, 2.4), (130.0, 234.8, 3.8, 2.4)])`。这两处都在“阴影部分请勿作答”区域里，模拟卡上该区域是空白的，减去底图后涂黑度从 0 起算；
- 把 `objective[3]`（46–55）改成 `qtype="judge"`，`labels=["T","F"]`，只保留每题前两个格。

- [ ] **Step 1: 写失败测试** `tests/test_engine_ext.py`
  - `test_decide_idx_matches_decide`：用 `np.random.default_rng(0)` 生成 200 组随机涂黑度，对 single、multiple 两种题型，`decide` 的结果都应等于由 `decide_idx` 下标拼出的答案和状态。
  - `test_ext_template_recognized(ext_template, tmp_path)`：用 `generate_dataset` 在 `ext_template` 上生成 20 张卡，`seed=3`，跑 `run_batch`（与计划 2 的测试用法相同）。凡是 truth 没有物理异常的卡，逐张比对：
    - `student.omr_fields.paper_type.value/status` 等于 truth；
    - `choose_one[0].chosen/status` 等于 truth；
    - 判断题 46–55 的答案和状态都等于 truth；
    - `scan.faces.front.sync.found == 12`，`max_offset_mm < 1.0`。
  - `test_old_state_without_new_fields_reloads`：把一份 `analysis_to_dict` 的结果去掉新键后，`analysis_from_dict` 不报错，三个新字段都是空 dict。
  - `test_ext_warnings_and_overlays`：构造一份 `choose_one` 为 `multiple`、`omr_fields` 为 `partial` 的 result dict，`warning_text` 分别返回“选做题多涂”和“AB 卷有位未识别”；`face_overlays(tpl, result, "front")` 中 kind 为 `choice` 或 `bad` 的覆盖项数量 ≥ 1。
  - `test_existing_dataset_unchanged`：确认没有新区域的模板不会改变随机序列。做法：在动手改代码之前（Task 2 完成后的 HEAD），用 199 模板（v2）运行 `generate_dataset(..., 3, seed=17)`，把 3 个 truth.json 的 sha256 记成测试里的常量；实现完成后重新生成，sha256 必须完全相同。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现**：按上面的接口改 engine、state、result、synth、app。`render_images` 为每个 omr_field 输出 `images/omr_{key}.jpg`，裁切范围是网格外包框加 2 mm 边距。
- [ ] **Step 4: 运行确认通过**：先跑 `tests/test_engine_ext.py`，再跑全部快速测试和 `-m slow`。
- [ ] **Step 5: Commit** `feat(engine): 识别自定义填涂字段、选做组、判断题，测量同步头偏移`

---

### Task 4: 可编辑模板文档、撤销栈与试识别

**Files:**
- Create: `src/scan_answer/template/doc.py`、`src/scan_answer/app/trial.py`
- Modify: `src/scan_answer/app/library.py`（新增 `user_templates_dir()`；`list_templates` 总是额外扫描它）
- Test: `tests/test_template_doc.py`、`tests/test_trial.py`

**Interfaces (doc.py):**
```python
KINDS = ("anchor", "frame", "sync", "exam_no", "omr_field", "barcode", "absent", "field",
         "objective", "subjective", "choose_one", "ignore")
KIND_LABELS = {"anchor": "定位块", "frame": "定位外框", "sync": "同步头", "exam_no": "考号", "omr_field": "填涂字段",
               "barcode": "条码", "absent": "缺考标记", "field": "信息栏", "objective": "客观题",
               "subjective": "主观题", "choose_one": "选做组", "ignore": "忽略区"}
KIND_COLOR = {"anchor": "loc", "frame": "loc", "sync": "loc", "exam_no": "id", "omr_field": "id", "barcode": "bar",
              "absent": "info", "field": "info", "objective": "obj", "subjective": "sub", "choose_one": "sub",
              "ignore": "info"}   # 取 theme.REGION 的键

@dataclass(frozen=True)
class RegionRef:
    kind: str
    index: int = 0       # 该类别中的序号；exam_no/absent 恒为 0
    sub: int = 0         # sync 的第几个同步头；subjective 的第几个区域

class TemplateDoc:
    def __init__(self, t: Template, bases: dict[str, np.ndarray], directory: Path | None): ...
    template: Template; bases: dict[str, np.ndarray]; directory: Path | None; dirty: bool
    @classmethod
    def load(cls, directory: str | Path) -> "TemplateDoc"
    @classmethod
    def new(cls, name: str, faces: dict[str, ImportedBase]) -> "TemplateDoc"
        # 每面自动设置定位方式：auto_anchors 成功则用 anchors，否则 auto_frames 非空则用 frames，
        # 都不行时设为 anchors 且列表为空（保存时由校验拦下）；同时把 auto_sync 结果加入 template.sync
    def regions(self, face: str) -> list[RegionRef]          # 该面所有可显示区域，按 KINDS 顺序
    def rect(self, ref: RegionRef) -> Rect                     # 网格类返回格子外包框
    def face_of(self, ref: RegionRef) -> str
    def title(self, ref: RegionRef) -> str                     # 树和画布标签：如 "1–15"、"写作 56 · 2"、"考号 15 位"
    def set_rect(self, ref: RegionRef, rect: Rect) -> None     # 网格类：所有格按外包框的变化做仿射缩放平移
    def add(self, kind: str, face: str, rect: Rect) -> RegionRef   # 新区域给默认参数（见下）
    def remove(self, refs: list[RegionRef]) -> None
    def params(self, ref: RegionRef) -> dict                   # 属性面板用（见 Task 6）
    def set_params(self, ref: RegionRef, p: dict) -> None      # 网格参数变化时用 grid_in_rect 在当前外包框内重新生成
    def snap(self, ref: RegionRef) -> SnapResult               # 成功则写回格子
    def auto_align(self, face: str, what: Literal["anchors", "frames", "sync"]) -> str   # 返回中文结果说明
    def problems(self) -> list[str]                            # validate_template + check_alignment
    def save(self, directory: Path | None = None) -> Path      # 见下
    def snapshot(self) -> dict
    def restore(self, snap: dict) -> None

class UndoStack:
    def __init__(self, doc: TemplateDoc, limit: int = 200): ...
    def push(self, label: str) -> None     # 在修改之前调用：记录当前快照，并清空重做栈
    def undo(self) -> bool; def redo(self) -> bool
    def can_undo(self) -> bool; def can_redo(self) -> bool
```
- **新区域的默认参数**：
  - objective：起始题号 = 现有最大题号 + 1，5 题，选项 A–D，单选，横排，`group=5`；
  - exam_no：10 位；
  - omr_field：key 依次为 `field1`、`field2`……，label 为 “填涂字段 N”，1 位，候选值 0–9；
  - subjective：题号 = 现有最大题号 + 1，`essay`，`zh`；
  - choose_one：题号取最后两道主观题；
  - field、barcode：key 依次为 `field1`、`barcode1`……。
- **`save(directory)` 的规则**：
  1. 有问题时抛 `ValueError("\n".join(problems))`。
  2. 目标目录：传了 `directory` 就用它；否则用 `self.directory`。但 `self.directory` 为空，或位于 `bundled_templates_dir()` 之下，或不可写时，改用 `user_templates_dir() / 安全文件名(模板名)`，已存在就加 `_2`、`_3`……。
  3. 写之前 `version += 1`。新模板的初始 version 为 0，第一次保存后为 1。
  4. 调用 `save_template(t, dir, bases)`，然后 `dirty=False`，`self.directory=dir`，返回 dir。
- 每个修改方法都把 `dirty` 置为 True。`snapshot` / `restore` 只处理 template（`template_to_dict` / `template_from_dict`），底图不进快照。
- **`app/trial.py`**：`trial_recognize(t: Template, bases: dict[str, np.ndarray], pairs: list[list[str]]) -> list[TrialSheet]`，其中 `TrialSheet(sources: list[str], ok: bool, message: str, decision: SheetDecision | None, warped: dict[str, np.ndarray])`：
  - `warped` 是按模板 DPI 对齐后的整页图，供画布显示；
  - 用 `analyze_sheet` + `decide_sheet`，阈值用默认的 `Thresholds()`；
  - `PhysicalError` 或读图失败时，`ok=False`，`message` 为 `"{代码}: {说明}"` 或 `"无法读取 {文件}"`；
  - 不写任何文件。
- `library.user_templates_dir() -> Path`：返回 `config_dir() / "templates"`，并确保目录存在。

- [ ] **Step 1: 写失败测试**
  - `tests/test_template_doc.py`（使用 `app_home` 夹具）:
    - `test_regions_cover_199`：`TemplateDoc.load(templates/guanzong199)`。正面区域包括 4 个 anchor、12 个 sync（`sub` 为 0–11）、exam_no、2 个 barcode、absent、2 个 field、4 个 objective、1 个 subjective 区域、1 个 ignore；背面包括 2 个 frame、1 个 field、3 个 subjective 区域。
    - `test_set_rect_scales_grid`：把第一个客观题块的外包框平移 (+2, +3) mm，每个格都平移 (+2, +3)；再把宽度放大到 1.1 倍，最右一列的格子中心按比例移动，格子宽度也按比例变化。
    - `test_set_params_regenerates_and_snap`：把第一个客观题块的题数改成 10，得到 10 组，`dirty=True`；对 199 原块执行 `snap`，结果 `ok`，格子变化 < 0.3 mm。
    - `test_undo_redo`：`push` 之后 `add("field", "front", (100,100,20,10))`，然后 `undo` → 字段数恢复，`redo` → 又多一个；`push` 新操作后 `can_redo() is False`。
    - `test_save_bundled_goes_to_user_dir`：打开随程序提供的 199 模板，改一个字段后 `save()`：返回路径在 `user_templates_dir()` 下；随程序提供的目录的 template.json 修改时间不变；新目录里的 version 为 3；`list_templates([])` 能找到它。
    - `test_save_blocked_on_problems`：删掉 3 个定位块后 `save()` 抛 ValueError，消息含“至少 4 个定位块”，并且没有写出任何文件。
    - `test_new_from_pdf_single_face`：用 199 PDF 第 1 页 `import_base` 后，`TemplateDoc.new("单面测试", {"front": ib})`：定位方式自动为 anchors，有 4 个定位块，sync 1 组；加一个客观题块后吸附通过；`save()` 成功；之后用 `generate_dataset` 在这个模板上生成 2 张卡，跑 `run_batch`，两张都识别成功。（这需要单面模板全流程可用：生成器没有背面时只输出一张图；导入图片时，单面模板每张图就是一张卡，不再两两配对。哪一环现在不支持，就在本任务里补上，并在报告里写明。）
  - `tests/test_trial.py`:
    - `test_trial_ok_and_failures(small_dataset)`：一组正常卡、一组空白页卡（`small_dataset` 的第 3 张）、一组损坏文件。结果依次为：ok 且 decision 有 55 道客观题；`ok=False`，message 以 `BLANK_PAGE` 开头；`ok=False`，message 含 “无法读取”。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** `doc.py`、`trial.py`、`library.user_templates_dir`。
- [ ] **Step 4: 运行确认通过**：先跑这两个测试文件，再跑全部快速测试。
- [ ] **Step 5: Commit** `feat(template): 可编辑模板文档、撤销重做、保存到用户模板目录；试识别`

---

### Task 5: 编辑器画布

**Files:**
- Create: `src/scan_answer/ui/editor/__init__.py`（空文件）、`src/scan_answer/ui/editor/canvas.py`
- Test: `tests/test_ui_editor.py`（本任务的部分）

**Interfaces:**
```python
class TemplateCanvas(QGraphicsView):
    regionEdited = Signal(object, object)   # (RegionRef, Rect)   拖动或缩放结束时发一次；方向键微调每次发一次
    regionDrawn = Signal(str, object)       # (kind, Rect)        画框工具松开鼠标时发出，宽和高都 ≥ 1 mm 才发
    selectionChanged = Signal(list)         # list[RegionRef]
    cursorMoved = Signal(float, float)      # mm
    def set_doc(self, doc: TemplateDoc, face: str) -> None   # 重建全部图元
    def refresh(self) -> None                                # 按 doc 当前内容同步图元位置和标签（不重建底图）
    def set_tool(self, kind: str | None) -> None             # None = 选择工具
    def select(self, refs: list[RegionRef]) -> None
    def selected(self) -> list[RegionRef]
    def set_background(self, img: np.ndarray | None) -> None  # 试识别时临时换成样卡对齐图；None 恢复底图
    def set_marks(self, marks: list[tuple[Rect, str, int]]) -> None  # 试识别叠层：(mm 矩形, 颜色, 填充透明度)
    def zoom(self) -> float; def set_zoom(self, z: float) -> None; def fit(self) -> None
```
- **场景坐标直接用 mm**：底图像素图元按 `25.4 / dpi` 缩放，区域矩形就是 mm 值，不用换算。
- `RegionItem(QGraphicsRectItem)`：
  - 描边颜色取 `theme.REGION[KIND_COLOR[kind]]`，填充 alpha 30，选中时加粗；
  - 左上角显示标签（`doc.title(ref)`），白字配同色底；
  - 网格类区域在框内画出每个格子（细线，cosmetic pen）；
  - 选中时出现 8 个手柄，手柄大小固定 8 屏幕像素，不随缩放变化；
  - 拖动手柄缩放，拖动框体移动；松开鼠标时发 `regionEdited`；宽或高不能被拖到小于 0.5 mm。
- 选择工具：单击选中；Shift 或 Ctrl + 单击加选；在空白处拖动出现橡皮筋框选；选中项变化时发 `selectionChanged`。
- 键盘：方向键使所有选中区域移动 0.1 mm，加 Shift 移动 1 mm；每个区域发一次 `regionEdited`。Delete 键不在画布里处理，由页面处理。
- 缩放：滚轮按 1.15 倍以光标为中心缩放；`set_zoom` 限制在 0.05–8 之间；中键或空格加拖动平移。
- 画布不直接修改 doc，只发信号；页面收到信号后调用 `UndoStack.push` 和 `doc.set_rect` / `doc.add`，再调用 `canvas.refresh()`。

- [ ] **Step 1: 写失败测试** `tests/test_ui_editor.py`（使用 `qtbot`、`app_home`）:
  - `test_canvas_items_and_drag(qtbot)`：`set_doc(TemplateDoc.load(199), "front")` 后，区域图元数等于 `len(doc.regions("front"))`。程序化选中第一个客观题块，模拟方向键 Right，`regionEdited` 发出一次，新 rect 的 x 比原来大 0.1（±1e-6）；加 Shift 再按一次，x 再增加 1.0。
  - `test_canvas_draw_tool(qtbot)`：`set_tool("field")` 后，在视口上从场景坐标 (100, 100) 拖到 (130, 112) mm（用 `mapFromScene` 换算后，经 `QTest.mousePress/mouseMove/mouseRelease` 发送），`regionDrawn` 收到 `("field", rect)`，rect 与 (100, 100, 30, 12) 的各分量误差 < 0.5 mm。只拖动 0.3 mm 时不发信号。
  - `test_canvas_rubber_band_and_handles(qtbot)`：框选一个只包含 4 个定位块之一的范围，`selected()` 恰好返回那 1 个；选中的图元有 8 个可见手柄。拖动右下手柄 +5 mm，得到的 rect 宽高各 +5（±0.2）。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** `canvas.py`。
- [ ] **Step 4: 运行确认通过** `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_ui_editor.py -q`。
- [ ] **Step 5: Commit** `feat(ui): 模板编辑画布——彩色区域、8 手柄、框选、方向键微调、画框工具`

---

### Task 6: 区域树与属性面板

**Files:**
- Create: `src/scan_answer/ui/editor/tree.py`、`src/scan_answer/ui/editor/props.py`
- Test: `tests/test_ui_editor.py`（追加）

**Interfaces:**
- `RegionTree(QTreeWidget)`：
  - `set_doc(doc, face)`、`refresh()`、`select(refs)`；信号 `selectionChanged(list)`。
  - 分组与正式稿一致，依次为：定位（anchor、frame、sync）、考号与填涂字段、条码、信息栏 / 缺考、客观题、主观题、选做组、忽略区。
  - 分组行显示色块、名称和右侧的数量；子行显示 `doc.title(ref)`，右侧是摘要（客观题“75 框”、主观题“正 + 背”、条码“条码”等）。
  - 只列出当前面的区域；主观题跨面时，两面都会出现。
- `PropsPanel(QWidget)`：
  - `set_target(doc, ref | None)`；信号 `paramsChanged(object, dict)`、`rectChanged(object, object)`、`snapRequested(object)`。
  - 页面收到信号后执行 push → `set_params` / `set_rect` / `snap` → refresh。
- 各类区域的表单字段，对应 `doc.params` / `set_params` 的键：

| kind | 字段 |
|---|---|
| objective | `start`(int ≥1)、`count`(1–200)、`options`(2–8，标签可编辑，默认 A–H 截取；判断题默认 T/F)、`qtype`(Segmented：单选/多选/判断)、`direction`(横排/竖排)、`group`(0–50) |
| exam_no | `digits`(1–30)、`direction` |
| omr_field | `key`、`label`、`digits`(1–30)、`values`(文本，逗号分隔或连续字符，如 `0123456789`、`AB`)、`direction` |
| barcode | `key` |
| field | `key`、`label` |
| subjective | `no`、`qtype`(填空/问答/作文)、`content`(中文/英文/数学)、区域列表（显示每个区域所在的面和序号，可删除；新增区域用画框工具，并在弹出菜单里选择“追加到当前主观题”） |
| choose_one | `key`、`label`、`questions`（逗号分隔；数量变化时格子按矩形均分重新生成） |
| sync | `direction`（只读）、数量（只读） |
| anchor / frame / absent / ignore | 只有位置 |

- 所有类型都有位置组：X、Y、宽、高，单位 mm，步长 0.1，编辑完成时发出 `rectChanged`。
- 网格类（objective、exam_no、omr_field）另有：
  - “自动吸附”按钮，发出 `snapRequested`；
  - 吸附结果标签，显示 `SnapResult.message`。失败时用 `theme.ERR` 颜色。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_ui_editor.py`）
  - `test_tree_groups_and_counts(qtbot)`：199 正面的树中，“定位”组数量为 16（4 个定位块 + 12 个同步头），“客观题”组数量为 4；选中“1-15”子行时，发出包含对应 RegionRef 的 `selectionChanged`。
  - `test_props_objective_edit(qtbot)`：把 count 改成 10 并提交，`paramsChanged` 的 dict 中 `count == 10`；切换到“判断”时，options 标签变为 T、F，并发出相应的参数。
  - `test_props_rect_and_snap(qtbot)`：修改 X 后发出 `rectChanged`，新 rect 的 x 与输入一致；点“自动吸附”发出 `snapRequested`。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** `tree.py`、`props.py`（样式遵守 QSS 约束）。
- [ ] **Step 4: 运行确认通过**。
- [ ] **Step 5: Commit** `feat(ui): 模板区域树与属性面板`

---

### Task 7: 模板页（编辑器整合）、新建模板、试识别、结果页展示新字段

**Files:**
- Create: `src/scan_answer/ui/editor/new_dialog.py`
- Modify（重写）: `src/scan_answer/ui/pages/template_page.py`
- Modify: `src/scan_answer/ui/pages/results_page.py`、`src/scan_answer/ui/main_window.py`、`tools/ui_screenshots.py`、`docs/gui.md`
- Test: `tests/test_ui_editor.py`（追加）、`tests/test_ui_pages.py`（如有需要，调整已有的模板页用例）

**Interfaces:**
- `TemplatePage(settings)`，信号 `changed`（保留）。页面用 `QStackedWidget` 分为两个视图：
  - **列表视图**：现有模板表格，增加“新建模板…”“编辑”“添加模板目录…”按钮，双击表格行进入编辑。
  - **编辑视图**：按正式稿 `final-template.png` 布局，从左到右为区域树、画布、属性面板。
    - 顶部栏：
      - 模板名和摘要（纸张尺寸、面数、未保存标记“●”）；
      - 自动找定位点；
      - 自动检测外框；
      - 自动识别同步头；
      - 区域工具：“选择”按钮，加一个下拉框，列出 `KINDS` 中除 anchor、frame、sync 之外的类型，另外手动画定位块和外框也放在这里；
      - 自动吸附（对选中的网格类区域逐个执行；没有选中时，对当前面的全部网格执行），结果显示在状态行；
      - 试识别…；
      - 撤销、重做；
      - 正面 / 背面 Segmented（只有一面时，背面按钮置灰）；
      - −、缩放百分比、+；
      - 返回列表；
      - 保存模板（`primary` 按钮）。
    - 底部状态行：光标坐标（mm）、本面定位状态（如“定位点 4 / 4”或“外框 2”）、纸张尺寸、操作提示。
- 方法：`refresh()`、`add_dir(d)`（保留）、`open_editor(directory: str | Path)`、`new_template(name: str, files: list[str])`（测试用，跳过对话框）、`save() -> bool`、`can_leave() -> bool`、`run_trial(files: list[str])`。
  - `can_leave()`：没有未保存修改时返回 True；有修改时弹 `QMessageBox.question("模板有未保存的修改", "放弃修改并离开？")`。
  - `save()`：
    - 有问题时，用 `QMessageBox.warning("模板还不能保存", 问题列表)` 显示，返回 False；
    - 成功时，状态行显示“已保存到 {目录}（版本 N）”，然后发出 `changed`；
    - 保存位置与原目录不同（另存到用户模板目录）时，再用 `QMessageBox.information` 说明原因。
  - `run_trial(files)`：先按扫描时的配对规则，把文件分组成每张卡一组。规则复用 `source/folder.py` 现有的配对函数，计划 2 的配对规则见设计文档 §5.2①。然后在后台线程（`threading.Thread`，结果经 Qt 信号回到界面）执行 `trial_recognize`。
    - 结果显示在属性面板位置的“试识别结果”列表里：每张卡一行，显示成功或失败原因。
    - 选中一张成功的卡后，画布 `set_background` 换成它的对齐图，再用 `set_marks` 叠加：选中的选项为绿色，状态不是 ok 的题为红色，考号和填涂字段为蓝色，选做组为橙色。
    - “退出试识别”按钮恢复底图，不改动模板。
- `NewTemplateDialog`：
  - 包含模板名、正面文件、背面文件（可选）三项。正面选的是 2 页的 PDF 时，自动把第 2 页作为背面。
  - 预览显示纸张尺寸、纠偏角度、检测到的定位方式。
  - 点“创建”后，返回 `(name, {face: ImportedBase})`。
  - 读取失败时，在对话框内显示错误，不关闭对话框。
  - PDF 超过 2 页时提示“PDF 有 N 页，只用前 2 页”。
- `MainWindow`：`show_page` 离开模板页之前，以及 `closeEvent` 中，都要先调用 `template_page.can_leave()`，返回 False 时取消切换或关闭。
- 结果页：
  - 信息行追加自定义填涂字段，格式为 “{label} {value}”；
  - 答案表末尾为每个选做组追加一行：题号列为 `选做：{label}`，答案列为 chosen 用“、”连接，状态列沿用 `STATUS_TEXT`。
- `tools/ui_screenshots.py` 增加编辑器截图 `template_editor.png`：打开 199 模板，选中“1-15”。
- `docs/gui.md` 增加“模板编辑器”一节，写明制作流程、各工具的用途、保存位置规则、试识别。

- [ ] **Step 1: 写失败测试**（追加到 `tests/test_ui_editor.py`，全部使用 `no_dialogs`）
  - `test_page_open_edit_undo_save(qtbot, app_home, no_dialogs)`：打开 199 模板，移动一个字段（通过画布的方向键）后，标题出现“●”；撤销后，字段位置恢复。再移动一次并 `save()`，返回 True，保存目录在用户模板目录下，`changed` 发出一次，`no_dialogs` 记录了一条 information。
  - `test_page_save_blocked_lists_problems(...)`：删除 3 个定位块（选中后按 Delete）后 `save()` 返回 False，`no_dialogs` 记录的 warning 中含“至少 4 个定位块”。
  - `test_page_new_template_from_pdf(...)`：`new_template("新卡", ["samples/raw/8k-199.pdf"])` 后进入编辑视图，正面和背面都可以切换，树中“定位”组数量 ≥ 4；用“区域工具 → 客观题”在画布上画出覆盖 1–15 题印刷区域的框（坐标取 199 模板的第一个块外包框加 1 mm 边距），然后调用自动吸附，状态行包含“已吸附”。
  - `test_page_trial(qtbot, small_dataset, ...)`：打开 199 模板后，对 `small_dataset` 的 4 张卡共 8 个文件调用 `run_trial`，等待结果。列表有 4 行，其中第 3 张失败，其余成功（失败行显示的文字包含“空白页”）；选中第 1 行后，画布背景已替换，叠层数 > 0；退出试识别后，叠层清空。
  - `test_leave_with_unsaved_changes(...)`：修改后调用 `show_page("exam")`，由 `no_dialogs` 让 question 返回 No，结果仍停在模板页；改为返回 Yes 后，切换成功。如果 `no_dialogs` 目前不支持控制 question 的返回值，就扩展它。
  - `test_results_page_shows_ext_fields(ext_template, ...)`：在 `ext_template` 上生成 2 张卡，`run_batch` 之后用 `ResultsPage.open_batch` 打开，信息行包含“AB 卷”，答案表最后一行以“选做：”开头。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现** `new_dialog.py`、`template_page.py`、results_page 和 main_window 的修改、截图工具、文档。
- [ ] **Step 4: 运行确认通过**：先跑 `tests/test_ui_editor.py` 和 `tests/test_ui_pages.py`，再跑全部快速测试，最后跑 `-m slow`。
- [ ] **Step 5: 截图对照**：运行 `QT_QPA_PLATFORM=offscreen .venv/bin/python tools/ui_screenshots.py /tmp/sa_ui_shots 12`，用 Read 打开 `template_editor.png`，与 `design-demos/shots/final-template.png` 对照：
  - 左侧区域树有分组、色块、数量；
  - 画布上各类区域按色码显示，选中块有 8 个手柄，网格内能看到格子；
  - 右侧属性面板有题目参数、位置、吸附结果；
  - 顶部工具栏与底部状态行齐全。

  明显不一致（错位、文字截断、颜色不对）时，报告 `DONE_WITH_CONCERNS` 并附截图路径。
- [ ] **Step 6: Commit** `feat(ui): 可视化模板编辑器——新建、编辑、吸附、试识别、保存；结果页显示填涂字段与选做组`

---

## 与设计文档的对应与偏差

| 设计文档 | 本计划 |
|---|---|
| 4.1 导入底图：扫描空白卡或导入 PDF/图片，统一换算成 300 DPI | Task 2 `load_pages` / `import_base`；PDF 用 pypdfium2 渲染 |
| 4.1 系统自动检测定位方式，用户在画布上确认或修正 | 新建时自动检测（Task 4 `TemplateDoc.new`）；工具栏可重新检测；也可以手动画定位块和外框 |
| 4.1 客观题块框选整块、填参数后自动吸附；个别格手动拖动 | 整块吸附（仿射拟合）；**逐个拖动单个格子不做**，吸附失败时可调整整块的位置、尺寸和参数 |
| 4.1 试识别 3–5 张样卡，结果叠加在画布上 | Task 4、7；张数不限 |
| 4.1 保存时带版本号 | 每次保存 version + 1；随程序提供的模板另存到用户模板目录 |
| 4.2 同步头：辅助定位，用于行对齐 | 按用户决定：只测量偏移并写进 result.json，不参与校正（实验依据见开头） |
| 4.2 自定义填涂字段、选做组 | Task 1、3、6、7 |
| 4.2 客观题“选项标签”“每组题数” | 标签可编辑；`group` 保存进模板，供重新生成网格时使用 |
| 4.2 主观题跨面、多个区域、拼接顺序 | 按区域列表的顺序拼接；属性面板可以删除区域，追加区域通过画框工具 |
| 8 模板页：撤销重做、框选多个、方向键微调、8 个手柄、缩放、拖动 | Task 5、7 |
| 4.4 `fingerprint` | 仍然不用（识别靠与底图的相关性，计划 1 已实现） |
| 199 模板 | 加入 12 个同步头，版本 2（考号格经实测原本正确，未改） |
