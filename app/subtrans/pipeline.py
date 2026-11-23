"""单个视频的完整处理流程：取英文字幕 → 翻译 → 写出 srt。"""

from __future__ import annotations

import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg_tools as ff
from .asr import AsrEngine, AsrError
from .ass import SubtitleStyle, write_ass
from .burnin import BURN_TAG, BurnError, burn_ass
from .paths import CACHE_FILE
from .srt import Cue, Subtitle, looks_english, read_srt, wrap_text, write_srt
from .translate import TranslateError, Translator

VIDEO_SUFFIXES = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".ts", ".mpg", ".mpeg", ".rmvb", ".rm", ".m2ts", ".vob", ".3gp", ".ogv",
}


class Cancelled(RuntimeError):
    pass


@dataclass
class Options:
    source: str = "auto"
    model: str = "small"
    keep_english: bool = True
    overwrite: bool = True
    output_dir: Path | None = None
    translate: bool = True
    max_line_width: float = 42.0
    bilingual: bool = False
    bilingual_format: str = "srt"
    translate_engine: str = "online"
    local_model: str = "fast"
    burn_in: bool = False
    burn_crf: int = 20
    burn_preset: str = "medium"
    bracket_mode: str = "drop"      # drop = 丢弃 [MUSIC]/(laughs) 这类标注；keep = 保留原文
    ass_style: SubtitleStyle = field(default_factory=SubtitleStyle)
    srt_colored: bool = False       # SRT 里也写 <font color> 标签（只有部分播放器认）


@dataclass
class Outcome:
    video: Path
    ok: bool = False
    message: str = ""
    srt_path: Path | None = None
    english_path: Path | None = None
    bilingual_path: Path | None = None
    video_path: Path | None = None
    source_label: str = ""
    cue_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _stage(report, percent: float, message: str) -> None:
    if report:
        report(max(0.0, min(0.999, percent / 100.0)), message)


def _extract_embedded(
    video: Path, info: dict, tmp: Path, opts: Options, out: Outcome
) -> Subtitle | None:
    streams = ff.subtitle_streams(info)
    if not streams:
        return None
    candidates = ff.english_candidates(streams)
    if not candidates:
        out.warnings.append("视频里有字幕轨，但不是文本字幕（如图形字幕）或语言不明")
        return None

    for stream in candidates:
        path = tmp / f"embedded_{stream.index}.srt"
        try:
            ff.extract_subtitle(video, stream.index, path)
        except ff.FFmpegError as exc:
            out.warnings.append(f"字幕轨 {stream.label} 提取失败：{exc}")
            continue
        cues = read_srt(path)
        if not cues:
            out.warnings.append(f"字幕轨 {stream.label} 内容为空")
            continue
        sample = "\n".join(c.text for c in cues[:8])
        confident_english = stream.lang in ff.ENGLISH_LANGS or any(
            k in stream.title.lower() for k in ("english", "eng", "英文", "英语")
        )
        if not confident_english and not looks_english(sample):
            out.warnings.append(
                f"字幕轨 {stream.label} 看起来不是英文，已跳过"
            )
            continue
        out.source_label = f"内嵌字幕轨 {stream.label}"
        return Subtitle(cues=cues, language="en")
    return None


def _run_asr(
    video: Path, info: dict, tmp: Path, opts: Options, out: Outcome,
    report, log, cancel,
) -> Subtitle | None:
    if not ff.has_audio(info):
        out.message = "视频没有音轨，无法语音识别"
        return None
    wav = tmp / "audio16k.wav"
    _stage(report, 3, "正在抽取音轨…")
    ff.extract_audio(video, wav)
    _stage(report, 10, "音轨已抽好，开始语音识别…")
    engine = AsrEngine(opts.model, log=log)
    try:
        cues = engine.transcribe(
            wav,
            duration=ff.duration_seconds(info),
            progress=lambda p: _stage(report, 10 + p * 30, "语音识别中…"),
            cancel=cancel,
        )
    except AsrError as exc:
        out.message = str(exc)
        return None
    finally:
        try:
            wav.unlink(missing_ok=True)
        except OSError:
            pass
    if not cues:
        out.message = "语音识别没有得到任何字幕（视频可能没有英语对白）"
        return None
    out.source_label = f"语音识别（Whisper {opts.model}）"
    return Subtitle(cues=cues, language="en")


