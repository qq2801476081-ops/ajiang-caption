from __future__ import annotations

import argparse
from collections import deque
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np
import soundcard as sc
from funasr import AutoModel
from modelscope import snapshot_download
from modelscope.hub.api import HubApi


SAMPLE_RATE = 16_000
CAPTURE_SECONDS = 0.2
MAX_UTTERANCE_SECONDS = 6.0
ROLLOVER_OVERLAP_SECONDS = 2.0
PRE_ROLL_SECONDS = 0.8
SILENCE_FINALIZE_CHUNKS = 4
CONTEXT_EXPIRY_CHUNKS = 25
MODEL_MARKER = "active-model.json"
LANGUAGE_NAMES = {"zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语"}
SUPPORTED_LANGUAGES = set(LANGUAGE_NAMES)
LIVE_STREAM_TERMS = {
    "en": "live stream 译为“直播”",
    "ja": "ライブ配信 译为“直播”",
    "ko": "라이브 방송 译为“直播”，不要译为“直播广播”",
}
EMIT_LOCK = threading.Lock()


def emit(message_type: str, text: str = "", **fields) -> None:
    message = {"type": message_type, "text": text}
    message.update(fields)
    with EMIT_LOCK:
        print(json.dumps(message, ensure_ascii=False), flush=True)


def format_bytes(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / 1024 / 1024 / 1024:.2f} GB"
    if value >= 1024 * 1024:
        return f"{value / 1024 / 1024:.1f} MB"
    return f"{value / 1024:.1f} KB"


def directory_size(path: Path) -> int:
    total = 0
    seen_files = set()
    for item in path.rglob("*") if path.exists() else ():
        if item.is_file():
            try:
                stat = item.stat()
                identity = (stat.st_dev, stat.st_ino)
                if identity not in seen_files:
                    seen_files.add(identity)
                    total += stat.st_size
            except OSError:
                pass
    return total


def get_model_total_size(model_id: str) -> int:
    try:
        files = HubApi().get_model_files(model_id, recursive=True)
        return sum(int(item.get("Size") or 0) for item in files)
    except Exception:
        logging.exception("获取模型文件清单失败")
        return 0


def monitor_download(model_dir: Path, stop_event: threading.Event, total_size: int) -> None:
    previous_size = directory_size(model_dir)
    previous_time = time.monotonic()
    while not stop_event.wait(0.5):
        current_size = directory_size(model_dir)
        current_time = time.monotonic()
        elapsed = max(0.001, current_time - previous_time)
        speed = max(0, current_size - previous_size) / elapsed
        percent = min(99, round(current_size * 100 / total_size)) if total_size else -1
        downloaded = format_bytes(current_size)
        total = f" / {format_bytes(total_size)}" if total_size else ""
        emit(
            "progress",
            f"已下载 {downloaded}{total} · {format_bytes(round(speed))}/s",
            percent=percent,
        )
        previous_size = current_size
        previous_time = current_time


def load_marker(model_dir: Path) -> dict:
    marker = model_dir / MODEL_MARKER
    if not marker.exists():
        return {}
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def find_local_model_path(model_dir: Path) -> Path | None:
    for config_name in ("configuration.json", "config.yaml", "model.pt"):
        for item in model_dir.rglob(config_name) if model_dir.exists() else ():
            if item.is_file():
                return item.parent
    return None


