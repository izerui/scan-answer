# 打包指南

本项目使用 PyInstaller 打包为桌面应用程序。

## 环境要求

- Python 3.14+
- 建议使用 [uv](https://github.com/astral-sh/uv) 管理虚拟环境（速度更快），也支持标准 `python -m venv`

### Windows 额外要求

- **64 位 TWAIN 驱动**：扫描仪功能需要安装扫描仪厂商提供的 64 位 TWAIN 驱动程序
- **TWAINDSM.dll**：通常随扫描仪驱动一起安装；如缺失，可从 [TWAIN 官网](https://twain.org) 下载

### macOS 说明

- macOS 版本仅支持**导入图片**模式，不支持扫描仪（TWAIN 仅限 Windows）
- 需要 Xcode Command Line Tools

## 打包步骤

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1
```

输出目录：`dist\答题卡扫描识别\`，运行 `dist\答题卡扫描识别\答题卡扫描识别.exe`。

### macOS

```bash
bash packaging/build_macos.sh
```

输出目录：`dist/答题卡扫描识别.app`，双击运行或用 `open dist/答题卡扫描识别.app`。

## 设置与数据目录

打包后的程序将用户设置和数据存放在以下位置：

| 平台 | 路径 |
|------|------|
| Windows | `%APPDATA%\scan-answer` |
| macOS | `~/Library/Application Support/scan-answer` |

### 模板目录

- **内置模板**：随程序打包，位于安装目录内，**只读**
- **用户模板**：保存到设置目录下的 `templates/` 子目录，可读写

## 常见问题

### 子进程启动失败

打包后的程序使用 `multiprocessing` 的 spawn 模式启动识别工作进程。入口文件 `packaging/entry.py` 在所有导入之前调用了 `multiprocessing.freeze_support()`，这是正常运行的前提。如果遇到子进程无限重启或启动失败，请确认：

1. 使用的是项目提供的 `packaging/entry.py` 作为入口
2. 没有修改入口文件中 `freeze_support()` 的位置

### 杀毒软件误报

PyInstaller 打包的程序可能被部分杀毒软件误报。解决办法：

- 将 `dist\答题卡扫描识别\` 目录添加到杀毒软件的排除列表
- 使用企业版杀毒软件时，联系 IT 管理员添加白名单

### 中文路径

程序已处理了 OpenCV 在 Windows 上不支持非 ASCII 路径的问题。但建议：

- 安装目录尽量使用英文路径
- 工作目录（存放扫描结果的目录）可以使用中文路径
