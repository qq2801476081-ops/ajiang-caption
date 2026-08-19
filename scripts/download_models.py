from __future__ import annotations

from pathlib import Path

from huggingface_hub import hf_hub_download
from modelscope import snapshot_download


ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


def main() -> None:
    MODELS.mkdir(exist_ok=True)
    print("正在下载 Paraformer-Large...")
    snapshot_download(
        "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
        local_dir=str(MODELS / "paraformer-large"),
    )
    print("正在下载 SenseVoiceSmall...")
    snapshot_download(
        "iic/SenseVoiceSmall",
        local_dir=str(MODELS / "sensevoice-small"),
    )
    print("正在下载 Qwen3-1.7B GGUF Q4_K_M...")
    hf_hub_download(
        repo_id="Qwen/Qwen3-1.7B-GGUF",
        filename="Qwen3-1.7B-Q4_K_M.gguf",
        local_dir=str(MODELS / "qwen3-1.7b"),
    )
    print(f"模型准备完成：{MODELS}")


if __name__ == "__main__":
    main()

