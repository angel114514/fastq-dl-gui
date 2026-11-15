# -*- coding: utf-8 -*-
"""构建 Windows 便携版：PyInstaller 打包 + 内置 aria2c + 压缩为 zip。

用法：
    python build_portable.py

输出：
    dist/fastq-dl-gui/                      可直接运行的目录
    dist/fastq-dl-gui-v<版本>-win64.zip     分发给其他电脑的压缩包
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "fastq-dl-gui"


def read_version() -> str:
    text = (ROOT / "fastq_dl_gui.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "0.0.0"


def find_real_aria2() -> Path | None:
    """找到真正的 aria2c 可执行文件（排除 pip 启动器）。"""
    try:
        import aria2c  # type: ignore

        candidate = Path(aria2c.ARIA2C)
        if candidate.is_file() and candidate.stat().st_size > 1_000_000:
            return candidate
    except Exception:
        pass
    found = shutil.which("aria2c")
    if found:
        candidate = Path(found)
        if candidate.is_file() and candidate.stat().st_size > 1_000_000:
            return candidate
    return None


def build(version: str) -> Path:
    if shutil.which("pyinstaller") is None and not _has_module("PyInstaller"):
        print("未安装 PyInstaller，正在安装…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "pyinstaller"],
            check=True,
        )

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        APP_NAME,
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(BUILD),
        str(ROOT / "fastq_dl_gui.py"),
    ]
    print("运行：", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))

    app_dir = DIST / APP_NAME
    if not (app_dir / f"{APP_NAME}.exe").is_file():
        raise SystemExit("构建失败：没有找到 exe")
    return app_dir


def _has_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


def write_readme(app_dir: Path, version: str) -> None:
    text = f"""fastq-dl GUI v{version} 便携版（Windows 64 位）

一、启动
  1. 把整个文件夹解压到任意目录，建议路径不要包含太多中文或特殊字符；
  2. 双击 fastq-dl-gui.exe 即可运行，不需要安装 Python。

二、关于下载引擎
  - 文件夹内已内置 aria2c.exe（aria2 {aria2_version()}），默认“自动”引擎会优先使用
    多连接下载；
  - 如果不需要 aria2，可在界面里把“下载引擎”改成“内置 requests”；
  - 想升级 aria2，下载新版 aria2c.exe 覆盖同目录的 aria2c.exe 即可；
  - 下载时“状态”列会显示百分比和实时速度，双端 Run 的 R1/R2 进度显示在“详情”列。

三、MD5 清单与校验
  - 下载前会自动保存 <前缀>-md5.tsv，记录每个 FASTQ 的远程 MD5、大小和下载地址；
  - 只想保存 MD5、不下载 FASTQ 时，点“仅保存 MD5 清单（不下载）”；
  - “MD5 校验”标签页可以选择清单和文件目录，逐个文件校验，并生成 -md5-check.tsv；
  - 校验不依赖下载，可把清单和 FASTQ 拷到其他电脑后单独校验。

四、输出与配置
  - 下载文件默认保存到：用户目录\\fastq-downloads，可在界面中修改；
  - 下载前生成 <前缀>-md5.tsv；下载结束生成 <前缀>-run-info.tsv；
  - 校验后生成 <前缀>-md5-check.tsv；
  - 配置保存在同目录的 fastq_dl_gui.json（第一次保存设置后生成）；
  - 下载日志可在“运行日志”标签页导出。

五、常见问题
  - Windows 提示“已保护你的电脑”：点“更多信息 → 仍要运行”；
  - 杀毒软件误报：PyInstaller 打包的程序偶尔会被误报，可加入信任或自行从源码运行；
  - 无法联网：检查代理；程序会读取 HTTPS_PROXY / HTTP_PROXY 环境变量；
  - 只支持 Windows 10/11 64 位，不支持 32 位系统。

六、数据来源
  ENA（https://www.ebi.ac.uk/ena）、NCBI SRA；流程参考 rpetit3/fastq-dl。

七、自检（可选）
  如果程序打不开或无法联网，可在命令提示符中运行：
      fastq-dl-gui.exe --diagnose
  程序会在同目录生成 self-check.txt，记录 aria2c、requests 和 ENA 连通性检查结果。
  正常时退出码为 0，有异常时为 1。
"""
    (app_dir / "使用说明.txt").write_text(text, encoding="utf-8-sig")


def aria2_version() -> str:
    exe = shutil.which("aria2c")
    if not exe:
        return "未知版本"
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=20
        ).stdout
        for line in out.splitlines():
            if line.lower().startswith("aria2 version"):
                return line.split("version", 1)[1].strip()
    except Exception:
        pass
    return "未知版本"


def package(app_dir: Path, version: str) -> Path:
    for name in ("fastq_dl_gui.json", "self-check.txt"):
        try:
            (app_dir / name).unlink()
        except FileNotFoundError:
            pass

    real_aria2 = find_real_aria2()
    if real_aria2:
        shutil.copy2(real_aria2, app_dir / "aria2c.exe")
        print(f"已内置 aria2c：{real_aria2}")
    else:
        print("警告：没有找到可打包的 aria2c，便携版将退回内置下载引擎")

    for name in ("README.md", "LICENSE"):
        source = ROOT / name
        if source.is_file():
            shutil.copy2(source, app_dir / name)

    write_readme(app_dir, version)

    zip_path = DIST / f"{APP_NAME}-v{version}-win64.zip"
    if zip_path.exists():
        zip_path.unlink()
    print("正在压缩…")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file in sorted(app_dir.rglob("*")):
            if file.is_file():
                archive.write(file, Path(APP_NAME) / file.relative_to(app_dir))
    return zip_path


def main() -> None:
    version = read_version()
    print(f"fastq-dl GUI v{version} 便携版构建")
    app_dir = build(version)
    zip_path = package(app_dir, version)
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print("")
    print(f"完成：{app_dir}")
    print(f"压缩包：{zip_path}（{size_mb:.1f} MB）")


if __name__ == "__main__":
    main()
