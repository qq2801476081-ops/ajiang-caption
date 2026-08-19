from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VERSION = "0.9.3"
BASE_PYTHON = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python312"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
RUNTIME_DIR = BUILD_DIR / "runtime"
RUNTIME_ZIP = BUILD_DIR / "runtime.zip"
LAUNCHER = BUILD_DIR / "阿江字幕.exe"
INSTALLER = BUILD_DIR / "installer.exe"
OUTPUT_INSTALLER = DIST_DIR / f"阿江字幕-{VERSION}-单文件安装包.exe"


def find_model_source() -> Path:
    local_model = ROOT / "models" / "paraformer-large"
    if (local_model / "model.pt").exists():
        return local_model
    install_root = Path(os.environ["LOCALAPPDATA"]) / "AjiangCaption"
    candidates = sorted(
        install_root.glob("*/models/paraformer-large"),
        key=lambda path: path.parent.parent.name,
        reverse=True,
    )
    for candidate in candidates:
        if (candidate / "model.pt").exists():
            return candidate
    raise RuntimeError("没有找到已安装的 Paraformer-Large 模型，无法制作离线安装包")


def required_local_model(relative_path: str) -> Path:
    path = ROOT / "models" / relative_path
    if not path.exists():
        raise RuntimeError(f"缺少构建所需离线模型：{path}")
    if path.name == "Qwen3-1.7B-Q4_K_M.gguf" and path.stat().st_size < 1_200_000_000:
        raise RuntimeError(f"Qwen3-1.7B 模型文件不完整：{path}")
    if path.name == "sensevoice-small" and (path / "model.pt").stat().st_size < 900_000_000:
        raise RuntimeError(f"SenseVoiceSmall 模型文件不完整：{path}")
    return path


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True, timeout=7200)


def copy_tree_filtered(source: Path, target: Path) -> None:
    ignored_dirs = {"Doc", "include", "__pycache__", "tests", "test", "idlelib"}
    ignored_suffixes = {".pyc", ".pyo"}
    unused_packages = {"av", "av.libs", "ctranslate2", "faster_whisper"}
    unused_pyside_dirs = {"qml", "resources", "translations"}
    unused_pyside_tokens = (
        "3d", "bluetooth", "charts", "datavisualization", "designer", "graphs",
        "help", "location", "multimedia", "nfc", "pdf", "positioning", "qml",
        "quick", "remoteobjects", "scxml", "sensors", "serial", "spatialaudio",
        "sql", "statemachine", "test", "webchannel", "webengine", "websockets",
    )
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if any(part in ignored_dirs for part in relative.parts):
            continue
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if len(lowered_parts) >= 3 and lowered_parts[:2] == ("lib", "site-packages"):
            package = lowered_parts[2]
            if package in unused_packages:
                continue
            if package == "pyside6":
                if len(lowered_parts) >= 4 and lowered_parts[3] in unused_pyside_dirs:
                    continue
                if item.is_file() and any(token in item.name.lower() for token in unused_pyside_tokens):
                    continue
                if item.name.lower().startswith(("avcodec-", "avformat-", "avutil-", "swresample-", "swscale-")):
                    continue
        if item.is_file() and item.suffix.lower() not in ignored_suffixes:
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)


