# -*- coding: utf-8 -*-
"""
fastq-dl GUI —— 下载引擎

参考 https://github.com/rpetit3/fastq-dl 的核心流程重新实现：
  1) 识别 BioProject / Study / BioSample / Sample / Experiment / Run accession
  2) 通过 ENA Portal API 查询该 accession 下的所有 Run 及 FASTQ 元数据
  3) 按 Run 下载 FASTQ（默认 ENA HTTPS，支持断点续传与 MD5 校验）
  4) 可选按 Experiment / Sample 合并多个 Run
  5) 输出 <prefix>-run-info.tsv、<prefix>-run-mergers.tsv

下载引擎：
  - 内置 requests：逐块读取，.part 续传，兼容性最好
  - aria2c 多连接：单文件多连接并发，速度更快；需要本机安装 aria2c
不依赖 wget；SRA 模式为可选项，检测到 sracha 时使用，否则回退 ENA。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import posixpath
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import requests
except Exception:  # 界面启动时会提示安装
    requests = None

USER_AGENT = "fastq-dl-gui/1.3.0"
ENA_SEARCH_URL = "https://www.ebi.ac.uk/ena/portal/api/search"
CHUNK_SIZE = 1024 * 1024

# 支持识别的 accession 类型（与 fastq-dl 一致）
ACCESSION_RULES = [
    (re.compile(r"^PRJ[EDN][A-Z][0-9]+$"), "BioProject"),
    (re.compile(r"^[EDS]RP[0-9]{6,}$"), "Study"),
    (re.compile(r"^SAM[EDN][A-Z]?[0-9]+$"), "BioSample"),
    (re.compile(r"^[EDS]RS[0-9]{6,}$"), "Sample"),
    (re.compile(r"^[EDS]RX[0-9]{6,}$"), "Experiment"),
    (re.compile(r"^[EDS]RR[0-9]{6,}$"), "Run"),
]


class DownloadCancelled(Exception):
    """用户停止下载。"""


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #
def human_size(n) -> str:
    """字节数 -> 人类可读字符串。"""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return "-"
    if n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    idx = 0
    while n >= 1024 and idx < len(units) - 1:
        n /= 1024.0
        idx += 1
    return f"{n:.1f} {units[idx]}" if idx else f"{int(n)} B"


def human_speed(bps) -> str:
    """字节/秒 -> 人类可读速度。"""
    try:
        bps = float(bps or 0)
    except (TypeError, ValueError):
        return "-"
    if bps <= 0:
        return "-"
    return f"{human_size(bps)}/s"


def safe_int(value, default: int = 0) -> int:
    try:
        text = str(value).strip()
        return int(float(text)) if text else default
    except (TypeError, ValueError):
        return default


def split_multi(value) -> list:
    """ENA 的 fastq_ftp / fastq_md5 / fastq_bytes 用 ';' 分隔多个文件。"""
    if not value:
        return []
    return [x.strip() for x in str(value).split(";") if x.strip()]


def with_scheme(path: str, scheme: str) -> str:
    path = (path or "").strip()
    if "://" in path:
        path = path.split("://", 1)[1]
    return f"{scheme}://{path}"


def parse_accessions(text: str) -> list:
    """一行一个，也支持逗号 / 空格 / 分号分隔；去重并转大写。"""
    seen = set()
    result = []
    for token in re.split(r"[\s,;]+", text or ""):
        token = token.strip().upper()
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def validate_accession(accession: str):
    """返回 (类型, ENA 查询表达式)，格式不合法时抛 ValueError。"""
    acc = (accession or "").strip().upper()
    for pattern, kind in ACCESSION_RULES:
        if pattern.match(acc):
            if kind in ("BioProject", "Study"):
                return kind, f"(study_accession={acc} OR secondary_study_accession={acc})"
            if kind in ("BioSample", "Sample"):
                return kind, f"(sample_accession={acc} OR secondary_sample_accession={acc})"
            if kind == "Experiment":
                return kind, f"experiment_accession={acc}"
            return kind, f"run_accession={acc}"
    raise ValueError(
        f"{accession} 不是可识别的 accession。支持："
        "PRJEB/PRJNA/PRJDB（BioProject）、ERP/DRP/SRP（Study）、"
        "SAMD/SAME/SAMN（BioSample）、ERS/DRS/SRS（Sample）、"
        "ERX/DRX/SRX（Experiment）、ERR/DRR/SRR（Run）。"
    )


def run_size(run: dict) -> int:
    """该 Run 所有 FASTQ 文件的总字节数。"""
    return sum(safe_int(x) for x in split_multi(run.get("fastq_bytes")))


def run_layout(run: dict) -> str:
    layout = (run.get("library_layout") or "").strip().upper()
    return "PAIRED" if layout == "PAIRED" else "SINGLE"


def select_fastq_files(run: dict) -> list:
    """
    从 ENA 元数据中选出需要下载的 FASTQ 文件。

    双端 Run 只保留 *_1.fastq.gz / *_2.fastq.gz；少数 Run 会附带第三个文件，
    按 fastq-dl 的规则跳过。单端 Run 保留全部 fastq_ftp 文件。
    """
    urls = split_multi(run.get("fastq_ftp"))
    md5s = split_multi(run.get("fastq_md5"))
    sizes = split_multi(run.get("fastq_bytes"))
    layout = run_layout(run)
    run_acc = run.get("run_accession", "")

    files = []
    for i, url in enumerate(urls):
        name = posixpath.basename(url)
        role = "r1"
        if layout == "PAIRED":
            if name.endswith("_2.fastq.gz"):
                role = "r2"
            elif name.endswith("_1.fastq.gz"):
                role = "r1"
            else:
                # 不是 _1/_2 的文件：只有在它是唯一文件时才保留
                if len(urls) != 1 and name != f"{run_acc}.fastq.gz":
                    continue
                role = "r1"
        files.append(
            {
                "url": url,
                "name": name,
                "role": role,
                "md5": md5s[i] if i < len(md5s) else "",
                "size": safe_int(sizes[i]) if i < len(sizes) else 0,
            }
        )
    return files


def md5_file(path: Path, stop_event: Optional[threading.Event] = None) -> str:
    """计算文件 MD5（分块读取，可被停止事件中断）。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        while True:
            if stop_event is not None and stop_event.is_set():
                raise DownloadCancelled()
            chunk = fh.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def merge_files(paths: list, output: Path, stop_event: Optional[threading.Event] = None) -> None:
    """按顺序拼接 FASTQ.gz 文件，成功后删除源文件（与 fastq-dl 行为一致）。"""
    paths = [Path(p) for p in paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"合并文件不存在：{', '.join(missing)}")

    if len(paths) == 1:
        os.replace(paths[0], output)
        return

    with open(output, "wb") as out:
        for path in paths:
            with open(path, "rb") as src:
                while True:
                    if stop_event is not None and stop_event.is_set():
                        raise DownloadCancelled()
                    chunk = src.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def _all_fieldnames(rows: list) -> list:
    names = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                names.append(key)
    return names


def write_tsv(rows: list, output: Path) -> Path:
    """把若干 dict 写成 TSV，列取所有行的并集（与 fastq-dl 一致）。"""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    fieldnames = _all_fieldnames(rows)
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=fieldnames, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
    return output


def rows_for_tsv(rows: list) -> list:
    """复制元数据行，去掉内部字段并补充 fastq_total_bytes。"""
    cleaned = []
    for row in rows:
        item = {k: v for k, v in row.items() if not str(k).startswith("_")}
        item["fastq_total_bytes"] = str(run_size(row)) if row.get("fastq_bytes") else ""
        cleaned.append(item)
    return cleaned


MD5_MANIFEST_FIELDS = ("run_accession", "role", "file", "size", "md5", "url")


