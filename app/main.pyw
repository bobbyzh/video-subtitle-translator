"""程序入口（配合 pythonw.exe / 打包后的 exe 运行，不弹控制台）。

两种用法：
    · 直接运行                              → 打开图形界面；
    · 视频字幕工具.exe <视频路径> 或 --file <视频路径>
      → 不开界面，按 data\\config.json 的设置处理指定文件（方便写批处理、跑测试），
        另有 --no-translate（只提取不翻译）、--burn（强制烧硬字幕）两个开关。
"""

from __future__ import annotations

import faulthandler
import sys
import traceback
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from subtrans.gui import run  # noqa: E402

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".wmv", ".m4v", ".ts", ".mpg")


def _cli(argv: list[str]) -> int:
    """无界面模式：处理 argv 里的视频路径。"""
    from subtrans.ass import SubtitleStyle
    from subtrans.config import load_config
    from subtrans.pipeline import Options, process_file

    if sys.stdout is None:  # 打包成窗口版 exe 后没有控制台，把输出接到日志文件
        from subtrans.paths import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(LOG_DIR / "cli.log", "a", encoding="utf-8")  # noqa: SIM115

    flags = [a for a in argv if a.startswith("-")]
    files = [a for a in argv if not a.startswith("-")]
    if not files:
        print("用法：视频字幕工具.exe <视频路径> [--no-translate] [--burn]")
        return 2

    cfg = load_config()

    def log(msg: str) -> None:
        print(msg, flush=True)

    def report(percent: float, message: str) -> None:
        log(f"[{percent * 100:5.1f}%] {message}")

    opts = Options(
        source=cfg.get("source", "auto"),
        model=cfg.get("model", "small"),
        keep_english=cfg.get("keep_english", True),
        overwrite=cfg.get("overwrite", True),
        output_dir=None,
        translate="--no-translate" not in flags and cfg.get("translate", True),
        max_line_width=float(cfg.get("max_line_width", 42.0)),
        bilingual=cfg.get("bilingual", False),
        bilingual_format=cfg.get("bilingual_format", "srt"),
        translate_engine=cfg.get("translate_engine", "auto"),
        local_model=cfg.get("local_model", "fast"),
        burn_in="--burn" in flags or bool(cfg.get("burn_in", False)),
        burn_crf=int(cfg.get("burn_crf", 20) or 20),
        burn_preset=str(cfg.get("burn_preset", "medium") or "medium"),
        bracket_mode=cfg.get("bracket_mode", "drop"),
        ass_style=SubtitleStyle(
            font=cfg.get("ass_font", "Microsoft YaHei"),
            font_en=cfg.get("ass_font_en", ""),
            size_zh=int(cfg.get("ass_size_zh", 58) or 58),
            size_en=int(cfg.get("ass_size_en", 42) or 42),
            color_zh=cfg.get("ass_color_zh", "#FFFFFF"),
            color_en=cfg.get("ass_color_en", "#00D7FF"),
            outline=float(cfg.get("ass_outline", 2.2) or 2.2),
            bold=bool(cfg.get("ass_bold", False)),
            position=cfg.get("ass_position", "bottom"),
        ),
        srt_colored=bool(cfg.get("srt_colored", False)),
    )

    failed = 0
    for name in files:
        video = Path(name)
        if not video.is_file():
            log(f"跳过（文件不存在）：{name}")
            failed += 1
            continue
        log(f"===== 处理 {video.name} =====")
        try:
            out = process_file(video, opts, report=report, log=log)
        except Exception:  # noqa: BLE001
            failed += 1
            log(f"异常：{video.name}\n{traceback.format_exc()}")
            continue
        if out.ok:
            target = out.srt_path or out.bilingual_path or out.video_path
            log(f"完成：{video.name} → {target}")
        else:
            failed += 1
            log(f"失败：{video.name}：{out.message}")
    return 0 if failed == 0 else 1


def _enable_native_crash_log() -> None:
    """把原生库（ctranslate2 / llama.cpp / av）崩溃时的调用栈也落到文件，
    否则 C 层崩溃会让 pythonw 直接消失、日志里什么都看不到。"""
    try:
        from subtrans.paths import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(LOG_DIR / "native_crash.log", "a", buffering=1, encoding="utf-8")
        fh.write(f"\n===== 启动 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        faulthandler.enable(fh, all_threads=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> None:
    argv = sys.argv[1:]
    if any(a.lower().endswith(VIDEO_EXT) for a in argv) or "--file" in argv or "-f" in argv:
        try:
            raise SystemExit(_cli(argv))
        except SystemExit:
            raise
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            raise SystemExit(1)

    try:
        _enable_native_crash_log()
        run()
    except Exception:  # noqa: BLE001
        try:
            from subtrans.paths import LOG_DIR

            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "crash.log").write_text(traceback.format_exc(), "utf-8")
        except Exception:  # noqa: BLE001
            pass
        try:
            import tkinter.messagebox as mb

            mb.showerror("程序出错", traceback.format_exc(limit=2))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    main()
