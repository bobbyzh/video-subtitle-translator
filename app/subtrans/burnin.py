"""把字幕烧进视频（硬字幕）：用 ffmpeg 的 libass 内部渲染，输出任何播放器都不会压行。"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from .paths import hidden_subprocess_flags

BURN_TAG = ".hardsub"


class BurnError(RuntimeError):
    pass


def _escape_filter_path(path: Path) -> str:
    """把 Windows 路径转成 filtergraph 里安全的形式。

    用单引号包住路径：引号内的 `:`、空格、方括号、逗号都会按字面处理，
    反斜杠统一换成 `/`（ffmpeg 在 Windows 上同样接受正斜杠）。
    路径里若出现单引号，用 `'\\''` 这种转义写法闭合再重开。
    """
    text = str(path).replace("\\", "/")
    if "'" in text:
        text = text.replace("'", "'\\''")
    return f"'{text}'"


def _build_cmd(ffmpeg: str, video: Path, ass_name: str, out_path: Path,
               crf: int, preset: str, audio_copy: bool) -> list[str]:
    # ass 滤镜里直接写绝对路径会被 ffmpeg 当成分隔符（盘符冒号、空格、方括号），
    # 所以把 ass 复制到临时目录后以「工作目录 + 纯文件名」的形式传进去，最稳。
    filt = f"ass={ass_name}"
    cmd = [
        ffmpeg, "-y", "-nostdin", "-hide_banner",
        "-loglevel", "error",
        "-progress", "pipe:1", "-nostats",
        "-i", str(video),
        "-vf", filt,
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
    ]
    cmd += ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "192k"]
    cmd += ["-sn", "-dn", "-map", "0:v:0", "-map", "0:a?", str(out_path)]
    return cmd


def burn_ass(
    video: Path,
    ass_path: Path,
    out_path: Path,
    ffmpeg: str,
    duration: float = 0.0,
    crf: int = 20,
    preset: str = "medium",
    report=None,
    cancel: threading.Event | None = None,
) -> Path:
    """把 ass_path 烧进 video，写到 out_path（mkv）。返回输出路径。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="burn_"))
    safe_ass = tmp / "sub.ass"
    shutil.copyfile(ass_path, safe_ass)

    try:
        for audio_copy in (True, False):
            cmd = _build_cmd(ffmpeg, video, "sub.ass", out_path, crf, preset, audio_copy)
            proc = subprocess.Popen(
                cmd,
                cwd=str(tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=hidden_subprocess_flags(),
            )
            tail: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    raise BurnError("已停止")
                if line.startswith("out_time_us=") and duration > 0 and report:
                    try:
                        done = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    report(min(1.0, done / duration))
            stderr = ""
            if proc.stderr is not None:
                stderr = proc.stderr.read()
            rc = proc.wait()
            if rc == 0 and out_path.exists() and out_path.stat().st_size > 1024:
                return out_path
            tail = [ln for ln in stderr.strip().splitlines() if ln.strip()][-3:]
            if audio_copy:
                # 音频无法直接拷贝时回退为 AAC 重编码
                try:
                    out_path.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            raise BurnError("烧字幕失败：" + ("；".join(tail) if tail else f"返回码 {rc}"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    raise BurnError("烧字幕失败")
