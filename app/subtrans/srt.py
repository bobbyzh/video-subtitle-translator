"""字幕数据结构、SRT 解析/写出与中文断行。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TIMING_RE = re.compile(
    r"(?P<h>\d{1,3}):(?P<m>\d{2}):(?P<s>\d{2})[,.](?P<ms>\d{1,3})\s*-->\s*"
    r"(?P<h2>\d{1,3}):(?P<m2>\d{2}):(?P<s2>\d{2})[,.](?P<ms2>\d{1,3})"
)

_ENCODINGS = ("utf-8-sig", "utf-8", "utf-16", "gb18030", "big5", "latin-1")


@dataclass
class Cue:
    start: float
    end: float
    text: str
    index: int = 0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Subtitle:
    cues: list[Cue] = field(default_factory=list)
    language: str = "en"

    def __len__(self) -> int:
        return len(self.cues)


def _ts_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")[:3]) / 1000.0


def format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def clean_text(text: str) -> str:
    """去掉 ASS 样式标签、HTML 标签与多余空白。"""
    text = re.sub(r"\{\\[^}]*\}", "", text)
    text = re.sub(r"<[^>]{1,12}>", "", text)
    text = text.replace("\\N", "\n").replace("\\n", "\n").replace("\u200b", "")
    lines = [re.sub(r"[ \t\u00a0]+", " ", ln).strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def parse_srt(text: str) -> list[Cue]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.lstrip("\ufeff")
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if not lines:
            continue
        canvas = "\n".join(lines)
        match = TIMING_RE.search(canvas)
        if not match:
            continue
        start = _ts_to_seconds(match["h"], match["m"], match["s"], match["ms"])
        end = _ts_to_seconds(match["h2"], match["m2"], match["s2"], match["ms2"])
        # 时间行之后的每一行都是字幕正文
        pos = canvas.find("\n", match.end())
        body = "" if pos < 0 else canvas[pos + 1 :]
        body = clean_text(body)
        if not body:
            continue
        if end <= start:
            end = start + 1.0
        cues.append(Cue(start=start, end=end, text=body))
    return cues


def read_srt(path: Path) -> list[Cue]:
    data = Path(path).read_bytes()
    for enc in _ENCODINGS:
        try:
            return parse_srt(data.decode(enc))
        except UnicodeDecodeError:
            continue
    return parse_srt(data.decode("utf-8", errors="replace"))


def write_srt(path: Path, cues: list[Cue], encoding: str = "utf-8-sig") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for i, cue in enumerate(cues, 1):
        out.append(str(i))
        out.append(f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}")
        out.append(cue.text.strip())
        out.append("")
    path.write_text("\n".join(out) + "\n", encoding=encoding, newline="\r\n")


def display_width(text: str) -> float:
    """粗略的显示宽度：全角字符算 2，半角算 1.1。"""
    width = 0.0
    for ch in text:
        code = ord(ch)
        if code >= 0x2E80 or ch in "，。！？；：、（）【】《》“”‘’…":
            width += 2.0
        else:
            width += 1.1
    return width


def _find_break(text: str, max_width: float) -> int:
    """返回不超过 max_width 的最佳断行位置（优先落在标点或空格后）。"""
    candidates = []
    for i, ch in enumerate(text, start=1):
        if display_width(text[:i]) > max_width:
            break
        if ch in "，。！？；：、,.!?;: " or ch in "）】》”’":
            candidates.append(i)
    if candidates:
        return candidates[-1]
    for i in range(1, len(text) + 1):
        if display_width(text[:i]) > max_width:
            return max(1, i - 1)
    return len(text)


def wrap_text(text: str, max_width: float = 42.0, max_lines: int = 2) -> str:
    """把一行字幕按显示宽度折成最多两行。"""
    text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if not text:
        return ""
    if display_width(text) <= max_width:
        return text
    lines: list[str] = []
    remaining = text
    while remaining and len(lines) < max_lines:
        if display_width(remaining) <= max_width:
            lines.append(remaining)
            remaining = ""
            break
        cut = _find_break(remaining, max_width)
        head, tail = remaining[:cut].strip(), remaining[cut:].strip()
        if not head:
            lines.append(remaining)
            remaining = ""
            break
        lines.append(head)
        remaining = tail
    if remaining:
        # 超长内容并入最后一行，保证不丢字
        lines[-1] = (lines[-1] + " " + remaining).strip()
    return "\n".join(lines)


def looks_english(text: str) -> bool:
    """粗判文本是否为英文（拉丁字母占绝对多数）。"""
    sample = text[:400]
    cjk = sum(1 for ch in sample if "\u4e00" <= ch <= "\u9fff")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    if cjk + latin == 0:
        return False
    return latin / (cjk + latin) >= 0.55 and cjk < latin


def deep_copy(cues: list[Cue]) -> list[Cue]:
    return [Cue(c.start, c.end, c.text, c.index) for c in cues]
