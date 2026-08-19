from __future__ import annotations

import os
import json
import logging
import re
import shutil
import sys
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QPoint,
    QLockFile,
    QSize,
    QTimer,
    Qt,
    QThread,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QIcon,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSlider,
    QSystemTrayIcon,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "LiveCaption"
MODEL_DIR = APP_DIR / "models"
SETTINGS_PATH = APP_DIR / "settings.json"
LOG_PATH = APP_DIR / "livecaption.log"
APP_VERSION = "0.9.3"
APP_NAME = "阿江字幕"
RESOURCE_DIR = Path(__file__).resolve().parent
ICON_PATH = RESOURCE_DIR / "app-icon.ico"
PYINSTALLER_BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", RESOURCE_DIR))
BUNDLED_MODEL_DIR = PYINSTALLER_BUNDLE_DIR / "models"
MODEL_ID = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
MODEL_NAME = "Paraformer-Large + SenseVoiceSmall + Qwen3-1.7B"
LANGUAGE_NAMES = {
    "zh": "中文",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
}
OVERLAY_CHARACTER_LIMIT = 30
SPLIT_STAGGER_MS = 5
SPLIT_DURATION_MS = 125
SPLIT_RISE_PX = 24
ANIMATION_FRAME_MS = 16

APP_DIR.mkdir(parents=True, exist_ok=True)
try:
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        encoding="utf-8",
    )
except OSError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
LOGGER = logging.getLogger("livecaption")


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOGGER.exception("读取设置失败")
        return {}


def latest_installed_model_dir(name: str) -> Path | None:
    install_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AjiangCaption"
    candidates = sorted(install_root.glob(f"*/models/{name}"), reverse=True)
    return next((path for path in candidates if path.exists()), None)


def bundled_model_dir(name: str) -> Path | None:
    local_path = BUNDLED_MODEL_DIR / name
    if local_path.exists():
        return local_path
    return latest_installed_model_dir(name)


def paraformer_model_is_ready() -> bool:
    bundled = bundled_model_dir("paraformer-large")
    if bundled is not None and find_model_path(bundled) is not None:
        return True
    marker = MODEL_DIR / "active-model.json"
    if not marker.exists():
        return find_model_path(MODEL_DIR) is not None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        marked_path = Path(data.get("model_path", ""))
        return (
            data.get("model_id") == MODEL_ID and marked_path.exists()
        ) or find_model_path(MODEL_DIR) is not None
    except (OSError, ValueError, TypeError):
        return find_model_path(MODEL_DIR) is not None


def model_is_ready() -> bool:
    sensevoice = bundled_model_dir("sensevoice-small")
    translation = bundled_model_dir("qwen3-1.7b")
    return (
        paraformer_model_is_ready()
        and sensevoice is not None
        and (sensevoice / "model.pt").exists()
        and (sensevoice / "model.pt").stat().st_size > 900_000_000
        and translation is not None
        and (translation / "Qwen3-1.7B-Q4_K_M.gguf").exists()
        and (translation / "Qwen3-1.7B-Q4_K_M.gguf").stat().st_size > 1_200_000_000
    )


def find_model_path(root: Path) -> Path | None:
    for config_name in ("configuration.json", "config.yaml", "model.pt"):
        for item in root.rglob(config_name) if root.exists() else ():
            if item.is_file():
                return item.parent
    return None


def active_model_dir() -> Path:
    bundled = bundled_model_dir("paraformer-large")
    if bundled is not None and find_model_path(bundled) is not None:
        return bundled
    return MODEL_DIR


def required_bundled_model_dir(name: str) -> Path:
    path = bundled_model_dir(name)
    if path is None:
        raise RuntimeError(f"缺少离线模型：{name}，请重新安装阿江字幕 {APP_VERSION}")
    return path


def cleanup_usage_data() -> None:
    logging.shutdown()
    disposable_paths = [
        LOG_PATH,
        APP_DIR / "history.tmp",
        Path(__file__).resolve().parent / "__pycache__",
    ]
    for path in disposable_paths:
        for attempt in range(3):
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                break
            except OSError:
                if attempt < 2:
                    time.sleep(0.1)


def clean_caption_text(text: str) -> str:
    text = re.sub(r"<\|[^|]+\|>", "", text)
    return " ".join(text.split())


def format_history_entry(entry: dict) -> str:
    stamp = entry["timestamp"]
    language = LANGUAGE_NAMES.get(entry.get("language", ""), "未知语种")
    source_text = entry.get("source_text", "")
    translation_text = entry.get("translation_text", "")
    lines = [f"[{stamp:%H:%M:%S}] [{language}] {source_text}"]
    if translation_text:
        lines.append(f"中文：{translation_text}")
    return "\n".join(lines)


def selected_translation_languages(enabled: bool, language_states: dict[str, bool]) -> set[str]:
    if not enabled:
        return set()
    return {code for code in ("en", "ja", "ko") if language_states.get(code, False)}


