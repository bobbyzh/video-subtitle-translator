"""程序内各目录与外部工具的位置。"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def app_root() -> Path:
    """程序根目录（含 runtime / bin / models / app）。

    打包成 exe 之后 __file__ 在 PyInstaller 的临时/内部目录里，不能再用它推算，
    改成「exe 所在目录」——这样 bin\\、models\\、data\\ 仍然放在 exe 旁边。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


ROOT = app_root()
APP_DIR = ROOT / "app"
BIN_DIR = ROOT / "bin"
MODEL_DIR = ROOT / "models"
DATA_DIR = ROOT / "data"
CONFIG_FILE = DATA_DIR / "config.json"
CACHE_FILE = DATA_DIR / "translation_cache.json"
GLOSSARY_FILE = DATA_DIR / "glossary.txt"
LOG_DIR = DATA_DIR / "logs"

IS_WINDOWS = os.name == "nt"


def _find_tool(name: str) -> str | None:
    exe = name + (".exe" if IS_WINDOWS else "")
    candidate = BIN_DIR / exe
    if candidate.is_file():
        return str(candidate)
    found = shutil.which(name)
    return found


def ffmpeg_path() -> str | None:
    return _find_tool("ffmpeg")


def ffprobe_path() -> str | None:
    return _find_tool("ffprobe")


def ensure_dirs() -> None:
    for d in (MODEL_DIR, DATA_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


def hidden_subprocess_flags() -> int:
    """pythonw 启动时不弹出黑色控制台窗口。"""
    if IS_WINDOWS:
        return 0x08000000  # CREATE_NO_WINDOW
    return 0


def python_executable() -> str:
    return sys.executable