def prepare_runtime() -> None:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    DIST_DIR.mkdir(exist_ok=True)
    RUNTIME_DIR.mkdir(parents=True)
    copy_tree_filtered(BASE_PYTHON, RUNTIME_DIR / "python")
    shutil.copy2(ROOT / "app.py", RUNTIME_DIR / "app.py")
    shutil.copy2(ROOT / "engine.py", RUNTIME_DIR / "engine.py")
    shutil.copy2(ROOT / "app-icon.ico", RUNTIME_DIR / "app-icon.ico")
    shutil.copy2(ROOT / "LICENSE", RUNTIME_DIR / "LICENSE")
    shutil.copy2(ROOT / "MODEL_LICENSES.md", RUNTIME_DIR / "MODEL_LICENSES.md")
    vulkan_source = ROOT / "vendor" / "llama-vulkan"
    required_vulkan_files = {
        "ggml-base.dll", "ggml-cpu.dll", "ggml-vulkan.dll",
        "ggml.dll", "llama.dll", "mtmd.dll",
    }
    missing_vulkan_files = [
        name for name in required_vulkan_files if not (vulkan_source / name).exists()
    ]
    if missing_vulkan_files:
        raise RuntimeError(
            "缺少 Vulkan 翻译运行库：" + ", ".join(sorted(missing_vulkan_files))
        )
    shutil.copytree(vulkan_source, RUNTIME_DIR / "vendor" / "llama-vulkan")
    shutil.copytree(find_model_source(), RUNTIME_DIR / "models" / "paraformer-large")
    shutil.copytree(
        required_local_model("sensevoice-small"),
        RUNTIME_DIR / "models" / "sensevoice-small",
    )
    qwen_target = RUNTIME_DIR / "models" / "qwen3-1.7b"
    qwen_target.mkdir(parents=True)
    shutil.copy2(
        required_local_model("qwen3-1.7b/Qwen3-1.7B-Q4_K_M.gguf"),
        qwen_target / "Qwen3-1.7B-Q4_K_M.gguf",
    )
    (RUNTIME_DIR / "version.txt").write_text(VERSION, encoding="ascii")


def zip_runtime() -> None:
    with zipfile.ZipFile(RUNTIME_ZIP, "w", allowZip64=True) as archive:
        for path in RUNTIME_DIR.rglob("*"):
            if path.is_file():
                relative = path.relative_to(RUNTIME_DIR)
                compression = zipfile.ZIP_STORED if relative.parts[0] == "models" else zipfile.ZIP_DEFLATED
                archive.write(path, relative, compress_type=compression, compresslevel=6)
    shutil.rmtree(RUNTIME_DIR)


def compile_binaries() -> None:
    csc = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
    if not csc.exists():
        raise RuntimeError("未找到 Windows C# 编译器 csc.exe")
    run([
        str(csc), "/nologo", "/target:winexe", f"/out:{LAUNCHER}", f"/win32icon:{ROOT / 'app-icon.ico'}",
        "/reference:System.Windows.Forms.dll", str(ROOT / "launcher_python.cs"),
    ])
    run([
        str(csc), "/nologo", "/target:winexe", f"/out:{INSTALLER}", f"/win32icon:{ROOT / 'app-icon.ico'}",
        f"/resource:{LAUNCHER},launcher.exe", "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
        "/reference:System.IO.Compression.dll",
        "/reference:System.IO.Compression.FileSystem.dll", str(ROOT / "installer.cs"),
    ])


def append_runtime_payload() -> None:
    marker = b"AJIANG_RUNTIME_V1"
    length = RUNTIME_ZIP.stat().st_size
    with INSTALLER.open("ab") as output, RUNTIME_ZIP.open("rb") as payload:
        output.write(marker)
        output.write(length.to_bytes(8, "little", signed=True))
        shutil.copyfileobj(payload, output, length=1024 * 1024)
    shutil.move(INSTALLER, OUTPUT_INSTALLER)


def write_report() -> dict:
    report = {
        "version": VERSION,
        "mode": "single-file-offline-installer",
        "runtime_zip_bytes": RUNTIME_ZIP.stat().st_size,
        "installer": str(OUTPUT_INSTALLER),
        "installer_bytes": OUTPUT_INSTALLER.stat().st_size,
        "sha256": hashlib.sha256(OUTPUT_INSTALLER.read_bytes()).hexdigest().upper(),
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (DIST_DIR / "release-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    try:
        OUTPUT_INSTALLER.unlink(missing_ok=True)
        prepare_runtime()
        zip_runtime()
        compile_binaries()
        append_runtime_payload()
        print(json.dumps(write_report(), ensure_ascii=False))
    finally:
        shutil.rmtree(BUILD_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()

