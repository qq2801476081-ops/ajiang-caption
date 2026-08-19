from __future__ import annotations

import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "vendor" / "llama-vulkan"
URL = (
    "https://github.com/abetlen/llama-cpp-python/releases/download/"
    "v0.3.34-vulkan/llama_cpp_python-0.3.34-py3-none-win_amd64.whl"
)
EXPECTED_SHA256 = "8cfeb11af0405a2c76e7b93a677573575c631e8bc176b9af57a49dca7caffb17"
DLL_NAMES = {
    "ggml-base.dll",
    "ggml-cpu.dll",
    "ggml-vulkan.dll",
    "ggml.dll",
    "llama.dll",
    "mtmd.dll",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ajiang-vulkan-") as temporary:
        wheel = Path(temporary) / "llama_cpp_python.whl"
        print("正在下载官方 Windows Vulkan 运行库...")
        urllib.request.urlretrieve(URL, wheel)
        actual = sha256(wheel)
        if actual != EXPECTED_SHA256:
            raise RuntimeError(f"Vulkan 运行库校验失败：{actual}")

        TARGET.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(wheel) as archive:
            members = {
                Path(name).name: name
                for name in archive.namelist()
                if name.startswith("llama_cpp/lib/") and Path(name).name in DLL_NAMES
            }
            missing = DLL_NAMES - members.keys()
            if missing:
                raise RuntimeError("Vulkan 运行库缺少：" + ", ".join(sorted(missing)))
            for name, member in members.items():
                with archive.open(member) as source, (TARGET / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
    print(f"Vulkan 运行库准备完成：{TARGET}")


if __name__ == "__main__":
    main()

