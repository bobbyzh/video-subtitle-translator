"""基于 faster-whisper 的英文字幕语音识别。"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .paths import MODEL_DIR, hidden_subprocess_flags  # noqa: F401
from .srt import Cue, clean_text

MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
}

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class AsrError(RuntimeError):
    pass


def _model_dir(size: str) -> Path:
    return MODEL_DIR / f"faster-whisper-{size}"


_MIN_MODEL_BYTES = {
    "tiny": 40 << 20,
    "base": 100 << 20,
    "small": 400 << 20,
    "medium": 1200 << 20,
}


def model_available(size: str) -> bool:
    d = _model_dir(size)
    model_bin = d / "model.bin"
    if not model_bin.is_file():
        return False
    if model_bin.stat().st_size < _MIN_MODEL_BYTES.get(size, 40 << 20):
        return False
    return (d / "config.json").is_file() and (d / "tokenizer.json").is_file()


def _head_size(url: str) -> int:
    import urllib.request

    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return int(resp.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _fetch_file(url: str, target, log, attempts: int = 6) -> None:
    """带断点续传与长度校验的下载，防止大文件被截断。"""
    import shutil
    import urllib.request

    tmp = target.with_name(target.name + ".part")
    expected = _head_size(url)
    for attempt in range(1, attempts + 1):
        existing = tmp.stat().st_size if tmp.is_file() else 0
        if expected and existing >= expected:
            tmp.replace(target)
            return
        headers = {"User-Agent": "Mozilla/5.0"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "ab") as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)
        size = tmp.stat().st_size
        if expected:
            if size == expected:
                tmp.replace(target)
                return
            log(f"模型下载不完整（{size}/{expected} 字节），第 {attempt} 次续传…")
        elif size > 0:
            tmp.replace(target)
            return
    raise AsrError(f"模型文件下载失败：{target.name}")


def _download_model(size: str, log) -> Path:
    """从 HuggingFace / 镜像直接下载模型文件到 models 目录。"""
    import urllib.error

    dest = _model_dir(size)
    dest.mkdir(parents=True, exist_ok=True)
    repo = MODEL_REPOS[size]
    files = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt",
             "preprocessor_config.json", "vocabulary.json"]
    required = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
    endpoints = []
    if os.environ.get("HF_ENDPOINT"):
        endpoints.append(os.environ["HF_ENDPOINT"].rstrip("/"))
    endpoints += ["https://huggingface.co", "https://hf-mirror.com"]

    last: Exception | None = None
    for endpoint in endpoints:
        try:
            log(f"正在下载语音模型 {size}（来源 {endpoint}），首次使用需要几分钟…")
            for name in files:
                target = dest / name
                url = f"{endpoint}/{repo}/resolve/main/{name}"
                if name == "model.bin":
                    expected = _head_size(url)
                    if target.is_file() and expected and target.stat().st_size != expected:
                        part = target.with_name(target.name + ".part")
                        log(f"发现不完整的模型文件（{target.stat().st_size}/{expected} 字节），尝试续传…")
                        if part.is_file() and part.stat().st_size >= target.stat().st_size:
                            target.unlink(missing_ok=True)
                        else:
                            target.replace(part)
                if target.is_file() and target.stat().st_size > 1000:
                    continue
                try:
                    _fetch_file(url, target, log)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        continue
                    raise
            if all((dest / n).is_file() for n in required):
                log(f"语音模型 {size} 下载完成")
                return dest
            raise AsrError("模型文件不完整")
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"模型下载失败（{endpoint}）：{exc}")
    raise AsrError(f"语音模型 {size} 下载失败：{last}")


class AsrEngine:
    """封装 WhisperModel，模型只加载一次。"""

    _cache: dict[tuple[str, str, str], object] = {}

    def __init__(self, size: str = "small", log=None):
        self.size = size if size in MODEL_REPOS else "small"
        self.log = log or (lambda msg: None)
        self._model = None

    def load(self):
        if self._model is not None:
            return self._model
        try:
            import ctranslate2
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AsrError(
                "未安装 faster-whisper，无法做语音识别。请先运行「安装依赖.bat」。"
            ) from exc

        device, compute_type = "cpu", "int8"
        try:
            if ctranslate2.get_cuda_device_count() > 0:
                device, compute_type = "cuda", "float16"
        except Exception:  # noqa: BLE001
            pass

        if model_available(self.size):
            source = str(_model_dir(self.size))
        else:
            source = str(_download_model(self.size, self.log))

        key = (source, device, compute_type)
        cached = AsrEngine._cache.get(key)
        if cached is not None:
            self._model = cached
            return self._model

        self.log(f"正在加载语音模型 {self.size}（{device} / {compute_type}）…")
        try:
            model = WhisperModel(source, device=device, compute_type=compute_type)
        except Exception as exc:  # noqa: BLE001
            if device == "cpu":
                raise AsrError(f"语音模型加载失败：{exc}") from exc
            self.log(f"GPU 加载失败，改用 CPU：{exc}")
            model = WhisperModel(source, device="cpu", compute_type="int8")
        AsrEngine._cache[key] = model
        self._model = model
        self.log("语音模型已就绪")
        return model

    def transcribe(
        self,
        wav: Path,
        duration: float,
        progress=None,
        cancel=None,
    ) -> list[Cue]:
        model = self.load()
        samples = _read_pcm16(wav)
        segments, info = model.transcribe(
            samples,
            language="en",
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 400},
            condition_on_previous_text=False,
            word_timestamps=True,      # 有逐词时间，字幕才能真正对齐说话时刻
        )
        total = duration or getattr(info, "duration", 0) or 0
        raw: list[Cue] = []
        for seg in segments:
            if cancel and cancel.is_set():
                raise AsrError("已停止")
            text = clean_text(seg.text or "")
            if text:
                pieces = _cues_from_segment(seg, text)
                raw.extend(pieces)
            if progress and total:
                progress(min(0.999, float(seg.end) / total))
        self.log(f"语音识别完成，共 {len(raw)} 段")
        cues = _trim_to_audio(_normalize(raw), samples)
        return _fix_overlaps(cues)


def _frame_energy(samples, sr: int = 16000, win: float = 0.02):
    """按 20ms 窗算 RMS，用来判断“这一刻到底有没有人在说话”。"""
    import numpy as np

    step = max(1, int(sr * win))
    n = len(samples) // step
    if n <= 0:
        return np.zeros(0, dtype="float32"), win
    frames = samples[: n * step].astype("float32").reshape(n, step)
    return np.sqrt((frames ** 2).mean(axis=1)), win


def _trim_to_audio(cues: list[Cue], samples, sr: int = 16000) -> list[Cue]:
    """用音频能量把字幕的头尾再收紧：不提前显示、不拖到说完后的停顿里。"""
    energy, win = _frame_energy(samples, sr)
    if len(cues) == 0 or len(energy) == 0:
        return cues
    import numpy as np

    # 噪声底噪取 20 分位，阈值取底噪的 4 倍（至少 0.008）
    floor = float(np.percentile(energy, 20))
    threshold = max(0.008, floor * 4.0)
    voiced = energy > threshold

    for cue in cues:
        i0 = max(0, int(cue.start / win))
        i1 = min(len(voiced), int(cue.end / win))
        if i1 <= i0:
            continue
        seg = voiced[i0:i1]
        idx = np.flatnonzero(seg)
        if len(idx) == 0:
            continue
        first, last = i0 + int(idx[0]), i0 + int(idx[-1])
        # 起点：第一个有声窗往前留 0.05 秒；终点：最后一个有声窗往后留 0.12 秒
        new_start = max(0.0, first * win - 0.05)
        new_end = (last + 1) * win + 0.12
        if new_end - new_start >= 0.6:
            cue.start = round(new_start, 3)
            cue.end = round(new_end, 3)
    return cues


# 字幕时间轴：起点提前一点点，终点多留一点点，但都不拖泥带水
_LEAD = 0.06          # 字幕比第一个词早 0.06 秒出现
_TAIL = 0.15          # 最后一个词说完后再留 0.15 秒
_MIN_DUR = 0.9        # 单条最短显示时间
_MAX_DUR = 6.5        # 单条最长显示时间


def _cues_from_segment(seg, text: str) -> list[Cue]:
    """按逐词时间把识别结果切成字幕，而不是把整段时长按字数均分。"""
    words = [w for w in getattr(seg, "words", None) or [] if (w.word or "").strip()]
    if not words:
        # 没有词级时间（极少见）时退回按比例切分，至少别丢内容
        return _split_cue(Cue(float(seg.start), float(seg.end), text))

    groups: list[list] = []
    cur: list = []
    cur_chars = 0
    for w in words:
        token = (w.word or "").strip()
        if not token:
            continue
        gap = float(w.start) - float(cur[-1].end) if cur else 0.0
        dur = float(w.end) - float(cur[0].start) if cur else 0.0
        if cur and (
            cur_chars + len(token) + 1 > 42
            or dur > _MAX_DUR
            or gap > 0.6
            or (token.endswith((".", "!", "?")) and cur_chars >= 18)
        ):
            groups.append(cur)
            cur, cur_chars = [], 0
        cur.append(w)
        cur_chars += len(token) + (1 if cur_chars else 0)
    if cur:
        groups.append(cur)

    cues: list[Cue] = []
    for group in groups:
        start = max(0.0, float(group[0].start) - _LEAD)
        end = float(group[-1].end) + _TAIL
        if end - start < _MIN_DUR:
            end = start + _MIN_DUR
        if end - start > _MAX_DUR + 0.5:
            end = start + _MAX_DUR + 0.5
        line = " ".join((w.word or "").strip() for w in group)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            cues.append(Cue(start, end, line))
    return cues or _split_cue(Cue(float(seg.start), float(seg.end), text))


def _normalize(cues: list[Cue]) -> list[Cue]:
    """合并过碎的短句，并消掉时间轴重叠（字幕不会前一条还没走、后一条已经来）。"""
    return _fix_overlaps(_merge_tiny(cues))


def _fix_overlaps(cues: list[Cue]) -> list[Cue]:
    fixed: list[Cue] = []
    for cue in cues:
        if fixed and cue.start < fixed[-1].end:
            prev = fixed[-1]
            if prev.start + 0.55 <= cue.start - 0.02:
                prev.end = cue.start - 0.02
            else:
                cue.start = prev.end + 0.02
        if cue.end - cue.start < 0.5:
            cue.end = cue.start + 0.5
        fixed.append(cue)
    return fixed


def _split_cue(cue: Cue, max_chars: int = 84, max_duration: float = 8.0) -> list[Cue]:
    text = cue.text.replace("\n", " ")
    if len(text) <= max_chars and cue.duration <= max_duration:
        return [cue]

    sentences = [s for s in _SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        sentences = [text]

    groups: list[str] = []
    buf = ""
    for sentence in sentences:
        if len(buf) + len(sentence) + 1 <= max_chars:
            buf = (buf + " " + sentence).strip()
        else:
            if buf:
                groups.append(buf)
            while len(sentence) > max_chars:
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > max_chars // 3 else max_chars
                groups.append(sentence[:cut].strip())
                sentence = sentence[cut:].strip()
            buf = sentence
    if buf:
        groups.append(buf)

    if len(groups) <= 1:
        return [cue]

    total_chars = sum(len(g) for g in groups) or 1
    span = max(cue.duration, 0.5)
    result: list[Cue] = []
    start = cue.start
    for i, group in enumerate(groups):
        share = len(group) / total_chars
        piece = max(span * share, 0.6)
        end = cue.end if i == len(groups) - 1 else min(cue.end, start + piece)
        if end <= start:
            end = start + 0.6
        result.append(Cue(start, end, group))
        start = end
    return result


def _read_pcm16(wav: Path):
    """读取 16kHz 单声道 PCM wav，返回 float32 numpy 数组。"""
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as fh:
        if fh.getsampwidth() != 2:
            raise AsrError("音轨格式异常，需要 16bit PCM")
        frames = fh.readframes(fh.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _merge_tiny(cues: list[Cue], max_chars: int = 54, max_gap: float = 0.06) -> list[Cue]:
    merged: list[Cue] = []
    for cue in cues:
        if (
            merged
            and cue.start - merged[-1].end <= max_gap
            and len(merged[-1].text) + len(cue.text) + 1 <= max_chars
            and cue.end - merged[-1].start <= 7.0
        ):
            prev = merged[-1]
            prev.text = f"{prev.text} {cue.text}".strip()
            prev.end = cue.end
        else:
            merged.append(Cue(cue.start, cue.end, cue.text))
    return merged


def download_model_for_install(size: str, log=print) -> Path:
    """供安装脚本调用：下载指定语音模型。"""
    return _download_model(size, log)
