# 模型与第三方组件说明

阿江字幕的源代码采用 Apache-2.0 许可证。模型权重与第三方组件不是本项目源代码的一部分，分别受其上游许可证约束。下载、使用或再分发模型前，请阅读对应模型卡和许可证。

## 离线模型

| 模型 | 项目与来源 | 在阿江字幕中的用途 | 权重许可 |
|---|---|---|---|
| Paraformer-Large | [FunASR](https://github.com/modelscope/FunASR) / [ModelScope](https://modelscope.cn/models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch) | 中文语音识别 | [FunASR Model License Agreement](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)；使用、复制、修改或分享时须保留模型来源与名称 |
| SenseVoiceSmall | [SenseVoice](https://github.com/FunAudioLLM/SenseVoice) / [ModelScope](https://modelscope.cn/models/iic/SenseVoiceSmall) | 中、英、日、韩语种识别以及外语转写 | 模型卡所列 FunASR Model License Agreement；SenseVoice 仓库源码为 MIT |
| Qwen3-1.7B GGUF（Q4_K_M） | [Qwen](https://huggingface.co/Qwen/Qwen3-1.7B-GGUF) | 英语、日语、韩语到简体中文的本地翻译 | [Apache License 2.0](https://huggingface.co/Qwen/Qwen3-1.7B/blob/main/LICENSE) |

本项目没有训练或修改上述模型，也不宣称拥有模型权重。安装包仅为离线使用而随附模型副本，并保留上游模型名称与来源。

## 推理组件

- [FunASR](https://github.com/modelscope/FunASR)：语音识别推理框架。
- [llama.cpp](https://github.com/ggml-org/llama.cpp) 与 [llama-cpp-python](https://github.com/abetlen/llama-cpp-python)：Qwen GGUF 本地推理；Windows Vulkan 动态库来自 llama-cpp-python `v0.3.34-vulkan` 官方 Release。
- [PySide6](https://doc.qt.io/qtforpython-6/)：桌面界面。

第三方名称和商标归各自权利人所有。本说明不替代对应上游许可证原文。