def create_lock_icon(locked: bool) -> QIcon:
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(Qt.GlobalColor.white, 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.drawRoundedRect(5, 10, 14, 11, 2, 2)
    shackle = QPainterPath()
    if locked:
        shackle.moveTo(8, 10)
        shackle.lineTo(8, 7)
        shackle.cubicTo(8, 2.5, 16, 2.5, 16, 7)
        shackle.lineTo(16, 10)
    else:
        shackle.moveTo(8, 10)
        shackle.lineTo(8, 7)
        shackle.cubicTo(8, 2.5, 16, 2.5, 16, 7)
        shackle.lineTo(19, 5)
    painter.drawPath(shackle)
    painter.drawLine(12, 14, 12, 17)
    painter.end()
    return QIcon(pixmap)


class OutlinedCaptionLabel(QLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._background_alpha = 210
        self._text_color = QColor("#FFFFFF")
        self._outline_width = 2.0
        self._animation_from_index = len(text)
        self._animation_started = time.perf_counter()
        self._animation_timer = QTimer(self)
        self._animation_timer.setInterval(ANIMATION_FRAME_MS)
        self._animation_timer.timeout.connect(self._advance_animation)

    def set_background_alpha(self, alpha: int) -> None:
        self._background_alpha = max(0, min(255, alpha))
        self.update()

    def set_text_color(self, color: QColor) -> None:
        self._text_color = QColor(color)
        self.update()

    def set_outline_width(self, width: int) -> None:
        self._outline_width = float(max(0, width))
        self.update()

    def set_animated_text(
        self,
        text: str,
        animate: bool = True,
        restart_animation: bool = False,
    ) -> None:
        previous = self.text()
        super().setText(text)
        if not animate:
            self._animation_from_index = len(text)
        elif restart_animation:
            self._animation_from_index = 0
        else:
            self._animation_from_index = min(len(previous), len(text))
        self._animation_started = time.perf_counter()
        if animate and self._animation_from_index < len(text):
            self._animation_timer.start()
        else:
            self._animation_timer.stop()
        self.update()

    def _advance_animation(self) -> None:
        animated_count = len(self.text()) - self._animation_from_index
        total_ms = SPLIT_DURATION_MS + max(0, animated_count - 1) * SPLIT_STAGGER_MS
        if (time.perf_counter() - self._animation_started) * 1000 >= total_ms:
            self._animation_timer.stop()
        self.update()

    def _character_progress(self, index: int) -> float:
        if index < self._animation_from_index:
            return 1.0
        elapsed_ms = (time.perf_counter() - self._animation_started) * 1000
        delay_ms = (index - self._animation_from_index) * SPLIT_STAGGER_MS
        linear = max(0.0, min(1.0, (elapsed_ms - delay_ms) / SPLIT_DURATION_MS))
        return 1.0 - (1.0 - linear) ** 3

    def _layout_lines(self, text: str, available_width: float):
        metrics = QFontMetricsF(self.font())
        lines = []
        current = []
        current_width = 0.0
        for index, character in enumerate(text):
            width = metrics.horizontalAdvance(character)
            if current and current_width + width > available_width:
                lines.append((current, current_width))
                current = []
                current_width = 0.0
            current.append((index, character, width))
            current_width += width
        if current:
            lines.append((current, current_width))
        return metrics, lines

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._background_alpha:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, self._background_alpha))
            painter.drawRoundedRect(self.rect(), 10, 10)
        rendered_text = self.text()
        if not rendered_text:
            return
        content_left = 18.0
        content_width = max(1.0, self.width() - 74.0)
        metrics, lines = self._layout_lines(rendered_text, content_width)
        line_height = metrics.height()
        total_height = line_height * len(lines)
        baseline = max(metrics.ascent(), (self.height() - total_height) / 2 + metrics.ascent())
        outline = QPen(QColor("#000000"), self._outline_width * 2)
        outline.setJoinStyle(Qt.PenJoinStyle.RoundJoin)

        for line_index, (characters, line_width) in enumerate(lines):
            x = content_left + (content_width - line_width) / 2
            line_baseline = baseline + line_index * line_height
            for index, character, character_width in characters:
                progress = self._character_progress(index)
                painter.setOpacity(progress)
                path = QPainterPath()
                path.addText(
                    x,
                    line_baseline + (1.0 - progress) * SPLIT_RISE_PX,
                    self.font(),
                    character,
                )
                if self._outline_width:
                    painter.strokePath(path, outline)
                painter.fillPath(path, self._text_color)
                x += character_width
        painter.setOpacity(1.0)


