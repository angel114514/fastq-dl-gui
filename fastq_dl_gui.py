# -*- coding: utf-8 -*-
"""
fastq-dl GUI —— Tkinter 图形界面

运行：python fastq_dl_gui.py
依赖：pip install requests
可选：安装 aria2 后可启用 aria2c 多连接下载（见 README）
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import shutil
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from fastqdl_core import (
    DownloadManager,
    DownloadOptions,
    Md5Verifier,
    find_aria2c,
    human_size,
    human_speed,
    parse_accessions,
    read_md5_manifest,
    rows_for_tsv,
    run_layout,
    run_size,
    safe_int,
    select_fastq_files,
    validate_accession,
    write_md5_manifest,
    write_tsv,
)

try:
    import requests
except Exception:
    requests = None

APP_NAME = "fastq-dl GUI"
APP_VERSION = "1.3.0"
ENA_BROWSER_URL = "https://www.ebi.ac.uk/ena/browser/view/{accession}"

APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
CONFIG_PATH = APP_DIR / "fastq_dl_gui.json"

BG = "#f3f4f6"
CARD = "#ffffff"
BORDER = "#d1d5db"
TEXT = "#111827"
MUTED = "#6b7280"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
HEADER_BG = "#0f172a"

PROVIDERS = [("ENA（推荐，无需额外工具）", "ena"), ("SRA（需要 sracha）", "sra")]
PROTOCOLS = [("HTTPS（推荐，可断点续传）", "https"), ("FTP（ENA 原生）", "ftp")]
ENGINES = [
    ("自动（有 aria2c 时优先使用）", "auto"),
    ("内置 requests（单连接）", "builtin"),
    ("aria2c 多连接（需安装）", "aria2"),
]
GROUPS = [
    ("不合并（每个 Run 单独保存）", "none"),
    ("按 Experiment 合并", "experiment"),
    ("按 Sample 合并", "sample"),
]
PROVIDER_VALUE = {label: value for label, value in PROVIDERS}
PROTOCOL_VALUE = {label: value for label, value in PROTOCOLS}
ENGINE_VALUE = {label: value for label, value in ENGINES}
GROUP_VALUE = {label: value for label, value in GROUPS}
LABEL_BY_PROVIDER = {value: label for label, value in PROVIDERS}
LABEL_BY_PROTOCOL = {value: label for label, value in PROTOCOLS}
LABEL_BY_ENGINE = {value: label for label, value in ENGINES}
LABEL_BY_GROUP = {value: label for label, value in GROUPS}


def pick_font(root) -> str:
    try:
        families = set(tkfont.families(root))
    except Exception:
        families = set()
    for name in (
        "Microsoft YaHei UI",
        "Microsoft YaHei",
        "PingFang SC",
        "Noto Sans CJK SC",
        "WenQuanYi Micro Hei",
        "Segoe UI",
    ):
        if name in families:
            return name
    return "TkDefaultFont"


def fmt_int(value) -> str:
    number = safe_int(value, -1)
    return f"{number:,}" if number >= 0 else "-"


class ScrollableFrame(ttk.Frame):
    """左侧设置区：内容过高时可滚动。"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(
            self, borderwidth=0, highlightthickness=0, background=BG
        )
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.content = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(
            (0, 0), window=self.content, anchor="nw"
        )
        self.content.bind("<Configure>", self._on_content)
        self.canvas.bind("<Configure>", self._on_canvas)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _on_content(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class FastqDlApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.manager: DownloadManager | None = None
        self.verifier: Md5Verifier | None = None
        self.rows: list = []
        self.task_rows: dict = {}
        self.session_mode = "download"

        self.cfg = self._read_config()
        self.font_family = pick_font(root)
        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._apply_config()
        self._update_accession_hint()
        self._update_sra_tool_label()
        self._update_engine_label()
        self.root.after(120, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # 窗口 / 样式
    # ------------------------------------------------------------------ #
    def _setup_window(self):
        self.root.title(f"{APP_NAME} v{APP_VERSION} — ENA / SRA FASTQ 下载器")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(1320, max(1080, screen_w - 120))
        height = min(920, max(700, screen_h - 100))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - 20)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(1060, 660)
        self.root.configure(background=BG)

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        font = (self.font_family, 10)
        style.configure(".", font=font, background=BG, foreground=TEXT)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabelframe", background=CARD, bordercolor=BORDER, relief="solid")
        style.configure(
            "TLabelframe.Label",
            background=CARD,
            foreground=TEXT,
            font=(self.font_family, 10, "bold"),
        )
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Hint.TLabel", background=CARD, foreground=MUTED)
        style.configure("OkHint.TLabel", background=CARD, foreground="#15803d")
        style.configure("WarnHint.TLabel", background=CARD, foreground="#b45309")
        style.configure("ErrorHint.TLabel", background=CARD, foreground="#b91c1c")
        style.configure("Status.TLabel", background=BG, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("TCheckbutton", background=CARD, foreground=TEXT)
        style.map("TCheckbutton", background=[("active", CARD)])
        style.configure(
            "TButton",
            background="#e5e7eb",
            foreground=TEXT,
            bordercolor=BORDER,
            padding=(10, 6),
            relief="flat",
        )
        style.map("TButton", background=[("active", "#d1d5db"), ("disabled", "#f3f4f6")])
        style.configure(
            "Accent.TButton",
            background=ACCENT,
            foreground="white",
            bordercolor=ACCENT,
            padding=(12, 7),
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#9db8d6")],
            foreground=[("disabled", "#eef2ff")],
        )
        style.configure(
            "TEntry",
            fieldbackground="white",
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            padding=4,
        )
        style.configure(
            "TCombobox",
            fieldbackground="white",
            background="white",
            bordercolor=BORDER,
            arrowcolor=TEXT,
            padding=4,
        )
        style.configure("TSpinbox", fieldbackground="white", bordercolor=BORDER, padding=4)
        style.configure(
            "TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0)
        )
        style.configure("TNotebook.Tab", padding=(16, 7), background="#e5e7eb")
        style.map(
            "TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", ACCENT)],
        )
        style.configure(
            "Treeview",
            background="white",
            fieldbackground="white",
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=27,
        )
        style.configure(
            "Treeview.Heading",
            background="#f9fafb",
            foreground=TEXT,
            font=(self.font_family, 9, "bold"),
            padding=(6, 6),
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#e5e7eb",
            background=ACCENT,
            bordercolor=BG,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=10,
        )

    # ------------------------------------------------------------------ #
    # 界面
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        header = tk.Frame(self.root, background=HEADER_BG, height=64)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="fastq-dl GUI",
            background=HEADER_BG,
            foreground="white",
            font=(self.font_family, 17, "bold"),
        ).pack(side="left", padx=(20, 10), pady=14)
        tk.Label(
            header,
            text="从 ENA / SRA 下载 FASTQ · 参考 rpetit3/fastq-dl",
            background=HEADER_BG,
            foreground="#cbd5e1",
            font=(self.font_family, 10),
        ).pack(side="left", pady=18)
        tk.Label(
            header,
            text=f"v{APP_VERSION}",
            background=HEADER_BG,
            foreground="#94a3b8",
            font=(self.font_family, 9),
        ).pack(side="right", padx=20)

        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=12, pady=(10, 6))
        body.columnconfigure(0, weight=0, minsize=392)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self.left_wrap = ttk.Frame(body)
        self.left_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.left_wrap.columnconfigure(0, weight=1)
        self.left_wrap.rowconfigure(0, weight=1)

        self.left = ScrollableFrame(self.left_wrap)
        self.left.grid(row=0, column=0, sticky="nsew")

        self.actions = ttk.Frame(self.left_wrap)
        self.actions.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self._build_left_panel()
        self._build_right_panel(right)
        self._build_footer()

    def _section(self, title: str) -> ttk.LabelFrame:
        box = ttk.LabelFrame(self.left.content, text=title, padding=(12, 8, 12, 10))
        box.pack(fill="x", padx=2, pady=(0, 10))
        box.columnconfigure(0, weight=1)
        return box

    def _build_left_panel(self):
        # 1. accession 输入
        box = self._section(" 1. 输入 Accession ")
        input_frame = ttk.Frame(box)
        input_frame.grid(row=0, column=0, sticky="ew")
        input_frame.columnconfigure(0, weight=1)

        self.acc_text = tk.Text(
            input_frame,
            height=4,
            wrap="word",
            font=(self.font_family, 10),
            background="white",
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            padx=6,
            pady=4,
        )
        acc_scroll = ttk.Scrollbar(input_frame, orient="vertical", command=self.acc_text.yview)
        self.acc_text.configure(yscrollcommand=acc_scroll.set)
        self.acc_text.grid(row=0, column=0, sticky="ew")
        acc_scroll.grid(row=0, column=1, sticky="ns")
        self.acc_text.bind("<<Modified>>", self._on_acc_modified)

        ttk.Label(
            box,
            text="一行一个，也支持逗号 / 空格分隔。支持 BioProject、Study、BioSample、Sample、Experiment、Run。",
            style="Hint.TLabel",
            wraplength=330,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(6, 2))
        self.acc_hint = ttk.Label(box, text="", style="Hint.TLabel", wraplength=330, justify="left")
        self.acc_hint.grid(row=2, column=0, sticky="w")

        sample_row = ttk.Frame(box)
        sample_row.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(
            sample_row, text="填入示例 SRR390728", command=self._fill_sample
        ).pack(side="left")

        # 2. 下载设置
        box = self._section(" 2. 下载设置 ")
        grid = ttk.Frame(box)
        grid.grid(row=0, column=0, sticky="ew")
        grid.columnconfigure(1, weight=1)

        ttk.Label(grid, text="保存目录", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 6), padx=(0, 8)
        )
        outdir_row = ttk.Frame(grid)
        outdir_row.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        outdir_row.columnconfigure(0, weight=1)
        self.var_outdir = tk.StringVar()
        ttk.Entry(outdir_row, textvariable=self.var_outdir).grid(row=0, column=0, sticky="ew")
        ttk.Button(outdir_row, text="浏览…", width=7, command=self._browse_outdir).grid(
            row=0, column=1, padx=(6, 0)
        )

        ttk.Label(grid, text="文件前缀", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", pady=4, padx=(0, 8)
        )
        self.var_prefix = tk.StringVar(value="fastq")
        ttk.Entry(grid, textvariable=self.var_prefix).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(grid, text="数据来源", style="Card.TLabel").grid(
            row=2, column=0, sticky="w", pady=4, padx=(0, 8)
        )
        self.var_provider = tk.StringVar()
        self.cmb_provider = ttk.Combobox(
            grid,
            textvariable=self.var_provider,
            values=[x[0] for x in PROVIDERS],
            state="readonly",
        )
        self.cmb_provider.grid(row=2, column=1, sticky="ew", pady=4)
        self.cmb_provider.bind("<<ComboboxSelected>>", lambda _e: self._update_sra_tool_label())

        ttk.Label(grid, text="ENA 协议", style="Card.TLabel").grid(
            row=3, column=0, sticky="w", pady=4, padx=(0, 8)
        )
        self.var_protocol = tk.StringVar()
        ttk.Combobox(
            grid,
            textvariable=self.var_protocol,
            values=[x[0] for x in PROTOCOLS],
            state="readonly",
        ).grid(row=3, column=1, sticky="ew", pady=4)

        ttk.Label(grid, text="下载引擎", style="Card.TLabel").grid(
            row=4, column=0, sticky="w", pady=4, padx=(0, 8)
        )
        self.var_engine = tk.StringVar()
        self.cmb_engine = ttk.Combobox(
            grid,
            textvariable=self.var_engine,
            values=[x[0] for x in ENGINES],
            state="readonly",
        )
        self.cmb_engine.grid(row=4, column=1, sticky="ew", pady=4)
        self.cmb_engine.bind("<<ComboboxSelected>>", lambda _e: self._update_engine_label())

        ttk.Label(grid, text="单文件连接数", style="Card.TLabel").grid(
            row=5, column=0, sticky="w", pady=4, padx=(0, 8)
        )
        conn_row = ttk.Frame(grid)
        conn_row.grid(row=5, column=1, sticky="ew", pady=4)
        self.var_connections = tk.StringVar(value="8")
        self.spin_connections = ttk.Spinbox(
            conn_row, from_=1, to=16, width=4, textvariable=self.var_connections
        )
        self.spin_connections.grid(row=0, column=0, sticky="w")
        ttk.Label(
            conn_row, text="仅 aria2c 引擎生效（1–16）", style="Hint.TLabel"
        ).grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.engine_label = ttk.Label(
            grid, text="", style="Hint.TLabel", wraplength=330, justify="left"
        )
        self.engine_label.grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 2))

        nums = ttk.Frame(grid)
        nums.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for col in (1, 3, 5):
            nums.columnconfigure(col, weight=1)
        self.var_workers = tk.StringVar(value="3")
        self.var_attempts = tk.StringVar(value="3")
        self.var_sleep = tk.StringVar(value="5")
        ttk.Label(nums, text="并发文件", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(nums, from_=1, to=8, width=4, textvariable=self.var_workers).grid(
            row=0, column=1, sticky="w", padx=(4, 12)
        )
        ttk.Label(nums, text="重试次数", style="Card.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(nums, from_=1, to=10, width=4, textvariable=self.var_attempts).grid(
            row=0, column=3, sticky="w", padx=(4, 12)
        )
        ttk.Label(nums, text="重试间隔(秒)", style="Card.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(nums, from_=0, to=120, width=4, textvariable=self.var_sleep).grid(
            row=0, column=5, sticky="w", padx=(4, 0)
        )

        # 3. 选项
        box = self._section(" 3. 下载选项 ")
        opt = ttk.Frame(box)
        opt.grid(row=0, column=0, sticky="ew")
        opt.columnconfigure(0, weight=1)

        ttk.Label(opt, text="Run 合并方式", style="Card.TLabel").grid(row=0, column=0, sticky="w")
        self.var_group = tk.StringVar()
        ttk.Combobox(
            opt,
            textvariable=self.var_group,
            values=[x[0] for x in GROUPS],
            state="readonly",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 4))
        ttk.Label(
            opt,
            text="合并后源文件会被删除，输出 <accession>_R1/_R2.fastq.gz。",
            style="Hint.TLabel",
            wraplength=330,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(0, 6))

        self.var_metadata_only = tk.BooleanVar(value=False)
        self.var_force = tk.BooleanVar(value=False)
        self.var_ignore_md5 = tk.BooleanVar(value=False)
        self.var_only_provider = tk.BooleanVar(value=False)
        for text, var in (
            ("仅获取元数据（不下载 FASTQ）", self.var_metadata_only),
            ("覆盖已存在的文件", self.var_force),
            ("跳过 MD5 校验", self.var_ignore_md5),
            ("仅使用选定来源，不自动回退", self.var_only_provider),
        ):
            ttk.Checkbutton(opt, text=text, variable=var).grid(
                row=opt.grid_size()[1], column=0, sticky="w", pady=1
            )

        sra_box = ttk.LabelFrame(box, text=" SRA 选项（仅 SRA 模式生效） ", padding=(10, 6))
        sra_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        sra_box.columnconfigure(0, weight=1)
        self.var_sra_lite = tk.BooleanVar(value=False)
        self.var_skip_compression = tk.BooleanVar(value=False)
        ttk.Checkbutton(sra_box, text="使用 SRA Lite（质量值简化为 Q30）", variable=self.var_sra_lite).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(sra_box, text="跳过 SRA 压缩（更省时间、更占空间）", variable=self.var_skip_compression).grid(
            row=1, column=0, sticky="w"
        )
        gz = ttk.Frame(sra_box)
        gz.grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Label(gz, text=" gzip 级别 1-9：", style="Card.TLabel").pack(side="left")
        self.var_gzip = tk.StringVar(value="1")
        ttk.Spinbox(gz, from_=1, to=9, width=4, textvariable=self.var_gzip).pack(side="left")
        self.sra_tool_label = ttk.Label(sra_box, text="", style="Hint.TLabel", wraplength=320, justify="left")
        self.sra_tool_label.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # 4. 操作按钮（固定在左侧底部，不随设置区滚动）
        actions = self.actions
        actions.columnconfigure(0, weight=1)
        self.btn_query = ttk.Button(actions, text="查询 Run 列表", command=lambda: self._start("query"))
        self.btn_query.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.btn_start = ttk.Button(
            actions, text="开始下载", style="Accent.TButton", command=lambda: self._start("download")
        )
        self.btn_start.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.btn_md5_only = ttk.Button(
            actions, text="仅保存 MD5 清单（不下载）", command=lambda: self._start("md5")
        )
        self.btn_md5_only.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.btn_pause = ttk.Button(actions, text="暂停", command=self._toggle_pause, state="disabled")
        self.btn_pause.grid(row=3, column=0, sticky="ew", padx=(0, 3))
        self.btn_stop = ttk.Button(actions, text="停止", command=self._stop, state="disabled")
        self.btn_stop.grid(row=3, column=1, sticky="ew", padx=(3, 0))
        self.btn_open = ttk.Button(actions, text="打开输出目录", command=self._open_outdir)
        self.btn_open.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_right_panel(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        # 任务页
        tab_tasks = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_tasks, text="  下载任务  ")
        tab_tasks.rowconfigure(1, weight=1)
        tab_tasks.columnconfigure(0, weight=1)
        ttk.Label(
            tab_tasks,
            text="查询后显示 Run 列表。双击某行可在浏览器打开 ENA 页面。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        task_frame = ttk.Frame(tab_tasks)
        task_frame.grid(row=1, column=0, sticky="nsew")
        task_frame.rowconfigure(0, weight=1)
        task_frame.columnconfigure(0, weight=1)
        columns = ("run", "layout", "files", "size", "status", "detail")
        headings = ("Run", "布局", "文件", "大小", "状态", "详情")
        widths = (120, 60, 50, 95, 210, 300)
        self.task_tree = ttk.Treeview(
            task_frame, columns=columns, show="headings", selectmode="browse"
        )
        for col, head, width in zip(columns, headings, widths):
            self.task_tree.heading(col, text=head)
            self.task_tree.column(
                col, width=width, minwidth=45, anchor="w" if col in ("run", "detail") else "center"
            )
        ysb = ttk.Scrollbar(task_frame, orient="vertical", command=self.task_tree.yview)
        xsb = ttk.Scrollbar(task_frame, orient="horizontal", command=self.task_tree.xview)
        self.task_tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        self.task_tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        self._setup_tree_tags(self.task_tree)
        self.task_tree.bind("<Double-1>", self._open_selected_run)
        self.task_tree.bind("<Return>", self._open_selected_run)

        # 元数据页
        tab_meta = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_meta, text="  Run 元数据  ")
        tab_meta.rowconfigure(1, weight=1)
        tab_meta.columnconfigure(0, weight=1)
        meta_top = ttk.Frame(tab_meta)
        meta_top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.meta_count = ttk.Label(meta_top, text="尚未查询", style="Muted.TLabel")
        self.meta_count.pack(side="left")
        ttk.Button(meta_top, text="保存 MD5 清单", command=self._export_md5_manifest).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(meta_top, text="导出 TSV", command=self._export_metadata).pack(side="right")
        meta_frame = ttk.Frame(tab_meta)
        meta_frame.grid(row=1, column=0, sticky="nsew")
        meta_frame.rowconfigure(0, weight=1)
        meta_frame.columnconfigure(0, weight=1)
        meta_cols = (
            "run",
            "experiment",
            "sample",
            "study",
            "layout",
            "platform",
            "reads",
            "bases",
            "size",
            "organism",
        )
        meta_heads = (
            "Run",
            "Experiment",
            "Sample",
            "Study",
            "布局",
            "测序平台",
            "Reads",
            "Bases",
            "FASTQ 大小",
            "物种",
        )
        meta_widths = (110, 105, 110, 110, 70, 145, 90, 110, 95, 170)
        self.meta_tree = ttk.Treeview(
            meta_frame, columns=meta_cols, show="headings", selectmode="browse"
        )
        for col, head, width in zip(meta_cols, meta_heads, meta_widths):
            self.meta_tree.heading(col, text=head)
            self.meta_tree.column(
                col, width=width, minwidth=50, anchor="w" if col in ("run", "platform", "organism") else "center"
            )
        mysb = ttk.Scrollbar(meta_frame, orient="vertical", command=self.meta_tree.yview)
        mxsb = ttk.Scrollbar(meta_frame, orient="horizontal", command=self.meta_tree.xview)
        self.meta_tree.configure(yscrollcommand=mysb.set, xscrollcommand=mxsb.set)
        self.meta_tree.grid(row=0, column=0, sticky="nsew")
        mysb.grid(row=0, column=1, sticky="ns")
        mxsb.grid(row=1, column=0, sticky="ew")
        self._setup_tree_tags(self.meta_tree)
        self.meta_tree.bind("<Double-1>", self._open_selected_run)

        # MD5 校验页
        tab_md5 = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_md5, text="  MD5 校验  ")
        tab_md5.columnconfigure(0, weight=1)
        tab_md5.rowconfigure(4, weight=1)
        ttk.Label(
            tab_md5,
            text="选择下载前保存的 -md5.tsv 清单，程序会逐个文件计算 MD5，并生成 -md5-check.tsv。",
            style="Muted.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        manifest_row = ttk.Frame(tab_md5)
        manifest_row.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        manifest_row.columnconfigure(1, weight=1)
        ttk.Label(manifest_row, text="MD5 清单", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.var_md5_manifest = tk.StringVar()
        ttk.Entry(manifest_row, textvariable=self.var_md5_manifest).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(
            manifest_row, text="浏览…", width=7, command=self._browse_md5_manifest
        ).grid(row=0, column=2, padx=(6, 0))

        data_row = ttk.Frame(tab_md5)
        data_row.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        data_row.columnconfigure(1, weight=1)
        ttk.Label(data_row, text="数据目录", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        self.var_md5_outdir = tk.StringVar()
        ttk.Entry(data_row, textvariable=self.var_md5_outdir).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(
            data_row, text="浏览…", width=7, command=self._browse_md5_outdir
        ).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(data_row, text="留空则使用清单所在目录", style="Hint.TLabel").grid(
            row=0, column=3, padx=(8, 0)
        )

        md5_toolbar = ttk.Frame(tab_md5)
        md5_toolbar.grid(row=3, column=0, sticky="ew", pady=(0, 6))
        self.btn_md5_verify = ttk.Button(
            md5_toolbar, text="开始校验", style="Accent.TButton", command=self._start_verify
        )
        self.btn_md5_verify.pack(side="left")
        self.btn_md5_stop = ttk.Button(
            md5_toolbar, text="停止", command=self._stop_verify, state="disabled"
        )
        self.btn_md5_stop.pack(side="left", padx=(6, 0))
        self.md5_progress = ttk.Progressbar(
            md5_toolbar,
            mode="determinate",
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        )
        self.md5_progress.pack(side="left", fill="x", expand=True, padx=(12, 10))
        self.md5_summary = ttk.Label(md5_toolbar, text="尚未校验", style="Muted.TLabel")
        self.md5_summary.pack(side="right")

        md5_frame = ttk.Frame(tab_md5)
        md5_frame.grid(row=4, column=0, sticky="nsew")
        md5_frame.rowconfigure(0, weight=1)
        md5_frame.columnconfigure(0, weight=1)
        md5_cols = ("file", "run", "expected", "actual", "status", "message")
        md5_heads = ("文件", "Run", "期望 MD5", "实际 MD5", "状态", "说明")
        md5_widths = (250, 105, 300, 300, 90, 180)
        self.md5_tree = ttk.Treeview(
            md5_frame, columns=md5_cols, show="headings", selectmode="browse"
        )
        for col, head, width in zip(md5_cols, md5_heads, md5_widths):
            self.md5_tree.heading(col, text=head)
            self.md5_tree.column(
                col,
                width=width,
                minwidth=55,
                anchor="w" if col in ("file", "message") else "center",
            )
        md5_ysb = ttk.Scrollbar(md5_frame, orient="vertical", command=self.md5_tree.yview)
        md5_xsb = ttk.Scrollbar(md5_frame, orient="horizontal", command=self.md5_tree.xview)
        self.md5_tree.configure(yscrollcommand=md5_ysb.set, xscrollcommand=md5_xsb.set)
        self.md5_tree.grid(row=0, column=0, sticky="nsew")
        md5_ysb.grid(row=0, column=1, sticky="ns")
        md5_xsb.grid(row=1, column=0, sticky="ew")
        self._setup_tree_tags(self.md5_tree)

        # 日志页
        tab_log = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(tab_log, text="  运行日志  ")
        tab_log.rowconfigure(1, weight=1)
        tab_log.columnconfigure(0, weight=1)
        log_top = ttk.Frame(tab_log)
        log_top.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(log_top, text="下载进度、重试和错误都会记录在这里。", style="Muted.TLabel").pack(side="left")
        ttk.Button(log_top, text="保存日志", command=self._save_log).pack(side="right")
        ttk.Button(log_top, text="清空", command=self._clear_log).pack(side="right", padx=(0, 6))
        log_frame = ttk.Frame(tab_log)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            font=(self.font_family, 9),
            background="#0b1220",
            foreground="#d1d5db",
            insertbackground="white",
            relief="flat",
            padx=8,
            pady=6,
            state="disabled",
        )
        lsb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.tag_configure("info", foreground="#d1d5db")
        self.log_text.tag_configure("ok", foreground="#86efac")
        self.log_text.tag_configure("warn", foreground="#fbbf24")
        self.log_text.tag_configure("error", foreground="#fca5a5")
        self.log_text.tag_configure("debug", foreground="#94a3b8")

    def _build_footer(self):
        footer = ttk.Frame(self.root)
        footer.pack(fill="x", padx=14, pady=(0, 12))
        self.progress = ttk.Progressbar(
            footer,
            mode="determinate",
            maximum=100,
            style="Accent.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x")
        row = ttk.Frame(footer)
        row.pack(fill="x", pady=(5, 0))
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(row, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        self.speed_var = tk.StringVar(value="")
        ttk.Label(row, textvariable=self.speed_var, style="Muted.TLabel").pack(side="right")

    @staticmethod
    def _setup_tree_tags(tree: ttk.Treeview):
        tree.tag_configure("ok", foreground="#15803d")
        tree.tag_configure("fail", foreground="#b91c1c")
        tree.tag_configure("run", foreground="#1d4ed8")
        tree.tag_configure("muted", foreground="#6b7280")

    # ------------------------------------------------------------------ #
    # 配置
    # ------------------------------------------------------------------ #
    def _read_config(self) -> dict:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _collect_config(self) -> dict:
        return {
            "accessions": self.acc_text.get("1.0", "end").strip(),
            "outdir": self.var_outdir.get().strip(),
            "prefix": self.var_prefix.get().strip(),
            "provider": PROVIDER_VALUE.get(self.var_provider.get(), "ena"),
            "protocol": PROTOCOL_VALUE.get(self.var_protocol.get(), "https"),
            "engine": ENGINE_VALUE.get(self.var_engine.get(), "auto"),
            "aria2_connections": safe_int(self.var_connections.get(), 8),
            "workers": safe_int(self.var_workers.get(), 3),
            "max_attempts": safe_int(self.var_attempts.get(), 3),
            "sleep": safe_int(self.var_sleep.get(), 5),
            "group_by": GROUP_VALUE.get(self.var_group.get(), "none"),
            "metadata_only": bool(self.var_metadata_only.get()),
            "force": bool(self.var_force.get()),
            "ignore_md5": bool(self.var_ignore_md5.get()),
            "only_provider": bool(self.var_only_provider.get()),
            "sra_lite": bool(self.var_sra_lite.get()),
            "skip_compression": bool(self.var_skip_compression.get()),
            "gzip_level": safe_int(self.var_gzip.get(), 1),
            "md5_manifest": self.var_md5_manifest.get().strip(),
            "md5_outdir": self.var_md5_outdir.get().strip(),
        }

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(self._collect_config(), fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _apply_config(self):
        cfg = self.cfg
        default_out = str(Path.home() / "fastq-downloads")
        self.var_outdir.set(cfg.get("outdir", default_out))
        self.var_prefix.set(cfg.get("prefix", "fastq"))
        self.var_provider.set(
            LABEL_BY_PROVIDER.get(cfg.get("provider", "ena"), PROVIDERS[0][0])
        )
        self.var_protocol.set(
            LABEL_BY_PROTOCOL.get(cfg.get("protocol", "https"), PROTOCOLS[0][0])
        )
        self.var_engine.set(
            LABEL_BY_ENGINE.get(cfg.get("engine", "auto"), ENGINES[0][0])
        )
        self.var_connections.set(str(cfg.get("aria2_connections", 8)))
        self.var_group.set(LABEL_BY_GROUP.get(cfg.get("group_by", "none"), GROUPS[0][0]))
        self.var_workers.set(str(cfg.get("workers", 3)))
        self.var_attempts.set(str(cfg.get("max_attempts", 3)))
        self.var_sleep.set(str(cfg.get("sleep", 5)))
        self.var_metadata_only.set(bool(cfg.get("metadata_only", False)))
        self.var_force.set(bool(cfg.get("force", False)))
        self.var_ignore_md5.set(bool(cfg.get("ignore_md5", False)))
        self.var_only_provider.set(bool(cfg.get("only_provider", False)))
        self.var_sra_lite.set(bool(cfg.get("sra_lite", False)))
        self.var_skip_compression.set(bool(cfg.get("skip_compression", False)))
        self.var_gzip.set(str(cfg.get("gzip_level", 1)))
        self.var_md5_manifest.set(str(cfg.get("md5_manifest", "")))
        self.var_md5_outdir.set(str(cfg.get("md5_outdir", "")))
        text = cfg.get("accessions", "")
        if text:
            self.acc_text.delete("1.0", "end")
            self.acc_text.insert("1.0", text)

    # ------------------------------------------------------------------ #
    # 输入辅助
    # ------------------------------------------------------------------ #
    def _on_acc_modified(self, _event=None):
        if self.acc_text.edit_modified():
            self.acc_text.edit_modified(False)
            self._update_accession_hint()

    def _update_accession_hint(self):
        accessions = parse_accessions(self.acc_text.get("1.0", "end"))
        if not accessions:
            self.acc_hint.configure(
                text="尚未输入 accession。", style="Hint.TLabel"
            )
            return
        kinds = []
        invalid = []
        for acc in accessions:
            try:
                kind, _ = validate_accession(acc)
                kinds.append(kind)
            except ValueError:
                invalid.append(acc)
        if invalid:
            self.acc_hint.configure(
                text=f"⚠ 无法识别：{', '.join(invalid[:4])}"
                + (" …" if len(invalid) > 4 else ""),
                style="ErrorHint.TLabel",
            )
            return
        counts: dict = {}
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        summary = "，".join(f"{kind} × {count}" for kind, count in counts.items())
        self.acc_hint.configure(
            text=f"✓ 已识别 {len(accessions)} 个：{summary}", style="OkHint.TLabel"
        )

    def _fill_sample(self):
        self.acc_text.delete("1.0", "end")
        self.acc_text.insert("1.0", "SRR390728")
        self.acc_text.edit_modified(False)
        self._update_accession_hint()

    def _browse_outdir(self):
        current = self.var_outdir.get().strip()
        initial = current if current and Path(current).exists() else str(Path.home())
        chosen = filedialog.askdirectory(title="选择保存目录", initialdir=initial)
        if chosen:
            self.var_outdir.set(chosen)

    def _open_outdir(self):
        path = Path(self.var_outdir.get().strip() or (Path.cwd() / "fastq-downloads"))
        try:
            path.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                import subprocess

                subprocess.Popen(["open", str(path)])
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("无法打开目录", str(exc))

    def _update_sra_tool_label(self):
        if not hasattr(self, "sra_tool_label"):
            return
        sracha = shutil.which("sracha")
        if sracha:
            self.sra_tool_label.configure(
                text=f"✓ 已检测到 sracha：{sracha}", style="OkHint.TLabel"
            )
        else:
            self.sra_tool_label.configure(
                text="未检测到 sracha。选择 SRA 模式时会自动回退到 ENA。",
                style="WarnHint.TLabel",
            )

    def _update_engine_label(self):
        if not hasattr(self, "engine_label"):
            return
        aria2c = find_aria2c()
        engine = ENGINE_VALUE.get(self.var_engine.get(), "auto")
        if aria2c:
            text = "✓ 已检测到 aria2c，可使用多连接下载"
            if engine == "builtin":
                text = "✓ 已检测到 aria2c，但当前使用内置引擎"
        else:
            text = (
                "未检测到 aria2c，自动模式下会使用内置引擎。\n"
                "安装示例：conda install -c conda-forge aria2　或　"
                "scoop install aria2 / brew install aria2"
            )
        style = "OkHint.TLabel" if aria2c else "WarnHint.TLabel"
        self.engine_label.configure(text=text, style=style)
        if hasattr(self, "spin_connections"):
            active = engine != "builtin"
            self.spin_connections.configure(state="normal" if active else "disabled")

    # ------------------------------------------------------------------ #
    # 启动 / 控制
    # ------------------------------------------------------------------ #
    def _start(self, mode: str):
        if self.manager is not None and self.manager.is_running:
            return
        if requests is None:
            messagebox.showerror(
                "缺少依赖", "未安装 requests。\n\n请先执行：pip install requests"
            )
            return

        accessions = parse_accessions(self.acc_text.get("1.0", "end"))
        if not accessions:
            messagebox.showwarning("没有输入", "请先输入至少一个 accession。")
            return

        invalid = []
        for acc in accessions:
            try:
                validate_accession(acc)
            except ValueError as exc:
                invalid.append(str(exc))
        if invalid:
            messagebox.showerror(
                "Accession 格式有误", "\n\n".join(invalid[:5])
            )
            return

        outdir_text = self.var_outdir.get().strip()
        outdir = Path(outdir_text).expanduser() if outdir_text else Path.cwd() / "fastq-downloads"
        engine = ENGINE_VALUE.get(self.var_engine.get(), "auto")
        if mode == "download" and engine == "aria2" and not find_aria2c():
            messagebox.showwarning(
                "未找到 aria2c",
                "当前系统未检测到 aria2c，本次将自动改用内置下载引擎。\n\n"
                "安装 aria2 后重新选择“aria2c 多连接”即可启用。",
            )
        if mode in ("download", "md5"):
            try:
                outdir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                messagebox.showerror("目录不可用", f"无法创建保存目录：\n{outdir}\n\n{exc}")
                return
        self.var_outdir.set(str(outdir))

        self._save_config()
        self.session_mode = mode
        self._reset_tables()
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.speed_var.set("")
        self._set_busy(True, mode)

        self._append_log("-" * 60, "debug")
        action = {"query": "查询", "md5": "保存 MD5 清单"}.get(mode, "开始下载")
        self._append_log(f"{action}：{', '.join(accessions)}", "info")

        options = self._build_options(mode)
        self.manager = DownloadManager(self.events, options)
        self.manager.start(accessions, mode)

    def _build_options(self, mode: str) -> DownloadOptions:
        return DownloadOptions(
            outdir=Path(self.var_outdir.get().strip() or (Path.cwd() / "fastq-downloads")),
            prefix=self.var_prefix.get().strip() or "fastq",
            provider=PROVIDER_VALUE.get(self.var_provider.get(), "ena"),
            protocol=PROTOCOL_VALUE.get(self.var_protocol.get(), "https"),
            engine=ENGINE_VALUE.get(self.var_engine.get(), "auto"),
            aria2_connections=safe_int(self.var_connections.get(), 8),
            workers=safe_int(self.var_workers.get(), 3),
            max_attempts=safe_int(self.var_attempts.get(), 3),
            sleep=safe_int(self.var_sleep.get(), 5),
            force=bool(self.var_force.get()),
            ignore_md5=bool(self.var_ignore_md5.get()),
            metadata_only=bool(self.var_metadata_only.get()) if mode == "download" else False,
            group_by=GROUP_VALUE.get(self.var_group.get(), "none"),
            only_provider=bool(self.var_only_provider.get()),
            sra_lite=bool(self.var_sra_lite.get()),
            skip_compression=bool(self.var_skip_compression.get()),
            gzip_level=safe_int(self.var_gzip.get(), 1),
        )

    def _set_busy(self, busy: bool, mode: str = "download"):
        normal = "disabled" if busy else "normal"
        self.btn_query.configure(state=normal)
        self.btn_start.configure(state=normal)
        self.btn_md5_only.configure(state=normal)
        if hasattr(self, "btn_md5_verify"):
            self.btn_md5_verify.configure(state=normal)
        control = "normal" if busy and mode == "download" else "disabled"
        self.btn_pause.configure(state=control)
        self.btn_stop.configure(state=control)
        if not busy:
            self.btn_pause.configure(text="暂停")
        self.btn_open.configure(state="normal")

    def _toggle_pause(self):
        if self.manager is None:
            return
        if self.manager.paused:
            self.manager.resume()
            self.btn_pause.configure(text="暂停")
            self.status_var.set("继续下载…")
        else:
            self.manager.pause()
            self.btn_pause.configure(text="继续")
            self.status_var.set("已暂停")

    def _stop(self):
        if self.manager is None:
            return
        self.manager.stop()
        self.btn_pause.configure(state="disabled")
        self.btn_stop.configure(state="disabled")
        self.status_var.set("正在停止…")

    # ------------------------------------------------------------------ #
    # 事件处理
    # ------------------------------------------------------------------ #
    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_events)

    def _handle_event(self, event):
        if not event:
            return
        kind = event[0]
        if kind == "log":
            self._append_log(event[2], event[1])
        elif kind == "status":
            self.status_var.set(event[1])
        elif kind == "mode":
            self.session_mode = event[1]
        elif kind == "runs":
            self._show_runs(event[1])
        elif kind == "run_status":
            self._update_run_status(*event[1:])
        elif kind == "run_progress":
            self._update_run_progress(*event[1:])
        elif kind == "md5_start":
            self._md5_start(*event[1:])
        elif kind == "md5_progress":
            self._md5_progress(*event[1:])
        elif kind == "md5_file":
            self._md5_file(event[1])
        elif kind == "md5_done":
            self._md5_done(event[1])
        elif kind == "progress_mode":
            if event[1] == "indeterminate":
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.configure(mode="determinate")
        elif kind == "total":
            total = max(1, safe_int(event[1]))
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.configure(maximum=total, value=0)
        elif kind == "overall":
            done, total, speed = event[1], event[2], event[3]
            if self.progress.cget("mode") == "determinate":
                if total > 0:
                    self.progress.configure(
                        maximum=max(1, int(total)),
                        value=min(int(done), int(total)),
                    )
                else:
                    self.progress.configure(value=0)
            if total > 0:
                self.speed_var.set(
                    f"{human_size(done)} / {human_size(total)}   {human_speed(speed)}"
                )
            else:
                self.speed_var.set(f"{human_size(done)}   {human_speed(speed)}")
        elif kind == "done":
            self._handle_done(event[1])

    def _handle_done(self, payload: dict):
        self._set_busy(False)
        self.progress.stop()
        self.manager = None
        if (
            payload.get("ok")
            and not payload.get("partial")
            and payload.get("mode") != "query"
            and not payload.get("metadata_only")
        ):
            self.progress.configure(mode="determinate")
            self.progress.configure(value=self.progress.cget("maximum"))
        elif payload.get("mode") == "query":
            self.progress.configure(mode="determinate", value=0)
        message = payload.get("message", "完成。")
        self.status_var.set(message.splitlines()[0])
        self._append_log(message, "ok" if payload.get("ok") else "warn")

        manifest = payload.get("md5_manifest")
        if manifest:
            self.var_md5_manifest.set(manifest)
            if payload.get("outdir"):
                self.var_md5_outdir.set(payload["outdir"])
            if payload.get("ok") and not payload.get("md5_only"):
                message += f"\nMD5 清单：{manifest}"

        if payload.get("stopped"):
            return
        if payload.get("md5_only"):
            if payload.get("ok"):
                messagebox.showinfo("MD5 清单已保存", message)
            else:
                messagebox.showerror("MD5 清单保存失败", message)
            return
        if payload.get("metadata_only") or payload.get("mode") == "query":
            return
        if payload.get("ok"):
            if payload.get("partial"):
                messagebox.showwarning("部分完成", message)
            else:
                messagebox.showinfo("下载完成", message)
        else:
            messagebox.showerror("下载失败", message)

    def _show_runs(self, rows: list):
        self.rows = list(rows)
        self.task_tree.delete(*self.task_tree.get_children())
        self.meta_tree.delete(*self.meta_tree.get_children())
        self.task_rows.clear()

        for run in sorted(rows, key=lambda item: item.get("run_accession", "")):
            acc = run.get("run_accession", "")
            if not acc:
                continue
            files = select_fastq_files(run)
            size = run_size(run)
            layout = run_layout(run)
            if not files:
                status, tag = "无 FASTQ", "fail"
            elif self.session_mode == "query":
                status, tag = "已查询", "muted"
            else:
                status, tag = "等待下载", "muted"

            self.task_tree.insert(
                "",
                "end",
                iid=acc,
                values=(
                    acc,
                    "双端" if layout == "PAIRED" else "单端",
                    len(files),
                    human_size(size) if size else "-",
                    status,
                    "",
                ),
                tags=(tag,),
            )
            self.task_rows[acc] = {
                "run": acc,
                "layout": "双端" if layout == "PAIRED" else "单端",
                "files": len(files),
                "size": human_size(size) if size else "-",
                "status": status,
                "detail": "",
                "tag": tag,
            }

            self.meta_tree.insert(
                "",
                "end",
                iid=acc,
                values=(
                    acc,
                    run.get("experiment_accession", ""),
                    run.get("sample_accession", ""),
                    run.get("study_accession", ""),
                    "双端" if layout == "PAIRED" else "单端",
                    run.get("instrument_model", ""),
                    fmt_int(run.get("read_count")),
                    fmt_int(run.get("base_count")),
                    human_size(size) if size else "-",
                    run.get("scientific_name", ""),
                ),
            )

        self.meta_count.configure(text=f"共 {len(self.rows)} 个 Run")
        self.status_var.set(f"查询完成，共 {len(self.rows)} 个 Run。")
        self.notebook.select(0)

    def _update_run_status(self, acc, status, tag, detail=None):
        data = self.task_rows.get(acc)
        if data is None:
            return
        data["status"] = status
        data["tag"] = tag or "muted"
        if detail is not None:
            data["detail"] = detail
        self._refresh_task_row(data)

    def _update_run_progress(self, acc, done, total, speed, detail=None):
        """状态列显示总进度百分比和实时速度，详情列显示每个文件的进度。"""
        data = self.task_rows.get(acc)
        if data is None:
            return
        if total > 0:
            percent = max(0.0, min(100.0, done * 100.0 / total))
            status = f"下载中 {percent:.1f}%"
        else:
            status = f"下载中 {human_size(done)}"
        if speed and speed > 0:
            status += f" · {human_speed(speed)}"
        data["status"] = status
        data["tag"] = "run"
        if detail:
            data["detail"] = detail
        self._refresh_task_row(data)

    def _refresh_task_row(self, data):
        try:
            self.task_tree.item(
                data["run"],
                values=(
                    data["run"],
                    data["layout"],
                    data["files"],
                    data["size"],
                    data["status"],
                    data["detail"],
                ),
                tags=(data["tag"],),
            )
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 表格 / 日志辅助
    # ------------------------------------------------------------------ #
    def _reset_tables(self):
        self.rows = []
        self.task_rows.clear()
        for tree in (self.task_tree, self.meta_tree):
            try:
                tree.delete(*tree.get_children())
            except tk.TclError:
                pass
        self.meta_count.configure(text="尚未查询")

    def _open_selected_run(self, _event=None):
        current = self.notebook.index(self.notebook.select())
        selection = self.meta_tree.selection() if current == 1 else self.task_tree.selection()
        if not selection:
            selection = self.meta_tree.selection() or self.task_tree.selection()
        if not selection:
            return
        acc = selection[0]
        webbrowser.open(ENA_BROWSER_URL.format(accession=acc))

    def _append_log(self, text: str, level: str = "info"):
        if not hasattr(self, "log_text"):
            return
        stamp = time.strftime("%H:%M:%S")
        tag = level if level in ("info", "ok", "warn", "error", "debug") else "info"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {text}\n", tag)
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 6000:
            self.log_text.delete("1.0", "800.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _save_log(self):
        content = self.log_text.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo("日志为空", "当前没有可保存的日志。")
            return
        default_name = f"fastq-dl-{time.strftime('%Y%m%d-%H%M%S')}.log"
        path = filedialog.asksaveasfilename(
            title="保存日志",
            defaultextension=".log",
            initialfile=default_name,
            filetypes=[("日志文件", "*.log"), ("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content + "\n")
            self._append_log(f"日志已保存：{path}", "ok")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _export_metadata(self):
        if not self.rows:
            messagebox.showinfo("没有数据", "请先查询 Run 元数据。")
            return
        default_name = f"{self.var_prefix.get().strip() or 'fastq'}-run-info.tsv"
        path = filedialog.asksaveasfilename(
            title="导出元数据",
            defaultextension=".tsv",
            initialfile=default_name,
            filetypes=[("TSV 文件", "*.tsv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            write_tsv(rows_for_tsv(self.rows), Path(path))
            self._append_log(f"元数据已导出：{path}", "ok")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _export_md5_manifest(self):
        if not self.rows:
            messagebox.showinfo("没有数据", "请先查询 Run 元数据。")
            return
        default_name = f"{self.var_prefix.get().strip() or 'fastq'}-md5.tsv"
        path = filedialog.asksaveasfilename(
            title="保存 MD5 清单",
            defaultextension=".tsv",
            initialfile=default_name,
            filetypes=[("TSV 文件", "*.tsv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        protocol = PROTOCOL_VALUE.get(self.var_protocol.get(), "https")
        try:
            output = write_md5_manifest(self.rows, Path(path), protocol=protocol)
            self.var_md5_manifest.set(str(output))
            self._append_log(f"MD5 清单已保存：{output}", "ok")
            messagebox.showinfo("已保存", f"MD5 清单已保存：\n{output}")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def _browse_md5_manifest(self):
        path = filedialog.askopenfilename(
            title="选择 MD5 清单",
            filetypes=[("TSV 文件", "*.tsv"), ("所有文件", "*.*")],
        )
        if path:
            self.var_md5_manifest.set(path)

    def _browse_md5_outdir(self):
        path = filedialog.askdirectory(title="选择 FASTQ 数据目录")
        if path:
            self.var_md5_outdir.set(path)

    def _set_verifier_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.btn_md5_verify.configure(state=state)
        self.btn_md5_stop.configure(state="normal" if busy else "disabled")
        self.btn_query.configure(state=state)
        self.btn_start.configure(state=state)
        self.btn_md5_only.configure(state=state)
        if busy:
            self.btn_pause.configure(state="disabled")
            self.btn_stop.configure(state="disabled")

    def _start_verify(self):
        if self.verifier is not None and self.verifier.is_running:
            return
        if self.manager is not None and self.manager.is_running:
            messagebox.showwarning("正在下载", "请先停止当前下载，再进行 MD5 校验。")
            return
        manifest_text = self.var_md5_manifest.get().strip()
        if not manifest_text:
            messagebox.showwarning("没有清单", "请先选择 MD5 清单文件（-md5.tsv）。")
            return
        manifest = Path(manifest_text).expanduser()
        if not manifest.is_file():
            messagebox.showerror("文件不存在", f"找不到 MD5 清单：\n{manifest}")
            return
        try:
            rows = read_md5_manifest(manifest)
        except Exception as exc:
            messagebox.showerror("读取失败", str(exc))
            return
        if not rows:
            messagebox.showwarning("清单为空", "MD5 清单中没有可校验的文件。")
            return
        outdir_text = self.var_md5_outdir.get().strip()
        outdir = Path(outdir_text).expanduser() if outdir_text else manifest.parent
        self.md5_tree.delete(*self.md5_tree.get_children())
        self.md5_progress.stop()
        self.md5_progress.configure(mode="determinate", maximum=100, value=0)
        self.md5_summary.configure(text=f"准备校验 {len(rows)} 个文件…")
        self._set_verifier_busy(True)
        self.verifier = Md5Verifier(self.events, manifest, outdir)
        self.verifier.start()
        self._append_log("-" * 60, "debug")
        self._append_log(f"开始校验 MD5：{manifest}", "info")

    def _stop_verify(self):
        if self.verifier is None:
            return
        self.verifier.stop()
        self.btn_md5_stop.configure(state="disabled")
        self.md5_summary.configure(text="正在停止校验…")

    def _md5_start(self, total_files, total_bytes):
        self.md5_tree.delete(*self.md5_tree.get_children())
        self.md5_progress.stop()
        if total_bytes > 0:
            self.md5_progress.configure(
                mode="determinate", maximum=max(1, int(total_bytes)), value=0
            )
        else:
            self.md5_progress.configure(
                mode="determinate", maximum=max(1, int(total_files)), value=0
            )
        self.md5_summary.configure(text=f"校验中：0/{total_files} 个文件")

    def _md5_progress(self, done_files, total_files, done_bytes, total_bytes, speed, current):
        total_files = max(1, safe_int(total_files, 1))
        if total_bytes > 0:
            self.md5_progress.configure(
                maximum=max(1, int(total_bytes)),
                value=min(int(done_bytes), int(total_bytes)),
            )
            percent = min(100.0, done_bytes * 100.0 / total_bytes)
        else:
            self.md5_progress.configure(
                maximum=total_files, value=min(int(done_files), total_files)
            )
            percent = min(100.0, done_files * 100.0 / total_files)
        text = (
            f"校验中 {percent:.1f}% · {done_files}/{total_files} 个文件"
        )
        if speed and speed > 0:
            text += f" · {human_speed(speed)}"
        if current:
            text += f" · {current}"
        self.md5_summary.configure(text=text)

    def _md5_file(self, result):
        status = result.get("status", "")
        if status == "通过":
            tag = "ok"
        elif status == "无 MD5":
            tag = "muted"
        else:
            tag = "fail"
        self.md5_tree.insert(
            "",
            "end",
            values=(
                result.get("file", ""),
                result.get("run_accession", ""),
                result.get("expected_md5", ""),
                result.get("actual_md5", ""),
                status,
                result.get("message", ""),
            ),
            tags=(tag,),
        )
        self.md5_tree.yview_moveto(1.0)

    def _md5_done(self, payload: dict):
        self._set_verifier_busy(False)
        self.verifier = None
        self.md5_progress.stop()
        message = payload.get("message", "校验结束。")
        self.md5_summary.configure(text=message)
        self._append_log(message, "ok" if payload.get("ok") else "warn")
        if payload.get("stopped"):
            return
        output = payload.get("output", "")
        detail = message + (f"\n\n结果已写入：\n{output}" if output else "")
        if payload.get("ok"):
            self.md5_progress.configure(value=self.md5_progress.cget("maximum"))
            messagebox.showinfo("MD5 校验完成", detail)
        else:
            messagebox.showwarning("MD5 校验结果", detail)

    def _on_close(self):
        busy = (
            (self.manager is not None and self.manager.is_running)
            or (self.verifier is not None and self.verifier.is_running)
        )
        if busy:
            if not messagebox.askyesno(
                "退出",
                "任务仍在进行，确定退出吗？\n未完成的下载会保留 .part，下次可断点续传。",
            ):
                return
            if self.manager is not None:
                self.manager.stop()
            if self.verifier is not None:
                self.verifier.stop()
        self._save_config()
        self.root.destroy()


def _diagnose() -> int:
    """命令行自检：检查依赖、aria2c 和 ENA 连通性，结果写入程序目录。"""
    import subprocess as _subprocess

    lines = [
        f"{APP_NAME} v{APP_VERSION} 自检",
        f"程序目录：{APP_DIR}",
        f"Python：{sys.version.split()[0]}",
        f"requests：{'已安装' if requests is not None else '缺失'}",
    ]
    aria2c = find_aria2c()
    if aria2c:
        version = ""
        try:
            out = _subprocess.run(
                [aria2c, "--version"], capture_output=True, text=True, timeout=20
            ).stdout
            version = out.splitlines()[0].strip() if out else ""
        except Exception:
            pass
        lines.append(f"aria2c：{aria2c}（{version or '版本未知'}）")
    else:
        lines.append("aria2c：未找到（将使用内置引擎）")

    ok = True
    if requests is None:
        lines.append("ENA 查询：跳过（缺少 requests）")
        ok = False
    else:
        try:
            manager = DownloadManager(
                queue.Queue(),
                DownloadOptions(outdir=APP_DIR, max_attempts=1, sleep=0),
            )
            rows = manager._query_ena("run_accession=SRR390728")
            lines.append(f"ENA 查询：成功（SRR390728 返回 {len(rows)} 个 Run）")
            ok = ok and bool(rows)
        except Exception as exc:
            lines.append(f"ENA 查询：失败（{exc}）")
            ok = False

    lines.append("结果：" + ("正常" if ok else "存在问题"))
    text = "\n".join(lines)
    try:
        (APP_DIR / "self-check.txt").write_text(text, encoding="utf-8")
    except Exception:
        pass
    try:
        if sys.stdout:
            print(text)
    except Exception:
        pass
    return 0 if ok else 1


def main():
    if "--diagnose" in sys.argv or "--self-check" in sys.argv:
        raise SystemExit(_diagnose())
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    FastqDlApp(root)
    if requests is None:
        messagebox.showwarning(
            "缺少依赖",
            "未安装 requests，查询和下载将不可用。\n\n请执行：pip install requests",
        )
    root.mainloop()


if __name__ == "__main__":
    main()

