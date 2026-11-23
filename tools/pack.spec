# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包脚本：把 a/main.pyw 打成一个文件夹形式的 exe。

用法（在项目根目录）：
    runtime\python\python.exe -m PyInstaller --noconfirm --clean tools\pack.spec

产物在 dist\视频字幕工具\：
    视频字幕工具.exe        ← 双击即可
    _internal\              ← Python 运行时与依赖（不用管）
之后由 tools\build_exe.py 把 bin\、models\、data\ 复制到同一目录。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_data_files

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))   # spec 里__file__不可用，用 SPECPATH
APP = os.path.join(ROOT, "app")
ENTRY = os.path.join(APP, "main.pyw")

# faster_whisper 的静音检测模型（silero_vad_v6.onnx）是包内数据文件，
# 不做静态分析，必须显式收进来，否则跑到语音识别就报 "File doesn't exist"
DATAS = collect_data_files("faster_whisper")
DATAS += collect_data_files("tkinterdnd2")

HIDDEN = [
    "subtrans",
    "subtrans.gui",
    "subtrans.pipeline",
    "subtrans.asr",
    "subtrans.translate",
    "subtrans.localmt",
    "subtrans.ffmpeg_tools",
    "subtrans.burnin",
    "subtrans.ass",
    "subtrans.srt",
    "subtrans.config",
    "subtrans.paths",
    # asr.py 里是函数内导入，PyInstaller 静态分析看不到
    "faster_whisper",
    "ctranslate2",
    "tokenizers",
    "huggingface_hub",
    "numpy",
    "av",
    "onnxruntime",
    "tkinterdnd2",
]

a = Analysis(
    [ENTRY],
    pathex=[APP],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "PIL", "scipy", "pandas", "IPython",
        "pytest", "setuptools", "pip", "PyInstaller",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="视频字幕工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 双击不弹黑框
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="视频字幕工具",
)