class CaptionWorker(QObject):
    caption = Signal(str, bool, str, str, str)
    status = Signal(str)
    progress = Signal(int, str)
    error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        source: str,
        translation_languages: set[str],
        model_id: str = MODEL_ID,
    ) -> None:
        super().__init__()
        self.source = source
        self.translation_languages = translation_languages
        self.model_id = model_id
        self._stop = threading.Event()
        self._process: subprocess.Popen | None = None
        self._process_lock = threading.Lock()
        self._reported_error = False

    def stop(self) -> None:
        self._stop.set()
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def run(self) -> None:
        try:
            command = self._engine_command()
            creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            with self._process_lock:
                self._process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation_flags,
                )
            assert self._process.stdout is not None
            for line in self._process.stdout:
                if self._stop.is_set():
                    break
                self._handle_engine_message(line)
            return_code = self._process.wait()
            if return_code != 0 and not self._stop.is_set() and not self._reported_error:
                raise RuntimeError(f"识别引擎异常退出（代码 {return_code}），请打开日志查看详情")
        except Exception as exc:
            LOGGER.exception("识别线程失败")
            self.error.emit(str(exc))
        finally:
            with self._process_lock:
                self._process = None
            self.finished.emit()

    def _engine_command(self) -> list[str]:
        if getattr(sys, "frozen", False):
            command = [str(Path(sys.executable).resolve()), "--engine"]
        else:
            command = [sys.executable, str(Path(__file__).with_name("engine.py"))]
        return command + [
            "--source", self.source,
            "--model-id", self.model_id,
            "--model-dir", str(active_model_dir()),
            "--sensevoice-model-dir", str(required_bundled_model_dir("sensevoice-small")),
            "--translation-model", str(
                required_bundled_model_dir("qwen3-1.7b") / "Qwen3-1.7B-Q4_K_M.gguf"
            ),
            "--translate-languages", ",".join(sorted(self.translation_languages)),
            "--log-path", str(LOG_PATH),
        ]

    def _handle_engine_message(self, line: str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            LOGGER.warning("识别引擎返回了无法解析的消息：%s", line.strip())
            return
        message_type = message.get("type")
        text = str(message.get("text", ""))
        if message_type == "caption" and text:
            text = clean_caption_text(text)
            if not text:
                return
            partial = bool(message.get("partial", False))
            language = str(message.get("language", "unknown"))
            source_text = clean_caption_text(str(message.get("source_text", text))) or text
            translation_text = clean_caption_text(str(message.get("translation_text", "")))
            self.caption.emit(text, partial, language, source_text, translation_text)
        elif message_type == "status" and text:
            self.status.emit(text)
        elif message_type == "progress":
            self.progress.emit(max(-1, min(100, int(message.get("percent", 0)))), text)
        elif message_type == "error" and text:
            self._reported_error = True
            self.error.emit(text)


class CaptionOverlay(QWidget):
    lockRequested = Signal(bool)
    geometryChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._drag_origin: QPoint | None = None
        self._resize_edges = ""
        self._resize_origin: QPoint | None = None
        self._resize_geometry = None
        self._locked = False
        self._resize_margin = 10
        self._background_alpha = 210
        self._chrome_visible = True
        self._target_caption = ""
        self.setWindowTitle(f"{APP_NAME} 字幕")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(720, 128)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.caption_view = OutlinedCaptionLabel("等待开始识别")
        self.caption_view.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.caption_view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption_view.setWordWrap(True)
        self._apply_caption_style()
        layout.addWidget(self.caption_view)
        self.lock_button = QToolButton()
        self.lock_button.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.lock_button.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.lock_button.setCheckable(True)
        self.lock_button.setFixedSize(36, 32)
        self.lock_button.setIconSize(QSize(24, 24))
        self.lock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lock_button.setStyleSheet(
            "QToolButton { background: rgba(20, 20, 20, 185); color: white; "
            "border: 1px solid rgba(255, 255, 255, 100); border-radius: 4px; }"
            "QToolButton:hover { background: rgba(60, 60, 60, 220); "
            "border-color: rgba(255, 255, 255, 190); }"
            "QToolButton:pressed { background: rgba(0, 0, 0, 230); }"
        )
        self.lock_button.clicked.connect(self._request_lock_change)
        self._update_lock_button()
        self._position_lock_button()
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(100)
        self._hover_timer.timeout.connect(self._update_locked_hover)
        self._hover_timer.start()

    def _request_lock_change(self, locked: bool) -> None:
        self.lockRequested.emit(locked)

    def set_caption_text(
        self,
        text: str,
        animate: bool = True,
        restart_animation: bool = False,
    ) -> None:
        if text == self._target_caption:
            return
        self._target_caption = text
        self.caption_view.set_animated_text(text, animate, restart_animation)

    def clear_caption_text(self) -> None:
        self._target_caption = ""
        self.caption_view.set_animated_text("", False)

    def _update_lock_button(self) -> None:
        self.lock_button.setIcon(create_lock_icon(self._locked))
        self.lock_button.setToolTip("解除悬浮窗锁定" if self._locked else "锁定悬浮窗位置和大小")

    def _apply_caption_style(self) -> None:
        alpha = self._background_alpha if self._chrome_visible else 0
        self.caption_view.set_background_alpha(alpha)

    def _set_chrome_visible(self, visible: bool) -> None:
        if self._chrome_visible == visible:
            self._position_lock_button()
            return
        self._chrome_visible = visible
        self._apply_caption_style()
        self._position_lock_button()

    def _update_locked_hover(self) -> None:
        if not self._locked:
            self._set_chrome_visible(True)
            return
        cursor = QCursor.pos()
        hovered = self.frameGeometry().contains(cursor)
        if self.lock_button.isVisible():
            hovered = hovered or self.lock_button.frameGeometry().contains(cursor)
        self._set_chrome_visible(hovered)

    def _position_lock_button(self) -> None:
        if not self.isVisible() or (self._locked and not self._chrome_visible):
            self.lock_button.hide()
            return
        position = self.mapToGlobal(
            QPoint(max(8, self.width() - self.lock_button.width() - 8), 8)
        )
        self.lock_button.move(position)
        self.lock_button.show()
        self.lock_button.raise_()

    def set_locked(self, locked: bool) -> None:
        self._locked = locked
        self._resize_edges = ""
        self._drag_origin = None
        self._resize_origin = None
        self._resize_geometry = None
        self.lock_button.blockSignals(True)
        self.lock_button.setChecked(locked)
        self.lock_button.blockSignals(False)
        self._update_lock_button()
        self._set_chrome_visible(not locked)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def _hit_test(self, position: QPoint) -> str:
        if self._locked:
            return ""
        margin = self._resize_margin
        edges = ""
        if position.x() <= margin:
            edges += "l"
        elif position.x() >= self.width() - margin:
            edges += "r"
        if position.y() <= margin:
            edges += "t"
        elif position.y() >= self.height() - margin:
            edges += "b"
        return edges

    def _update_cursor(self, edges: str) -> None:
        if edges in {"lt", "rb"}:
            cursor = Qt.CursorShape.SizeFDiagCursor
        elif edges in {"rt", "lb"}:
            cursor = Qt.CursorShape.SizeBDiagCursor
        elif edges in {"l", "r"}:
            cursor = Qt.CursorShape.SizeHorCursor
        elif edges in {"t", "b"}:
            cursor = Qt.CursorShape.SizeVerCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self.setCursor(cursor)

    def set_font_size(self, value: int) -> None:
        font = QFont("Microsoft YaHei", value)
        font.setBold(True)
        self.caption_view.setFont(font)

    def set_opacity(self, value: int) -> None:
        self._background_alpha = round(value * 2.55)
        self._apply_caption_style()

    def set_text_color(self, color: QColor) -> None:
        self.caption_view.set_text_color(color)

    def set_outline_width(self, width: int) -> None:
        self.caption_view.set_outline_width(width)

    def set_click_through(self, enabled: bool) -> None:
        flags = self.windowFlags() | Qt.WindowType.WindowStaysOnTopHint
        if enabled:
            flags |= Qt.WindowType.WindowTransparentForInput
        else:
            flags &= ~Qt.WindowType.WindowTransparentForInput
        self.setWindowFlags(flags)
        self.show()
        self._position_lock_button()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._locked:
                event.ignore()
                return
            edges = self._hit_test(event.position().toPoint())
            if edges:
                self._resize_edges = edges
                self._resize_origin = event.globalPosition().toPoint()
                self._resize_geometry = self.geometry()
            else:
                self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._locked:
            event.ignore()
            return
        if self._resize_edges and self._resize_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.globalPosition().toPoint() - self._resize_origin
            geometry = self._resize_geometry
            left, top = geometry.left(), geometry.top()
            right, bottom = geometry.right(), geometry.bottom()
            if "l" in self._resize_edges:
                left = min(left + delta.x(), right - 280)
            if "r" in self._resize_edges:
                right = max(right + delta.x(), left + 279)
            if "t" in self._resize_edges:
                top = min(top + delta.y(), bottom - 72)
            if "b" in self._resize_edges:
                bottom = max(bottom + delta.y(), top + 71)
            self.setGeometry(left, top, right - left + 1, bottom - top + 1)
            event.accept()
        elif self._drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()
        else:
            self._update_cursor(self._hit_test(event.position().toPoint()))
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_origin = None
        self._resize_edges = ""
        self._resize_origin = None
        self._resize_geometry = None
        self._update_cursor(self._hit_test(event.position().toPoint()))
        super().mouseReleaseEvent(event)

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._position_lock_button()
        self.geometryChanged.emit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if hasattr(self, "lock_button"):
            self._position_lock_button()
        self.geometryChanged.emit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._position_lock_button()

    def hideEvent(self, event) -> None:
        self.lock_button.hide()
        super().hideEvent(event)

    def closeEvent(self, event) -> None:
        self.lock_button.close()
        super().closeEvent(event)


class CaptionWindow(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self._thread: QThread | None = None
        self._worker: CaptionWorker | None = None
        self._history: list[dict] = []
        self._overlay_committed = ""
        self._overlay_page_start = -1
        self._exit_requested = False
        self.overlay = CaptionOverlay()
        self.overlay.lockRequested.connect(self._set_overlay_lock_from_overlay)
        self.overlay.geometryChanged.connect(self._on_overlay_geometry_changed)
        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        if ICON_PATH.exists():
            self.setWindowIcon(QIcon(str(ICON_PATH)))
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        saved_width = max(820, int(self.settings.get("console_width", 860)))
        saved_height = max(620, int(self.settings.get("console_height", 680)))
        if available is not None:
            saved_width = min(saved_width, max(820, available.width() - 80))
            saved_height = min(saved_height, max(620, available.height() - 80))
        self.resize(saved_width, saved_height)
        self.setMinimumSize(820, 620)
        self._build_ui()
        self._create_tray()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            "QWidget { background: #17191C; color: #E9EDF1; font-family: 'Microsoft YaHei'; font-size: 13px; }"
            "QGroupBox { border: 1px solid #343941; margin-top: 10px; padding: 12px; font-weight: 600; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #BFC6CE; }"
            "QLabel#appTitle { font-size: 20px; font-weight: 700; color: #FFFFFF; }"
            "QLabel#appSubtitle { color: #8F99A5; }"
            "QLabel#statusPill { background: #23362D; color: #91E6AF; border: 1px solid #315840; padding: 6px 10px; }"
            "QComboBox, QTextEdit { background: #202328; border: 1px solid #3A4048; padding: 7px; selection-background-color: #2D8A57; }"
            "QComboBox:hover, QTextEdit:focus { border-color: #68727E; }"
            "QPushButton { background: #2A2E34; border: 1px solid #454B54; padding: 8px 14px; }"
            "QPushButton:hover { background: #343941; border-color: #68727E; }"
            "QPushButton:disabled { color: #69717A; background: #22252A; border-color: #30343A; }"
            "QPushButton#primaryButton { background: #278553; border-color: #36A568; color: #FFFFFF; font-weight: 700; }"
            "QPushButton#primaryButton:hover { background: #309B61; }"
            "QPushButton#stopButton { color: #F2B7B7; }"
            "QProgressBar { background: #24282D; border: 1px solid #3A4048; height: 14px; text-align: center; }"
            "QProgressBar::chunk { background: #C89532; }"
            "QSlider::groove:horizontal { background: #353A41; height: 4px; }"
            "QSlider::handle:horizontal { background: #C8D0D8; border: 1px solid #59616B; width: 14px; margin: -6px 0; }"
            "QCheckBox { spacing: 8px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        icon_label = QLabel()
        if ICON_PATH.exists():
            icon_label.setPixmap(QIcon(str(ICON_PATH)).pixmap(46, 46))
        icon_label.setFixedSize(50, 50)
        header.addWidget(icon_label)
        title_layout = QVBoxLayout()
        title_layout.setSpacing(1)
        title = QLabel(APP_NAME)
        title.setObjectName("appTitle")
        subtitle = QLabel("本地实时语音字幕")
        subtitle.setObjectName("appSubtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        header.addLayout(title_layout)
        header.addStretch()

        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("statusPill")
        header.addWidget(self.status_label)
        layout.addLayout(header)

        model_group = QGroupBox("识别引擎")
        model_layout = QVBoxLayout(model_group)
        self.model_label = QLabel()
        model_layout.addWidget(self.model_label)

        self.model_progress = QProgressBar()
        self.model_progress.setRange(0, 100)
        self.model_progress.setValue(0)
        self.model_progress.setFormat("模型下载 %p%")
        self.model_progress.setVisible(False)
        model_layout.addWidget(self.model_progress)

        self.model_progress_label = QLabel()
        self.model_progress_label.setVisible(False)
        model_layout.addWidget(self.model_progress_label)
        layout.addWidget(model_group)

        settings_group = QGroupBox("字幕与音频")
        settings_layout = QHBoxLayout(settings_group)
        controls = QFormLayout()
        controls.setSpacing(10)
        secondary_controls = QFormLayout()
        secondary_controls.setSpacing(10)
        self.source_combo = QComboBox()
        self.source_combo.addItem("系统播放声音", "speaker")
        self.source_combo.addItem("麦克风", "microphone")
        self.source_combo.addItem("系统声音 + 麦克风", "mixed")
        saved_source = self.settings.get("source", "speaker")
        source_index = self.source_combo.findData(saved_source)
        self.source_combo.setCurrentIndex(max(0, source_index))
        self.source_combo.currentIndexChanged.connect(self._save_settings)
        controls.addRow("音频来源", self.source_combo)

        self.translation_enabled = QCheckBox("自动翻译为中文")
        self.translation_enabled.setChecked(bool(self.settings.get("translation_enabled", True)))
        self.translation_enabled.toggled.connect(self._update_translation_controls)
        self.translation_enabled.toggled.connect(self._save_settings)
        controls.addRow("自动翻译", self.translation_enabled)

        translation_languages = QWidget()
        translation_languages.setStyleSheet("background: transparent;")
        translation_languages_layout = QHBoxLayout(translation_languages)
        translation_languages_layout.setContentsMargins(0, 0, 0, 0)
        translation_languages_layout.setSpacing(14)
        self.translate_en = QCheckBox("英语")
        self.translate_ja = QCheckBox("日语")
        self.translate_ko = QCheckBox("韩语")
        for code, checkbox in (
            ("en", self.translate_en),
            ("ja", self.translate_ja),
            ("ko", self.translate_ko),
        ):
            checkbox.setChecked(bool(self.settings.get(f"translate_{code}", True)))
            checkbox.toggled.connect(self._save_settings)
            translation_languages_layout.addWidget(checkbox)
        translation_languages_layout.addStretch()
        controls.addRow("翻译语种", translation_languages)

        self.font_slider = QSlider(Qt.Orientation.Horizontal)
        self.font_slider.setRange(20, 64)
        self.font_slider.setValue(int(self.settings.get("font_size", 34)))
        self.font_slider.valueChanged.connect(self._update_font)
        self.font_slider.valueChanged.connect(self._save_settings)
        controls.addRow("字幕大小", self.font_slider)

        self.text_color = QColor(str(self.settings.get("text_color", "#FFFFFF")))
        if not self.text_color.isValid():
            self.text_color = QColor("#FFFFFF")
        self.text_color_button = QPushButton()
        self.text_color_button.setFixedHeight(26)
        self.text_color_button.setToolTip("选择字幕颜色")
        self.text_color_button.clicked.connect(self._choose_text_color)
        self._update_color_swatch()
        controls.addRow("字幕颜色", self.text_color_button)

        self.outline_slider = QSlider(Qt.Orientation.Horizontal)
        self.outline_slider.setRange(0, 6)
        self.outline_slider.setValue(int(self.settings.get("outline_width", 2)))
        self.outline_slider.valueChanged.connect(self._update_outline)
        self.outline_slider.valueChanged.connect(self._save_settings)
        controls.addRow("字体描边", self.outline_slider)

        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(40, 100)
        self.opacity_slider.setValue(int(self.settings.get("opacity", 82)))
        self.opacity_slider.valueChanged.connect(self._update_opacity)
        self.opacity_slider.valueChanged.connect(self._save_settings)
        secondary_controls.addRow("字幕透明度", self.opacity_slider)

        self.width_slider = QSlider(Qt.Orientation.Horizontal)
        self.width_slider.setRange(360, 1200)
        self.width_slider.setValue(int(self.settings.get("overlay_width", 720)))
        self.width_slider.valueChanged.connect(self._update_width)
        self.width_slider.valueChanged.connect(self._save_settings)
        secondary_controls.addRow("悬浮窗宽度", self.width_slider)

        self.height_slider = QSlider(Qt.Orientation.Horizontal)
        self.height_slider.setRange(72, 600)
        self.height_slider.setValue(int(self.settings.get("overlay_height", 128)))
        self.height_slider.valueChanged.connect(self._update_height)
        self.height_slider.valueChanged.connect(self._save_settings)
        secondary_controls.addRow("悬浮窗高度", self.height_slider)

        self.click_through = QCheckBox("鼠标穿透字幕区域")
        self.click_through.toggled.connect(self._toggle_click_through)
        self.click_through.toggled.connect(self._save_settings)
        secondary_controls.addRow("交互", self.click_through)

        self.lock_overlay = QCheckBox("锁定悬浮窗位置和大小")
        self.lock_overlay.setChecked(bool(self.settings.get("overlay_locked", False)))
        self.lock_overlay.toggled.connect(self._toggle_overlay_lock)
        self.lock_overlay.toggled.connect(self._save_settings)
        secondary_controls.addRow("固定", self.lock_overlay)

        self.autostart_checkbox = QCheckBox("开机启动并自动识别")
        self.autostart_checkbox.setChecked(self._autostart_enabled())
        self.autostart_checkbox.toggled.connect(self._toggle_autostart)
        secondary_controls.addRow("启动", self.autostart_checkbox)
        settings_layout.addLayout(controls, 1)
        settings_layout.addLayout(secondary_controls, 1)
        layout.addWidget(settings_group)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始识别")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_caption)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_caption)
        buttons.addWidget(self.stop_button)
        export_button = QPushButton("导出 TXT")
        export_button.clicked.connect(self.export_history)
        buttons.addWidget(export_button)
        clear_button = QPushButton("清空记录")
        clear_button.clicked.connect(self.clear_history)
        buttons.addWidget(clear_button)
        log_button = QPushButton("打开日志")
        log_button.clicked.connect(self.open_log)
        buttons.addWidget(log_button)
        layout.addLayout(buttons)

        self.history_view = QTextEdit()
        self.history_view.setReadOnly(True)
        self.history_view.setPlaceholderText("识别出的字幕会记录在这里")
        history_group = QGroupBox("字幕记录")
        history_layout = QVBoxLayout(history_group)
        history_layout.addWidget(self.history_view)
        layout.addWidget(history_group, 1)

        brand_label = QLabel("抖音阿江")
        brand_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        brand_label.setStyleSheet("color: #727B85; padding-right: 4px;")
        layout.addWidget(brand_label)
        self._update_font(self.font_slider.value())
        self.overlay.set_text_color(self.text_color)
        self._update_outline(self.outline_slider.value())
        self._update_opacity(self.opacity_slider.value())
        self._update_model_label()
        self._update_translation_controls(self.translation_enabled.isChecked())
        self._restore_overlay_geometry()
        self.click_through.setChecked(bool(self.settings.get("click_through", False)))
        if self.lock_overlay.isChecked():
            self._toggle_overlay_lock(True)
        else:
            self.overlay.set_locked(False)

    def _update_font(self, value: int) -> None:
        self.overlay.set_font_size(value)

    def _update_color_swatch(self) -> None:
        self.text_color_button.setStyleSheet(
            f"QPushButton {{ background: {self.text_color.name()}; "
            "border: 1px solid #777777; border-radius: 3px; }}"
            "QPushButton:hover { border: 2px solid #BBBBBB; }"
        )

    def _choose_text_color(self) -> None:
        color = QColorDialog.getColor(self.text_color, self, "选择字幕颜色")
        if not color.isValid():
            return
        self.text_color = color
        self.overlay.set_text_color(color)
        self._update_color_swatch()
        self._save_settings()

    def _update_outline(self, value: int) -> None:
        self.overlay.set_outline_width(value)

    def _update_opacity(self, value: int) -> None:
        self.overlay.set_opacity(value)

    def _update_width(self, value: int) -> None:
        self.overlay.resize(value, self.overlay.height())

    def _update_height(self, value: int) -> None:
        self.overlay.resize(self.overlay.width(), value)

    def _update_model_label(self) -> None:
        if model_is_ready():
            self.model_label.setText("本地模型：已就绪（离线可用）")
        else:
            self.model_label.setText(f"本地模型不完整：需要 {MODEL_NAME}，请重新安装")

    def _restore_overlay_geometry(self) -> None:
        self.overlay.resize(self.width_slider.value(), self.height_slider.value())
        if "overlay_x" in self.settings and "overlay_y" in self.settings:
            self.overlay.move(int(self.settings["overlay_x"]), int(self.settings["overlay_y"]))
        else:
            self._position_overlay()

    def _save_settings(self) -> None:
        if not hasattr(self, "source_combo"):
            return
        self.settings.update(
            {
                "source": self.source_combo.currentData(),
                "translation_enabled": self.translation_enabled.isChecked(),
                "translate_en": self.translate_en.isChecked(),
                "translate_ja": self.translate_ja.isChecked(),
                "translate_ko": self.translate_ko.isChecked(),
                "font_size": self.font_slider.value(),
                "text_color": self.text_color.name(),
                "outline_width": self.outline_slider.value(),
                "opacity": self.opacity_slider.value(),
                "overlay_width": self.overlay.width(),
                "overlay_height": self.overlay.height(),
                "click_through": self.click_through.isChecked(),
                "overlay_locked": self.lock_overlay.isChecked(),
                "overlay_x": self.overlay.x(),
                "overlay_y": self.overlay.y(),
                "overlay_visible": self.overlay.isVisible(),
                "console_width": self.width(),
                "console_height": self.height(),
            }
        )
        try:
            SETTINGS_PATH.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            LOGGER.exception("保存设置失败")

    def _update_translation_controls(self, enabled: bool) -> None:
        for checkbox in (self.translate_en, self.translate_ja, self.translate_ko):
            checkbox.setEnabled(enabled and self._thread is None)

    def _translation_languages(self) -> set[str]:
        return selected_translation_languages(
            self.translation_enabled.isChecked(),
            {
                "en": self.translate_en.isChecked(),
                "ja": self.translate_ja.isChecked(),
                "ko": self.translate_ko.isChecked(),
            },
        )

    def _position_overlay(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.overlay.move(
            area.left() + (area.width() - self.overlay.width()) // 2,
            area.top() + area.height() - self.overlay.height() - 70,
        )

    def _toggle_click_through(self, enabled: bool) -> None:
        if self.overlay._locked and not enabled:
            self.click_through.blockSignals(True)
            self.click_through.setChecked(True)
            self.click_through.blockSignals(False)
            return
        self.overlay.set_click_through(enabled)

    def _toggle_overlay_lock(self, locked: bool) -> None:
        self.overlay.set_locked(locked)
        self.click_through.blockSignals(True)
        self.click_through.setChecked(locked)
        self.click_through.blockSignals(False)
        self.overlay.set_click_through(locked)

    def _set_overlay_lock_from_overlay(self, locked: bool) -> None:
        self.lock_overlay.setChecked(locked)

    def _on_overlay_geometry_changed(self) -> None:
        if not hasattr(self, "width_slider"):
            return
        width = max(self.width_slider.minimum(), min(self.width_slider.maximum(), self.overlay.width()))
        self.width_slider.blockSignals(True)
        self.width_slider.setValue(width)
        self.width_slider.blockSignals(False)
        height = max(self.height_slider.minimum(), min(self.height_slider.maximum(), self.overlay.height()))
        self.height_slider.blockSignals(True)
        self.height_slider.setValue(height)
        self.height_slider.blockSignals(False)
        self._save_settings()

    def _autostart_command(self) -> str:
        if getattr(sys, "frozen", False):
            return f'"{Path(sys.executable).resolve()}" --autostart'
        return f'"{Path(sys.executable).resolve()}" "{Path(__file__).resolve()}" --autostart'

    def _autostart_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
            ) as key:
                value, _ = winreg.QueryValueEx(key, APP_NAME)
                return bool(value)
        except (FileNotFoundError, OSError):
            return False

    def _toggle_autostart(self, enabled: bool) -> None:
        if sys.platform != "win32":
            return
        try:
            import winreg

            run_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_path) as key:
                if enabled:
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self._autostart_command())
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            self._save_settings()
            self.status_label.setText("开机启动设置已更新")
        except OSError as exc:
            LOGGER.exception("设置开机启动失败")
            self.autostart_checkbox.blockSignals(True)
            self.autostart_checkbox.setChecked(not enabled)
            self.autostart_checkbox.blockSignals(False)
            QMessageBox.warning(self, "设置失败", f"无法更新开机启动设置：{exc}")

    def _on_status(self, message: str) -> None:
        self.status_label.setText(message)
        if "加载本地识别模型" in message:
            self.model_label.setText("本地模型：正在加载或下载...")
        if "准备模型下载" in message:
            self.model_label.setText("本地模型：正在下载...")
            self.model_progress.setRange(0, 100)
            self.model_progress.setValue(0)
            self.model_progress.setVisible(True)
            self.model_progress_label.setText("正在获取 ModelScope 模型文件清单...")
            self.model_progress_label.setVisible(True)
        elif message == "识别中":
            self._ensure_overlay_visible()
            self.model_progress.setVisible(False)
            self.model_progress_label.setVisible(False)
            self._update_model_label()

    def _on_progress(self, percent: int, detail: str) -> None:
        if percent < 0:
            self.model_progress.setRange(0, 0)
            self.model_progress.setFormat("正在下载模型...")
        else:
            self.model_progress.setRange(0, 100)
            self.model_progress.setValue(percent)
            self.model_progress.setFormat("模型下载 %p%")
        self.model_progress.setVisible(True)
        self.model_progress_label.setText(detail)
        self.model_progress_label.setVisible(True)

    def start_caption(self) -> None:
        if self._thread is not None:
            return
        self._overlay_committed = ""
        self._overlay_page_start = -1
        self.overlay.set_caption_text("抖音阿江", animate=False)
        self._ensure_overlay_visible()
        self._thread = QThread(self)
        self._worker = CaptionWorker(
            self.source_combo.currentData(),
            self._translation_languages(),
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.caption.connect(self.on_caption)
        self._worker.status.connect(self._on_status)
        self._worker.progress.connect(self._on_progress)
        self._worker.error.connect(self.on_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._caption_finished)
        self._thread.start()
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.source_combo.setEnabled(False)
        self.translation_enabled.setEnabled(False)
        self._update_translation_controls(self.translation_enabled.isChecked())

    def stop_caption(self) -> None:
        if self._worker is not None:
            self._worker.stop()
        self.status_label.setText("正在停止...")

    def _caption_finished(self) -> None:
        self._thread = None
        self._worker = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.source_combo.setEnabled(True)
        self.translation_enabled.setEnabled(True)
        self._update_translation_controls(self.translation_enabled.isChecked())
        self.status_label.setText("已停止")

    def _ensure_overlay_visible(self) -> None:
        if not self.overlay.isVisible():
            self.overlay.show()
        self.overlay.raise_()

    def _show_overlay_page(self, text: str) -> None:
        if not text:
            self._overlay_page_start = -1
            self.overlay.clear_caption_text()
            return
        page_start = ((len(text) - 1) // OVERLAY_CHARACTER_LIMIT) * OVERLAY_CHARACTER_LIMIT
        page = text[page_start:]
        page_changed = page_start != self._overlay_page_start
        self._overlay_page_start = page_start
        self.overlay.set_caption_text(page, restart_animation=page_changed)

    def on_caption(
        self,
        text: str,
        partial: bool,
        language: str,
        source_text: str,
        translation_text: str,
    ) -> None:
        self._show_overlay_page(self._overlay_committed + text)
        if not partial:
            self._overlay_committed += text
            now = datetime.now()
            entry = {
                "timestamp": now,
                "language": language,
                "source_text": source_text or text,
                "translation_text": translation_text,
                "display_text": text,
            }
            self._history.append(entry)
            self.history_view.append(format_history_entry(entry))

    def on_error(self, message: str) -> None:
        self.status_label.setText("识别失败")
        self._update_model_label()
        LOGGER.error("识别失败：%s", message)
        QMessageBox.critical(self, "识别失败", message)

    def clear_history(self) -> None:
        self._history.clear()
        self.history_view.clear()

    def open_log(self) -> None:
        try:
            os.startfile(str(APP_DIR))
        except OSError as exc:
            LOGGER.exception("打开日志目录失败")
            QMessageBox.warning(self, "打开失败", f"无法打开日志目录：{exc}")

    def export_history(self) -> None:
        if not self._history:
            QMessageBox.information(self, "没有记录", "当前没有可导出的字幕记录。")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出字幕", f"字幕记录-{datetime.now():%Y%m%d-%H%M%S}.txt", "TXT 文件 (*.txt)"
        )
        if filename:
            Path(filename).write_text(
                "\n\n".join(format_history_entry(entry) for entry in self._history),
                encoding="utf-8",
            )

    def _create_tray(self) -> None:
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QMenu

        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(QIcon(str(ICON_PATH)))
        self.tray.setToolTip(f"{APP_NAME} 中文字幕")
        menu = QMenu()
        show_action = QAction("显示控制台", self)
        show_action.triggered.connect(self._show_console)
        menu.addAction(show_action)
        toggle_action = QAction("显示/隐藏字幕", self)
        toggle_action.triggered.connect(self._toggle_overlay)
        menu.addAction(toggle_action)
        menu.addSeparator()
        exit_action = QAction(f"退出 {APP_NAME}", self)
        exit_action.triggered.connect(self._exit_application)
        menu.addAction(exit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _show_console(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _toggle_overlay(self) -> None:
        self.overlay.setVisible(not self.overlay.isVisible())
        self._save_settings()

    def _tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_console()

    def _exit_application(self) -> None:
        self._exit_requested = True
        if self._worker is not None:
            self._worker.stop()
        self._save_settings()
        self.tray.hide()
        if self.overlay is not None:
            self.overlay.hide()
        self.close()
        QApplication.quit()

    def closeEvent(self, event) -> None:
        if not self._exit_requested:
            self._save_settings()
            self.hide()
            event.ignore()
            return
        if self._worker is not None:
            self._worker.stop()
        self._save_settings()
        if hasattr(self, "tray"):
            self.tray.hide()
        self.overlay.close()
        event.accept()


def main(app: QApplication | None = None) -> None:
    if "--engine" in sys.argv:
        sys.argv.remove("--engine")
        from engine import main as engine_main

        sys.exit(engine_main())
    startup_started = time.perf_counter()
    LOGGER.info("%s %s 正在启动", APP_NAME, APP_VERSION)
    owns_event_loop = app is None
    if app is None:
        app = QApplication(sys.argv)
    LOGGER.info("Qt 应用对象已就绪")
    app.setApplicationName(APP_NAME)
    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setQuitOnLastWindowClosed(False)
    LOGGER.info("Qt 应用属性已设置")
    lock = QLockFile(str(APP_DIR / "instance.lock"))
    lock.setStaleLockTime(5_000)
    if not lock.tryLock(100):
        lock.removeStaleLockFile()
        if not lock.tryLock(100):
            QMessageBox.information(None, APP_NAME, "软件已经在运行中，请从通知区域打开控制台。")
            return
    LOGGER.info("单实例锁已获取")
    window = CaptionWindow()
    LOGGER.info("主窗口对象已创建")
    autostart = "--autostart" in sys.argv
    if autostart:
        window.hide()
    else:
        LOGGER.info("准备显示控制台")
        window.show()
        LOGGER.info("控制台已显示")
    if window.settings.get("overlay_visible", True):
        window.overlay.show()
    LOGGER.info("主窗口已显示，启动耗时 %.3f 秒", time.perf_counter() - startup_started)
    if autostart:
        QTimer.singleShot(300, window.start_caption)
    if owns_event_loop:
        try:
            exit_code = app.exec()
        finally:
            cleanup_usage_data()
        sys.exit(exit_code)


if __name__ == "__main__":
    main()