def build_md5_manifest(rows: list, protocol: str = "https") -> list:
    """从 ENA 元数据生成 MD5 清单行（每个 FASTQ 一行）。"""
    entries = []
    for run in rows:
        acc = run.get("run_accession", "")
        for item in select_fastq_files(run):
            entries.append(
                {
                    "run_accession": acc,
                    "role": item.get("role", ""),
                    "file": item.get("name", ""),
                    "size": str(item.get("size") or ""),
                    "md5": item.get("md5", ""),
                    "url": with_scheme(item.get("url", ""), protocol),
                }
            )
    return entries


def write_md5_manifest(rows: list, output: Path, protocol: str = "https") -> Path:
    """把远程 MD5 清单写入 TSV，供下载前保存、下载后独立校验。"""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = build_md5_manifest(rows, protocol)
    with open(output, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=MD5_MANIFEST_FIELDS, delimiter="\t", extrasaction="ignore"
        )
        writer.writeheader()
        for entry in entries:
            writer.writerow(entry)
    return output


def read_md5_manifest(path: Path) -> list:
    """读取 MD5 清单 TSV，兼容 BOM、空行和旧版 name/checksum 列名。"""
    path = Path(path)
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh, delimiter="\t"):
            row = {str(k).strip(): (v or "").strip() for k, v in raw.items() if k}
            if not row.get("file"):
                row["file"] = row.get("name", "")
            if not row.get("md5"):
                row["md5"] = row.get("expected_md5") or row.get("checksum", "")
            if row.get("file"):
                rows.append(row)
    return rows


def sum_sizes(rows: list) -> int:
    return sum(run_size(r) for r in rows)


def safe_prefix(prefix: str) -> str:
    prefix = (prefix or "fastq").strip() or "fastq"
    return re.sub(r'[\\/:*?"<>|]+', "_", prefix)