def prepare_model(model_id: str, model_dir: Path) -> str:
    marker = load_marker(model_dir)
    marked_path = Path(marker.get("model_path", ""))
    if marker.get("model_id") == model_id and marked_path.exists():
        emit("status", "正在加载本地识别模型...")
        return str(marked_path)

    local_model = find_local_model_path(model_dir)
    if local_model is not None:
        emit("status", "正在加载内置离线识别模型...")
        return str(local_model)

    if model_dir.exists():
        shutil.rmtree(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    emit("status", "正在准备模型下载...")
    total_size = get_model_total_size(model_id)
    detail = f"模型总大小 {format_bytes(total_size)}" if total_size else "正在连接 ModelScope..."
    emit("progress", detail, percent=0 if total_size else -1)
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_download,
        args=(model_dir, stop_event, total_size),
        daemon=True,
    )
    monitor.start()
    try:
        model_path = Path(snapshot_download(model_id, cache_dir=str(model_dir)))
    finally:
        stop_event.set()
        monitor.join(timeout=1)
    (model_dir / MODEL_MARKER).write_text(
        json.dumps({"model_id": model_id, "model_path": str(model_path)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    emit("progress", "模型下载完成", percent=100)
    return str(model_path)


def get_microphones(source: str):
    microphone = sc.default_microphone()
    speaker = sc.default_speaker()
    loopback = None
    if speaker is not None:
        loopbacks = sc.all_microphones(include_loopback=True)
        loopback = next((item for item in loopbacks if item.name == speaker.name), None)
    if source == "microphone":
        return [microphone] if microphone is not None else []
    if source == "mixed":
        return [item for item in (loopback, microphone) if item is not None]
    if loopback is not None:
        return [loopback]
    return [microphone] if microphone is not None else []


def mix_audio(chunks):
    chunks = [chunk for chunk in chunks if len(chunk) > 0]
    if not chunks:
        return np.array([], dtype=np.float32)
    length = min(len(chunk) for chunk in chunks)
    if len(chunks) == 1:
        return chunks[0][:length]
    mixed = np.zeros(length, dtype=np.float32)
    for chunk in chunks:
        mixed += chunk[:length] / len(chunks)
    return np.clip(mixed, -1.0, 1.0)


def read_audio_batch(audio_queues: list[queue.Queue]) -> list[np.ndarray | Exception]:
    chunks_by_source = [[audio_queue.get()] for audio_queue in audio_queues]
    pending_chunks = min(audio_queue.qsize() for audio_queue in audio_queues)
    for _ in range(pending_chunks):
        for source_chunks, audio_queue in zip(chunks_by_source, audio_queues):
            source_chunks.append(audio_queue.get_nowait())
    batches: list[np.ndarray | Exception] = []
    for source_chunks in chunks_by_source:
        error = next((chunk for chunk in source_chunks if isinstance(chunk, Exception)), None)
        if error is not None:
            batches.append(error)
        else:
            batches.append(np.concatenate(source_chunks))
    return batches


def append_ring(buffer: np.ndarray, chunk: np.ndarray, maximum_frames: int) -> np.ndarray:
    combined = np.concatenate((buffer, chunk))
    return combined[-maximum_frames:].copy()


def prepare_audio(audio: np.ndarray) -> np.ndarray:
    if len(audio) == 0:
        return audio
    audio = audio - float(np.mean(audio))
    rms = float(np.sqrt(np.mean(audio * audio)))
    if 0 < rms < 0.06:
        audio = audio * min(4.0, 0.06 / rms)
    return np.clip(audio, -1.0, 1.0)


def capture_audio(microphone, output: queue.Queue) -> None:
    try:
        with microphone.recorder(samplerate=SAMPLE_RATE, channels=1) as recorder:
            while True:
                chunk = np.asarray(
                    recorder.record(numframes=round(SAMPLE_RATE * CAPTURE_SECONDS)),
                    dtype=np.float32,
                ).reshape(-1)
                output.put(chunk)
    except Exception as exc:
        output.put(exc)


def clean_transcription(text: str) -> str:
    text = re.sub(r"<\|[^|]+\|>", "", text)
    return " ".join(text.split())


def parse_sensevoice_result(text: str) -> tuple[str, str]:
    tags = re.findall(r"<\|([^|]+)\|>", text)
    language = next((tag.lower() for tag in tags if tag.lower() in SUPPORTED_LANGUAGES | {"zn"}), "")
    if language == "zn":
        language = "zh"
    return language, clean_transcription(text)


def caption_payload(
    source_text: str,
    language: str,
    translation_text: str = "",
    partial: bool = False,
) -> dict:
    return {
        "text": translation_text or source_text,
        "partial": partial,
        "language": language,
        "source_text": source_text,
        "translation_text": translation_text,
    }


def translated_caption_payload(
    translator,
    translate_languages: set[str],
    source_text: str,
    language: str,
    partial: bool,
) -> dict | None:
    should_translate = translator is not None and language in translate_languages
    if not should_translate:
        return caption_payload(source_text, language, partial=partial)
    translation_text = translator.translate(language, source_text)
    if not translation_text:
        return None
    return caption_payload(source_text, language, translation_text, partial=partial)


class OfflineTranslator:
    def __init__(self, model_path: Path):
        vulkan_dir = Path(__file__).resolve().parent / "vendor" / "llama-vulkan"
        use_vulkan = False
        if sys.platform == "win32" and (vulkan_dir / "llama.dll").exists():
            try:
                import ctypes

                ctypes.WinDLL("vulkan-1.dll")
                ctypes.WinDLL(str(vulkan_dir / "llama.dll"))
                os.environ["LLAMA_CPP_LIB_PATH"] = str(vulkan_dir)
                use_vulkan = True
            except OSError:
                os.environ.pop("LLAMA_CPP_LIB_PATH", None)

        from llama_cpp import Llama

        model_options = {
            "model_path": str(model_path),
            "n_ctx": 512,
            "n_threads": min(4, max(1, (os.cpu_count() or 4) // 2)),
            "n_threads_batch": min(20, max(1, os.cpu_count() or 4)),
            "verbose": False,
        }
        try:
            self.model = Llama(
                **model_options,
                n_gpu_layers=-1 if use_vulkan else 0,
            )
        except Exception:
            if not use_vulkan:
                raise
            logging.exception("Vulkan 显卡加速不可用，回退到 CPU")
            use_vulkan = False
            self.model = Llama(**model_options, n_gpu_layers=0)
        self.backend_name = "Vulkan 显卡加速" if use_vulkan else "CPU 兼容模式"
        self.cache: dict[tuple[str, str], str] = {}
        # 在“正在加载模型”阶段覆盖三种语言和不同提示长度的首次推理，
        # 避免 Vulkan 在正式字幕期间临时编译新管线。
        if use_vulkan:
            warmups = (
                ("en", "Hello everyone, welcome to the live stream."),
                ("ja", "皆さん、ライブ配信へようこそ。"),
                ("ko", "여러분, 라이브 방송에 오신 것을 환영합니다."),
            )
        else:
            warmups = (("en", "Hello."),)
        for language, text in warmups:
            self.translate(language, text)

    def translate(self, language: str, text: str) -> str:
        key = (language, text)
        if key in self.cache:
            return self.cache[key]
        language_name = LANGUAGE_NAMES.get(language, "外语")
        terminology = LIVE_STREAM_TERMS.get(language, "")
        prompt = (
            "/no_think\n"
            f"将{language_name}忠实翻译成自然简洁的简体中文字幕。\n"
            f"常用术语：{terminology}。\n"
            "只输出译文；保留人名、数字和专有名词。\n"
            f"原文：{text}\n译文："
        )
        response = self.model.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=96,
        )
        translated = clean_translation_response(response)
        if translation_contains_source_script(language, translated):
            retry_prompt = (
                "/no_think\n"
                f"必须把下面的{language_name}完整翻译成自然的简体中文。"
                "不得照抄原文，不得残留日语假名或韩文，不解释。\n"
                f"原文：{text}\n简体中文译文："
            )
            response = self.model.create_chat_completion(
                messages=[{"role": "user", "content": retry_prompt}],
                temperature=0.0,
                max_tokens=96,
            )
            translated = clean_translation_response(response)
        if translated:
            self.cache[key] = translated
        return translated


def clean_translation_response(response: dict) -> str:
    translated = str(response["choices"][0]["message"]["content"] or "")
    translated = re.sub(r"<think>.*?</think>", "", translated, flags=re.DOTALL).strip()
    return translated.strip('"“”')


def translation_contains_source_script(language: str, text: str) -> bool:
    if language == "ja":
        return bool(re.search(r"[\u3040-\u30ff]", text))
    if language == "ko":
        return bool(re.search(r"[\uac00-\ud7af]", text))
    return False


class RealtimeTranslationDispatcher:
    """Translates on a dedicated thread and drops queued stale partial captions."""

    def __init__(self, translator, translate_languages: set[str], emit_callback=emit):
        self.translator = translator
        self.translate_languages = translate_languages
        self.emit_callback = emit_callback
        self._condition = threading.Condition()
        self._finals = deque()
        self._latest_partial = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, source_text: str, language: str, partial: bool) -> None:
        if self.translator is None or language not in self.translate_languages:
            self.emit_callback(
                "caption", **caption_payload(source_text, language, partial=partial)
            )
            return
        task = (source_text, language, partial)
        with self._condition:
            if partial:
                self._latest_partial = task
            else:
                self._latest_partial = None
                self._finals.append(task)
            self._condition.notify()

    def _next_task(self):
        with self._condition:
            while not self._finals and self._latest_partial is None:
                self._condition.wait()
            if self._finals:
                return self._finals.popleft()
            task = self._latest_partial
            self._latest_partial = None
            return task

    def _run(self) -> None:
        while True:
            source_text, language, partial = self._next_task()
            try:
                payload = translated_caption_payload(
                    self.translator,
                    self.translate_languages,
                    source_text,
                    language,
                    partial,
                )
                if payload is not None:
                    self.emit_callback("caption", **payload)
            except Exception as exc:
                logging.exception("实时翻译失败")
                self.emit_callback("error", text=f"实时翻译失败：{exc}")


def remove_rollover_overlap(previous: str, current: str) -> str:
    maximum = min(len(previous), len(current), 16)
    for overlap in range(maximum, 1, -1):
        if previous.endswith(current[:overlap]):
            return current[overlap:]
    return current


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("speaker", "microphone", "mixed"), default="speaker")
    parser.add_argument(
        "--model-id",
        default="iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    )
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--sensevoice-model-dir", type=Path)
    parser.add_argument("--translation-model", type=Path)
    parser.add_argument("--translate-languages", default="en,ja,ko")
    parser.add_argument("--log-path", type=Path)
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    if args.log_path:
        logging.basicConfig(filename=args.log_path, level=logging.INFO, encoding="utf-8")
    try:
        if args.model_dir is None:
            raise RuntimeError("没有指定模型目录")
        if args.sensevoice_model_dir is None or not args.sensevoice_model_dir.exists():
            raise RuntimeError("缺少 SenseVoiceSmall 离线模型")
        model_path = prepare_model(args.model_id, args.model_dir)
        paraformer = AutoModel(
            model=model_path,
            device="cpu",
            disable_update=True,
        )
        sensevoice = AutoModel(
            model=str(args.sensevoice_model_dir),
            device="cpu",
            disable_update=True,
        )
        translate_languages = {
            code.strip().lower()
            for code in args.translate_languages.split(",")
            if code.strip().lower() in {"en", "ja", "ko"}
        }
        translator = None
        if translate_languages:
            if args.translation_model is None or not args.translation_model.exists():
                raise RuntimeError("缺少 Qwen3-1.7B 离线翻译模型")
            emit("status", "正在加载本地翻译模型...")
            translator = OfflineTranslator(args.translation_model)
            emit("status", f"翻译引擎：{translator.backend_name}")

        def recognize(audio: np.ndarray) -> tuple[str, str, str]:
            sense_result = sensevoice.generate(
                input=audio,
                language="auto",
                use_itn=True,
                batch_size_s=MAX_UTTERANCE_SECONDS,
            )
            sense_raw = str(sense_result[0].get("text", "")) if sense_result else ""
            language, sense_text = parse_sensevoice_result(sense_raw)
            if language != "zh":
                return language, sense_text, sense_text
            paraformer_result = paraformer.generate(
                input=audio,
                batch_size_s=MAX_UTTERANCE_SECONDS,
            )
            paraformer_text = clean_transcription(
                str(paraformer_result[0].get("text", "")) if paraformer_result else ""
            )
            return language, paraformer_text or sense_text, paraformer_text or sense_text

        translation_dispatcher = RealtimeTranslationDispatcher(
            translator, translate_languages
        )

        def finalize(source_text: str, language: str) -> None:
            translation_dispatcher.submit(source_text, language, partial=False)

        microphones = get_microphones(args.source)
        if not microphones:
            raise RuntimeError("没有找到可用的音频输入设备")
        emit("status", "识别中")
        audio_queues = [queue.Queue(maxsize=120) for _ in microphones]
        for microphone, audio_queue in zip(microphones, audio_queues):
            threading.Thread(
                target=capture_audio,
                args=(microphone, audio_queue),
                daemon=True,
            ).start()

        audio_buffer = np.array([], dtype=np.float32)
        last_text = ""
        last_raw_text = ""
        last_language = ""
        rollover_reference = ""
        pre_roll = np.array([], dtype=np.float32)
        carry_audio = np.array([], dtype=np.float32)
        speech_tail = np.array([], dtype=np.float32)
        silence_chunks = 0
        max_frames = round(SAMPLE_RATE * MAX_UTTERANCE_SECONDS)
        overlap_frames = round(SAMPLE_RATE * ROLLOVER_OVERLAP_SECONDS)
        pre_roll_frames = round(SAMPLE_RATE * PRE_ROLL_SECONDS)
        while True:
            chunks = read_audio_batch(audio_queues)
            error = next((chunk for chunk in chunks if isinstance(chunk, Exception)), None)
            if error is not None:
                raise error
            raw_audio = mix_audio(chunks)
            if len(raw_audio) == 0:
                continue
            peak = float(np.max(np.abs(raw_audio)))
            rms = float(np.sqrt(np.mean(raw_audio * raw_audio)))
            is_speech = peak >= 0.004 or rms >= 0.001
            if not is_speech:
                silence_chunks += 1
                if len(audio_buffer) > 0 and silence_chunks < SILENCE_FINALIZE_CHUNKS:
                    audio_buffer = np.concatenate((audio_buffer, prepare_audio(raw_audio)))
                else:
                    pre_roll = append_ring(pre_roll, raw_audio, pre_roll_frames)
                if silence_chunks == SILENCE_FINALIZE_CHUNKS:
                    if last_text:
                        finalize(last_text, last_language)
                    carry_audio = speech_tail.copy()
                    rollover_reference = last_raw_text
                    last_text = ""
                    last_language = ""
                    audio_buffer = np.array([], dtype=np.float32)
                if silence_chunks >= CONTEXT_EXPIRY_CHUNKS:
                    carry_audio = np.array([], dtype=np.float32)
                    rollover_reference = ""
                continue

            silence_chunks = 0
            prepared_chunks = []
            if len(audio_buffer) == 0 and len(carry_audio) > 0:
                prepared_chunks.append(carry_audio)
            if len(audio_buffer) == 0 and len(pre_roll) > 0:
                prepared_chunks.append(prepare_audio(pre_roll))
            prepared_chunks.append(prepare_audio(raw_audio))
            audio_buffer = np.concatenate((audio_buffer, *prepared_chunks))
            carry_audio = np.array([], dtype=np.float32)
            pre_roll = np.array([], dtype=np.float32)
            speech_tail = audio_buffer[-overlap_frames:].copy()
            language, source_text, raw_text = recognize(audio_buffer)
            last_raw_text = raw_text
            text = remove_rollover_overlap(rollover_reference, source_text)
            if text and text != last_text:
                last_text = text
                last_language = language
                translation_dispatcher.submit(text, language, partial=True)
            if len(audio_buffer) >= max_frames:
                if last_text:
                    finalize(last_text, last_language)
                rollover_reference = raw_text
                audio_buffer = audio_buffer[-overlap_frames:].copy()
                speech_tail = audio_buffer.copy()
                last_text = ""
                last_language = ""
    except Exception as exc:
        logging.exception("识别引擎失败")
        emit("error", str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
