"""一键打包 exe：PyInstaller 构建 + 把 bin/models/data 复制到 dist 目录。

用法：
    runtime\\python\\python.exe tools\\build_exe.py
产物：dist\\VideoSubtitleTool\\VideoSubtitleTool.exe（同目录还有 bin\\、models\\、data\\）
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "VideoSubtitleTool"
HF_CACHE = ROOT / "models" / "faster-whisper-small"


def run_pyinstaller() -> None:
    print("== 1/3 PyInstaller 打包（首次约 1~3 分钟）…")
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--noconfirm", "--clean",
            str(ROOT / "tools" / "pack.spec"),
        ],
        cwd=str(ROOT),
        check=True,
    )


def copy_tree(src: Path, dst: Path, skip_hf_cache: bool = False) -> None:
    if not src.is_dir():
        print(f"   跳过（不存在）：{src}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    for item in src.iterdir():
        if skip_hf_cache and item.name == ".cache":
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
        total += 1
    print(f"   已复制 {src.name}\\（{total} 项）")


def main() -> int:
    run_pyinstaller()

    print("== 2/3 复制外部依赖到 dist …")
    copy_tree(ROOT / "bin", DIST / "bin")                     # ffmpeg / ffprobe / llama.cpp
    models = DIST / "models"
    models.mkdir(parents=True, exist_ok=True)
    copy_tree(ROOT / "models" / "hy-mt2", models / "hy-mt2")  # 1.8B（约 1.1GB）
    copy_tree(ROOT / "models" / "hy-mt2-7b", models / "hy-mt2-7b")
    copy_tree(HF_CACHE, models / "faster-whisper-small", skip_hf_cache=True)

    data = DIST / "data"
    data.mkdir(parents=True, exist_ok=True)
    for name in ("config.json", "glossary.txt"):
        src = ROOT / "data" / name
        if src.is_file():
            shutil.copy2(src, data / name)
            print(f"   已复制 data\\{name}")

    readme = DIST / "使用说明.txt"
    readme.write_text(
        "视频字幕工具（免安装版）\n\n"
        "双击「VideoSubtitleTool.exe」即可运行，不需要装 Python。\n\n"
        "目录说明：\n"
        "  VideoSubtitleTool.exe   主程序\n"
        "  _internal\\          程序运行库，别删也别动\n"
        "  bin\\                ffmpeg / ffprobe / llama.cpp（离线识别与翻译、硬字幕都靠它）\n"
        "  models\\             离线模型：hy-mt2\\ 是 1.8B 翻译模型，hy-mt2-7b\\ 是 7B（可选），\n"
        "                      faster-whisper-small\\ 是语音识别模型\n"
        "  data\\               配置、术语表、翻译缓存（删掉翻译缓存重跑就会重新译）\n\n"
        "想用 7B：把 Hunyuan-MT-7B.Q4_K_M.gguf 放进 models\\hy-mt2-7b\\ 后，界面里选 7B 档位。\n"
        "整个文件夹可以整体拷到别的 Windows 电脑上用（64 位，Win10 及以上）。\n",
        "utf-8",
    )

    size = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"== 3/3 完成：{DIST}（共 {size / 1024 ** 3:.2f} GB）")
    print(f"   主程序：{DIST / 'VideoSubtitleTool.exe'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