def find_aria2c() -> Optional[str]:
    """查找 aria2c：优先程序目录（便携版），其次 aria2c 包内的真实二进制，最后 PATH。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent
    names = ["aria2c.exe", "aria2c"] if os.name == "nt" else ["aria2c"]
    for folder in (base, base / "bin"):
        for name in names:
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)

    # pip 安装的 `aria2` 包会带一个真正的 aria2c 二进制；PATH 里的通常是
    # 转发启动器，不会把 aria2 的进度输出传给父进程，所以优先用包内二进制。
    try:
        import importlib.util

        spec = importlib.util.find_spec("aria2c")
        if spec and spec.origin:
            for name in names:
                candidate = Path(spec.origin).parent / "bin" / name
                if candidate.is_file() and candidate.stat().st_size > 1_000_000:
                    return str(candidate)
    except Exception:
        pass

    return shutil.which("aria2c")


def free_local_port() -> int:
    """获取一个本机空闲端口，供 aria2c 的 RPC 使用。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Aria2Rpc:
    """极简 aria2 JSON-RPC 客户端，只用于读取单个任务的实时进度。"""

    _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __init__(self, port: int, secret: str, gid: str):
        self.port = port
        self.secret = secret
        self.gid = gid

    def call(self, method: str, params=None, timeout: float = 1.5):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "fastq-dl-gui",
                "method": method,
                "params": [f"token:{self.secret}"] + list(params or []),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/jsonrpc",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._opener.open(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("error"):
            return None
        return data.get("result") if isinstance(data, dict) else None

    def wait_ready(self, proc, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                return False
            try:
                if self.call("aria2.getVersion", timeout=0.8):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def status(self):
        try:
            return self.call(
                "aria2.tellStatus",
                [
                    self.gid,
                    [
                        "completedLength",
                        "totalLength",
                        "downloadSpeed",
                        "status",
                        "errorCode",
                        "errorMessage",
                    ],
                ],
                timeout=1.5,
            )
        except Exception:
            return None


def part_files(dest: Path) -> tuple:
    """返回下载临时文件：(.part, aria2 控制文件, 本程序标记文件)。"""
    part = dest.with_name(dest.name + ".part")
    ctrl = part.with_name(part.name + ".aria2")
    mark = part.with_name(part.name + ".aria2tmp")
    return part, ctrl, mark


def remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


class Md5Verifier:
    """
    独立校验 MD5 清单中的文件，不依赖 FASTQ 下载流程。

    事件格式：
      ("md5_start", total_files, total_bytes)
      ("md5_progress", done_files, total_files, done_bytes, total_bytes, speed_bps, current)
      ("md5_file", result_row)
      ("md5_done", payload)
    """

    def __init__(
        self, events: queue.Queue, manifest: Path, outdir: Optional[Path] = None
    ):
        self.events = events
        self.manifest = Path(manifest).expanduser()
        self.outdir = (
            Path(outdir).expanduser() if outdir else self.manifest.parent
        )
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._done_files = 0
        self._total_files = 0
        self._done_bytes = 0
        self._total_bytes = 0
        self._speed = 0.0
        self._last_emit = 0.0
        self._last_time = time.time()
        self._last_bytes = 0

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._cancel.set()

    def emit(self, *event) -> None:
        try:
            self.events.put_nowait(event)
        except Exception:
            pass

    def log(self, text: str, level: str = "info") -> None:
        self.emit("log", level, text)

    def _row_path(self, row: dict) -> Path:
        name = str(row.get("file") or row.get("name") or "").strip()
        path = Path(name)
        return path if path.is_absolute() else self.outdir / path

    def _check_path(self) -> Path:
        name = self.manifest.name
        if name.lower().endswith("-md5.tsv"):
            stem = name[: -len("-md5.tsv")]
            return self.manifest.with_name(f"{stem}-md5-check.tsv")
        return self.manifest.with_name(f"{self.manifest.stem}-md5-check.tsv")

    def _emit_progress(self, current: str = "", force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not force and now - self._last_emit < 0.25:
                return
            elapsed = now - self._last_time
            if elapsed > 0:
                inst = (self._done_bytes - self._last_bytes) / elapsed
                self._speed = (
                    inst if self._speed <= 0 else self._speed * 0.7 + inst * 0.3
                )
            self._last_time = now
            self._last_bytes = self._done_bytes
            self._last_emit = now
            payload = (
                self._done_files,
                self._total_files,
                self._done_bytes,
                self._total_bytes,
                self._speed,
                current,
            )
        self.emit("md5_progress", *payload)

    def _hash_file(self, path: Path) -> str:
        digest = hashlib.md5()
        with open(path, "rb") as fh:
            while True:
                if self._cancel.is_set():
                    raise DownloadCancelled()
                chunk = fh.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                with self._lock:
                    self._done_bytes += len(chunk)
                self._emit_progress(path.name)
        return digest.hexdigest()

    def _run(self) -> None:
        started = time.time()
        try:
            rows = read_md5_manifest(self.manifest)
        except Exception as exc:
            self.emit(
                "md5_done", {"ok": False, "message": f"无法读取 MD5 清单：{exc}"}
            )
            return
        if not rows:
            self.emit("md5_done", {"ok": False, "message": "MD5 清单为空。"})
            return

        total_bytes = 0
        for row in rows:
            path = self._row_path(row)
            size = safe_int(row.get("size"))
            if path.is_file():
                if not size:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        size = 0
                if str(row.get("md5") or "").strip():
                    total_bytes += size
            row["_size"] = size
        with self._lock:
            self._done_files = 0
            self._total_files = len(rows)
            self._done_bytes = 0
            self._total_bytes = total_bytes
            self._speed = 0.0
            self._last_time = time.time()
            self._last_bytes = 0
            self._last_emit = 0.0
        self.emit("md5_start", len(rows), total_bytes)
        self.log(f"开始校验 {len(rows)} 个文件（清单：{self.manifest.name}）", "info")

        results = []
        passed = failed = missing = no_md5 = 0
        for row in rows:
            if self._cancel.is_set():
                break
            path = self._row_path(row)
            name = row.get("file") or path.name
            expected = str(row.get("md5") or "").strip().lower()
            actual = ""
            if not path.is_file():
                status, message = "缺失", "文件不存在"
                missing += 1
            elif not expected:
                status, message = "无 MD5", "清单未提供 MD5"
                no_md5 += 1
            else:
                self._emit_progress(name, force=True)
                try:
                    actual = self._hash_file(path)
                except DownloadCancelled:
                    break
                except Exception as exc:
                    status, message = "读取失败", str(exc)
                    failed += 1
                else:
                    if actual == expected:
                        status, message = "通过", ""
                        passed += 1
                    else:
                        status, message = "不匹配", "MD5 不一致"
                        failed += 1

            result = {
                "run_accession": row.get("run_accession", ""),
                "role": row.get("role", ""),
                "file": name,
                "size": str(row.get("_size") or ""),
                "expected_md5": expected,
                "actual_md5": actual,
                "status": status,
                "message": message,
            }
            results.append(result)
            with self._lock:
                self._done_files += 1
            self.emit("md5_file", result)
            self._emit_progress(name, force=True)
            level = "ok" if status == "通过" else "warn"
            self.log(
                f"{name}：{status}" + (f"（{message}）" if message else ""), level
            )

        stopped = self._cancel.is_set()
        check_path = self._check_path()
        try:
            write_tsv(results, check_path)
            output = str(check_path)
        except Exception as exc:
            self.log(f"写入校验结果失败：{exc}", "error")
            output = ""

        elapsed = max(0.001, time.time() - started)
        if stopped:
            message = f"已停止。已校验 {len(results)}/{len(rows)} 个文件。"
        else:
            message = (
                f"校验完成：通过 {passed} 个，不匹配 {failed} 个，"
                f"缺失 {missing} 个，无 MD5 {no_md5} 个，用时 {elapsed:.0f} 秒。"
            )
        self.emit(
            "md5_done",
            {
                "ok": not stopped and failed == 0 and missing == 0,
                "stopped": stopped,
                "message": message,
                "total": len(rows),
                "passed": passed,
                "failed": failed,
                "missing": missing,
                "no_md5": no_md5,
                "output": output,
                "manifest": str(self.manifest),
                "results": results,
            },
        )


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
@dataclass
class DownloadOptions:
    outdir: Path
    prefix: str = "fastq"
    provider: str = "ena"          # ena | sra
    protocol: str = "https"        # https | ftp
    workers: int = 3
    max_attempts: int = 3
    sleep: int = 5
    force: bool = False
    ignore_md5: bool = False
    metadata_only: bool = False
    group_by: str = "none"         # none | experiment | sample
    only_provider: bool = False
    sra_lite: bool = False
    skip_compression: bool = False
    gzip_level: int = 1
    engine: str = "auto"           # auto | builtin | aria2
    aria2_connections: int = 8

    def normalized(self) -> "DownloadOptions":
        self.outdir = Path(self.outdir).expanduser()
        self.prefix = safe_prefix(self.prefix)
        self.provider = "sra" if str(self.provider).lower() == "sra" else "ena"
        self.protocol = "ftp" if str(self.protocol).lower() == "ftp" else "https"
        self.engine = str(self.engine).lower()
        if self.engine not in ("auto", "builtin", "aria2"):
            self.engine = "auto"
        self.workers = max(1, min(8, safe_int(self.workers, 3)))
        self.max_attempts = max(1, min(10, safe_int(self.max_attempts, 3)))
        self.sleep = max(0, min(120, safe_int(self.sleep, 5)))
        self.aria2_connections = max(1, min(16, safe_int(self.aria2_connections, 8)))
        self.group_by = (
            self.group_by if self.group_by in ("experiment", "sample") else "none"
        )
        self.gzip_level = max(1, min(9, safe_int(self.gzip_level, 1)))
        return self


@dataclass
class FileTask:
    run_acc: str
    url: str
    name: str
    role: str
    md5: str
    size: int
    dest: Path
    skip: bool = False
    ok: bool = False
    error: str = ""
    started: bool = False
    finished: bool = False
    accounted: int = 0
    speed: float = 0.0
    sample_time: float = 0.0
    sample_bytes: int = 0
    run_tasks: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 下载管理器
# --------------------------------------------------------------------------- #
class DownloadManager:
    """
    后台下载引擎。

    所有界面更新都通过 events 队列发送，事件格式：
      ("mode", "query"|"download")
      ("log", level, text)
      ("status", text)
      ("runs", rows)
      ("run_status", run_acc, status, tag, detail|None)
      ("run_progress", run_acc, done_bytes, total_bytes, speed_bps, detail|None)
      ("progress_mode", "determinate"|"indeterminate")
      ("total", total_bytes)
      ("overall", done_bytes, total_bytes, speed_bps)
      ("done", payload)
    """

    def __init__(self, events: queue.Queue, options: DownloadOptions):
        self.events = events
        self.opt = options.normalized()

        self.aria2c = find_aria2c()
        self.use_aria2 = self.opt.engine != "builtin" and bool(self.aria2c)

        self._pause = threading.Event()
        self._pause.set()  # set = 运行中；clear = 暂停
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._lock = threading.Lock()
        self._done = 0
        self._total = 0
        self._speed = 0.0
        self._last_time = time.time()
        self._last_bytes = 0
        self._last_emit = 0.0
        self._last_log_time = 0.0
        self._run_emit: dict = {}
        self._run_tasks: dict = {}

    # ---------------------------- 外部控制 ---------------------------- #
    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, accessions: list, mode: str = "download") -> None:
        if self.is_running:
            return
        self._thread = threading.Thread(
            target=self._run, args=(list(accessions), mode), daemon=True
        )
        self._thread.start()

    def pause(self) -> None:
        if self.is_running:
            self._pause.clear()
            for acc, tasks in list(self._run_tasks.items()):
                if any(t.started and not t.finished for t in tasks):
                    self.emit("run_status", acc, "已暂停", "warn", None)
            self.log("已暂停（当前分块写完后停止读取）", "warn")

    def resume(self) -> None:
        self._pause.set()
        for tasks in list(self._run_tasks.values()):
            for task in tasks:
                if task.started and not task.finished:
                    self._emit_run_progress(task, force=True)
        self.log("继续下载", "ok")

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    def stop(self) -> None:
        self._cancel.set()
        self._pause.set()

    # ---------------------------- 事件工具 ---------------------------- #
    def emit(self, *event) -> None:
        try:
            self.events.put_nowait(event)
        except Exception:
            pass

    def log(self, text: str, level: str = "info") -> None:
        # 避免高速进度把日志刷爆（相同内容 0.5 秒内只记一次）
        now = time.time()
        key = (level, text)
        if getattr(self, "_last_log_key", None) == key and now - self._last_log_time < 0.5:
            return
        self._last_log_key = key
        self._last_log_time = now
        self.emit("log", level, text)

    def status(self, text: str) -> None:
        self.emit("status", text)

    def _wait_if_paused(self) -> None:
        while not self._pause.is_set():
            if self._cancel.is_set():
                raise DownloadCancelled()
            time.sleep(0.2)

    def _sleep_with_cancel(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self._cancel.is_set():
                raise DownloadCancelled()
            self._wait_if_paused()
            time.sleep(0.2)

    # ---------------------------- 进度统计 ---------------------------- #
    def _add_total(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self._total += n
            total = self._total
        self.emit("total", total)

    def _advance(self, n: int, task: Optional[FileTask] = None) -> None:
        if n > 0:
            now = time.time()
            emit = False
            with self._lock:
                self._done += n
                if now - self._last_emit >= 0.25:
                    elapsed = now - self._last_time
                    if elapsed > 0:
                        inst = (self._done - self._last_bytes) / elapsed
                        self._speed = (
                            inst if self._speed <= 0 else self._speed * 0.7 + inst * 0.3
                        )
                    self._last_time = now
                    self._last_bytes = self._done
                    self._last_emit = now
                    emit = True
                done, total, speed = self._done, self._total, self._speed
            if emit:
                self.emit("overall", done, total, speed)
        if task is not None:
            self._sample_task(task)
            self._emit_run_progress(task)

    def _sample_task(self, task: FileTask) -> None:
        """按文件采样，估算单个文件的实时下载速度。"""
        now = time.time()
        if task.sample_time <= 0:
            task.sample_time = now
            task.sample_bytes = task.accounted
            return
        elapsed = now - task.sample_time
        if elapsed < 0.3:
            return
        delta = max(0, task.accounted - task.sample_bytes)
        inst = delta / elapsed
        task.speed = inst if task.speed <= 0 else task.speed * 0.6 + inst * 0.4
        task.sample_time = now
        task.sample_bytes = task.accounted

    @staticmethod
    def _task_percent(task: FileTask) -> Optional[float]:
        if not task.size:
            return None
        return max(0.0, min(100.0, task.accounted * 100.0 / task.size))

    def _progress_detail(self, tasks: list) -> Optional[str]:
        """详情列：单文件显示文件名，多文件显示每个文件的进度和速度。"""
        active = [t for t in tasks if t.started and not t.finished]
        if not active:
            return None
        if len(tasks) == 1:
            return active[0].name
        parts = []
        for task in active:
            label = "R1" if task.role == "r1" else "R2" if task.role == "r2" else task.name
            percent = self._task_percent(task)
            text = f"{label} {percent:.0f}%" if percent is not None else (
                f"{label} {human_size(task.accounted)}"
            )
            if task.speed > 0:
                text += f" {human_speed(task.speed)}"
            parts.append(text)
        return " · ".join(parts)

    def _emit_run_progress(self, task: FileTask, force: bool = False) -> None:
        """汇总同一 Run 下所有文件的进度，供界面显示百分比和速度。"""
        tasks = task.run_tasks
        if not tasks:
            return
        now = time.time()
        key = task.run_acc
        if not force and now - self._run_emit.get(key, 0.0) < 0.5:
            return
        self._run_emit[key] = now

        done = total = 0
        speed = 0.0
        for item in tasks:
            done += (item.size or item.accounted) if item.skip else item.accounted
            total += item.size
            if item.started and not item.finished and not item.skip:
                speed += max(0.0, item.speed)
        self.emit("run_progress", key, done, total, speed, self._progress_detail(tasks))

    def _flush_progress(self) -> None:
        with self._lock:
            done, total, speed = self._done, self._total, self._speed
        self.emit("overall", done, total, speed)

    # ---------------------------- 主流程 ---------------------------- #
    def _run(self, accessions: list, mode: str) -> None:
        self.emit("mode", mode)
        started = time.time()
        rows_all: list = []
        try:
            seen = set()
            for acc in accessions:
                if self._cancel.is_set():
                    break
                try:
                    kind, query = validate_accession(acc)
                except ValueError as exc:
                    self.log(str(exc), "error")
                    continue

                self.status(f"正在查询 {acc}（{kind}）…")
                self.log(f"查询 {acc}（{kind}）", "info")
                try:
                    rows = self._query_ena(query)
                except DownloadCancelled:
                    break
                except Exception as exc:
                    self.log(f"{acc} 查询失败：{exc}", "error")
                    continue

                if not rows:
                    self.log(f"{acc}：没有查到 Run", "warn")
                    continue
                self.log(f"{acc}：找到 {len(rows)} 个 Run", "ok")
                for row in rows:
                    run_acc = row.get("run_accession", "")
                    if not run_acc or run_acc in seen:
                        continue
                    seen.add(run_acc)
                    row["input_accession"] = acc
                    row["_size"] = run_size(row)
                    rows_all.append(row)

            if self._cancel.is_set():
                self.emit(
                    "done",
                    {
                        "ok": False,
                        "stopped": True,
                        "message": "已停止。",
                        "rows": rows_all,
                        "outdir": str(self.opt.outdir),
                    },
                )
                return

            if not rows_all:
                self.emit(
                    "done",
                    {
                        "ok": False,
                        "message": "没有找到任何可下载的 Run，请检查 accession 或网络。",
                        "rows": [],
                        "outdir": str(self.opt.outdir),
                    },
                )
                return

            total_size = sum_sizes(rows_all)
            self.log(
                f"共 {len(rows_all)} 个 Run，FASTQ 合计 {human_size(total_size)}",
                "ok",
            )
            self.emit("runs", rows_all)

            if mode == "query":
                self.emit(
                    "done",
                    {
                        "ok": True,
                        "mode": "query",
                        "message": f"查询完成，共 {len(rows_all)} 个 Run。",
                        "rows": rows_all,
                        "outdir": str(self.opt.outdir),
                    },
                )
                return

            self.opt.outdir.mkdir(parents=True, exist_ok=True)

            # 下载/导出之前先保存远程 MD5，之后可脱离本程序独立校验。
            md5_manifest = None
            manifest_count = sum(len(select_fastq_files(row)) for row in rows_all)
            try:
                md5_manifest = write_md5_manifest(
                    rows_all,
                    self.opt.outdir / f"{self.opt.prefix}-md5.tsv",
                    protocol=self.opt.protocol,
                )
                self.log(
                    f"MD5 清单已保存：{md5_manifest}（{manifest_count} 个文件）", "ok"
                )
            except Exception as exc:
                self.log(f"保存 MD5 清单失败：{exc}", "warn")

            if mode == "md5":
                for row in rows_all:
                    self.emit(
                        "run_status", row["run_accession"], "已保存 MD5", "ok", None
                    )
                if not manifest_count:
                    message = "没有可写入的 FASTQ，MD5 清单为空。"
                    self.log(message, "warn")
                elif md5_manifest:
                    message = (
                        f"MD5 清单已保存：{md5_manifest}\n"
                        f"共 {manifest_count} 个文件。"
                    )
                else:
                    message = "MD5 清单保存失败，详情见日志。"
                self.emit(
                    "done",
                    {
                        "ok": bool(md5_manifest) and bool(manifest_count),
                        "mode": "md5",
                        "md5_only": True,
                        "message": message,
                        "rows": rows_all,
                        "outdir": str(self.opt.outdir),
                        "md5_manifest": str(md5_manifest) if md5_manifest else "",
                    },
                )
                return

            if self.opt.metadata_only:
                for row in rows_all:
                    self.emit("run_status", row["run_accession"], "仅元数据", "muted", None)
                path = self._write_run_info(rows_all)
                self.log(f"元数据已写入 {path}", "ok")
                self.emit(
                    "done",
                    {
                        "ok": True,
                        "metadata_only": True,
                        "message": f"元数据已写入 {path.name}，共 {len(rows_all)} 个 Run。",
                        "rows": rows_all,
                        "outdir": str(self.opt.outdir),
                        "run_info": str(path),
                        "md5_manifest": str(md5_manifest) if md5_manifest else "",
                    },
                )
                return

            sracha = shutil.which("sracha")
            provider = self.opt.provider
            if provider == "sra" and not sracha:
                if self.opt.only_provider:
                    self.emit(
                        "done",
                        {
                            "ok": False,
                            "message": "未找到 sracha，无法使用 SRA 模式。请安装 sracha 或改用 ENA。",
                            "rows": rows_all,
                            "outdir": str(self.opt.outdir),
                        },
                    )
                    return
                self.log("未找到 sracha，自动改用 ENA 直连。", "warn")
                provider = "ena"

            results: dict = {}
            failures: dict = {}

            if provider == "sra":
                results, failures = self._sra_phase(rows_all, sracha)
                if failures and not self.opt.only_provider:
                    fallback_rows = [
                        r for r in rows_all if r["run_accession"] in failures
                    ]
                    self.log(
                        f"SRA 失败 {len(fallback_rows)} 个 Run，尝试回退到 ENA…", "warn"
                    )
                    res2, fail2 = self._ena_phase(fallback_rows)
                    results.update(res2)
                    for acc in res2:
                        failures.pop(acc, None)
                    failures.update(fail2)
            else:
                results, failures = self._ena_phase(rows_all)
                if failures and not self.opt.only_provider and sracha:
                    fallback_rows = [
                        r for r in rows_all if r["run_accession"] in failures
                    ]
                    self.log(
                        f"ENA 失败 {len(fallback_rows)} 个 Run，尝试回退到 SRA…", "warn"
                    )
                    res2, fail2 = self._sra_phase(fallback_rows, sracha)
                    results.update(res2)
                    for acc in res2:
                        failures.pop(acc, None)
                    failures.update(fail2)

            if self._cancel.is_set():
                self.emit(
                    "done",
                    {
                        "ok": False,
                        "stopped": True,
                        "message": "已停止。已下载的文件会保留，再次下载可断点续传。",
                        "rows": rows_all,
                        "outdir": str(self.opt.outdir),
                        "downloaded": len(results),
                        "failed": len(failures),
                        "md5_manifest": str(md5_manifest) if md5_manifest else "",
                    },
                )
                return

            # 合并
            merge_rows = {}
            if self.opt.group_by != "none" and results:
                merge_rows = self._merge_groups(rows_all, results)

            # 写 run-info.tsv（失败原因一并写入）
            for row in rows_all:
                acc = row["run_accession"]
                if acc in failures:
                    row["error"] = failures[acc]
            run_info = self._write_run_info(rows_all)
            self.log(f"元数据已写入 {run_info}", "ok")

            self._flush_progress()
            elapsed = time.time() - started
            ok_count = len(results)
            fail_count = len(failures)
            if fail_count == 0:
                message = (
                    f"全部完成：{ok_count} 个 Run，用时 {elapsed:.0f} 秒。\n"
                    f"输出目录：{self.opt.outdir}"
                )
            elif ok_count:
                message = (
                    f"部分完成：成功 {ok_count} 个，失败 {fail_count} 个，"
                    f"用时 {elapsed:.0f} 秒。\n失败详情见日志和 -run-info.tsv。"
                )
            else:
                message = f"下载失败：{fail_count} 个 Run 均未成功，详情见日志。"

            self.emit(
                "done",
                {
                    "ok": ok_count > 0,
                    "partial": bool(ok_count and fail_count),
                    "message": message,
                    "rows": rows_all,
                    "outdir": str(self.opt.outdir),
                    "run_info": str(run_info),
                    "md5_manifest": str(md5_manifest) if md5_manifest else "",
                    "downloaded": ok_count,
                    "failed": fail_count,
                    "failures": failures,
                    "mergers": merge_rows,
                },
            )
        except DownloadCancelled:
            self.emit(
                "done",
                {
                    "ok": False,
                    "stopped": True,
                    "message": "已停止。",
                    "rows": rows_all,
                    "outdir": str(self.opt.outdir),
                },
            )
        except Exception as exc:
            self.log(traceback.format_exc(), "error")
            self.emit(
                "done",
                {
                    "ok": False,
                    "message": f"发生错误：{exc}",
                    "rows": rows_all,
                    "outdir": str(self.opt.outdir),
                },
            )

    # ---------------------------- ENA 查询 ---------------------------- #
    def _query_ena(self, query: str) -> list:
        if requests is None:
            raise RuntimeError("缺少 requests，请先执行：pip install requests")
        params = {
            "result": "read_run",
            "format": "tsv",
            "fields": "all",
            "query": f'"{query}"',
        }
        last_error = None
        for attempt in range(1, self.opt.max_attempts + 1):
            if self._cancel.is_set():
                raise DownloadCancelled()
            try:
                response = requests.get(
                    ENA_SEARCH_URL,
                    params=params,
                    headers={"User-Agent": USER_AGENT},
                    timeout=(15, 90),
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {response.text[:200]}"
                    )
                rows = list(csv.DictReader(io.StringIO(response.text), delimiter="\t"))
                return rows
            except Exception as exc:
                last_error = exc
                self.log(
                    f"ENA 查询失败（第 {attempt}/{self.opt.max_attempts} 次）：{exc}",
                    "warn",
                )
                if attempt < self.opt.max_attempts:
                    self._sleep_with_cancel(self.opt.sleep)
        raise RuntimeError(f"ENA 查询失败：{last_error}")

    # ---------------------------- ENA 下载 ---------------------------- #
    def _ena_phase(self, runs: list) -> tuple:
        """下载一批 Run，返回 (results, failures)。"""
        self.emit("progress_mode", "determinate")
        if self.use_aria2:
            self.log(
                f"下载引擎：aria2c（{self.aria2c}，每文件最多 {self.opt.aria2_connections} 个连接）",
                "info",
            )
        elif self.opt.engine == "aria2":
            self.log("未找到 aria2c，已自动改用内置下载引擎。", "warn")
        else:
            self.log("下载引擎：内置 requests", "info")
        results: dict = {}
        failures: dict = {}
        task_map: dict = {}

        for run in runs:
            if self._cancel.is_set():
                break
            acc = run.get("run_accession", "")
            files = select_fastq_files(run)
            self.emit("run_status", acc, f"排队中（{len(files)} 个文件）", "muted", None)
            if not files:
                self.log(f"{acc}：ENA 没有可用的 FASTQ（可能尚未同步）", "warn")
                failures[acc] = "ENA_NO_FASTQS"
                self.emit("run_status", acc, "无 FASTQ", "fail", "ENA fastq_ftp 为空")
                continue
            task_map[acc] = []
            for item in files:
                task = FileTask(
                    run_acc=acc,
                    url=item["url"],
                    name=item["name"],
                    role=item["role"],
                    md5=item["md5"],
                    size=item["size"],
                    dest=self.opt.outdir / item["name"],
                )
                task_map[acc].append(task)
        self._run_tasks = task_map
        for tasks in task_map.values():
            for task in tasks:
                task.run_tasks = tasks

        # 预检已有文件（跳过 / 校验 / 删除后重下）
        for acc, tasks in list(task_map.items()):
            if self._cancel.is_set():
                break
            for task in tasks:
                try:
                    self._preflight(task)
                except DownloadCancelled:
                    break
                except Exception as exc:
                    self.log(f"{task.name} 预检失败：{exc}", "warn")

        todo = [
            task
            for tasks in task_map.values()
            for task in tasks
            if not task.skip and not self._cancel.is_set()
        ]
        new_total = sum(task.size for task in todo)
        self._add_total(new_total)

        # 已存在的文件直接算完成
        for tasks in task_map.values():
            for task in tasks:
                if task.skip:
                    task.ok = True

        state = {
            acc: {"total": len(tasks), "done": sum(1 for t in tasks if t.skip), "failed": 0}
            for acc, tasks in task_map.items()
        }
        for acc, st in state.items():
            if st["done"] == st["total"] and st["total"] > 0:
                self.emit("run_status", acc, "已存在（跳过）", "muted", None)
            elif st["total"]:
                self.emit("run_status", acc, "下载中…", "run", None)

        def on_finished(task: FileTask, ok: bool, error: str) -> None:
            task.finished = True
            task.speed = 0.0
            st = state.get(task.run_acc)
            if st is None:
                return
            if ok:
                st["done"] += 1
            else:
                st["failed"] += 1
            if st["done"] + st["failed"] >= st["total"]:
                if st["failed"]:
                    msg = f"失败（{st['failed']} 个文件）"
                    self.emit("run_status", task.run_acc, msg, "fail", error or None)
                else:
                    self.emit(
                        "run_status",
                        task.run_acc,
                        "完成",
                        "ok",
                        f"{st['total']} 个文件",
                    )
            else:
                # 剩余文件继续下载时，让状态列保持百分比和速度
                self._emit_run_progress(task, force=True)

        if todo:
            with ThreadPoolExecutor(max_workers=self.opt.workers) as executor:
                futures = {
                    executor.submit(self._download_task, task): task for task in todo
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        ok, error = future.result()
                    except Exception as exc:
                        ok, error = False, str(exc)
                    task.ok = ok
                    task.error = error
                    on_finished(task, ok, error)
        elif self._cancel.is_set():
            pass
        else:
            for acc, st in state.items():
                if st["total"] and st["done"] == st["total"]:
                    self.emit("run_status", acc, "已存在（跳过）", "muted", None)

        # 汇总每个 Run 的结果
        for acc, tasks in task_map.items():
            if state.get(acc, {}).get("failed", 0) > 0:
                failures[acc] = "ENA_DOWNLOAD_FAILED"
                continue
            if any(not task.ok and not task.skip for task in tasks):
                failures[acc] = "ENA_DOWNLOAD_FAILED"
                continue
            if self._cancel.is_set():
                break
            r1 = next(
                (
                    t.dest
                    for t in tasks
                    if t.role == "r1" and t.dest.exists() and t.ok
                ),
                None,
            )
            r2 = next(
                (
                    t.dest
                    for t in tasks
                    if t.role == "r2" and t.dest.exists() and t.ok
                ),
                None,
            )
            if r1 is None and r2 is not None:
                r1, r2 = r2, None
            if r1 is None:
                failures[acc] = "ENA_DOWNLOAD_FAILED"
                self.log(f"{acc}：没有成功下载的文件", "error")
                continue
            results[acc] = {
                "r1": str(r1),
                "r2": str(r2) if r2 else "",
                "single_end": r2 is None,
            }
            self.emit(
                "run_status",
                acc,
                "完成",
                "ok",
                f"{len(tasks)} 个文件",
            )

        self._flush_progress()
        return results, failures

    def _preflight(self, task: FileTask) -> None:
        """检查目标文件是否已存在且可用，决定跳过或重新下载。"""
        dest = task.dest
        part, ctrl, mark = part_files(dest)

        if self.opt.force:
            if dest.exists():
                self.log(f"覆盖已有文件：{dest.name}", "warn")
                dest.unlink()
            if part.exists():
                part.unlink()
            remove_file(ctrl)
            remove_file(mark)
            return

        if not dest.exists():
            return

        if self.opt.ignore_md5:
            if task.size and dest.stat().st_size != task.size:
                self.log(
                    f"{dest.name} 大小不符，重新下载（{human_size(dest.stat().st_size)} != {human_size(task.size)}）",
                    "warn",
                )
                dest.unlink()
                return
            task.skip = True
            self.log(f"跳过已存在文件：{dest.name}", "info")
            return

        if task.md5:
            if task.size and dest.stat().st_size != task.size:
                self.log(f"{dest.name} 大小不符，重新下载", "warn")
                dest.unlink()
                return
            self.status(f"校验已有文件 {dest.name}…")
            actual = md5_file(dest, self._cancel)
            if actual == task.md5:
                task.skip = True
                self.log(f"MD5 一致，跳过：{dest.name}", "info")
            else:
                self.log(f"{dest.name} MD5 不一致，重新下载", "warn")
                dest.unlink()
            return

        if task.size and dest.stat().st_size != task.size:
            self.log(f"{dest.name} 大小不符，重新下载", "warn")
            dest.unlink()
            return
        task.skip = True
        self.log(f"跳过已存在文件：{dest.name}", "info")

    def _download_task(self, task: FileTask) -> tuple:
        """下载单个文件；返回 (是否成功, 错误信息)。"""
        dest = task.dest
        part = dest.with_name(dest.name + ".part")
        verify_md5 = bool(task.md5) and not self.opt.ignore_md5
        last_error = ""

        if dest.exists() and not self.opt.force:
            task.ok = True
            return True, ""

        if not task.started:
            task.started = True
            task.sample_time = time.time()
            task.sample_bytes = task.accounted
            self._emit_run_progress(task, force=True)

        for attempt in range(1, self.opt.max_attempts + 1):
            if self._cancel.is_set():
                return False, "已停止"
            try:
                task.sample_time = time.time()
                task.sample_bytes = task.accounted
                task.speed = 0.0
                self._wait_if_paused()
                self._download_one(task, part)

                if task.size and dest.stat().st_size != task.size:
                    raise IOError(
                        f"大小不符（{human_size(dest.stat().st_size)} != {human_size(task.size)}）"
                    )

                if verify_md5:
                    self.status(f"校验 MD5 {dest.name}…")
                    actual = md5_file(dest, self._cancel)
                    if actual != task.md5:
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        raise IOError(f"MD5 不一致（得到 {actual}）")

                self.log(f"已下载 {dest.name}（{human_size(dest.stat().st_size)}）", "ok")
                return True, ""
            except DownloadCancelled:
                return False, "已停止"
            except Exception as exc:
                last_error = str(exc)
                self.log(
                    f"{dest.name} 第 {attempt}/{self.opt.max_attempts} 次失败：{exc}",
                    "warn",
                )
                if attempt < self.opt.max_attempts and not self._cancel.is_set():
                    try:
                        self._sleep_with_cancel(self.opt.sleep * attempt)
                    except DownloadCancelled:
                        return False, "已停止"

        return False, last_error

    def _download_one(self, task: FileTask, part: Path) -> None:
        dest = task.dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        if self.use_aria2:
            self._download_aria2(task)
            return

        _, ctrl, mark = part_files(dest)
        if ctrl.exists() or mark.exists():
            # aria2 的多连接分块文件是稀疏文件，内置引擎无法安全追加
            self.log(f"{task.name}：清理 aria2 中断留下的分块，重新下载", "warn")
            remove_file(part)
            remove_file(ctrl)
            remove_file(mark)

        if self.opt.protocol == "ftp":
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass
            task.accounted = 0
            url = with_scheme(task.url, "ftp")
            with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as fh:
                content_length = safe_int(response.headers.get("Content-Length"))
                if content_length and not task.size:
                    self._add_total(content_length)
                while True:
                    self._wait_if_paused()
                    if self._cancel.is_set():
                        raise DownloadCancelled()
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    task.accounted += len(chunk)
                    self._advance(len(chunk), task)
        else:
            offset = part.stat().st_size if part.exists() else 0
            url = with_scheme(task.url, "https")
            headers = {"User-Agent": USER_AGENT}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            with requests.get(
                url, headers=headers, stream=True, timeout=(30, 120)
            ) as response:
                if response.status_code == 416 and offset:
                    # 本地 .part 已经是完整文件
                    delta = max(0, offset - task.accounted)
                    task.accounted = max(task.accounted, offset)
                    self._advance(delta, task)
                    self._finalize_part(part, dest)
                    return
                response.raise_for_status()

                content_length = safe_int(response.headers.get("Content-Length"))
                if offset and response.status_code == 206:
                    mode = "ab"
                    delta = max(0, offset - task.accounted)
                    task.accounted = max(task.accounted, offset)
                    self._advance(delta, task)
                    if content_length and not task.size:
                        self._add_total(offset + content_length)
                else:
                    mode = "wb"
                    offset = 0
                    task.accounted = 0
                    if content_length and not task.size:
                        self._add_total(content_length)

                with open(part, mode) as fh:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        self._wait_if_paused()
                        if self._cancel.is_set():
                            raise DownloadCancelled()
                        if not chunk:
                            continue
                        fh.write(chunk)
                        task.accounted += len(chunk)
                        self._advance(len(chunk), task)

        self._finalize_part(part, dest)

    @staticmethod
    def _finalize_part(part: Path, dest: Path) -> None:
        if dest.exists():
            try:
                dest.unlink()
            except OSError:
                pass
        os.replace(part, dest)

    # ---------------------------- aria2 下载 ---------------------------- #
    def _aria2_command(self, task: FileTask, part: Path) -> tuple:
        """构造 aria2c 命令，返回 (命令, 连接数)。"""
        ctrl = part.with_name(part.name + ".aria2")
        split = self.opt.aria2_connections
        if part.exists() and not ctrl.exists():
            # 没有控制文件的 .part 是连续前缀，只能单连接续传，否则会写坏文件
            split = 1
        cmd = [
            self.aria2c,
            "--no-conf=true",
            f"--dir={part.parent}",
            f"--out={part.name}",
            "--continue=true",
            f"--max-connection-per-server={split}",
            f"--split={split}",
            "--min-split-size=1M",
            "--file-allocation=none",
            "--auto-file-renaming=false",
            "--allow-overwrite=true",
            "--auto-save-interval=1",
            "--connect-timeout=30",
            "--timeout=60",
            "--max-tries=1",
            "--retry-wait=0",
            "--console-log-level=warn",
            "--summary-interval=1",
            "--show-console-readout=false",
            "--download-result=hide",
            f"--user-agent={USER_AGENT}",
            with_scheme(task.url, self.opt.protocol),
        ]
        return cmd, split

    def _prepare_aria2_part(self, task: FileTask, part: Path, ctrl: Path, mark: Path) -> None:
        """若分块文件来自被强制结束的 aria2 且控制文件丢失，则丢弃它。"""
        if part.exists() and not ctrl.exists() and mark.exists():
            self.log(f"{task.name}：分块状态不可信，重新下载", "warn")
            remove_file(part)

    def _download_aria2(self, task: FileTask) -> None:
        """用 aria2c 下载单个文件；暂停/停止时结束进程，依靠 .aria2 续传。"""
        dest = task.dest
        part, ctrl, mark = part_files(dest)

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        while True:
            self._wait_if_paused()
            if self._cancel.is_set():
                raise DownloadCancelled()

            self._prepare_aria2_part(task, part, ctrl, mark)
            rpc_port = free_local_port()
            rpc_secret = os.urandom(8).hex()
            rpc_gid = os.urandom(8).hex()
            cmd, split = self._aria2_command(task, part)
            cmd[-1:-1] = [
                "--enable-rpc=true",
                "--rpc-listen-all=false",
                f"--rpc-listen-port={rpc_port}",
                f"--rpc-secret={rpc_secret}",
                f"--gid={rpc_gid}",
            ]
            if split == 1 and self.opt.aria2_connections > 1 and part.exists():
                self.log(f"{task.name}：续传已有分块（单连接模式）", "info")
            try:
                mark.touch()
            except OSError:
                pass

            proc = subprocess.Popen(
                cmd,
                cwd=str(part.parent),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                creationflags=creationflags,
            )
            tail: list = []
            progress = {"bytes": 0}

            def _drain():
                try:
                    for line in proc.stdout:
                        line = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
                        if not line:
                            continue
                        tail.append(line)
                        del tail[:-10]
                        value = self._parse_aria2_line(line)
                        if value:
                            progress["bytes"] = value
                except Exception:
                    pass

            reader = threading.Thread(target=_drain, daemon=True)
            reader.start()
            rpc = Aria2Rpc(rpc_port, rpc_secret, rpc_gid)
            rpc_ready = rpc.wait_ready(proc)
            paused_here = False
            rpc_complete = False
            rpc_error = ""
            try:
                while proc.poll() is None:
                    status = rpc.status() if rpc_ready else None
                    if status:
                        self._sync_task_bytes(
                            task,
                            safe_int(status.get("completedLength")),
                            speed=safe_int(status.get("downloadSpeed")),
                        )
                        state = status.get("status")
                        if state == "complete":
                            # 开启 RPC 后 aria2 完成下载也不会退出，需要主动关闭
                            rpc_complete = True
                            break
                        if state in ("error", "removed"):
                            rpc_error = (
                                status.get("errorMessage")
                                or f"aria2 错误 {status.get('errorCode') or state}"
                            )
                            break
                    else:
                        self._sync_task_bytes(task, progress["bytes"])
                    if self._cancel.is_set():
                        raise DownloadCancelled()
                    if self.paused:
                        paused_here = True
                        break
                    time.sleep(0.25)
            finally:
                if proc.poll() is None and not rpc_complete and not rpc_error:
                    self._terminate(proc)

            if rpc_complete and proc.poll() is None:
                try:
                    rpc.call("aria2.shutdown", timeout=2)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    self._terminate(proc)
            elif rpc_error and proc.poll() is None:
                self._terminate(proc)

            reader.join(timeout=3)
            self._sync_task_bytes(task, progress["bytes"])

            if paused_here:
                self.log(f"已暂停：{dest.name}", "warn")
                continue

            if rpc_error:
                if task.size and part.exists() and part.stat().st_size >= task.size:
                    remove_file(part)
                    remove_file(ctrl)
                    remove_file(mark)
                raise RuntimeError(rpc_error)

            if proc.returncode != 0 and not rpc_complete:
                message = tail[-1][:300] if tail else f"aria2c 退出码 {proc.returncode}"
                if task.size and part.exists() and part.stat().st_size >= task.size:
                    # 大小已到目标却仍失败，控制文件可能损坏，清空后可安全重试
                    remove_file(part)
                    remove_file(ctrl)
                    remove_file(mark)
                raise RuntimeError(message)

            remove_file(ctrl)
            remove_file(mark)
            if not part.exists():
                raise IOError("aria2c 没有生成下载文件")
            self._finish_task_progress(task, part)
            self._finalize_part(part, dest)
            return

    def _sync_task_bytes(
        self, task: FileTask, value: int, speed: Optional[float] = None
    ) -> None:
        """按 aria2 报告的总下载量推进进度（value 含续传部分）。"""
        if value > 0:
            if task.size:
                value = min(value, task.size)
            if value > task.accounted:
                delta = value - task.accounted
                task.accounted = value
                if speed is None:
                    self._advance(delta, task)
                    return
                self._advance(delta)
        if speed is not None:
            # aria2 RPC 直接给出实时速度，比按采样估算更准确
            task.speed = max(0.0, float(speed))
            self._emit_run_progress(task)
            return
        # 即使本次没有新字节，也刷新一次速度（让停滞的速度逐渐下降）
        self._sample_task(task)
        self._emit_run_progress(task)

    def _finish_task_progress(self, task: FileTask, part: Path) -> None:
        try:
            target = task.size or (part.stat().st_size if part.exists() else 0)
        except OSError:
            return
        if target > task.accounted:
            delta = target - task.accounted
            task.accounted = target
            self._advance(delta, task)

    _ARIA2_POWER = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}

    @classmethod
    def _aria2_size(cls, value: str, unit: str) -> int:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0
        key = (unit or "B").upper().replace("IB", "").rstrip("B")
        if key not in cls._ARIA2_POWER:
            return 0
        return int(number * (1024 ** cls._ARIA2_POWER[key]))

    @classmethod
    def _parse_aria2_line(cls, line: str) -> int:
        """解析 aria2 进度摘要：[#gid 6.2MiB/8.0MiB(78%) CN:4 DL:6.2MiB]。"""
        line = re.sub(r"\x1b\[[0-9;]*m", "", line)
        match = re.search(
            r"\[#[0-9a-zA-Z]+\s+([0-9.]+)\s*([A-Za-z]+)\s*/\s*([0-9.]+)\s*([A-Za-z]+)",
            line,
        )
        if not match:
            return 0
        return cls._aria2_size(match.group(1), match.group(2))

    @staticmethod
    def _terminate(proc) -> None:
        """结束 aria2c；Windows 上用 taskkill /T 连包装器启动的子进程一起结束。"""
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
                    timeout=20,
                )
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        except Exception:
            pass

    # ---------------------------- SRA 下载 ---------------------------- #
    def _sra_phase(self, runs: list, sracha: str) -> tuple:
        """使用 sracha 下载；失败时返回失败表供调用方回退 ENA。"""
        self.emit("progress_mode", "indeterminate")
        results: dict = {}
        failures: dict = {}
        if not sracha:
            for run in runs:
                acc = run.get("run_accession", "")
                failures[acc] = "SRA_NO_SRACHA"
                self.emit("run_status", acc, "缺少 sracha", "fail", None)
            return results, failures

        self.opt.outdir.mkdir(parents=True, exist_ok=True)
        for run in runs:
            if self._cancel.is_set():
                break
            acc = run.get("run_accession", "")
            self.emit("run_status", acc, "SRA 下载中…", "run", None)
            self.status(f"sracha 正在下载 {acc}…")
            try:
                r1, r2 = self._sracha_download(run, sracha)
            except DownloadCancelled:
                break
            except Exception as exc:
                failures[acc] = f"SRA_DOWNLOAD_FAILED: {exc}"
                self.log(f"{acc} SRA 下载失败：{exc}", "error")
                self.emit("run_status", acc, "SRA 失败", "fail", str(exc))
                continue
            results[acc] = {
                "r1": str(r1),
                "r2": str(r2) if r2 else "",
                "single_end": r2 is None,
            }
            self.log(f"{acc} SRA 下载完成", "ok")
            self.emit("run_status", acc, "完成", "ok", None)

        self._flush_progress()
        return results, failures

    def _sracha_download(self, run: dict, sracha: str) -> tuple:
        acc = run.get("run_accession", "")
        cmd = [
            sracha,
            "get",
            acc,
            "-O",
            str(self.opt.outdir),
            "--threads",
            str(max(1, min(8, os.cpu_count() or 4))),
            "--connections",
            "8",
            "--no-progress",
            "-y",
        ]
        if self.opt.force:
            cmd.append("--force")
        if self.opt.ignore_md5:
            cmd.append("--no-strict")
        if self.opt.sra_lite:
            cmd.extend(["--format", "sralite"])
        if self.opt.skip_compression:
            cmd.append("--no-gzip")
        elif self.opt.gzip_level != 1:
            cmd.extend(["--gzip-level", str(self.opt.gzip_level)])

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.opt.outdir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=creationflags,
        )

        last_size = -1
        try:
            while proc.poll() is None:
                if self._cancel.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise DownloadCancelled()
                size = self._sra_written(acc)
                if size != last_size:
                    last_size = size
                    self.status(f"sracha 下载 {acc}：已写入 {human_size(size)}")
                time.sleep(0.5)
            stdout, stderr = proc.communicate()
        finally:
            if proc.poll() is None:
                proc.kill()

        if proc.returncode != 0:
            message = (stderr or stdout or "").strip()
            message = message.splitlines()[-1] if message else f"退出码 {proc.returncode}"
            raise RuntimeError(message[:500])

        r1, r2 = self._sra_output_files(acc)
        if r1 is None:
            raise RuntimeError("sracha 执行成功，但没有找到 FASTQ 文件")
        return r1, r2

    def _sra_written(self, acc: str) -> int:
        total = 0
        for pattern in (f"{acc}*.fastq.gz", f"{acc}*.fastq"):
            for path in self.opt.outdir.glob(pattern):
                try:
                    total += path.stat().st_size
                except OSError:
                    pass
        return total

    def _sra_output_files(self, acc: str):
        se = self.opt.outdir / f"{acc}.fastq.gz"
        pe1 = self.opt.outdir / f"{acc}_1.fastq.gz"
        pe2 = self.opt.outdir / f"{acc}_2.fastq.gz"
        if pe1.exists() and pe2.exists():
            return pe1, pe2
        if se.exists():
            return se, None
        # 兼容 --no-gzip
        se_u = self.opt.outdir / f"{acc}.fastq"
        pe1_u = self.opt.outdir / f"{acc}_1.fastq"
        pe2_u = self.opt.outdir / f"{acc}_2.fastq"
        if pe1_u.exists() and pe2_u.exists():
            return pe1_u, pe2_u
        if se_u.exists():
            return se_u, None
        candidates = sorted(self.opt.outdir.glob(f"{acc}*.fastq.gz"))
        candidates += sorted(self.opt.outdir.glob(f"{acc}*.fastq"))
        if candidates:
            r1 = next((p for p in candidates if "_1." in p.name), candidates[0])
            r2 = next((p for p in candidates if "_2." in p.name), None)
            return r1, r2
        return None, None

    # ---------------------------- 合并 / 输出 ---------------------------- #
    def _merge_groups(self, runs: list, results: dict) -> dict:
        groups: dict = {}
        for run in runs:
            acc = run.get("run_accession", "")
            info = results.get(acc)
            if not info:
                continue
            if self.opt.group_by == "experiment":
                key = run.get("experiment_accession", "")
            else:
                key = run.get("sample_accession", "")
            if not key:
                continue
            group = groups.setdefault(key, {"r1": [], "r2": []})
            group["r1"].append(info["r1"])
            if info["r2"]:
                group["r2"].append(info["r2"])

        merge_rows = []
        for key, group in groups.items():
            if self._cancel.is_set():
                raise DownloadCancelled()
            r1_list = group["r1"]
            r2_list = group["r2"]
            if not r1_list:
                continue
            self.status(f"正在合并 {key}…")
            self.log(f"合并 {key}（{len(r1_list)} 个 Run）", "info")
            paired = bool(r2_list) and len(r1_list) == len(r2_list)
            if paired:
                out1 = self.opt.outdir / f"{key}_R1.fastq.gz"
                out2 = self.opt.outdir / f"{key}_R2.fastq.gz"
                merge_files(r1_list, out1, self._cancel)
                merge_files(r2_list, out2, self._cancel)
                self.log(f"已合并为 {out1.name} / {out2.name}", "ok")
            else:
                out1 = self.opt.outdir / f"{key}.fastq.gz"
                merge_files(r1_list, out1, self._cancel)
                self.log(f"已合并为 {out1.name}", "ok")

            for run in runs:
                if run.get("run_accession") in results:
                    same = (
                        run.get("experiment_accession") == key
                        if self.opt.group_by == "experiment"
                        else run.get("sample_accession") == key
                    )
                    if same:
                        self.emit(
                            "run_status",
                            run["run_accession"],
                            "已合并",
                            "ok",
                            key,
                        )
            merge_rows.append(
                {
                    "accession": key,
                    "r1": ";".join(r1_list),
                    "r2": ";".join(r2_list),
                }
            )

        if merge_rows:
            mergers = self.opt.outdir / f"{self.opt.prefix}-run-mergers.tsv"
            with open(mergers, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh, fieldnames=["accession", "r1", "r2"], delimiter="\t"
                )
                writer.writeheader()
                writer.writerows(merge_rows)
            self.log(f"合并信息已写入 {mergers}", "ok")
        return {"rows": merge_rows, "path": str(mergers) if merge_rows else ""}

    def _write_run_info(self, rows: list) -> Path:
        path = self.opt.outdir / f"{self.opt.prefix}-run-info.tsv"
        write_tsv(rows_for_tsv(rows), path)
        return path
