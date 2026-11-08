# 工作包 1 实施记录：PDF 输入与完整题图

本包基于 `d169ace`，改动在当前工作区，尚未提交或重新打包。

## 已实现

- 扫描页、命令行和模板试识别可输入图片、填写 PDF；扫描页可选择单个文件或目录。
- 每个 PDF 在自己的文件内按模板面数配组，不跨文件补页；奇数尾页进入缺面处理。
- 原 PDF 按 SHA-256 归档到批次 `sources/`；渲染页保存在 `raw/`。manifest、state、result 记录文件、页码、内容摘要和对应页面；对账恢复也保留来源。
- 所有客观题均裁图；主观题按区域顺序输出，JSON 增加 `sheet_id`、`question_id`、`images`、`regions` 和 `input_pages`。
- 图片分类存储，编码特殊字符和大写，长键使用稳定摘要短名；原始字段和题号保留在 JSON。不允许重复路径静默覆盖，也不发布缺题图或无作答区域的结果。
- PDF 调用共用原生访问锁；退出输入关闭读取器；后续坏 PDF 不影响已提交好卡完成登记。
- 保留旧结果读取和既有图片接口；新路径由 JSON 提供，消费者不应自行拼接旧文件名。

## 验证

| 项目 | 结果 |
|---|---|
| 修改前快速基线 | 443 passed，1 deselected |
| 最终快速回归 | 472 passed，1 deselected；29 条新增用例 |
| 现有 200 张模拟回归 | 1 passed，472 deselected，270.34s；沿用固定 seed=2026 的原有回归 |
| PDF 纸幅参数化 | A3、8K、A4、A5、自定义尺寸渲染用例通过；不等同于所有版式识别验收 |
| 来源与文件关联 | 真实 8K 合成卡转 PDF、识别、奇数尾页、目录搬移和 manifest 对账用例通过 |
| PDF 错误和资源释放 | 损坏/密码保护 PDF、后台读取串行、停止关闭和多进程错误收尾用例通过 |
| 界面 | offscreen 入口测试通过；扫描页截图已检查 |
| 独立审查 | 提出的长名称和来源恢复问题均以失败用例确认并修复 |
| 静态检查 | 修改的生产代码、新测试 Ruff F 检查通过；git diff --check 通过 |
| IDE 检查 | 当前工具列表未提供 JetBrains MCP，未执行 IDE lint/build |

测试中出现 5 条依赖的 SWIG 弃用警告，与修改前一致。两个既有测试文件中的 6 条未使用项 lint 提示已与 HEAD 对照，属于原有问题，本包未顺带清理。

## 范围边界

- 这是第一个工作包，不代表整个验收报告已整改完。
- 尚未实现全套导入去重/预检、PDF 专用重扫恢复界面、完整试识别题图预览、模板修订闭环、扫描仪背压/释放确认、OCR 统一任务管理等后续事项。
- PDF 读取失败时中止当前输入并明确报错；已提交的卡完成登记，批次不自动标为已完成。密码保护文件暂不提供输入密码解锁的界面。
- 不修改现有 8K 样卡和模板，不重新解释纸张为 A3；继续支持按模板毫米尺寸处理 A3 及以下纸幅。
- Windows 真机、驱动能力、性能和新安装包未在本包验收。旧 `.app` 不包含本次源码改动。

## 使用入口

从源码启动 `.venv/bin/scan-answer-gui`，在扫描页选择“文件导入”，点击“文件…”选择填写 PDF，或“文件夹…”选择答卷目录；模板页“试识别…”也接受 PDF。

命令行沿用原入口：

```bash
.venv/bin/scan-answer recognize --template templates/guanzong199 --input /path/to/answers.pdf --out /path/to/output
```

输出字段和新图片路径见 [输出格式](/Users/liuyuhua/github/scan-answer/docs/output-format.md)；具体设计见 [工作包 1 设计](/Users/liuyuhua/github/scan-answer/docs/superpowers/specs/2026-11-08-document-input-evidence-design.md)。
