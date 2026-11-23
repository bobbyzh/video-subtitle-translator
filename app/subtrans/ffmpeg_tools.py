"""调用 ffmpeg / ffprobe：探测视频、提取内嵌字幕与抽取音轨。"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .paths import ffmpeg_path, ffprobe_path, hidden_subprocess_flags

TEXT_SUB_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "hdmv_text_subtitle",
    "eia_608", "microdvd", "subviewer", "subviewer1", "jacosub", "sami", "realtext",
}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

ENGLISH_LANGS = {"eng", "en", "en-us", "en-gb", "en_us", "en_gb", "english"}


class FFmpegError(RuntimeError):
    pass


@dataclass
class SubtitleStream:
    index: int
    codec: str
    lang: str
    title: str
    text_based: bool

    @property
    def label(self) -> str:
        parts = [f"#{self.index}", self.codec]
        if self.lang:
            parts.append(self.lang)
        if self.title:
            parts.append(self.title)
        return " / ".join(parts)


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=hidden_subprocess_flags(),
    )


def ensure_tools() -> tuple[str, str]:
    ffmpeg, ffprobe = ffmpeg_path(), ffprobe_path()
    if not ffmpeg or not ffprobe:
        raise FFmpegError(
            "未找到 ffmpeg/ffprobe。请先运行根目录的「安装依赖.bat」，"
            "或把 ffmpeg.exe、ffprobe.exe 放到程序 bin 目录。"
        )
    return ffmpeg, ffprobe


def probe(video: Path) -> dict:
    """返回 ffprobe 的 format + streams。"""
    _, ffprobe = ensure_tools()
    proc = _run(
        [ffprobe, "-v", "error", "-hide_banner", "-print_format", "json",
         "-show_format", "-show_streams", "-i", str(video)],
        timeout=60,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        msg = (proc.stderr or "无法解析视频文件").strip().splitlines()
        raise FFmpegError(f"读取视频信息失败：{msg[-1] if msg else '未知错误'}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise FFmpegError(f"解析视频信息失败：{exc}") from exc


def duration_seconds(info: dict) -> float:
    try:
        return float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def has_audio(info: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def video_size(info: dict) -> tuple[int, int] | None:
    """返回视频分辨率 (宽, 高)，取不到返回 None。"""
    for s in info.get("streams", []):
        if s.get("codec_type") != "video":
            continue
        try:
            w = int(s.get("width") or 0)
            h = int(s.get("height") or 0)
        except (TypeError, ValueError):
            continue
        if w > 0 and h > 0:
            return w, h
    return None


def subtitle_streams(info: dict) -> list[SubtitleStream]:
    result: list[SubtitleStream] = []
    for s in info.get("streams", []):
        if s.get("codec_type") != "subtitle":
            continue
        tags = s.get("tags") or {}
        codec = (s.get("codec_name") or "").lower()
        lang = (tags.get("language") or "").lower()
        title = tags.get("title") or tags.get("handler_name") or ""
        result.append(
            SubtitleStream(
                index=int(s.get("index", 0)),
                codec=codec,
                lang=lang,
                title=title,
                text_based=codec in TEXT_SUB_CODECS,
            )
        )
    return result


def _score(stream: SubtitleStream) -> int:
    if not stream.text_based:
        return -1
    if stream.lang in ENGLISH_LANGS:
        return 10
    lowered = stream.title.lower()
    if any(k in lowered for k in ("english", "eng", "英文", "英语")):
        return 8
    if stream.lang in ("", "und", "unknown", "mul"):
        return 4
    return 0


def english_candidates(streams: list[SubtitleStream]) -> list[SubtitleStream]:
    scored = [( _score(s), s) for s in streams]
    scored = [item for item in scored if item[0] >= 0]
    scored.sort(key=lambda item: -item[0])
    return [s for _, s in scored]


def extract_subtitle(video: Path, stream_index: int, out_srt: Path) -> Path:
    """把指定字幕轨转成 srt。"""
    ffmpeg, _ = ensure_tools()
    out_srt.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [ffmpeg, "-y", "-nostdin", "-v", "error", "-i", str(video),
         "-map", f"0:{stream_index}", "-c:s", "srt", str(out_srt)],
        timeout=300,
    )
    if proc.returncode != 0 or not out_srt.exists():
        detail = (proc.stderr or "").strip().splitlines()
        raise FFmpegError(
            f"提取内嵌字幕失败：{detail[-1] if detail else '未知错误'}"
        )
    return out_srt


def extract_audio(video: Path, out_wav: Path) -> Path:
    """抽取 16 kHz 单声道 wav，供语音识别使用。"""
    ffmpeg, _ = ensure_tools()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    proc = _run(
        [ffmpeg, "-y", "-nostdin", "-v", "error", "-i", str(video),
         "-vn", "-sn", "-dn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out_wav)],
        timeout=1800,
    )
    if proc.returncode != 0 or not out_wav.exists() or out_wav.stat().st_size < 1024:
        detail = (proc.stderr or "").strip().splitlines()
        raise FFmpegError(
            f"抽取音轨失败（视频可能没有音轨）：{detail[-1] if detail else '未知错误'}"
        )
    return out_wav


def media_info_text(info: dict) -> str:
    dur = duration_seconds(info)
    w = h = None
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            w, h = s.get("width"), s.get("height")
            break
    parts = []
    if dur:
        parts.append(f"{int(dur // 60):d}:{int(dur % 60):02d}")
    if w and h:
        parts.append(f"{w}x{h}")
    subs = subtitle_streams(info)
    if subs:
        parts.append(f"内嵌字幕 {len(subs)} 条")
    return " / ".join(parts)


def ffmpeg_version() -> str:
    try:
        ffmpeg, _ = ensure_tools()
    except FFmpegError:
        return ""
    proc = _run([ffmpeg, "-version"], timeout=30)
    first = proc.stdout.splitlines()[0] if proc.stdout else ""
    match = re.search(r"ffmpeg version (\S+)", first)
    return match.group(1) if match else first
