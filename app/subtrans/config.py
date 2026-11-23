"""配置读写。"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import CONFIG_FILE, DATA_DIR

DEFAULTS = {
    "source": "auto",          # auto / embedded / asr
    "model": "small",          # tiny / base / small / medium
    "keep_english": True,      # 是否额外保留英文 srt
    "overwrite": True,         # 同名文件是否覆盖
    "output_dir": "",          # 空 = 与视频同目录
    "translate": True,         # 关掉则只输出英文字幕
    "bilingual": True,         # 是否输出中英对照字幕（中文在上、英文在下）
    "bilingual_format": "srt",  # srt = 两条独立字幕（最兼容）/ ass = ASS 排版固定 / both
    "translate_engine": "auto",  # auto = 离线优先失败回落在线 / local / online
    "local_model": "fast",     # fast = Hy-MT2-1.8B（快）/ quality = Hunyuan-MT-7B（质量优先）
    "burn_in": False,          # 是否把字幕烧进视频（硬字幕，输出 .hardsub.mkv）
    "burn_crf": 20,            # 硬字幕视频的 x264 质量（越小越清晰、文件越大）
    "burn_preset": "medium",   # 硬字幕转码速度档
    "bracket_mode": "drop",   # 纯标注（[MUSIC]、(laughs)、【掌声】）：drop = 丢弃 / keep = 保留原文不译
    "ass_font": "Microsoft YaHei",  # 字幕字体（要用系统里装了的字体名）
    "ass_font_en": "",         # 英文单独字体，空 = 跟中文同一个
    "ass_size_zh": 58,         # 中文字号（1080p 基准，其它分辨率自动缩放）
    "ass_size_en": 42,         # 英文字号（1080p 基准）
    "ass_color_zh": "#FFFFFF",  # 中文颜色 #RRGGBB
    "ass_color_en": "#00D7FF",  # 英文颜色 #RRGGBB
    "ass_outline": 2.2,        # 描边粗细（1080p 基准）
    "ass_bold": False,         # 是否加粗
    "ass_position": "bottom",  # bottom = 底部 / top = 顶部
    "srt_colored": False,      # SRT 也写 <font color> 标签（只有部分播放器认，SRT 不支持字体/字号）
    "max_line_width": 42.0,    # 中文每行显示宽度
    "last_dir": "",
    "win_geometry": "",        # 上次退出时的窗口位置尺寸，如 "1996x1339+308+308"
    "sash_ratio": 0.0,         # 视频列表/日志分栏位置（占整个分栏高度的比例）
    "style_dlg_geometry": "",  # 「字幕样式」弹窗上次的尺寸位置
    "col_widths": {"name": 400, "info": 140, "source": 210, "status": 180},  # 视频列表各栏宽度
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        if CONFIG_FILE.is_file():
            data = json.loads(CONFIG_FILE.read_text("utf-8"))
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
        CONFIG_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), "utf-8"
        )
    except OSError:
        pass
