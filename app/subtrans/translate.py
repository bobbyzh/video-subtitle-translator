"""翻译通道：在线（Google 为主、MyMemory 兜底）或离线（本地 Hy-MT2 模型）。"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

MAX_CHUNK_CHARS = 1400
MAX_CHUNK_LINES = 24
MYMEMORY_LIMIT = 480


class TranslateError(RuntimeError):
    pass


class _Http:
    @staticmethod
    def get(url: str, timeout: int = 25) -> str:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    @staticmethod
    def post(url: str, data: bytes, headers: dict, timeout: int = 25) -> str:
        base = {"User-Agent": UA}
        base.update(headers)
        req = urllib.request.Request(url, data=data, headers=base)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


class GoogleProvider:
    """translate.googleapis.com 免费网页接口，支持多行批量。"""

    name = "Google"
    endpoint = "https://translate.googleapis.com/translate_a/single"

    def translate_batch(self, texts: list[str], target: str = "zh-CN") -> list[str]:
        joined = "\n".join(texts)
        payload = urllib.parse.urlencode(
            {"client": "gtx", "sl": "en", "tl": target, "dt": "t", "q": joined}
        ).encode("utf-8")
        raw = _Http.post(
            self.endpoint,
            payload,
            {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslateError(f"翻译返回格式异常：{raw[:80]}") from exc
        translated = "".join(seg[0] for seg in data[0] if seg and seg[0])
        lines = [ln.strip() for ln in translated.split("\n") if ln.strip()]
        if len(lines) == len(texts):
            return lines
        raise TranslateError(
            f"批量译文行数不一致（原文 {len(texts)} 行 / 译文 {len(lines)} 行）"
        )

    def translate_one(self, text: str, target: str = "zh-CN") -> str:
        return self.translate_batch([text], target)[0]


class MyMemoryProvider:
    """MyMemory 免费接口（无需 Key），逐句翻译，单次约 500 字符。"""

    name = "MyMemory"
    endpoint = "https://api.mymemory.translated.net/get"

    def translate_one(self, text: str, target: str = "zh-CN") -> str:
        url = self.endpoint + "?" + urllib.parse.urlencode(
            {"q": text, "langpair": f"en|{target}"}
        )
        raw = _Http.get(url)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TranslateError("MyMemory 返回格式异常") from exc
        if data.get("quotaFinished"):
            raise TranslateError("MyMemory 当日免费额度已用完")
        if str(data.get("responseStatus")) not in ("200", "206"):
            raise TranslateError(str(data.get("responseDetails") or "MyMemory 请求失败"))
        detail = data.get("responseData") or {}
        text_out = (detail.get("translatedText") or "").strip()
        if not text_out:
            raise TranslateError("MyMemory 未返回译文")
        return text_out

    def translate_batch(self, texts: list[str], target: str = "zh-CN") -> list[str]:
        return [self.translate_one(t, target) for t in texts]


def _need_translate(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


# 字幕里的非对白标注：[MUSIC]、[door slams]、(laughs)、【掌声】
# 注意 ♪ 不算标注（里面通常是歌词，是有内容的），不带进这个正则
_BRACKETS = re.compile(
    r"\[([^\]]*)\]|【([^】]*)】|（([^）]*)）|\(([^)]*)\)|〔([^〕]*)〕"
)

# 纯标注判定：短、无数字、无问句/感叹、只有字母和中文
_ANN_CHARS = re.compile(r"[A-Za-z\u4e00-\u9fff '\-·&/:,]*")
_ROUND = "({（〔"


def _is_annotation(inner: str) -> bool:
    """括号里到底是不是「标注」而不是正文。

    要挡住的典型误伤：(2019) 这种年份、[1] 这种编号、(你确定吗？) 这种台词。
    """
    s = inner.strip()
    if not s or len(s) > 40:
        return False
    if not _ANN_CHARS.fullmatch(s):        # 含数字/问号/感叹号/括号等一律不算
        return False
    if len(s.split()) > 8:                 # 太长就不像标注了
        return False
    return True


def _annotation_spans(text: str) -> list[tuple[int, int]]:
    """返回「应该整段删掉」的标注位置。

    方括号 / 【】 里的标注删在任何位置都安全（字幕里它们就是标注）；
    圆括号类只在句子开头或结尾才删，避免把 The file (final).srt 这种句中括注吃掉。
    """
    spans: list[tuple[int, int]] = []
    for m in _BRACKETS.finditer(text):
        inner = next((g for g in m.groups() if g is not None), "")
        if not _is_annotation(inner):
            continue
        if m.group(0)[0] in _ROUND:
            head = text[: m.start()].strip(" -—~·♪")
            tail = text[m.end():].strip(" -—~·♪")
            if head and tail:
                continue
        spans.append((m.start(), m.end()))
    return spans


def strip_brackets(text: str) -> str:
    """只去掉「纯标注」，正文性的括号内容原样保留。"""
    spans = _annotation_spans(text)
    if not spans:
        return text.strip()
    out: list[str] = []
    last = 0
    for start, end in spans:
        out.append(text[last:start])
        out.append(" ")
        last = end
    out.append(text[last:])
    text = re.sub(r"\s{2,}", " ", "".join(out))
    return text.strip(" -—~·")


def has_brackets(text: str) -> bool:
    """这句里有没有「会被去掉」的纯标注。"""
    return bool(_annotation_spans(text))


class LocalProvider:
    """本地离线翻译（Hy-MT2，1.8B 或 7B 档位 + llama.cpp），逐句并发请求。"""

    name = "离线模型"

    def __init__(self, log=None, tier: str = "fast"):
        self.log = log or (lambda msg: None)
        self.tier = tier
        self._server = None
        self.start_failed = False

    def _ensure(self):
        if self._server is None:
            from .localmt import shared_server

            self._server = shared_server(self.log)
            try:
                self._server.start(self.tier)
            except Exception:
                self.start_failed = True
                raise
        return self._server

    def translate_batch(self, texts: list[str], target: str = "zh-CN",
                        cancel=None, prev: list[str] | None = None) -> list[str]:
        server = self._ensure()
        try:
            out = server.translate_batch(texts, cancel=cancel, prev=prev, tier=self.tier)
        except Exception as exc:  # noqa: BLE001
            from .localmt import LocalMtError, _Cancelled

            if isinstance(exc, _Cancelled):
                raise TranslateError("已停止") from exc
            if isinstance(exc, LocalMtError):
                raise TranslateError(str(exc)) from exc
            raise
        if not any(out):
            raise TranslateError("离线翻译没有返回结果")
        return out

    def translate_one(self, text: str, target: str = "zh-CN") -> str:
        return self.translate_batch([text], target)[0]


def engine_available(engine: str, tier: str = "fast") -> tuple[bool, str]:
    """离线引擎是否就绪（在线/自动模式默认可用）。"""
    if engine not in ("local", "auto"):
        return True, ""
    from . import localmt

    if localmt.is_available(tier):
        return True, localmt.availability_note(tier)
    if engine == "auto":
        return True, "离线模型未安装，将使用在线接口"
    return False, localmt.availability_note(tier)



def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = max(
            remaining.rfind("。", 0, limit),
            remaining.rfind(". ", 0, limit),
            remaining.rfind("! ", 0, limit),
            remaining.rfind("? ", 0, limit),
            remaining.rfind(", ", 0, limit),
        )
        if cut < limit // 3:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 3:
            cut = limit
        else:
            cut += 1 if remaining[cut : cut + 1] in ".!?" else 1
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


class Translator:
    def __init__(
        self,
        target: str = "zh-CN",
        cache_file: Path | None = None,
        log=None,
        engine: str = "online",
        local_model: str = "fast",
        bracket_mode: str = "drop",
    ):
        self.target = target
        self.local_model = local_model if local_model in ("fast", "quality") else "fast"
        self.engine = engine if engine in ("online", "local", "auto") else "online"
        self.bracket_mode = bracket_mode if bracket_mode in ("drop", "keep") else "drop"
        self.cache_file = Path(cache_file) if cache_file else None
        self.log = log or (lambda msg: None)
        self.cache: dict[str, str] = {}
        self._load_cache()

    # ---------- 缓存 ----------
    def _load_cache(self) -> None:
        if not self.cache_file or not self.cache_file.is_file():
            return
        try:
            self.cache = json.loads(self.cache_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            self.cache = {}

    def save_cache(self) -> None:
        if not self.cache_file:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cache, ensure_ascii=False), "utf-8")
            tmp.replace(self.cache_file)
        except OSError:
            pass

    @staticmethod
    def _key(text: str, namespace: str = "") -> str:
        return hashlib.sha1(f"{namespace}\x1f{text}".encode("utf-8")).hexdigest()

    # ---------- 主流程 ----------
    def translate_texts(
        self,
        texts: list[str],
        progress=None,
        cancel=None,
        max_retries: int = 3,
    ) -> tuple[list[str], int]:
        """返回（译文列表，失败行数）。progress(done,total)。"""
        results: list[str] = [""] * len(texts)
        pending: list[int] = []
        # 缓存按「引擎+模型档位」分开：切到 7B 时不会拿 1.8B/在线的旧译文冒充
        namespace = f"{self.engine}:{self.local_model}"
        hits = 0
        # 方括号标注（[MUSIC]、(laughs)、♪）按设置处理：默认丢弃，不送进模型硬译
        sources = list(texts)
        if self.bracket_mode == "drop":
            dropped = 0
            for i, text in enumerate(texts):
                if not has_brackets(text):
                    continue
                clean = strip_brackets(text.replace("\n", " "))
                dropped += 1
                sources[i] = clean
            if dropped:
                self.log(f"已去掉 {dropped} 条字幕里的方括号标注")
        for i, text in enumerate(sources):
            if not text:
                results[i] = ""          # 整条都是标注 → 译文留空，后面会被过滤掉
                continue
            if not _need_translate(text):
                results[i] = text
                continue
            cached = self.cache.get(self._key(text, namespace))
            if cached:
                results[i] = cached
                hits += 1
            else:
                pending.append(i)

        if hits:
            self.log(f"缓存命中 {hits} 条（{namespace} 的旧译文直接复用），"
                     f"新译 {len(pending)} 条")

        total = len(pending)
        done = 0
        failures = 0
        if progress:
            progress(0, max(total, 1))

        google = GoogleProvider()
        fallback = MyMemoryProvider()
        local: LocalProvider | None = None
        if self.engine in ("local", "auto"):
            from . import localmt

            if not localmt.is_available(self.local_model) and self.local_model != "fast":
                # 选了 7B 但模型缺失/没下完：自动退回 1.8B，别让整批任务直接失败
                if localmt.is_available("fast"):
                    self.log(
                        "7B 模型不可用（" + localmt.availability_note(self.local_model) + "），"
                        "本次先用 Hy-MT2-1.8B"
                    )
                    self.local_model = "fast"
            if localmt.is_available(self.local_model):
                local = LocalProvider(log=self.log, tier=self.local_model)
            elif self.engine == "local":
                raise TranslateError(localmt.availability_note(self.local_model))
            else:
                self.log("离线模型未安装，本次改用在线接口")

        for chunk_indices in self._chunks(pending, sources, self.engine):
            if cancel and cancel.is_set():
                raise TranslateError("已停止")
            chunk = [sources[i] for i in chunk_indices]
            prev = [sources[i - 1] if i > 0 else "" for i in chunk_indices]
            if local is not None:
                translated = self._translate_chunk_local(chunk, local, cancel, prev)
                if self.engine == "auto" and local is not None:
                    # 自动模式：离线没出的行用在线补译
                    if local.start_failed:
                        self.log("离线引擎启动失败，本次全部改用在线接口")
                        local = None
                        translated = self._translate_chunk(
                            chunk, google, fallback, max_retries
                        )
                    else:
                        missing = [i for i, t in enumerate(translated) if not t]
                        if missing:
                            fix = self._translate_chunk(
                                [chunk[i] for i in missing], google, fallback, max_retries
                            )
                            for i, value in zip(missing, fix):
                                translated[i] = value
                            self.log(
                                f"离线有 {len(missing)} 条未成功，已用在线接口补译"
                            )
            else:
                translated = self._translate_chunk(chunk, google, fallback, max_retries)
            failures = 0
            for idx, src, out in zip(chunk_indices, chunk, translated):
                if out:
                    results[idx] = out
                    self.cache[self._key(src, namespace)] = out
                elif has_brackets(texts[idx]) and self.bracket_mode == "drop":
                    # 去掉标注后为空（整条都是 [MUSIC] 这类）：不写出译文，当作已处理
                    results[idx] = ""
                else:
                    results[idx] = src
                    failures += 1
            done += len(chunk)
            if progress:
                progress(done, max(total, 1))
            if local is None:
                time.sleep(0.18 + random.random() * 0.12)

        if failures:
            self.log(f"有 {failures} 条字幕翻译失败，已保留英文原文")
        self.save_cache()
        return results, failures

    def _chunks(self, pending: list[int], texts: list[str], engine: str = "online"):
        chunk: list[int] = []
        chars = 0
        limit_chars = 6000 if engine in ("local", "auto") else MAX_CHUNK_CHARS
        limit_lines = 12 if engine in ("local", "auto") else MAX_CHUNK_LINES
        for idx in pending:
            text = texts[idx]
            text_len = len(text)
            if chunk and (chars + text_len > limit_chars or len(chunk) >= limit_lines):
                yield chunk
                chunk, chars = [], 0
            chunk.append(idx)
            chars += text_len
        if chunk:
            yield chunk

    def _translate_chunk_local(
        self, chunk: list[str], local: LocalProvider, cancel,
        prev: list[str] | None = None,
    ) -> list[str]:
        try:
            out = local.translate_batch(chunk, self.target, cancel=cancel, prev=prev)
        except TranslateError as exc:
            if cancel and cancel.is_set():
                raise
            self.log(f"离线翻译这一批失败：{exc}")
            out = [""] * len(chunk)
        return out

    def _translate_chunk(
        self,
        chunk: list[str],
        google: GoogleProvider,
        fallback: MyMemoryProvider,
        max_retries: int,
    ) -> list[str]:
        # 1) 优先整块调用 Google（快、质量好）
        for attempt in range(1, max_retries + 1):
            try:
                return google.translate_batch(chunk, self.target)
            except urllib.error.HTTPError as exc:
                wait = self._wait_for(exc.code, attempt)
                self.log(f"Google 翻译失败（HTTP {exc.code}），{wait}s 后重试")
            except (TranslateError, urllib.error.URLError, TimeoutError, OSError) as exc:
                wait = min(2 ** attempt, 8)
                self.log(f"Google 翻译异常：{exc}，{wait}s 后重试")
            time.sleep(wait)

        # 2) 整块失败则逐句改用 Google，再失败换 MyMemory
        out: list[str] = []
        for line in chunk:
            out.append(self._translate_single(line, google, fallback))
        return out

    def _translate_single(
        self, text: str, google: GoogleProvider, fallback: MyMemoryProvider
    ) -> str:
        for provider in (google, fallback):
            for attempt in range(1, 3):
                try:
                    if provider is google:
                        return provider.translate_one(text, self.target)
                    return self._mymemory(text, fallback)
                except urllib.error.HTTPError as exc:
                    time.sleep(self._wait_for(exc.code, attempt))
                except (TranslateError, urllib.error.URLError, TimeoutError, OSError):
                    time.sleep(min(2 ** attempt, 6))
            self.log(f"{provider.name} 单句翻译失败，切换下一个通道")
        return ""

    def _mymemory(self, text: str, provider: MyMemoryProvider) -> str:
        parts = _split_long(text, MYMEMORY_LIMIT)
        return "".join(provider.translate_one(p, self.target) for p in parts)

    @staticmethod
    def _wait_for(status_code: int, attempt: int) -> float:
        if status_code == 429:
            return min(6 * attempt, 30)
        if 500 <= status_code < 600:
            return min(2 * attempt, 10)
        return min(1.5 * attempt, 6)


def check_translation(target: str = "zh-CN", log=None, engine: str = "online",
                      local_model: str = "fast") -> tuple[bool, str]:
    """启动时自检：确认翻译通道可用。"""
    if engine in ("local", "auto"):
        from . import localmt

        if not localmt.is_available(local_model):
            if engine == "auto":
                return True, "离线模型未安装，本次使用在线接口"
            return False, localmt.availability_note(local_model)
        try:
            out = LocalProvider(log, tier=local_model).translate_one(
                "Welcome to this subtitle extraction test.", target
            )
            note = "离线优先，失败回落在线" if engine == "auto" else "离线翻译引擎正常"
            return True, f"{note}（示例译文：{out}）"
        except Exception as exc:  # noqa: BLE001
            if engine == "auto":
                return True, f"离线引擎异常（{exc}），将回落在线接口"
            return False, f"离线翻译引擎异常：{exc}"
    try:
        out = GoogleProvider().translate_one("Hello world", target)
        return True, f"Google 接口正常（示例译文：{out}）"
    except Exception as exc:  # noqa: BLE001
        try:
            MyMemoryProvider().translate_one("Hello world", target)
            return True, f"Google 不可用（{exc}），已切换到 MyMemory"
        except Exception as exc2:  # noqa: BLE001
            return False, f"翻译接口暂不可用：{exc2}"
