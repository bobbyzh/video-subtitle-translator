"""按需下载更大的离线翻译模型（Hunyuan-MT-7B，Q4_K_M，约 4.3GB）。

用法：运行「下载7B模型.bat」，或
    runtime\\python\\python.exe tools\\download_model.py

纯标准库实现，支持断点续传、308 跳转、边写边刷新进度；中断后重跑会从断点继续。
"""

from __future__ import annotations

import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.subtrans.paths import MODEL_DIR  # noqa: E402

REPO = "mradermacher/Hunyuan-MT-7B-GGUF"
FILENAME = "Hunyuan-MT-7B.Q4_K_M.gguf"
TARGET = MODEL_DIR / "hy-mt2-7b" / FILENAME
EXPECTED = 4_624_950_272
SOURCES = (
    "https://hf-mirror.com/%s/resolve/main/%s" % (REPO, FILENAME),
    "https://huggingface.co/%s/resolve/main/%s" % (REPO, FILENAME),
)
UA = {"User-Agent": "subtrans-downloader/1.0"}

CHUNK = 4 * 1024 * 1024         # 每次读 4MB
WINDOW = 16 * 1024 * 1024       # 每个连接只下 16MB：大文件直连会在 ~64MB 处静默卡死


def human(n: float) -> str:
    return f"{n / 1024 ** 3:.2f} GB"


def _open(url: str, offset: int, end: int | None = None) -> tuple[object, int]:
    """打开下载流，自动跟进 308/307/302 跳转。返回 (response, 起始偏移)。"""
    for _ in range(6):
        headers = dict(UA)
        if offset:
            headers["Range"] = (
                f"bytes={offset}-{end}" if end is not None else f"bytes={offset}-"
            )
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=45)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                loc = exc.headers.get("Location")
                if not loc:
                    raise
                url = loc
                continue
            if exc.code == 416:  # 已经下完
                return None, offset
            raise
        if resp.status == 206:
            return resp, offset
        if resp.status == 200:
            return resp, 0
        loc = resp.headers.get("Location")
        if loc:
            url = loc
            continue
        return resp, offset
    raise RuntimeError("跳转过多，无法定位下载地址")


def main() -> int:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    src_index = 0
    tries = 0
    while True:
        have = TARGET.stat().st_size if TARGET.is_file() else 0
        if have >= EXPECTED:
            print(f"\n完成：{TARGET}（{human(have)}）")
            return 0
        if tries > 2000:
            print("\n下载失败：请检查网络后重新运行「下载7B模型.bat」（会从断点继续）")
            return 1
        tries += 1
        url = SOURCES[src_index % len(SOURCES)]
        # 每个连接只下一小段（Range 指定结束位置），断了就从新位置续，不会卡死在半路
        window_end = min(have + WINDOW - 1, EXPECTED - 1)
        try:
            resp, offset = _open(url, have, window_end)
            if resp is None:
                continue
            total = EXPECTED
            mode = "ab" if offset else "wb"
            t0 = last = time.time()
            done = offset
            window_goal = window_end + 1
            with open(TARGET, mode) as fh:
                while done < window_goal:
                    chunk = resp.read(min(CHUNK, window_goal - done))
                    if not chunk:
                        break
                    fh.write(chunk)
                    fh.flush()
                    done += len(chunk)
                    now = time.time()
                    if now - last > 2:
                        last = now
                        speed = (done - offset) / max(now - t0, 0.1)
                        pct = done / total * 100 if total else 0
                        eta = (total - done) / speed / 60 if speed else 0
                        print(
                            f"\r{pct:5.1f}%  {human(done)}/{human(total)}  "
                            f"{speed / 1024 ** 2:4.1f} MB/s  剩余 {eta:2.0f} 分钟 ",
                            end="", flush=True,
                        )
            resp.close()
        except Exception as exc:  # noqa: BLE001
            size = TARGET.stat().st_size if TARGET.is_file() else 0
            print(f"\n中断：{exc}，2 秒后从 {human(size)} 续传")
            time.sleep(2)
            src_index += 1
            continue
        # 这一段没下满 → 换个源继续
        if TARGET.is_file() and TARGET.stat().st_size < EXPECTED:
            src_index += 1


if __name__ == "__main__":
    raise SystemExit(main())