def _target_path(
    video: Path, outdir: Path, overwrite: bool, tag: str = "", ext: str = ".srt"
) -> Path:
    base = video.stem + tag
    candidate = outdir / f"{base}{ext}"
    if overwrite or not candidate.exists():
        return candidate
    for i in range(2, 100):
        alt = outdir / f"{base}({i}){ext}"
        if not alt.exists():
            return alt
    return outdir / f"{base}({i}){ext}"


def process_file(
    video: Path,
    opts: Options,
    report=None,
    log=None,
    cancel: threading.Event | None = None,
) -> Outcome:
    """处理一个视频；report(percent 0~1, message)，log(text)。"""
    log = log or (lambda msg: None)
    out = Outcome(video=Path(video))
    _stage(report, 1, "读取视频信息…")
    try:
        info = ff.probe(out.video)
    except ff.FFmpegError as exc:
        out.message = str(exc)
        log(f"{out.video.name}：{exc}")
        return out
    log(f"{out.video.name}：{ff.media_info_text(info)}")

    subtitle: Subtitle | None = None
    with tempfile.TemporaryDirectory(prefix="subtrans_") as tmp_str:
        tmp = Path(tmp_str)

        if opts.source in ("auto", "embedded"):
            _stage(report, 4, "检查内嵌字幕…")
            subtitle = _extract_embedded(out.video, info, tmp, opts, out)
            if subtitle:
                log(f"{out.video.name}：使用{out.source_label}，共 {len(subtitle)} 条")

        if subtitle is None and opts.source in ("auto", "asr"):
            if out.warnings:
                log(f"{out.video.name}：" + "；".join(out.warnings))
            if out.message:
                log(f"{out.video.name}：{out.message}")
                out.ok = False
                return out
            subtitle = _run_asr(out.video, info, tmp, opts, out, report, log, cancel)
            if subtitle is None:
                return out
            log(f"{out.video.name}：语音识别得到 {len(subtitle)} 条字幕")

        if subtitle is None:
            if not out.message:
                out.message = (
                    "没有找到可用的英文内嵌字幕轨"
                    if opts.source == "embedded"
                    else "没有找到可用的英文字幕（内嵌字幕或音轨）"
                )
            return out

        cues = subtitle.cues
        if cancel and cancel.is_set():
            raise Cancelled("已停止")

        # 英文原文另存一份，便于对照
        outdir = Path(opts.output_dir) if opts.output_dir else out.video.parent
        outdir.mkdir(parents=True, exist_ok=True)

        translated_texts: list[str] | None = None
        if opts.translate and cues:
            _stage(report, 40, "翻译为中文…")

            def on_progress(done: int, total: int) -> None:
                pct = 40 + 34 * (done / max(total, 1))
                _stage(report, pct, f"翻译中 {done}/{total}")

            translator = Translator(
                target="zh-CN", cache_file=CACHE_FILE, log=log,
                engine=opts.translate_engine,
                local_model=opts.local_model,
                bracket_mode=opts.bracket_mode,
            )
            try:
                translated_texts, failures = translator.translate_texts(
                    [c.text.replace("\n", " ").strip() for c in cues],
                    progress=on_progress,
                    cancel=cancel,
                )
                if failures:
                    out.warnings.append(f"{failures} 条字幕未翻译成功，已保留英文")
            except TranslateError as exc:
                if cancel and cancel.is_set():
                    raise Cancelled("已停止")
                out.warnings.append(f"翻译中断：{exc}")
                log(f"{out.video.name}：翻译失败（{exc}），本次只输出英文字幕")
                translated_texts = None

        _stage(report, 80, "写出字幕文件…")

        zh_cues_built: list[Cue] = []
        zh_target_path: Path | None = None
        zh_written = False

        def _write_zh() -> None:
            """写纯中文 srt；只有「没开对照、也没烧硬字幕」或硬字幕失败时才真正落盘。"""
            nonlocal zh_written
            if zh_written or zh_target_path is None or not zh_cues_built:
                return
            write_srt(zh_target_path, zh_cues_built)
            out.srt_path = zh_target_path
            out.cue_count = len(zh_cues_built)
            zh_written = True
            log(f"{out.video.name}：已保存 {zh_target_path.name}")

        if translated_texts:
            # 标注行（[MUSIC] 之类）译文为空，中文轨直接不写这一条，缩写/对照都对齐过滤
            pairs = [
                (cue, text) for cue, text in zip(cues, translated_texts) if text.strip()
            ]
            def _srt_tinted(text: str, color: str) -> str:
                """SRT 没有字体/字号，只能塞 <font color> 标签，且只有部分播放器认。"""
                if not opts.srt_colored or not text:
                    return text
                return f'<font color="{color}">{text}</font>'

            zh_cues = [
                Cue(
                    start=cue.start,
                    end=cue.end,
                    text=_srt_tinted(
                        wrap_text(text, opts.max_line_width), opts.ass_style.color_zh
                    ),
                )
                for cue, text in pairs
            ]
            zh_cues_built.extend(zh_cues)
            # 条数按实际译出的条目算：不写纯中文 srt 时（走对照/硬字幕）也要显示正确条数
            out.cue_count = len(zh_cues_built)
            zh_target_path = _target_path(out.video, outdir, opts.overwrite)

            # 已经输出对照字幕或硬字幕时，目录里再多一份纯中文 .srt 只会互相干扰
            # （PotPlayer 等会优先加载 .srt，把对照字幕的样式盖掉）
            if opts.bilingual or opts.burn_in:
                log(f"{out.video.name}：中文已含在对照字幕 / 硬字幕里，不再单独写 "
                    f"{zh_target_path.name}")
            else:
                _write_zh()

            if opts.bilingual:
                fmt = (opts.bilingual_format or "srt").lower()
                def _hard_break(text: str) -> str:
                    return text.replace("\r\n", "\n").replace("\n", "\\N")

                if fmt in ("ass", "both"):
                    entries = [
                        (
                            cue.start,
                            cue.end,
                            _hard_break(wrap_text(zh_text, opts.max_line_width)),
                            _hard_break(wrap_text(cue.text, opts.max_line_width)),
                        )
                        for cue, zh_text in pairs
                    ]
                    ass_target = _target_path(
                        out.video, outdir, opts.overwrite, tag=".bilingual", ext=".ass"
                    )
                    write_ass(ass_target, entries, style=opts.ass_style)
                    out.bilingual_path = ass_target
                    log(f"{out.video.name}：已保存 {ass_target.name}（ASS 对照，中文在上）")
                    if fmt == "ass" and opts.overwrite:
                        stale = outdir / f"{out.video.stem}.bilingual.srt"
                        if stale.exists():
                            try:
                                stale.unlink()
                                log("已移除旧的 .bilingual.srt，避免播放器优先加载 SRT")
                            except OSError:
                                pass

                if fmt in ("srt", "both"):
                    # SRT 版：中文、英文各一条独立字幕，时间轴相同
                    bi_cues: list[Cue] = []
                    for cue, zh_text in pairs:
                        bi_cues.append(
                            Cue(cue.start, cue.end,
                                _srt_tinted(
                                    _hard_break(wrap_text(zh_text, opts.max_line_width)),
                                    opts.ass_style.color_zh,
                                ))
                        )
                        bi_cues.append(
                            Cue(cue.start, cue.end,
                                _srt_tinted(
                                    _hard_break(wrap_text(cue.text, opts.max_line_width)),
                                    opts.ass_style.color_en,
                                ))
                        )
                    bi_target = _target_path(
                        out.video, outdir, opts.overwrite, tag=".bilingual"
                    )
                    write_srt(bi_target, bi_cues)
                    if out.bilingual_path is None or fmt == "both":
                        out.bilingual_path = bi_target
                    log(f"{out.video.name}：已保存 {bi_target.name}（SRT 对照，中文在上）")

        want_en = opts.keep_english or not translated_texts
        # 已经输出中英对照时不再单独写 .en.srt：同名目录里同时存在 .srt/.en.srt，
        # 播放器（尤其 PotPlayer 双字幕）容易把两份字幕并排显示成一行
        if want_en and opts.bilingual and translated_texts:
            want_en = False
            log(f"{out.video.name}：已跳过 .en.srt（对照字幕里已含英文）")
        if want_en:
            en_cues = [
                Cue(cue.start, cue.end, wrap_text(cue.text, opts.max_line_width))
                for cue in cues
            ]
            en_target = _target_path(out.video, outdir, opts.overwrite, tag=".en")
            write_srt(en_target, en_cues)
            out.english_path = en_target
            if not translated_texts:
                out.srt_path = en_target
                out.cue_count = len(en_cues)
            log(f"{out.video.name}：已保存 {en_target.name}")

        # 把字幕烧进视频（硬字幕，libass 渲染，任何播放器都不会压行）
        if opts.burn_in:
            _stage(report, 82, "准备硬字幕视频…")
            try:
                entries: list[tuple[float, float, str, str]] = []
                if translated_texts:
                    for cue, zh_text in pairs:
                        entries.append((
                            cue.start, cue.end,
                            wrap_text(zh_text, opts.max_line_width),
                            wrap_text(cue.text, opts.max_line_width),
                        ))
                else:
                    for cue in cues:
                        entries.append((
                            cue.start, cue.end, "",
                            wrap_text(cue.text, opts.max_line_width),
                        ))
                size = ff.video_size(info) or (1920, 1080)
                burn_ass_file = tmp / "burn.ass"
                write_ass(burn_ass_file, entries, play_res=size, style=opts.ass_style)
                ffmpeg, _ = ff.ensure_tools()
                burn_target = _target_path(
                    out.video, outdir, opts.overwrite, tag=BURN_TAG, ext=".mkv"
                )
                burn_ass(
                    out.video, burn_ass_file, burn_target, ffmpeg,
                    duration=ff.duration_seconds(info),
                    crf=opts.burn_crf, preset=opts.burn_preset,
                    report=lambda p: _stage(
                        report, 82 + 16 * p, f"烧字幕到视频 {int(p * 100)}%"
                    ),
                    cancel=cancel,
                )
                out.video_path = burn_target
                log(f"{out.video.name}：已输出硬字幕视频 {burn_target.name}")
            except BurnError as exc:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("已停止")
                out.warnings.append(f"烧字幕失败：{exc}")
                if out.bilingual_path is None:
                    log(f"{out.video.name}：烧字幕失败（{exc}），补写中文字幕文件")
                    _write_zh()
                else:
                    log(f"{out.video.name}：烧字幕失败（{exc}），对照字幕仍可用")
            except ff.FFmpegError as exc:
                out.warnings.append(f"烧字幕失败：{exc}")
                if out.bilingual_path is None:
                    log(f"{out.video.name}：烧字幕失败（{exc}），补写中文字幕文件")
                    _write_zh()
                else:
                    log(f"{out.video.name}：烧字幕失败（{exc}），对照字幕仍可用")

    if (out.srt_path is None and out.video_path is None
            and out.bilingual_path is None):
        if not out.message:
            out.message = "没有生成字幕文件"
        return out

    out.ok = True
    out.message = "完成"
    _stage(report, 99, "完成")
    return out
