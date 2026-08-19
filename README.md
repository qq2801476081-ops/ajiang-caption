# 阿江字幕 Ajiang Caption

一款面向 Windows 10/11 的完全离线实时字幕与翻译工具。它可以监听系统播放声音、麦克风或两者混合，自动识别中文、英语、日语和韩语，并按用户设置把外语直接显示为简体中文字幕。

> 当前版本：`0.9.3`。音频、识别文字和译文均在本机处理，不上传到网络。

## 功能

- 自动识别中文、英语、日语和韩语。
- 英语、日语、韩语可分别设置是否翻译成简体中文。
- 开启翻译后，悬浮窗只显示中文，不先闪现外语原文。
- 可捕获系统声音、麦克风或混合声音。
- 悬浮字幕支持拖动、锁定、透明度、字号、宽度和位置设置。
- 单页最多显示 30 个字符，并自动连续翻页。
- 历史记录和导出保留检测语种、原文与中文译文。
- 支持 NVIDIA、AMD、Intel 的 Vulkan 显卡自动加速；不可用时回退 CPU。
- 全程离线运行，保护音频和字幕隐私。

## 模型架构

阿江字幕采用中国团队开源模型组成的离线混合流水线：

| 模型 | 团队 | 作用 |
|---|---|---|
| Paraformer-Large | 阿里巴巴 FunASR | 中文语音识别，保留较好的中文识别能力 |
| SenseVoiceSmall | FunAudioLLM / 阿里巴巴 | 自动判断中、英、日、韩语种，并负责外语语音转写 |
| Qwen3-1.7B GGUF Q4_K_M | 阿里云通义千问 | 在本机把英语、日语、韩语翻译成简体中文 |

识别和翻译运行在不同线程。翻译线程只保留最新的临时字幕，避免旧字幕排队；最终句仍会完整写入历史记录。详细来源及许可见 [MODEL_LICENSES.md](MODEL_LICENSES.md)。

## 性能说明

在 RTX 3060 Laptop GPU 的开发机上，模型预热后的短句翻译阶段实测约为 `0.08–0.19 秒`，需要外语字符残留校验并重译的句子约为 `0.32 秒`。这些数字不包含音频分段和语音识别时间，也不代表所有电脑都能达到同样结果。

实际准确率和端到端延迟会受到麦克风、背景噪声、口音、多人重叠说话、句长、显卡驱动及硬件性能影响。本项目不承诺所有场景固定达到 95% 准确率或 0.2 秒总延迟。

## 下载与安装

由于完整离线安装包约 3.49 GB，超过 GitHub 单个 Release 附件的大小限制，Release 中提供两个分卷：

1. 下载 `Ajiang-Caption-0.9.3-Installer.exe.part1`、`part2` 和 `merge-installer.cmd` 到同一目录。
2. 双击 `merge-installer.cmd`。
3. 校验生成文件的 SHA-256（Release 同时提供 `SHA256SUMS.txt`）。
4. 运行生成的 `Ajiang-Caption-0.9.3-Installer.exe`。

建议配置：Windows 10/11 x64、16 GB 内存、约 8 GB 可用磁盘空间。支持 Vulkan 的 NVIDIA/AMD/Intel 显卡可显著提升翻译速度；无兼容显卡时仍可使用 CPU。

## 从源码运行

需要 Python 3.12：

```powershell
git clone https://github.com/qq2801476081-ops/ajiang-caption.git
cd ajiang-caption
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python scripts/download_models.py
python scripts/prepare_vulkan_runtime.py
python app.py
```

模型下载体积较大。首次准备源码环境需要联网，模型准备完成后，应用运行不需要联网。

## 构建单文件安装包

先按上面的步骤准备三个模型和 Vulkan 运行库，然后执行：

```powershell
python build_installer.py
```

构建结果位于 `dist/阿江字幕-0.9.3-单文件安装包.exe`。`dist/release-report.json` 包含大小、SHA-256 和构建时间。

## 测试

```powershell
python -m py_compile app.py engine.py build_installer.py test_caption.py
python -m unittest -v test_caption.py
```

## 隐私

- 软件运行时不会主动联网。
- 音频、原文、译文和历史记录不会上传到云端。
- 用户应自行确认捕获和处理音频符合当地法律、平台规则及相关人员授权要求。

## 许可证

项目源代码采用 [Apache License 2.0](LICENSE)。模型权重和第三方组件采用各自的上游许可证，详见 [MODEL_LICENSES.md](MODEL_LICENSES.md)。

