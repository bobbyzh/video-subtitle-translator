# -*- coding: utf-8 -*-
# 视频字幕提取翻译工具 —— 一键安装/修复环境
# 用法：右键“以 PowerShell 运行”，或在命令行执行
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1
# 可选：-Model small|base|medium|tiny   -SkipModel（不预下载语音模型）
#       -SkipOffline（不安装离线翻译引擎 Hy-MT2 + llama.cpp）

param(
    [string]$Model = "small",
    [switch]$SkipModel,
    [switch]$SkipOffline
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Py = Join-Path $Root "runtime\python\python.exe"
$PipIndexes = @(
    "https://pypi.tuna.tsinghua.edu.cn/simple",
    "https://mirrors.aliyun.com/pypi/simple",
    "https://pypi.org/simple"
)

function Write-Step($text) {
    Write-Host ""
    Write-Host "==> $text" -ForegroundColor Cyan
}

if (-not (Test-Path $Py)) {
    Write-Host "找不到便携运行环境 runtime\python\python.exe，安装包可能不完整。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Step "检查/安装 Python 依赖（faster-whisper、tkinterdnd2 等）"
$installed = $false
foreach ($idx in $PipIndexes) {
    Write-Host "    使用源：$idx"
    & $Py -m pip install --no-warn-script-location --disable-pip-version-check `
        -i $idx faster-whisper tkinterdnd2 2>&1 | Select-Object -Last 4
    if ($LASTEXITCODE -eq 0) { $installed = $true; break }
    Write-Host "    该源安装失败，尝试下一个源…" -ForegroundColor Yellow
}
if (-not $installed) {
    Write-Host "依赖安装失败，请检查网络后重试。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Step "检查 ffmpeg / ffprobe"
$Ffmpeg = Join-Path $Root "bin\ffmpeg.exe"
$Ffprobe = Join-Path $Root "bin\ffprobe.exe"
if (-not (Test-Path $Ffmpeg) -or -not (Test-Path $Ffprobe)) {
    $zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
    $sources = @(
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    )
    $ok = $false
    foreach ($url in $sources) {
        try {
            Write-Host "    下载 ffmpeg：$url"
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 600
            $ok = $true
            break
        } catch {
            Write-Host "    下载失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }
    if (-not $ok) {
        Write-Host "ffmpeg 下载失败，请手动把 ffmpeg.exe / ffprobe.exe 放到 bin 目录。" -ForegroundColor Red
    } else {
        $tmp = Join-Path $env:TEMP ("ffmpeg_extract_" + [guid]::NewGuid().ToString("N"))
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $exes = Get-ChildItem $tmp -Recurse -Include ffmpeg.exe, ffprobe.exe | Select-Object -First 2
        New-Item -ItemType Directory -Force -Path (Join-Path $Root "bin") | Out-Null
        foreach ($exe in $exes) { Copy-Item $exe.FullName (Join-Path $Root "bin") -Force }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $zip -Force -ErrorAction SilentlyContinue
        Write-Host "    ffmpeg 已安装到 bin 目录"
    }
} else {
    Write-Host "    已存在，跳过"
}

if (-not $SkipModel) {
    Write-Step "预下载语音识别模型（$Model，约 500MB，仅首次需要）"
    $env:HF_HUB_DISABLE_TELEMETRY = "1"
    $code = "import sys; sys.path.insert(0, r'$Root\app'); from subtrans.asr import download_model_for_install; download_model_for_install('$Model')"
    & $Py -c $code
    if ($LASTEXITCODE -ne 0) {
        Write-Host "模型下载失败（不影响程序使用，首次处理视频时会自动重试下载）。" -ForegroundColor Yellow
    }
}

if (-not $SkipOffline) {
    Write-Step "安装离线翻译引擎（llama.cpp + Hy-MT2-1.8B，约 1.1GB）"
    $LlamaDir = Join-Path $Root "bin\llamacpp"
    $LlamaExe = Join-Path $LlamaDir "llama-server.exe"
    if (Test-Path $LlamaExe) {
        Write-Host "    llama.cpp 已存在，跳过"
    } else {
        $zip = Join-Path $env:TEMP "llamacpp-cpu.zip"
        $url = "https://github.com/ggml-org/llama.cpp/releases/download/b12602/llama-b12602-bin-win-cpu-x64.zip"
        try {
            Write-Host "    下载 llama.cpp（约 24MB）…"
            curl.exe -L --retry 5 --retry-all-errors -o $zip $url
            New-Item -ItemType Directory -Force -Path $LlamaDir | Out-Null
            Expand-Archive -Path $zip -DestinationPath $LlamaDir -Force
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            Write-Host "    llama.cpp 已安装到 bin\llamacpp"
        } catch {
            Write-Host "    llama.cpp 下载失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    $ModelDir = Join-Path $Root "models\hy-mt2"
    $Gguf = Join-Path $ModelDir "Hy-MT2-1.8B-Q4_K_M.gguf"
    $Want = 1133080448
    $have = if (Test-Path $Gguf) { (Get-Item $Gguf).Length } else { 0 }
    if ($have -eq $Want) {
        Write-Host "    Hy-MT2 模型已存在，跳过"
    } else {
        New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
        $mirror = "https://hf-mirror.com/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf"
        $official = "https://huggingface.co/tencent/Hy-MT2-1.8B-GGUF/resolve/main/Hy-MT2-1.8B-Q4_K_M.gguf"
        foreach ($src in @($mirror, $official)) {
            Write-Host "    下载 Hy-MT2 模型（1.1GB，可断点续传）：$src"
            curl.exe -L -C - --retry 10 --retry-all-errors --speed-time 30 --speed-limit 1000 -o $Gguf $src
            $have = if (Test-Path $Gguf) { (Get-Item $Gguf).Length } else { 0 }
            if ($have -eq $Want) { break }
            Write-Host "    下载不完整（$([math]::Round($have/1MB,1))MB / 1080.6MB），换源续传…" -ForegroundColor Yellow
        }
        $have = if (Test-Path $Gguf) { (Get-Item $Gguf).Length } else { 0 }
        if ($have -ne $Want) {
            Write-Host "    模型下载未完成，可稍后重跑本脚本继续下载。" -ForegroundColor Yellow
        } else {
            Write-Host "    Hy-MT2 模型已就绪"
        }
    }
}

Write-Step "环境检查"
& $Py -c "import faster_whisper, tkinter, os; print('  faster-whisper 可用'); print('  tkinter 可用')"
& (Join-Path $Root "bin\ffmpeg.exe") -version 2>$null | Select-Object -First 1

Write-Host ""
Write-Host "安装完成！双击「视频字幕工具.bat」即可启动。" -ForegroundColor Green
Read-Host "按回车退出"
