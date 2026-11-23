"""ASS 字幕写出：排版/字体/字号/颜色全部由 ASS 样式控制，不受播放器设置影响。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WIDTH = 1920
HEIGHT = 1080

_HEAD = """[Script Info]
; 由「视频字幕提取翻译工具」生成
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,{font},{zh_size},{ZH_COLOR},&H000000FF,&H00000000,&H64000000,{zh_bold},0,0,0,100,100,0,0,1,{zh_outline},0.5,{align},{margin},{margin},{zh_margin},1
Style: EN,{en_font},{en_size},{EN_COLOR},&H000000FF,&H00000000,&H64000000,{en_bold},0,0,0,100,100,0,0,1,{en_outline},0.5,{align},{margin},{margin},{en_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

ZH_COLOR = "&H00FFFFFF"
EN_COLOR = "&H0000D7FF"

# 字号按 1080p 为基准，其它分辨率等比缩放
BASE_ZH_SIZE = 58
BASE_EN_SIZE = 42


@dataclass
class SubtitleStyle:
    """字幕外观。字号是 1080p 下的基准值，实际输出会按视频高度缩放。"""

    font: str = "Microsoft YaHei"
    font_en: str = ""
    size_zh: int = BASE_ZH_SIZE
    size_en: int = BASE_EN_SIZE
    color_zh: str = "#FFFFFF"      # 常用十六进制，界面里好填好选
    color_en: str = "#00D7FF"
    outline: float = 2.2           # 描边粗细（1080p 基准）
    bold: bool = False
    position: str = "bottom"       # bottom = 底部 / top = 顶部

    def en_font(self) -> str:
        return self.font_en or self.font


def hex_to_ass(color: str, fallback: str) -> str:
    """#RRGGBB → ASS 的 &H00BBGGRR。"""
    text = (color or "").strip().lstrip("#")
    if len(text) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return fallback
    r, g, b = text[0:2], text[2:4], text[4:6]
    return f"&H00{b}{g}{r}".upper()


def _header(width: int, height: int, style: SubtitleStyle) -> str:
    scale = height / HEIGHT
    align = 8 if (style.position or "bottom").lower() == "top" else 2
    zh_size = max(16, round(style.size_zh * scale))
    en_size = max(12, round(style.size_en * scale))
    outline = max(0.0, float(style.outline)) * scale
    margin = max(10, round(40 * scale))
    base_margin = max(12, round(56 * scale))
    return _HEAD.format(
        w=width,
        h=height,
        font=style.font or "Microsoft YaHei",
        en_font=style.en_font() or "Microsoft YaHei",
        zh_size=zh_size,
        en_size=en_size,
        ZH_COLOR=hex_to_ass(style.color_zh, ZH_COLOR),
        EN_COLOR=hex_to_ass(style.color_en, EN_COLOR),
        zh_bold=-1 if style.bold else 0,
        en_bold=-1 if style.bold else 0,
        zh_outline=round(max(outline, 0.5), 1),
        en_outline=round(max(outline * 0.85, 0.4), 1),
        align=align,
        margin=margin,
        zh_margin=base_margin,
        en_margin=max(10, round(28 * scale)),
    )


def _ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    cs = int(round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    """去掉可能干扰 ASS 的字符，保留反斜杠 N 换行标记。"""
    text = text.replace("\r", "").replace("{", "(").replace("}", ")")
    text = re.sub(r"(?<!\\)\\n", r"\\N", text)
    return text.replace("\n", "\\N")


def write_ass(
    path: Path,
    entries: list[tuple[float, float, str, str]],
    play_res: tuple[int, int] = (WIDTH, HEIGHT),
    style: SubtitleStyle | None = None,
) -> None:
    """entries: (start, end, 中文文本, 英文文本)，文本内的换行会转成 \\N。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = play_res
    style = style or SubtitleStyle()
    align_tag = "\\an8" if (style.position or "bottom").lower() == "top" else "\\an2"
    lines = [_header(int(width), int(height), style).rstrip("\n")]
    for start, end, zh, en in entries:
        zh = _escape(zh.strip())
        en = _escape(en.strip())
        text = f"{{{align_tag}\\rZH}}{zh}"
        if en:
            text += f"\\N{{\\rEN}}{en}"
        lines.append(
            f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},ZH,,0,0,0,,{text}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\r\n")


def looks_like_ass(path: Path) -> bool:
    try:
        return "[V4+ Styles]" in Path(path).read_text("utf-8", errors="ignore")[:4000]
    except OSError:
        return False
