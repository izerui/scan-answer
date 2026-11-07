#!/usr/bin/env bash
# build_macos.sh — 在 macOS 上打包 答题卡扫描识别
# 用法: bash packaging/build_macos.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$PROJECT_ROOT/.build_venv"
SPEC_FILE="$PROJECT_ROOT/packaging/scan-answer.spec"

echo "=== 创建虚拟环境 ==="
if command -v uv &>/dev/null; then
    uv venv "$VENV_DIR"
else
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "=== 安装依赖 ==="
if command -v uv &>/dev/null; then
    uv pip install "$PROJECT_ROOT[dev]" pyinstaller
else
    pip install "$PROJECT_ROOT[dev]" pyinstaller
fi

echo "=== 运行 PyInstaller ==="
pyinstaller --noconfirm "$SPEC_FILE"

echo ""
echo "=== 打包完成 ==="
echo "输出目录: $PROJECT_ROOT/dist/答题卡扫描识别.app"
