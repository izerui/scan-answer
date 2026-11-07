# build_windows.ps1 — 在 Windows 上打包 答题卡扫描识别
# 用法: powershell -ExecutionPolicy Bypass -File packaging\build_windows.ps1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$VenvDir = Join-Path $ProjectRoot ".build_venv"
$SpecFile = Join-Path $ProjectRoot "packaging" "scan-answer.spec"

Write-Host "=== 创建虚拟环境 ==="
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv venv $VenvDir
} else {
    python -m venv $VenvDir
}

# Activate
$ActivateScript = Join-Path $VenvDir "Scripts" "Activate.ps1"
. $ActivateScript

Write-Host "=== 安装依赖 ==="
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install "$ProjectRoot[dev]" pyinstaller
} else {
    pip install "$ProjectRoot[dev]" pyinstaller
}

Write-Host "=== 运行 PyInstaller ==="
pyinstaller --noconfirm $SpecFile

$OutputDir = Join-Path $ProjectRoot "dist" "答题卡扫描识别"
Write-Host ""
Write-Host "=== 打包完成 ==="
Write-Host "输出目录: $OutputDir"
