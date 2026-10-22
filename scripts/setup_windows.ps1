<#
.SYNOPSIS
    CosyVoice Reader 一键安装（Windows）。

.DESCRIPTION
    按顺序完成：检查 Python → 建虚拟环境 → 装 PyTorch(CUDA) 与依赖 →
    拉取上游 CosyVoice 源码（固定 commit）→ 打 Windows 原生运行补丁 →
    （可选）下载模型权重。

    脚本可以重复执行：已存在的步骤会跳过。

.EXAMPLE
    # 全新安装，模型从 ModelScope 下（国内推荐，约 10 GB）
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

.EXAMPLE
    # 模型已经拷好了，只装环境
    powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Model none -ModelDir "D:\models\Fun-CosyVoice3-0.5B-2512"
#>
param(
    # 模型来源：modelscope / huggingface / none（不下载，自己准备）
    [ValidateSet('modelscope', 'huggingface', 'none')]
    [string]$Model = 'modelscope',

    # 模型存放目录。默认 <项目上级>\models\Fun-CosyVoice3-0.5B-2512
    [string]$ModelDir = '',

    # PyTorch 轮子来源。CUDA 13.0 对应 cu130
    [string]$TorchIndexUrl = 'https://download.pytorch.org/whl/cu130',
    [string]$TorchVersion = '2.11.0',

    # 跳过 PyTorch 安装（已经装过时可提速）
    [switch]$SkipTorch
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step([string]$text) { Write-Host "`n=== $text ===" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "  [OK] $text" -ForegroundColor Green }
function Warn([string]$text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }

# ── 1. Python ────────────────────────────────────────────────
Step '1/6 检查 Python'
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw '未找到 python。请安装 Python 3.11 并勾选 "Add python.exe to PATH"。' }
$ver = (& python -c "import sys;print('%d.%d'%sys.version_info[:2])").Trim()
Write-Host "  python $ver  ($($py.Source))"
if ([version]$ver -lt [version]'3.10' -or [version]$ver -ge [version]'3.13') {
    Warn "本项目在 3.11 上验证通过；$ver 可能遇到依赖编译问题。"
}

# ── 2. 虚拟环境 ──────────────────────────────────────────────
Step '2/6 创建虚拟环境 .venv'
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $venvPy) {
    Ok '已存在，跳过'
} else {
    & python -m venv .venv
    Ok "已创建 $venvPy"
}
$venvPy = Join-Path $root '.venv\Scripts\python.exe'

# ── 3. PyTorch ───────────────────────────────────────────────
Step '3/6 安装 PyTorch (CUDA)'
if ($SkipTorch) {
    Ok '按参数要求跳过'
} else {
    & $venvPy -m pip install --upgrade pip wheel
    & $venvPy -m pip install "torch==$TorchVersion" "torchaudio==$TorchVersion" --index-url $TorchIndexUrl
    if ($LASTEXITCODE -ne 0) {
        Warn "CUDA 版装失败，回退到默认源（CPU 版，速度会慢很多）"
        & $venvPy -m pip install "torch==$TorchVersion" "torchaudio==$TorchVersion"
    }
    Ok 'PyTorch 完成'
}

# ── 4. 项目依赖 ──────────────────────────────────────────────
Step '4/6 安装项目依赖 requirements.txt'
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'requirements.txt 安装失败，请看上方 pip 输出。' }
Ok '依赖完成'

# ── 5. 上游 CosyVoice 源码 + 补丁 ────────────────────────────
Step '5/6 拉取上游 CosyVoice 源码'
$cvDir = Join-Path $root 'CosyVoice'
$pin = '074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc'
if (Test-Path (Join-Path $cvDir '.git')) {
    Ok '已存在，跳过克隆'
} elseif (Test-Path $cvDir) {
    Warn "CosyVoice 目录存在但不是 git 仓库，请手动删除后重跑：$cvDir"
} else {
    git clone https://github.com/FunAudioLLM/CosyVoice.git $cvDir
    if ($LASTEXITCODE -ne 0) { throw '克隆 CosyVoice 失败，请检查网络或代理。' }
    git -C $cvDir checkout $pin
    git -C $cvDir submodule update --init --recursive
    Ok "已克隆并切到 $($pin.Substring(0,8))"
}
Write-Host '  应用 Windows 原生运行补丁...'
& $venvPy (Join-Path $root 'scripts\apply_patches.py')
if ($LASTEXITCODE -ne 0) { throw '补丁失败。' }

# ── 6. 模型权重 ──────────────────────────────────────────────
Step '6/6 模型权重（约 10 GB，不在仓库里）'
if ($Model -eq 'none') {
    if ($ModelDir) {
        Warn "未下载模型。请把模型放到：$ModelDir"
        Warn "或用环境变量指向：`$env:COSYVOICE_MODEL_DIR='$ModelDir'"
    } else {
        Warn '未下载模型。稍后可运行：.venv\Scripts\python.exe scripts\download_model.py --source modelscope'
    }
} else {
    $dlArgs = @((Join-Path $root 'scripts\download_model.py'), '--source', $Model)
    if ($ModelDir) { $dlArgs += @('--dest', $ModelDir) }
    & $venvPy @dlArgs
    if ($LASTEXITCODE -ne 0) { throw '模型下载失败，可重跑本脚本续传（已下载文件会跳过）。' }
}

Write-Host "`n安装完成。" -ForegroundColor Green
Write-Host '下一步：双击 scripts\start.bat（或运行 scripts\make_shortcut.ps1 建桌面快捷方式）'
Write-Host '首次启动要加载模型，约 30-120 秒；浏览器会自动打开 http://127.0.0.1:7860'
