"""Tkinter 图形界面。"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import colorchooser, filedialog, font as tkfont, messagebox, ttk

from .config import load_config, save_config
from .paths import DATA_DIR, LOG_DIR, ensure_dirs, ffmpeg_path
from .pipeline import VIDEO_SUFFIXES, Cancelled, Options, process_file
from .translate import check_translation

BG = "#f5f6f8"
CARD = "#ffffff"
TEXT = "#1f2937"
MUTED = "#6b7280"
ACCENT = "#2563eb"
OK_GREEN = "#15803d"
ERR_RED = "#b91c1c"

SOURCE_LABELS = {
    "auto": "自动（优先内嵌字幕，没有则语音识别）",
    "embedded": "仅使用内嵌字幕轨",
    "asr": "仅语音识别（Whisper）",
}
SOURCE_KEYS = {v: k for k, v in SOURCE_LABELS.items()}

ENGINE_LABELS = {
    "auto": "自动（优先离线，失败回落在线）",
    "local": "离线模型（不联网，较慢）",
    "online": "在线接口（Google，联网，速度快）",
}

LOCAL_MODEL_LABELS = {
    "fast": "Hy-MT2-1.8B（快，约 6 分钟/集）",
    "quality": "Hunyuan-MT-7B（质量优先，慢 3~4 倍）",
}

BILINGUAL_FORMATS = {
    "srt": "SRT（两条独立字幕，最兼容）",
    "ass": "ASS（排版固定，需播放器支持 libass）",
    "both": "ASS + SRT（两种都出）",
}

BRACKET_MODES = {
    "drop": "只丢弃纯标注（[MUSIC]、[敲门声] 等）",
    "keep": "保留原文（标注不翻译，原样留着）",
}


def _enable_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def _open_in_explorer(path: Path) -> None:
    if os.name == "nt" and path.exists():
        os.startfile(path)  # noqa: S606


class App:
    def __init__(self, root: tk.Tk, dnd_enabled: bool = False):
        self.root = root
        self.dnd_enabled = dnd_enabled
        self.queue: queue.Queue = queue.Queue()
        self.files: list[Path] = []
        self.row_ids: dict[str, str] = {}
        self.file_status: dict[str, str] = {}
        self.results: dict[str, object] = {}
        self.worker: threading.Thread | None = None
        self.cancel_evt = threading.Event()
        self.current_index = 0
        self.total_files = 0
        self.cfg = load_config()
        ensure_dirs()

        self._setup_window()
        self._setup_style()
        self._setup_style_vars()
        self._build_ui()
        self._apply_config()
        self.root.after(120, self._poll_queue)
        self.root.after(300, self._init_sash)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        threading.Thread(target=self._self_check,
                         args=(self._current_engine(), self._current_local_model()),
                         daemon=True).start()

    def _setup_style_vars(self) -> None:
        """字幕样式的取值放在 App 上，对话框只负责编辑。"""
        self.font_var = tk.StringVar()
        self.zh_size_var = tk.StringVar()
        self.en_size_var = tk.StringVar()
        self.zh_color_var = tk.StringVar()
        self.en_color_var = tk.StringVar()
        self.outline_var = tk.StringVar()
        self.bold_var = tk.BooleanVar()
        self.position_var = tk.StringVar()
        self.srt_color_var = tk.BooleanVar()
        try:
            self.font_families = sorted(set(tkfont.families()))
        except Exception:  # noqa: BLE001
            self.font_families = ["Microsoft YaHei", "SimHei", "SimSun", "KaiTi", "SimSun"]
        self._style_dialog: "StyleDialog | None" = None

    # ---------------- 窗口与样式 ----------------
    def _setup_window(self) -> None:
        self.root.title("视频字幕提取翻译工具")
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.configure(bg="#f0f0f0")
        saved = self._saved_geometry()
        if saved:
            w, h, x, y = saved
            w = min(max(w, 1100), sw - 40)
            h = min(max(h, 740), sh - 60)
            x = min(max(x, 0), max(0, sw - w))
            y = min(max(y, 0), max(0, sh - h))
            self.root.geometry(f"{w}x{h}+{x}+{y}")
        else:
            # 按屏幕比例取尺寸（高 DPI 下 Tk 报的是物理像素，比例法两种情况都适用）
            w = min(max(int(sw * 0.52), 1240), 2000)
            h = min(max(int(sh * 0.62), 760), 1500)
            self.root.geometry(f"{w}x{h}")
        self.root.minsize(1100, 740)

    def _saved_geometry(self) -> tuple[int, int, int, int] | None:
        """从配置里还原上次的窗口位置尺寸。"""
        raw = self.cfg.get("win_geometry")
        if not isinstance(raw, str) or "x" not in raw:
            return None
        try:
            size, _, rest = raw.partition("+")
            w, h = (int(v) for v in size.split("x"))
            y = 0
            x = 0
            parts = rest.split("+")
            if parts and parts[0].lstrip("-").isdigit():
                x = int(parts[0])
            if len(parts) > 1 and parts[1].lstrip("-").isdigit():
                y = int(parts[1])
            return w, h, x, y
        except (ValueError, TypeError):
            return None

    def _fit_window(self) -> None:
        """兜底：只在确实装不下时补足（延迟执行，等布局稳定后再量）。"""

    def _init_sash(self) -> None:
        """日志区高度：优先还原上次拖出来的比例，否则默认留 200px。"""
        try:
            h = self.paned.winfo_height()
            ratio = self.cfg.get("sash_ratio")
            if isinstance(ratio, (int, float)) and 0.2 <= float(ratio) <= 0.95:
                self.paned.sashpos(0, int(h * float(ratio)))
            elif h > 400:
                self.paned.sashpos(0, max(360, h - 200))
        except Exception:  # noqa: BLE001
            pass
        self.paned.bind("<ButtonRelease-1>", self._clamp_sash)

    def _paint_grip(self, hover: bool = False) -> None:
        """画一条可拖动的手柄：细横线 + 中间三道小横杠。"""
        try:
            c = self.list_grip
            w = c.winfo_width()
            if w < 20:
                return
            c.delete("all")
            c.create_line(0, 1, w, 1, fill="#d9d9d9")
            color = "#7d858c" if hover else "#b3b9bf"
            for y in (4, 6, 8):
                c.create_line(w // 2 - 18, y, w // 2 + 18, y, fill=color)
        except Exception:  # noqa: BLE001
            pass

    def _grip_press(self, event) -> None:
        self._grip_y = event.y_root
        self._grip_sash = self.paned.sashpos(0)

    def _grip_drag(self, event) -> None:
        """拖动「列表下方 / 设置上方」的横条：往下拖列表变大、日志变小。"""
        dy = event.y_root - getattr(self, "_grip_y", event.y_root)
        target = getattr(self, "_grip_sash", self.paned.sashpos(0)) + dy
        try:
            self.paned.sashpos(0, target)
            self._clamp_sash()
        except Exception:  # noqa: BLE001
            pass

    def _clamp_sash(self, _event=None) -> None:
        """拖动后把分栏位置夹住：设置区不被压扁，日志也留一条缝。"""
        try:
            h = self.paned.winfo_height()
            fixed = (self._header.winfo_reqheight() + self._opts_card.winfo_reqheight()
                     + self._actions.winfo_reqheight() + self.progress.winfo_reqheight()
                     + 60)
            low = min(fixed + 80, h - 140)   # 列表至少留一点
            high = max(low, h - 140)         # 日志区至少留 140px
            pos = self.paned.sashpos(0)
            if pos < low:
                self.paned.sashpos(0, low)
            elif pos > high:
                self.paned.sashpos(0, high)
        except Exception:  # noqa: BLE001
            pass

    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        for name in ("vista", "winnative", "xpnative", "clam"):
            try:
                style.theme_use(name)
                break
            except tk.TclError:
                continue
        family = "Microsoft YaHei UI" if os.name == "nt" else "Sans"
        base_font = (family, 10)
        self.root.option_add("*Font", base_font)
        style.configure(".", font=base_font)
        style.configure("TLabel", font=base_font)
        style.configure("Muted.TLabel", font=(family, 9), foreground="#4b5563")
        style.configure("Title.TLabel", font=(family, 14, "bold"))
        style.configure("TButton", font=(family, 10), padding=(8, 3))
        style.configure("Primary.TButton", font=(family, 10, "bold"), padding=(12, 5))
        style.configure("TEntry", padding=(4, 3))
        style.configure("TCombobox", padding=(2, 2))
        style.configure("Treeview", font=(family, 10), rowheight=30,
                        background="#ffffff", fieldbackground="#ffffff")
        style.configure("Treeview.Heading", font=(family, 10, "bold"))
        style.map("Treeview", background=[("selected", "#cce8ff")],
                  foreground=[("selected", "#000000")])

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=(16, 8, 16, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)

        # 上半部分（列表 + 设置 + 按钮）与日志区之间可拖动改变高度
        paned = ttk.PanedWindow(outer, orient="vertical")
        paned.grid(row=0, column=0, sticky="nsew")
        self.paned = paned

        top = ttk.Frame(paned)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=1, minsize=110)
        paned.add(top, weight=1)
        try:
            paned.paneconfigure(top, minsize=660)
        except Exception:  # noqa: BLE001
            pass

        header = ttk.Frame(top)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._header = header
        ttk.Label(header, text="视频字幕提取翻译工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="批量选择视频 → 提取英文字幕（优先内嵌，没有则语音识别）→ 翻译中文 → 输出同名 .srt",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        # 文件列表
        files_card = ttk.Frame(top, padding=(12, 8))
        files_card.grid(row=1, column=0, sticky="nsew")
        files_card.columnconfigure(0, weight=1)
        files_card.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(files_card)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Button(toolbar, text="添加视频…", command=self._add_files).pack(side="left")
        ttk.Button(toolbar, text="移除选中", command=self._remove_selected).pack(side="left", padx=8)
        ttk.Button(toolbar, text="清空列表", command=self._clear_files).pack(side="left")
        self.dnd_hint = ttk.Label(
            toolbar,
            text="也可以直接把视频拖进窗口" if self.dnd_enabled else "支持 mp4 / mkv / avi / mov / flv 等常见格式",
            style="Muted.TLabel",
        )
        self.dnd_hint.pack(side="right")

        tree_wrap = ttk.Frame(files_card)
        tree_wrap.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(8, 0))
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)
        columns = ("name", "info", "source", "status")
        self.tree = ttk.Treeview(tree_wrap, columns=columns, show="headings", height=5)
        self.tree.heading("name", text="视频文件")
        self.tree.heading("info", text="信息")
        self.tree.heading("source", text="字幕来源")
        self.tree.heading("status", text="状态")
        defaults = {"name": 400, "info": 140, "source": 210, "status": 180}
        saved = self.cfg.get("col_widths")
        if not isinstance(saved, dict):
            saved = {}
        for key, fallback in defaults.items():
            try:
                width = int(saved.get(key, fallback))
            except (TypeError, ValueError):
                width = fallback
            width = max(60, min(width, 2400))
            # stretch=False：列宽就按记住的值来，窗口变宽也不会把记忆的宽度摊平
            self.tree.column(key, width=width, anchor="w", stretch=False)
        vsb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(files_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=hsb.set)
        hsb.grid(row=2, column=0, sticky="ew")
        self.tree.tag_configure("ok", foreground=OK_GREEN)
        self.tree.tag_configure("fail", foreground=ERR_RED)
        self.tree.tag_configure("run", foreground=ACCENT)
        self.tree.bind("<Double-1>", self._on_row_double_click)
        # 拖完列宽就把新宽度记进配置（下次启动按记忆的宽度排布）
        self.tree.bind("<ButtonRelease-1>", self._remember_col_widths, add="+")

        # 列表与设置之间的拖动条：拖它同样改变列表/日志的高度分配
        self.list_grip = tk.Canvas(top, height=12, bg="#f0f0f0",
                                   highlightthickness=0, bd=0,
                                   cursor="sb_v_double_arrow")
        self.list_grip.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.list_grip.bind("<Configure>", lambda e: self._paint_grip())
        self.list_grip.bind("<ButtonPress-1>", self._grip_press)
        self.list_grip.bind("<B1-Motion>", self._grip_drag)
        self.list_grip.bind("<ButtonRelease-1>", lambda e: self._clamp_sash())
        self.list_grip.bind("<Enter>", lambda e: self._paint_grip(hover=True))
        self.list_grip.bind("<Leave>", lambda e: self._paint_grip(hover=False))

        # 设置（左右两列：左列=提取与输出，右列=翻译与样式）
        opts_card = ttk.LabelFrame(top, text=" 设置 ", padding=(14, 10))
        opts_card.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self._opts_card = opts_card
        opts_card.columnconfigure(0, weight=1, uniform="opt")
        opts_card.columnconfigure(1, weight=1, uniform="opt")

        left = ttk.Frame(opts_card)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 18))
        right = ttk.Frame(opts_card)
        right.grid(row=0, column=1, sticky="nsew")

        # 标签与控件同行：先量出最长的标签宽度，两列统一对齐
        try:
            measure_font = tkfont.Font(family="Microsoft YaHei UI", size=10)
            label_w = max(measure_font.measure(t) for t in
                          ("字幕来源", "语音识别模型", "输出目录",
                           "翻译引擎", "离线模型", "方括号标注", "字幕样式"))
        except Exception:  # noqa: BLE001
            label_w = 110
        left.columnconfigure(0, minsize=label_w + 14)
        left.columnconfigure(1, weight=1)
        right.columnconfigure(0, minsize=label_w + 14)

        # ---- 左列：字幕来源 → 把字幕烧进视频 ----
        ttk.Label(left, text="字幕来源").grid(row=0, column=0, sticky="w", pady=4)
        self.source_var = tk.StringVar()
        self.source_box = ttk.Combobox(
            left, textvariable=self.source_var, state="readonly",
            values=list(SOURCE_LABELS.values()),
        )
        self.source_box.grid(row=0, column=1, sticky="ew", pady=4)

        ttk.Label(left, text="语音识别模型").grid(row=1, column=0, sticky="w", pady=4)
        self.model_var = tk.StringVar()
        self.model_box = ttk.Combobox(
            left, textvariable=self.model_var, state="readonly",
            values=["tiny（最快）", "base（较快）", "small（推荐）", "medium（更准更慢）"],
        )
        self.model_box.grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(left, text="输出目录").grid(row=2, column=0, sticky="w", pady=4)
        out_row = ttk.Frame(left)
        out_row.grid(row=2, column=1, sticky="ew", pady=4)
        out_row.columnconfigure(0, weight=1)
        self.output_var = tk.StringVar()
        ttk.Entry(out_row, textvariable=self.output_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="浏览…", command=self._pick_output).grid(
            row=0, column=1, padx=(8, 0)
        )
        self.output_hint = ttk.Label(left, text="留空表示与视频放在同一目录",
                                     style="Muted.TLabel")
        self.output_hint.grid(row=3, column=1, sticky="w", pady=(0, 6))

        flags = ttk.Frame(left)
        flags.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.keep_en_var = tk.BooleanVar()
        self.overwrite_var = tk.BooleanVar()
        self.translate_var = tk.BooleanVar()
        self.bilingual_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="额外保留英文 srt（文件名加 .en）",
                        variable=self.keep_en_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(flags, text="覆盖同名文件",
                        variable=self.overwrite_var).grid(row=0, column=1, sticky="w", padx=(24, 0))
        ttk.Checkbutton(flags, text="翻译成中文",
                        variable=self.translate_var).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Checkbutton(flags, text="生成中英对照字幕（中文在上）",
                        variable=self.bilingual_var).grid(row=1, column=1, sticky="w",
                                                          padx=(24, 0), pady=(6, 0))

        ttk.Label(flags, text="对照字幕格式").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.bi_format_var = tk.StringVar()
        self.bi_format_box = ttk.Combobox(
            flags, textvariable=self.bi_format_var, state="readonly",
            values=list(BILINGUAL_FORMATS.values()), width=30,
        )
        self.bi_format_box.grid(row=2, column=1, sticky="w", padx=(24, 0), pady=(8, 0))

        self.burn_var = tk.BooleanVar()
        ttk.Checkbutton(flags, text="把字幕烧进视频（硬字幕，输出 .hardsub.mkv，较慢）",
                        variable=self.burn_var).grid(
                            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        flags.columnconfigure(2, weight=1)

        # ---- 右列：翻译引擎 → 字幕样式 ----
        ttk.Label(right, text="翻译引擎").grid(row=0, column=0, sticky="w", pady=4)
        self.engine_var = tk.StringVar()
        self.engine_box = ttk.Combobox(
            right, textvariable=self.engine_var, state="readonly",
            values=list(ENGINE_LABELS.values()), width=32,
        )
        self.engine_box.grid(row=0, column=1, sticky="w", pady=4)
        self.engine_hint = ttk.Label(right, text="", style="Muted.TLabel")
        self.engine_hint.grid(row=0, column=2, sticky="w", padx=(8, 0), pady=4)
        self.engine_box.bind("<<ComboboxSelected>>", lambda e: self._update_engine_hint())

        ttk.Label(right, text="离线模型").grid(row=1, column=0, sticky="w", pady=4)
        self.local_model_var = tk.StringVar()
        self.local_model_box = ttk.Combobox(
            right, textvariable=self.local_model_var, state="readonly",
            values=list(LOCAL_MODEL_LABELS.values()), width=34,
        )
        self.local_model_box.grid(row=1, column=1, sticky="w", pady=4)
        self.local_model_hint = ttk.Label(right, text="", style="Muted.TLabel")
        self.local_model_hint.grid(row=1, column=2, sticky="w", padx=(8, 0), pady=4)
        self.local_model_box.bind("<<ComboboxSelected>>", lambda e: self._update_engine_hint())

        ttk.Label(right, text="方括号标注").grid(row=2, column=0, sticky="w", pady=4)
        self.bracket_var = tk.StringVar()
        self.bracket_box = ttk.Combobox(
            right, textvariable=self.bracket_var, state="readonly",
            values=list(BRACKET_MODES.values()), width=34,
        )
        self.bracket_box.grid(row=2, column=1, sticky="w", pady=4)
        ttk.Label(right, text="年份/编号/括号台词不算标注",
                  style="Muted.TLabel").grid(row=2, column=2, sticky="w", padx=(8, 0))

        # 字幕样式（独立对话框设置，只对 ASS 与硬字幕生效；SRT 只能塞颜色标签）
        ttk.Label(right, text="字幕样式").grid(row=3, column=0, sticky="w", pady=4)
        style_row = ttk.Frame(right)
        style_row.grid(row=3, column=1, columnspan=2, sticky="ew", pady=4)
        ttk.Button(style_row, text="调整样式…",
                   command=self._open_style_dialog).pack(side="left")
        self.style_summary = ttk.Label(style_row, text="", style="Muted.TLabel",
                                       wraplength=430, justify="left")
        self.style_summary.pack(side="left", padx=(10, 0))

        # 操作区
        actions = ttk.Frame(top)
        actions.grid(row=4, column=0, sticky="ew", pady=(10, 4))
        self._actions = actions
        self.start_btn = ttk.Button(actions, text="开始处理", style="Primary.TButton",
                                    command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="打开输出目录", command=self._open_output).pack(side="left")
        ttk.Button(actions, text="使用说明", command=self._show_help).pack(side="left", padx=8)
        ttk.Button(actions, text="术语表", command=self._open_glossary).pack(side="left")

        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(actions, textvariable=self.status_var, style="Muted.TLabel").pack(side="right")

        self.progress = ttk.Progressbar(top, mode="determinate", maximum=100)
        self.progress.grid(row=5, column=0, sticky="ew", pady=(0, 8))

        log_frame = ttk.LabelFrame(paned, text=" 运行日志 ", padding=(8, 6))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        paned.add(log_frame, weight=0)
        try:
            paned.paneconfigure(log_frame, minsize=110)
        except Exception:  # noqa: BLE001
            pass
        log_box = tk.Text(
            log_frame, height=6, wrap="word", bg="#ffffff", fg="#111111",
            font=("Consolas" if os.name == "nt" else "Menlo", 10),
            relief="sunken", bd=1, padx=8, pady=6,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_box.yview)
        log_box.configure(yscrollcommand=log_scroll.set)
        log_box.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")
        log_box.configure(state="disabled")
        self.log_box = log_box

        if self.dnd_enabled:
            try:
                self.root.drop_target_register("DND_Files")  # type: ignore[attr-defined]
                self.root.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                self.dnd_enabled = False

    # ---------------- 配置 ----------------
    def _apply_config(self) -> None:
        cfg = self.cfg
        self.source_var.set(SOURCE_LABELS.get(cfg.get("source", "auto"), SOURCE_LABELS["auto"]))
        model_map = {"tiny": "tiny（最快）", "base": "base（较快）",
                     "small": "small（推荐）", "medium": "medium（更准更慢）"}
        self.model_var.set(model_map.get(cfg.get("model", "small"), model_map["small"]))
        self.output_var.set(cfg.get("output_dir", ""))
        self.keep_en_var.set(bool(cfg.get("keep_english", True)))
        self.overwrite_var.set(bool(cfg.get("overwrite", True)))
        self.translate_var.set(bool(cfg.get("translate", True)))
        self.bilingual_var.set(bool(cfg.get("bilingual", True)))
        self.burn_var.set(bool(cfg.get("burn_in", False)))
        fmt = cfg.get("bilingual_format", "srt")
        self.bi_format_var.set(BILINGUAL_FORMATS.get(fmt, BILINGUAL_FORMATS["srt"]))
        engine = cfg.get("translate_engine", "auto")
        self.engine_var.set(ENGINE_LABELS.get(engine, ENGINE_LABELS["auto"]))
        tier = cfg.get("local_model", "fast")
        self.local_model_var.set(
            LOCAL_MODEL_LABELS.get(tier, LOCAL_MODEL_LABELS["fast"])
        )
        self._update_engine_hint()
        mode = cfg.get("bracket_mode", "drop")
        self.bracket_var.set(BRACKET_MODES.get(mode, BRACKET_MODES["drop"]))
        # 字幕样式
        self.font_var.set(cfg.get("ass_font", "Microsoft YaHei"))
        self.zh_size_var.set(str(cfg.get("ass_size_zh", 58)))
        self.en_size_var.set(str(cfg.get("ass_size_en", 42)))
        self.zh_color_var.set(cfg.get("ass_color_zh", "#FFFFFF"))
        self.en_color_var.set(cfg.get("ass_color_en", "#00D7FF"))
        self.outline_var.set(str(cfg.get("ass_outline", 2.2)))
        self.bold_var.set(bool(cfg.get("ass_bold", False)))
        self.position_var.set("顶部" if cfg.get("ass_position", "bottom") == "top" else "底部")
        self.srt_color_var.set(bool(cfg.get("srt_colored", False)))
        self._update_style_summary()

    def _current_engine(self) -> str:
        label = self.engine_var.get()
        for key, text in ENGINE_LABELS.items():
            if label == text:
                return key
        return "auto"

    def _current_local_model(self) -> str:
        label = self.local_model_var.get()
        for key, text in LOCAL_MODEL_LABELS.items():
            if label == text:
                return key
        return "fast"

    def _current_bracket_mode(self) -> str:
        label = self.bracket_var.get()
        for key, text in BRACKET_MODES.items():
            if label == text:
                return key
        return "drop"

    # ---------- 字幕样式 ----------
    def _style_from_ui(self) -> "SubtitleStyle":
        from .ass import BASE_EN_SIZE, BASE_ZH_SIZE
        from .ass import SubtitleStyle

        def _int(var, fallback: int) -> int:
            try:
                return int(float(var.get()))
            except (TypeError, ValueError):
                return fallback

        def _float(var, fallback: float) -> float:
            try:
                return float(var.get())
            except (TypeError, ValueError):
                return fallback

        return SubtitleStyle(
            font=(self.font_var.get() or "Microsoft YaHei").strip(),
            size_zh=_int(self.zh_size_var, BASE_ZH_SIZE),
            size_en=_int(self.en_size_var, BASE_EN_SIZE),
            color_zh=self.zh_color_var.get() or "#FFFFFF",
            color_en=self.en_color_var.get() or "#00D7FF",
            outline=_float(self.outline_var, 2.2),
            bold=bool(self.bold_var.get()),
            position="top" if self.position_var.get() == "顶部" else "bottom",
        )

    def _update_style_summary(self) -> None:
        if not hasattr(self, "style_summary"):
            return
        style = self._style_from_ui()
        pos = "顶部" if style.position == "top" else "底部"
        text = (f"{style.font} · 中文 {style.size_zh} / 英文 {style.size_en} · "
                f"{style.color_zh} / {style.color_en} · {pos}")
        if style.bold:
            text += " · 加粗"
        if self.srt_color_var.get():
            text += " · SRT 带颜色标签"
        self.style_summary.configure(text=text)

    def _open_style_dialog(self) -> None:
        if self._style_dialog is not None and self._style_dialog.winfo_exists():
            self._style_dialog.lift()
            self._style_dialog.focus_force()
            return
        self._style_dialog = StyleDialog(self)

    def _current_bilingual_format(self) -> str:
        label = self.bi_format_var.get()
        for key, text in BILINGUAL_FORMATS.items():
            if label == text:
                return key
        return "srt"


    def _update_engine_hint(self) -> None:
        from . import localmt

        engine = self._current_engine()
        tier = self._current_local_model()
        spec = localmt.MODELS.get(tier, localmt.MODELS["fast"])
        has = localmt.is_available(tier)
        if engine == "online":
            self.engine_hint.configure(text="需要联网")
            self.local_model_hint.configure(text="")
        elif not has:
            path = spec["path"]
            hint = (
                "模型没下完，请运行「下载7B模型.bat」续传"
                if tier == "quality" and path.is_file()
                else ("请运行「下载7B模型.bat」" if tier == "quality" else "请运行「安装依赖.bat」")
            )
            self.engine_hint.configure(
                text="模型未安装，请运行「安装依赖.bat」下载"
                if engine == "local" else "模型未安装，将直接使用在线接口"
            )
            self.local_model_hint.configure(text=hint if engine == "local" else "")
        else:
            self.engine_hint.configure(
                text="已就绪" if engine == "local" else "已就绪，失败自动用在线补译"
            )
            self.local_model_hint.configure(
                text="约 19 分钟/集" if tier == "quality" else ""
            )

    def _collect_config(self) -> dict:
        cfg = dict(self.cfg)
        cfg["source"] = SOURCE_KEYS.get(self.source_var.get(), "auto")
        model_text = self.model_var.get()
        cfg["model"] = model_text.split("（")[0] if model_text else "small"
        cfg["output_dir"] = self.output_var.get().strip()
        cfg["keep_english"] = bool(self.keep_en_var.get())
        cfg["overwrite"] = bool(self.overwrite_var.get())
        cfg["translate"] = bool(self.translate_var.get())
        cfg["bilingual"] = bool(self.bilingual_var.get())
        cfg["burn_in"] = bool(self.burn_var.get())
        fmt_label = self.bi_format_var.get()
        cfg["bilingual_format"] = "srt"
        for key, text in BILINGUAL_FORMATS.items():
            if fmt_label == text:
                cfg["bilingual_format"] = key
        cfg["translate_engine"] = self._current_engine()
        cfg["local_model"] = self._current_local_model()
        cfg["bracket_mode"] = self._current_bracket_mode()
        style = self._style_from_ui()
        cfg["ass_font"] = style.font
        cfg["ass_size_zh"] = style.size_zh
        cfg["ass_size_en"] = style.size_en
        cfg["ass_color_zh"] = style.color_zh
        cfg["ass_color_en"] = style.color_en
        cfg["ass_outline"] = style.outline
        cfg["ass_bold"] = style.bold
        cfg["ass_position"] = style.position
        cfg["srt_colored"] = bool(self.srt_color_var.get())
        # 窗口位置尺寸与分栏比例，退出时按当前状态记住
        try:
            cfg["win_geometry"] = self.root.geometry()
        except Exception:  # noqa: BLE001
            pass
        try:
            pane_h = self.paned.winfo_height()
            if pane_h > 0:
                cfg["sash_ratio"] = round(self.paned.sashpos(0) / pane_h, 3)
        except Exception:  # noqa: BLE001
            pass
        self.cfg = cfg
        return cfg

    def _on_close(self) -> None:
        try:
            save_config(self._collect_config())
        except Exception:  # noqa: BLE001
            pass
        try:
            self._log("程序正常关闭")
        except Exception:  # noqa: BLE001
            pass
        try:
            from . import localmt

            localmt.shutdown()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    # ---------------- 文件列表 ----------------
    def _remember_col_widths(self, _event=None) -> None:  # noqa: ANN001
        """列宽改动后写进配置，下次启动按记忆的宽度排布。"""
        if not hasattr(self, "tree"):
            return
        try:
            widths = {c: int(self.tree.column(c, "width"))
                      for c in ("name", "info", "source", "status")}
        except Exception:  # noqa: BLE001
            return
        if widths != self.cfg.get("col_widths"):
            self.cfg["col_widths"] = widths
            try:
                save_config(self._collect_config())
            except Exception:  # noqa: BLE001
                pass

    def _add_files(self) -> None:
        patterns = " ".join(f"*{s}" for s in sorted(VIDEO_SUFFIXES))
        paths = filedialog.askopenfilenames(
            title="选择视频文件",
            initialdir=self.cfg.get("last_dir") or str(Path.home()),
            filetypes=[("视频文件", patterns), ("所有文件", "*.*")],
        )
        if paths:
            self.cfg["last_dir"] = str(Path(paths[0]).parent)
            self._add_paths([Path(p) for p in paths])

    def _on_drop(self, event) -> None:  # noqa: ANN001
        try:
            raw = self.root.tk.splitlist(event.data)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            raw = [event.data]
        paths = []
        for item in raw:
            p = Path(str(item))
            if p.is_dir():
                paths.extend(sorted(q for q in p.iterdir() if q.suffix.lower() in VIDEO_SUFFIXES))
            elif p.suffix.lower() in VIDEO_SUFFIXES:
                paths.append(p)
        self._add_paths(paths)

    def _add_paths(self, paths: list[Path]) -> None:
        added = 0
        for p in paths:
            p = p.resolve()
            if not p.is_file() or p.suffix.lower() not in VIDEO_SUFFIXES:
                continue
            if any(p == q for q in self.files):
                continue
            self.files.append(p)
            iid = self.tree.insert("", "end", values=(p.name, "", "", "等待中"))
            self.row_ids[str(p)] = iid
            self.file_status[str(p)] = "等待中"
            added += 1
        if added:
            self._log(f"已添加 {added} 个视频文件")
        self.status_var.set(f"共 {len(self.files)} 个视频")

    def _remove_selected(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "请先停止当前任务")
            return
        for iid in self.tree.selection():
            values = self.tree.item(iid, "values")
            name = values[0] if values else ""
            for key, row in list(self.row_ids.items()):
                if row == iid:
                    self.files = [f for f in self.files if str(f) != key]
                    del self.row_ids[key]
                    self.file_status.pop(key, None)
                    break
            self.tree.delete(iid)
        self.status_var.set(f"共 {len(self.files)} 个视频")

    def _clear_files(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("提示", "请先停止当前任务")
            return
        self.tree.delete(*self.tree.get_children())
        self.files.clear()
        self.row_ids.clear()
        self.file_status.clear()
        self.results.clear()
        self.status_var.set("就绪")

    def _on_row_double_click(self, _event) -> None:  # noqa: ANN001
        iid = self.tree.focus()
        if not iid:
            return
        for key, row in self.row_ids.items():
            if row == iid:
                result = self.results.get(key)
                target = None
                if result:
                    for attr in ("srt_path", "bilingual_path", "video_path"):
                        cand = getattr(result, attr, None)
                        if cand and Path(cand).exists():
                            target = cand
                            break
                if target:
                    _open_in_explorer(Path(target).parent)
                else:
                    _open_in_explorer(Path(key).parent)
                return

    # ---------------- 处理 ----------------
    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo("提示", "请先添加至少一个视频文件")
            return
        if not ffmpeg_path():
            messagebox.showerror(
                "缺少 ffmpeg",
                "未找到 ffmpeg。请先运行程序目录下的「安装依赖.bat」，\n"
                "或下载 ffmpeg 放到 bin 目录后再试。",
            )
            return
        cfg = self._collect_config()
        save_config(cfg)
        if cfg.get("translate") and cfg.get("translate_engine") == "local":
            from . import localmt

            if not localmt.is_available(cfg.get("local_model", "fast")):
                tier = cfg.get("local_model", "fast")
                detail = (
                    "离线翻译需要 Hunyuan-MT-7B 模型（约 4.3GB）。\n\n"
                    "请运行程序目录下的「下载7B模型.bat」完成下载，\n"
                    "或把「离线模型」改回 Hy-MT2-1.8B。"
                    if tier == "quality" else
                    "离线翻译需要 llama.cpp 与 Hy-MT2 模型（约 1.1GB）。\n\n"
                    "请运行程序目录下的「安装依赖.bat」完成下载，\n"
                    "或把「翻译引擎」改回「在线接口」。"
                )
                messagebox.showerror("离线翻译模型未安装", detail)
                return
        out_dir = Path(cfg["output_dir"]) if cfg["output_dir"] else None
        if out_dir and not out_dir.exists():
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("输出目录无效", f"无法创建输出目录：{exc}")
                return

        self.cancel_evt.clear()
        self.results.clear()
        self.total_files = len(self.files)
        self.current_index = 0
        self.progress.configure(value=0)
        for key in self.row_ids:
            self._set_row_status(key, "等待中", tag="")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set("处理中…")

        opts = Options(
            source=cfg["source"],
            model=cfg["model"],
            keep_english=cfg["keep_english"],
            overwrite=cfg["overwrite"],
            output_dir=out_dir,
            translate=cfg["translate"],
            max_line_width=float(cfg.get("max_line_width", 42.0)),
            bilingual=cfg.get("bilingual", True),
            bilingual_format=cfg.get("bilingual_format", "srt"),
            translate_engine=cfg.get("translate_engine", "auto"),
            local_model=cfg.get("local_model", "fast"),
            burn_in=bool(cfg.get("burn_in", False)),
            burn_crf=int(cfg.get("burn_crf", 20) or 20),
            burn_preset=str(cfg.get("burn_preset", "medium") or "medium"),
            bracket_mode=cfg.get("bracket_mode", "drop"),
            ass_style=self._style_from_ui(),
            srt_colored=bool(cfg.get("srt_colored", False)),
        )
        self.worker = threading.Thread(target=self._run_batch, args=(list(self.files), opts),
                                       daemon=True)
        self.worker.start()

    def _stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.cancel_evt.set()
            self.status_var.set("正在停止…")
            self.stop_btn.configure(state="disabled")

    def _run_batch(self, files: list[Path], opts: Options) -> None:
        summary_ok, summary_fail = 0, 0
        for idx, video in enumerate(files, 1):
            if self.cancel_evt.is_set():
                self.queue.put(("log", "已停止，剩余文件未处理"))
                break
            self.queue.put(("row_status", str(video), "处理中", "run"))
            self.queue.put(("status", f"({idx}/{len(files)}) 正在处理 {video.name}"))

            def report(pct: float, message: str, _video=video, _idx=idx) -> None:
                overall = ((_idx - 1) / len(files) + pct / len(files)) * 100
                self.queue.put(("progress", overall, message))

            def log(message: str, _video=video) -> None:
                self.queue.put(("log", message))

            try:
                outcome = process_file(
                    video, opts, report=report, log=log, cancel=self.cancel_evt
                )
            except Cancelled:
                self.queue.put(("row_status", str(video), "已停止", ""))
                break
            except Exception as exc:  # noqa: BLE001
                outcome = None
                self.queue.put(("log", f"{video.name}：处理失败 {exc}"))
                self.queue.put(("log", traceback.format_exc(limit=2).strip()))
                self.queue.put(("row_status", str(video), "失败", "fail"))
                summary_fail += 1
                continue

            self.results[str(video)] = outcome
            if outcome.ok:
                summary_ok += 1
                source = outcome.source_label or ""
                self.queue.put(("row_info", str(video), source))
                text = f"完成 · {outcome.cue_count} 条"
                if outcome.video_path:
                    text = f"完成 · {outcome.cue_count} 条 + 硬字幕视频"
                if outcome.warnings:
                    text = "完成（有提示）"
                self.queue.put(("row_status", str(video), text, "ok"))
            else:
                summary_fail += 1
                self.queue.put(("row_status", str(video), f"失败：{outcome.message}", "fail"))

        self.queue.put(("done", summary_ok, summary_fail))

    # ---------------- 队列轮询 ----------------
    def _poll_queue(self) -> None:
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._log(item[1])
                elif kind == "status":
                    self.status_var.set(item[1])
                elif kind == "progress":
                    self.progress.configure(value=item[1])
                    if item[2]:
                        self.status_var.set(item[2])
                elif kind == "row_status":
                    self._set_row_status(item[1], item[2], item[3] if len(item) > 3 else "")
                elif kind == "row_info":
                    iid = self.row_ids.get(item[1])
                    if iid:
                        values = list(self.tree.item(iid, "values"))
                        values[1] = item[2]
                        self.tree.item(iid, values=values)
                elif kind == "done":
                    self._finish(item[1], item[2])
        except queue.Empty:
            pass
        self.root.after(120, self._poll_queue)

    def _finish(self, ok: int, fail: int) -> None:
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if self.cancel_evt.is_set():
            self.status_var.set(f"已停止（成功 {ok}，失败 {fail}）")
            self._log(f"已停止：成功 {ok} 个，失败 {fail} 个")
        else:
            self.progress.configure(value=100)
            self.status_var.set(f"全部完成：成功 {ok}，失败 {fail}")
            self._log(f"处理结束：成功 {ok} 个，失败 {fail} 个")
        self.worker = None

    def _set_row_status(self, key: str, status: str, tag: str = "") -> None:
        iid = self.row_ids.get(key)
        if not iid:
            return
        values = list(self.tree.item(iid, "values"))
        values[3] = status
        self.tree.item(iid, values=values, tags=(tag,) if tag else ())

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{stamp}] {message}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / "app.log", "a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass

    # ---------------- 其它 ----------------
    def _pick_output(self) -> None:
        directory = filedialog.askdirectory(title="选择字幕输出目录")
        if directory:
            self.output_var.set(directory)

    def _open_output(self) -> None:
        for result in self.results.values():
            path = getattr(result, "srt_path", None)
            if path and Path(path).exists():
                _open_in_explorer(Path(path).parent)
                return
        out_dir = self.output_var.get().strip()
        if out_dir and Path(out_dir).exists():
            _open_in_explorer(Path(out_dir))
            return
        if self.files:
            _open_in_explorer(self.files[0].parent)
            return
        messagebox.showinfo("提示", "还没有生成字幕文件")

    def _open_glossary(self) -> None:
        from .localmt import load_terms
        from .paths import GLOSSARY_FILE

        try:
            load_terms()  # 首次运行会把示例写进文件
            os.startfile(str(GLOSSARY_FILE))
            self._log(
                f"已打开术语表：{GLOSSARY_FILE}（每行「英文=中文」，保存后下一集生效）"
            )
            self._log("提示：术语表改动不会自动让旧译文失效，需要重译时请删除 data\\translation_cache.json")
        except OSError as exc:
            messagebox.showerror("打不开术语表", f"{exc}")

    def _show_help(self) -> None:
        messagebox.showinfo(
            "使用说明",
            "1. 点击「添加视频…」选择一个或多个视频（也可直接拖入窗口）。\n"
            "2. 选择字幕来源：默认「自动」会先尝试视频内嵌的英文字幕轨，"
            "没有内嵌字幕时用 Whisper 做语音识别。\n"
            "3. 点击「开始处理」，程序会提取英文字幕、翻译成中文，"
            "并在视频旁生成同名 .srt 字幕文件。\n\n"
            "小提示：\n"
            "· 「翻译引擎」默认「自动」：先用本机离线模型翻译，个别句子没译出时\n"
            "  自动改用在线接口补译；断网时也能全程离线（速度较慢）。\n"
            "· 「离线模型」两档可选：Hy-MT2-1.8B 快（约 6 分钟/集）；\n"
            "  Hunyuan-MT-7B 质量更好但慢 3~4 倍（约 20~25 分钟/集），\n"
            "  首次使用需运行程序目录下的「下载7B模型.bat」下载 4.3GB。\n"
            "· 勾选「生成中英对照字幕」会额外输出 .bilingual.srt，中文在上、英文在下，\n"
            "  用硬换行分隔，播放器里固定分两行显示。\n"
            "· 字幕默认只输出中文，勾选「额外保留英文 srt」可同时保存纯英文版（.en.srt）。\n"
            "· 右上「术语表」按钮会打开 data\\glossary.txt，按「英文=中文」一行一条，\n"
            "  姓名不想被意译就写 Chris=克里斯；改完下一集生效。\n"
            "· 勾选「把字幕烧进视频」会再输出一个 xxx.hardsub.mkv：字幕直接画进画面，\n"
            "  任何播放器都不会压行、不会丢样式；代价是要重新编码，耗时约等于片长。\n"
            "· 翻译使用免费在线接口，长视频需要几分钟，失败的字幕会保留英文原文。\n"
            "· Whisper 模型首次使用需要下载（small 约 500MB），之后离线可用。",
        )

    def _self_check(self, engine: str, tier: str) -> None:
        if not ffmpeg_path():
            self.queue.put(("log", "⚠ 未检测到 ffmpeg，请运行「安装依赖.bat」"))
            return
        try:
            import ctranslate2  # noqa: F401
            from faster_whisper import WhisperModel  # noqa: F401
            self.queue.put(("log", "语音识别组件就绪（faster-whisper）"))
        except ImportError:
            self.queue.put(("log", "⚠ 未安装 faster-whisper，语音识别不可用，请运行「安装依赖.bat」"))
        ok, msg = check_translation(engine=engine, local_model=tier)
        self.queue.put(("log", ("翻译通道：" if engine == "online" else "离线翻译：") + msg
                       if ok else ("⚠ " + msg)))


STYLE_DEFAULTS = {
    "font": "Microsoft YaHei",
    "size_zh": "58",
    "size_en": "42",
    "color_zh": "#FFFFFF",
    "color_en": "#00D7FF",
    "outline": "2.2",
    "bold": False,
    "position": "底部",
    "srt_colored": False,
}


class StyleDialog(tk.Toplevel):
    """独立窗口里调整 ASS / 硬字幕样式，确认后才写回主窗口。"""

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("字幕样式")
        self.transient(app.root)
        self.resizable(True, True)
        self.minsize(880, 460)
        self.configure(bg="#f0f0f0")
        self.__initial = self._snapshot()
        self._build()
        self._trace_vars()
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._render()          # 先把说明文字撑开，量尺寸时才不会少算两行
        self.update_idletasks()
        w, h = self._pick_geometry()
        self.geometry(f"{w}x{h}")
        self._center(w, h)
        self.after(80, self._render)
        self.grab_set()
        self.focus_force()

    # ---------------- 状态 ----------------
    def _snapshot(self) -> dict:
        app = self.app
        return {
            "font": app.font_var.get(),
            "size_zh": app.zh_size_var.get(),
            "size_en": app.en_size_var.get(),
            "color_zh": app.zh_color_var.get(),
            "color_en": app.en_color_var.get(),
            "outline": app.outline_var.get(),
            "bold": bool(app.bold_var.get()),
            "position": app.position_var.get(),
            "srt_colored": bool(app.srt_color_var.get()),
        }

    def _restore(self, state: dict) -> None:
        app = self.app
        app.font_var.set(state["font"])
        app.zh_size_var.set(state["size_zh"])
        app.en_size_var.set(state["size_en"])
        app.zh_color_var.set(state["color_zh"])
        app.en_color_var.set(state["color_en"])
        app.outline_var.set(state["outline"])
        app.bold_var.set(state["bold"])
        app.position_var.set(state["position"])
        app.srt_color_var.set(state["srt_colored"])
        app._update_style_summary()

    # ---------------- 界面 ----------------
    def _build(self) -> None:
        app = self.app
        card = ttk.LabelFrame(self, text=" 字体与颜色 ", padding=(14, 10))
        card.pack(fill="x", padx=14, pady=(12, 0))

        row1 = ttk.Frame(card)
        row1.pack(fill="x")
        ttk.Label(row1, text="字体").pack(side="left")
        self.font_box = ttk.Combobox(row1, textvariable=app.font_var,
                                     values=app.font_families, width=24)
        self.font_box.pack(side="left", padx=(6, 0))
        self.bold_chk = ttk.Checkbutton(row1, text="加粗", variable=app.bold_var)
        self.bold_chk.pack(side="left", padx=(14, 0))
        ttk.Label(row1, text="位置", style="Muted.TLabel").pack(side="left", padx=(18, 4))
        self.position_box = ttk.Combobox(row1, textvariable=app.position_var,
                                         state="readonly", values=["底部", "顶部"], width=5)
        self.position_box.pack(side="left")

        row2 = ttk.Frame(card)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="中文字号").pack(side="left")
        ttk.Spinbox(row2, from_=16, to=120, increment=2, width=5,
                    textvariable=app.zh_size_var).pack(side="left", padx=(6, 0))
        ttk.Label(row2, text="英文字号").pack(side="left", padx=(18, 6))
        ttk.Spinbox(row2, from_=12, to=100, increment=2, width=5,
                    textvariable=app.en_size_var).pack(side="left")
        ttk.Label(row2, text="描边").pack(side="left", padx=(18, 6))
        ttk.Spinbox(row2, from_=0.5, to=5.0, increment=0.5, width=4,
                    textvariable=app.outline_var).pack(side="left")
        ttk.Label(row2, text="（1080p 基准，按视频分辨率自动缩放）",
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        row3 = ttk.Frame(card)
        row3.pack(fill="x", pady=(10, 0))
        ttk.Label(row3, text="字幕颜色").pack(side="left")
        self.zh_btn = ttk.Button(row3, text="中文", width=20,
                                 command=lambda: self._pick_color("zh"))
        self.zh_btn.pack(side="left", padx=(6, 0))
        self.en_btn = ttk.Button(row3, text="英文", width=20,
                                 command=lambda: self._pick_color("en"))
        self.en_btn.pack(side="left", padx=(8, 0))
        self.color_chip = tk.Canvas(row3, width=64, height=18, highlightthickness=1,
                                    highlightbackground="#b8b8b8")
        self.color_chip.pack(side="left", padx=(10, 0))

        preview_card = ttk.LabelFrame(self, text=" 预览（按 1280 宽折算，深色背景） ",
                                     padding=(10, 8))
        self.preview = tk.Canvas(preview_card, height=240, highlightthickness=0,
                                 bg="#141414")

        self.note = ttk.Label(self, text="", style="Muted.TLabel", wraplength=900,
                              justify="left")
        self.srt_chk = ttk.Checkbutton(
            self, text="SRT 也写颜色标签（<font color>，只有部分播放器认）",
            variable=app.srt_color_var)
        btns = ttk.Frame(self)
        ttk.Button(btns, text="恢复默认", command=self._reset).pack(side="left")
        ttk.Button(btns, text="取消", command=self._cancel).pack(side="right")
        ttk.Button(btns, text="确定", style="Primary.TButton",
                   command=self._ok).pack(side="right", padx=(0, 8))

        # 底部这一组先占位（side=bottom），预览区再撑满中间，窗口再小也不会把按钮挤掉
        btns.pack(side="bottom", fill="x", padx=14, pady=(10, 12))
        self.srt_chk.pack(side="bottom", anchor="w", padx=16, pady=(6, 0))
        self.note.pack(side="bottom", fill="x", padx=16, pady=(8, 0))
        preview_card.pack(side="top", fill="both", expand=True, padx=14, pady=(10, 0))
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", lambda e: self._render())

    def _trace_vars(self) -> None:
        app = self.app
        self._trace_ids: list[tuple[tk.Variable, str]] = []
        for var in (app.font_var, app.zh_size_var, app.en_size_var,
                    app.zh_color_var, app.en_color_var, app.outline_var,
                    app.position_var, app.bold_var, app.srt_color_var):
            self._trace_ids.append((var, var.trace_add("write", self._on_var_change)))

    def _on_var_change(self, *_args) -> None:
        if self.winfo_exists():
            self._render()

    def _pick_geometry(self) -> tuple[int, int]:
        """优先用上次记住的窗口尺寸，并且不超过屏幕可用区域。"""
        req_w = max(900, self.winfo_reqwidth())
        req_h = self.winfo_reqheight()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        max_w, max_h = sw - 60, sh - 110
        w, h = req_w, req_h
        saved = str(self.app.cfg.get("style_dlg_geometry") or "").strip()
        m = re.match(r"(\d+)x(\d+)", saved)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        w = min(max(w, req_w), max_w)
        h = min(max(h, req_h), max_h)
        # 高度不够时压缩预览画布，保证底部按钮始终可见
        self.update_idletasks()
        over = self.winfo_reqheight() - h
        if over > 0:
            self.preview.configure(height=max(110, 240 - over))
            self.update_idletasks()
            w = min(max(w, self.winfo_reqwidth()), max_w)
            h = min(self.winfo_reqheight(), max_h)
        return w, h

    def _center(self, w: int | None = None, h: int | None = None) -> None:
        self.update_idletasks()
        w = w or self.winfo_width()
        h = h or self.winfo_height()
        root = self.app.root
        pw, ph = root.winfo_width(), root.winfo_height()
        px, py = root.winfo_rootx(), root.winfo_rooty()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = min(max(0, px + (pw - w) // 2), max(0, sw - w - 10))
        y = min(max(0, py + (ph - h) // 3), max(0, sh - h - 50))
        saved = str(self.app.cfg.get("style_dlg_geometry") or "").strip()
        m = re.match(r"\d+x\d+([+-]\d+)([+-]\d+)", saved)
        if m:
            sx, sy = int(m.group(1)), int(m.group(2))
            if 0 <= sx <= sw - w and 0 <= sy <= sh - h:
                x, y = sx, sy
        self.geometry(f"+{x}+{y}")

    # ---------------- 交互 ----------------
    def _pick_color(self, which: str) -> None:
        app = self.app
        var = app.zh_color_var if which == "zh" else app.en_color_var
        initial = var.get() or ("#FFFFFF" if which == "zh" else "#00D7FF")
        chosen = colorchooser.askcolor(color=initial, title="选择字幕颜色",
                                       parent=self)[1]
        if chosen:
            var.set(chosen.upper())

    def _reset(self) -> None:
        self._restore(dict(STYLE_DEFAULTS))

    def _render(self) -> None:
        if not self.winfo_exists():
            return
        app = self.app
        style = app._style_from_ui()
        canvas = self.preview
        canvas.delete("all")
        w = canvas.winfo_width()
        h = canvas.winfo_height()
        if w < 20:
            w = 976
        if h < 20:
            h = 240
        canvas.create_rectangle(0, 0, w, h, fill="#141414", outline="")
        if w < 20 or h < 20:
            return
        # 预览按 1280 的视频宽度折算字号（真实输出按视频高度等比缩放）
        scale = w / 1280
        zh_px = max(10, round(style.size_zh * scale))
        en_px = max(9, round(style.size_en * scale))
        weight = "bold" if style.bold else "normal"
        gap = max(6, round(min(zh_px, en_px) * 0.35))          # 中英两行之间的空隙
        outline_px = max(0, round(float(style.outline or 0) * scale))
        if style.position == "top":
            zh_y = max(4 + zh_px / 2, round(h * 0.07) + zh_px / 2)
            en_y = zh_y + zh_px / 2 + gap + en_px / 2
        else:
            bottom_margin = max(10, round(h * 0.07))
            en_y = h - bottom_margin - en_px / 2
            zh_y = en_y - en_px / 2 - gap - zh_px / 2

        for text, y, px, color in (
            ("这才是中文字幕的效果", zh_y, zh_px, style.color_zh),
            ("So does the English line.", en_y, en_px, style.color_en),
        ):
            if outline_px:
                # 用四个偏移的黑色副本模拟 ASS 描边
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1),
                               (-1, -1), (1, 1), (-1, 1), (1, -1)):
                    canvas.create_text(
                        w / 2 + dx * outline_px, y + dy * outline_px, text=text,
                        fill="#000000", font=(style.font, -px, weight),
                    )
            canvas.create_text(
                w / 2, y, text=text, fill=color, font=(style.font, -px, weight),
            )
        # 颜色按钮上的色块与文字
        self.zh_btn.configure(text=f"中文  {style.color_zh}")
        self.en_btn.configure(text=f"英文  {style.color_en}")
        self.color_chip.delete("all")
        self.color_chip.create_rectangle(0, 0, 32, 18, fill=style.color_zh, outline="")
        self.color_chip.create_rectangle(32, 0, 64, 18, fill=style.color_en, outline="")

        fmt = app._current_bilingual_format()
        burn = bool(app.burn_var.get())
        if burn or fmt in ("ass", "both"):
            note = ("ASS 字幕和烧进视频的硬字幕会按这里的字体、字号、颜色输出；"
                    "播放器需用 libass 渲染 ASS 才会显示这些样式。")
        elif fmt == "srt":
            note = ("当前「对照字幕格式」是 SRT：SRT 本身不支持字体和字号，"
                    "要完整样式请改成 ASS 或勾选「把字幕烧进视频」。")
        else:
            note = "当前不会输出 ASS 或硬字幕，这些样式暂时用不上。"
        if app.srt_color_var.get():
            note += " SRT 里会写 <font color> 颜色标签。"
        self.note.configure(text=note)

    def _ok(self) -> None:
        self.app._update_style_summary()
        try:
            save_config(self.app._collect_config())
        except Exception:  # noqa: BLE001
            pass
        self._close()

    def _cancel(self) -> None:
        self._restore(self.__initial)
        self._close()

    def _close(self) -> None:
        # 记住弹窗自身的尺寸与位置（样式各项已写回主窗口变量）
        try:
            self.app.cfg["style_dlg_geometry"] = self.geometry()
            save_config(self.app._collect_config())
        except Exception:  # noqa: BLE001
            pass
        for var, tid in getattr(self, "_trace_ids", []):
            try:
                var.trace_remove("write", tid)
            except Exception:  # noqa: BLE001
                pass
        self._trace_ids = []
        try:
            self.grab_release()
        except Exception:  # noqa: BLE001
            pass
        if self.app._style_dialog is self:
            self.app._style_dialog = None
        self.destroy()


def run() -> None:
    _enable_dpi_awareness()
    dnd_enabled = False
    try:
        from tkinterdnd2 import TkinterDnD  # type: ignore

        root = TkinterDnD.Tk()
        dnd_enabled = True
    except ImportError:
        root = tk.Tk()
    app = App(root, dnd_enabled=dnd_enabled)
    app._log("程序已启动，选择视频后点击「开始处理」")
    root.mainloop()


if __name__ == "__main__":
    run()
