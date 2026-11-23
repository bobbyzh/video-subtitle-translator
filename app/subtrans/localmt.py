"""离线翻译：本地 llama.cpp + 混元 Hy-MT2-1.8B（GGUF）。

不依赖任何在线接口：启动一个本地 llama-server，用 OpenAI 兼容接口做翻译。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .paths import BIN_DIR, DATA_DIR, GLOSSARY_FILE, MODEL_DIR, hidden_subprocess_flags

SERVER_DIR = BIN_DIR / "llamacpp"

# 离线模型档位：fast = 1.8B（默认，速度优先）；quality = 7B（质量优先，慢 3~4 倍）
MODELS: dict[str, dict] = {
    "fast": {
        "label": "Hy-MT2-1.8B",
        "path": MODEL_DIR / "hy-mt2" / "Hy-MT2-1.8B-Q4_K_M.gguf",
        "size": "约 1.1GB",
        "bytes": 1_133_080_448,
        "threads": None,  # None = 用满物理核
        "workers": 4,
        "tokens": 160,
        "ctx": True,      # 1.8B 能听懂「上文仅参考」，带上文更连贯
    },
    "quality": {
        "label": "Hunyuan-MT-7B",
        "path": MODEL_DIR / "hy-mt2-7b" / "Hunyuan-MT-7B.Q4_K_M.gguf",
        "size": "约 4.3GB",
        "bytes": 4_624_950_272,
        "threads": None,
        "workers": 3,
        "tokens": 200,
        # 7B 偶尔会把「上文」那一行也当成待译内容拼进来（实测会串行），
        # 它本身上下文理解够强，索性不给上文
        "ctx": False,
    },
}
DEFAULT_TIER = "fast"


def model_file(tier: str = DEFAULT_TIER) -> Path:
    return MODELS.get(tier, MODELS[DEFAULT_TIER])["path"]


def model_label(tier: str = DEFAULT_TIER) -> str:
    return MODELS.get(tier, MODELS[DEFAULT_TIER])["label"]


def model_ready(tier: str = DEFAULT_TIER) -> bool:
    """模型文件是否下完整。

    下载中的半成品只比最终大小少一点，而 GGUF 少一个字节都加载不了，
    所以这里要求几乎一致（0.999），不能用宽松阈值。
    """
    spec = MODELS.get(tier, MODELS[DEFAULT_TIER])
    try:
        return spec["path"].is_file() and spec["path"].stat().st_size >= spec["bytes"] * 0.999
    except OSError:
        return False

# 字幕场景的固定术语，避免模型把 subtitle 译成“副标题/子标题”
DEFAULT_TERMS = {
    "subtitle": "字幕",
    "NARRATOR": "旁白",
    "SUBTITLE": "字幕",
}

PROMPT = (
    "{terms}"
    "{ctx}"
    "将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
)
# 短句（如 "Hello world"）模型偶尔会原样返回，用更直白的提示重试一次
PROMPT_STRICT = (
    "{terms}"
    "请把下面这句英文翻译成简体中文，只输出中文译文，不要保留英文：\n\n{text}"
)

WORKERS = 4
MAX_TOKENS = 160

STATE_FILE = DATA_DIR / "mt_server.json"


def _read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_state(pid: int, port: int, tier: str = DEFAULT_TIER) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps({"pid": pid, "port": port, "tier": tier}), "utf-8"
        )
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False


def _health_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        ) as resp:
            return resp.status == 200 and '"ok"' in resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def load_terms() -> dict[str, str]:
    """读取 data\\glossary.txt（每行 `英文=中文`），首次运行写入示例。"""
    terms = dict(DEFAULT_TERMS)
    try:
        if not GLOSSARY_FILE.is_file():
            GLOSSARY_FILE.parent.mkdir(parents=True, exist_ok=True)
            GLOSSARY_FILE.write_text(
                "# 自定义术语表：每行写「英文=中文」，翻译时会强制按这里的译法\n"
                "# 姓名/称呼想保留原文就写成 Chris=Chris\n"
                "subtitle=字幕\nNARRATOR=旁白\n",
                "utf-8",
            )
        for line in GLOSSARY_FILE.read_text("utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            src, dst = line.split("=", 1)
            src, dst = src.strip(), dst.strip()
            if src and dst:
                terms[src] = dst
    except OSError:
        pass
    return terms


def terms_prompt(terms: dict[str, str]) -> str:
    if not terms:
        return ""
    body = "\n".join(f"{k} 翻译成 {v}" for k, v in terms.items())
    return f"参考下面的翻译：\n{body}\n\n"


def matching_terms(text: str, terms: dict[str, str]) -> dict[str, str]:
    """只把本句里真正出现的术语塞进提示。

    整张术语表一股脑塞进去会让模型把术语表本身当成待译内容，
    输出里冒出「字幕：」「旁白：」这类多余前缀。
    """
    low = text.lower()
    return {k: v for k, v in terms.items() if k.lower() in low}


class LocalMtError(RuntimeError):
    pass


def server_exe() -> Path | None:
    for name in ("llama-server.exe", "llama-server"):
        p = SERVER_DIR / name
        if p.is_file():
            return p
    return None


def is_available(tier: str = DEFAULT_TIER) -> bool:
    return server_exe() is not None and model_ready(tier)


def availability_note(tier: str = DEFAULT_TIER) -> str:
    spec = MODELS.get(tier, MODELS[DEFAULT_TIER])
    if server_exe() is None:
        return "缺少 llama.cpp 程序（bin\\llamacpp\\llama-server.exe）"
    if not model_ready(tier):
        have = 0
        try:
            if spec["path"].is_file():
                have = spec["path"].stat().st_size
        except OSError:
            have = 0
        if have:
            return (
                f"{spec['label']} 模型只下载了 {have / 1024 ** 3:.2f}GB"
                f"（共 {spec['size']}），不完整无法加载，请运行「下载7B模型.bat」续传"
            )
        if tier == "quality":
            return (
                f"缺少 {spec['label']} 模型文件（{spec['path'].name}，{spec['size']}），"
                "请运行程序目录下的「下载7B模型.bat」"
            )
        return f"缺少 {spec['label']} 模型文件（{spec['path'].name}，{spec['size']}），请重新运行「安装依赖.bat」"
    return "离线翻译可用"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LocalMtServer:
    """常驻的本地翻译服务，进程内单例。"""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.proc: subprocess.Popen | None = None
        self.port: int | None = None
        self.external = False
        self.tier = DEFAULT_TIER
        self._lock = threading.Lock()
        self._bad_lines = 0

    # ---------- 生命周期 ----------
    def start(self, tier: str | None = None) -> None:
        tier = tier or self.tier or DEFAULT_TIER
        with self._lock:
            running = self.proc is not None and self.proc.poll() is None
            if running and self.tier == tier:
                return
            if self.external and self.port and self.tier == tier and _health_ok(self.port):
                return
            if running or self.external:
                # 档位换了（或旧引擎已失效）：先把旧的收掉，避免两个模型同时占 CPU
                self._stop_locked()
            self.tier = tier
            # 复用上一个进程留下的同档位引擎，避免同时跑两个模型互相抢 CPU
            state = _read_state()
            if (
                state
                and state.get("tier", "fast") == tier
                and _pid_alive(int(state.get("pid", 0)))
                and _health_ok(int(state.get("port", 0)))
            ):
                self.port = int(state["port"])
                self.proc = None
                self.external = True
                self.log("复用已在运行的离线翻译引擎（%s，端口 %d）"
                         % (model_label(tier), self.port))
                return
            exe = server_exe()
            if exe is None:
                raise LocalMtError(
                    "离线翻译缺少 llama.cpp（bin\\llamacpp\\llama-server.exe），"
                    "请重新运行「安装依赖.bat」"
                )
            spec = MODELS.get(tier, MODELS[DEFAULT_TIER])
            if not model_ready(tier):
                raise LocalMtError(availability_note(tier))
            self.port = _free_port()
            threads = spec["threads"] or max(2, min(8, (os.cpu_count() or 4)))
            cmd = [
                str(exe),
                "-m", str(spec["path"]),
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "-t", str(threads),
                "-c", "8192",
                "-np", str(spec["workers"]),
                "--no-warmup",
                "--no-webui",
            ]
            self.log(f"正在启动离线翻译引擎（{spec['label']}，{threads} 线程）…")
            creation = hidden_subprocess_flags()
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creation,
            )
            if not self._wait_health():
                self._stop_locked()  # 注意：不能用 self.stop()，这里已持有锁会死锁
                raise LocalMtError(
                    f"离线翻译引擎启动失败（{spec['label']}），请查看日志"
                )
            _write_state(self.proc.pid, self.port, tier)
            self.external = False
            self.log(f"离线翻译引擎已就绪（{spec['label']}）")

    def _wait_health(self, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/health", timeout=2
                ) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    if resp.status == 200 and '"ok"' in body:
                        return True
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(0.6)
        return False

    def _stop_locked(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            try:
                STATE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
        elif self.external and self.port:
            # 复用的外部引擎：它属于另一个实例，切档位时也一并收掉，免得两个模型抢 CPU
            state = _read_state() or {}
            pid = int(state.get("pid", 0))
            if pid and _pid_alive(pid):
                try:
                    import ctypes

                    handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
                    if handle:
                        ctypes.windll.kernel32.TerminateProcess(handle, 1)
                        ctypes.windll.kernel32.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
            try:
                STATE_FILE.unlink(missing_ok=True)
            except OSError:
                pass
        self.proc = None
        self.port = None
        self.external = False

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    # ---------- 翻译 ----------
    def translate_batch(self, texts: list[str], cancel=None,
                        prev: list[str] | None = None,
                        tier: str | None = None) -> list[str]:
        if not texts:
            return []
        if tier:
            self.tier = tier
        if self.proc is None or self.proc.poll() is not None:
            self.start(tier)
        spec = MODELS.get(self.tier, MODELS[DEFAULT_TIER])
        self._bad_lines = 0
        terms = load_terms()
        out: list[str] = [""] * len(texts)
        with ThreadPoolExecutor(max_workers=spec["workers"]) as pool:
            use_ctx = spec.get("ctx", True)
            futures = [
                pool.submit(self._translate_one, t, cancel,
                            (prev[i] if prev and i < len(prev) else "") if use_ctx else "",
                            terms, spec["tokens"])
                for i, t in enumerate(texts)
            ]
            for i, fut in enumerate(futures):
                try:
                    out[i] = fut.result()
                except _Cancelled:
                    raise
                except Exception:  # noqa: BLE001
                    out[i] = ""
        if self._bad_lines:
            self.log(f"离线翻译有 {self._bad_lines} 条结果异常，改用英文原文")
        return out

    def _translate_one(self, text: str, cancel=None, prev: str = "",
                       terms: dict[str, str] | None = None,
                       max_tokens: int = MAX_TOKENS) -> str:
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        last_err: Exception | None = None
        # 上文必须标明「不要翻译」，否则 7B 会把上一句一起译出来拼在本句后面
        ctx = f"上一句（仅作参考，不要翻译它）：{prev}\n" if prev and len(prev) < 120 else ""
        terms_text = terms_prompt(matching_terms(text, terms)) if terms else ""
        for attempt in range(2):
            if cancel is not None and cancel.is_set():
                raise _Cancelled()
            template = PROMPT if attempt == 0 else PROMPT_STRICT
            prompt = template.format(text=text, terms=terms_text, ctx=ctx)
            payload = {
                "messages": [
                    {"role": "system",
                     "content": "你是专业的影视字幕译者，译文简洁自然、口语化，只输出译文。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.6 if attempt == 0 else 0.3,
                "top_p": 0.6,
                "top_k": 20,
                "repetition_penalty": 1.05,
                "max_tokens": max_tokens,
                "stream": False,
            }
            try:
                raw = self._post(payload)
                return self._clean(self._parse(raw))
            except LocalMtError as exc:
                last_err = exc
                if "未返回中文" not in str(exc):
                    time.sleep(0.5 + attempt)
                if self.proc is None or self.proc.poll() is not None:
                    self.start()
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = exc
                time.sleep(0.5 + attempt)
                if self.proc is None or self.proc.poll() is not None:
                    self.start()
        self._bad_lines += 1
        raise LocalMtError(str(last_err) if last_err else "离线翻译失败")

    def _post(self, payload: dict) -> str:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/chat/completions",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def _parse(raw: str) -> str:
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"].strip()
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LocalMtError("离线翻译返回格式异常") from exc

    def _clean(self, text: str) -> str:
        text = text.strip()
        for prefix in ("译文：", "翻译：", "Translation:", "Chinese:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        # 去掉模型偶发加上的引号
        if len(text) >= 2 and text[0] in "“\"'" and text[-1] in "”\"'":
            text = text[1:-1].strip()
        # 去掉模型把提示语本身当内容译出来的前缀，如「（上一句仅供参考）：」
        text = re.sub(r"^[（(][^（()）]{1,30}[)）]\s*[:：]?\s*", "", text).strip()
        # 多行只取前几行并拼成一行（字幕本身不换行）
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            text = " ".join(lines)
        if not text or not any("\u4e00" <= ch <= "\u9fff" for ch in text):
            raise LocalMtError("离线翻译未返回中文")
        # 中文太少（多半是整句照抄英文）→ 当作失败，触发严格重试
        cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
        latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
        if latin > cjk * 1.6:
            raise LocalMtError("离线翻译英文字符过多")
        # 拼接自检：字幕单句不会又长又带好几个句号，多半是把上文也一起译了
        if len(text) > 60 and text.count("。") >= 2:
            raise LocalMtError("离线翻译疑似拼接多句")
        return _tidy(text)


class _Cancelled(RuntimeError):
    pass


_HALF_TO_FULL = {"，": ",", "。": ".", "！": "!", "？": "?", "；": ";", "：": ":"}
_FULL_TO_HALF = {v: k for k, v in _HALF_TO_FULL.items()}


def _tidy(text: str) -> str:
    """中文译文的标点/空格整理：中文语境里的半角标点换全角，去掉多余空格。"""
    out: list[str] = []
    for i, ch in enumerate(text):
        if ch in _FULL_TO_HALF:
            prev = text[i - 1] if i > 0 else ""
            nxt = text[i + 1] if i + 1 < len(text) else ""
            # 只有紧邻中文时才换全角，避免破坏 3.5 / A.B 这类英文写法
            if ("\u4e00" <= prev <= "\u9fff") or ("\u4e00" <= nxt <= "\u9fff"):
                out.append(_FULL_TO_HALF[ch])
                continue
        out.append(ch)
    text = "".join(out)
    # 中文之间的空格去掉
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_server: LocalMtServer | None = None
_server_lock = threading.Lock()


def shared_server(log=None) -> LocalMtServer:
    global _server
    with _server_lock:
        if _server is None:
            _server = LocalMtServer(log=log)
        elif log is not None:
            _server.log = log
    return _server


def shutdown() -> None:
    if _server is not None:
        _server.stop()


atexit.register(shutdown)
