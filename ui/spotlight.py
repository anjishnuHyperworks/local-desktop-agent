"""
Phase 2: Floating Spotlight UI

A borderless, semi-transparent Spotlight/Raycast-style input window triggered by
Ctrl+Space. All pynput-to-Qt cross-thread communication goes through pyqtSignal so
Qt objects are only touched from the main thread.

Threading model:
  - pynput GlobalHotKeys runs in its own daemon thread (never touches Qt directly).
  - It emits _show_signal / _hide_signal which are queued across thread boundaries.
  - The Esc abort listener runs in a second pynput thread; it emits _abort_signal.
  - The coordinator (added in Phase 4) connects to command_submitted and abort_requested.
"""

import logging
import threading
from typing import Optional

from PyQt6.QtCore import Qt, QPoint, pyqtSignal, QObject, QTimer
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPainterPath, QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from pynput import keyboard as pynput_keyboard

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal signal bridge — lives on the main thread so signals are always
# dispatched through Qt's queued connection mechanism.
# ---------------------------------------------------------------------------
class _SignalBridge(QObject):
    show_window = pyqtSignal()
    hide_window = pyqtSignal()
    abort = pyqtSignal()


# ---------------------------------------------------------------------------
# Styled input field
# ---------------------------------------------------------------------------
class _SpotlightInput(QLineEdit):
    """Single-line input with Spotlight aesthetics."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("What would you like to do?")
        self.setFont(QFont("Segoe UI", 16, QFont.Weight.Normal))
        self.setStyleSheet("""
            QLineEdit {
                background: transparent;
                border: none;
                color: #F0F0F0;
                padding: 0px 4px;
                selection-background-color: rgba(100, 149, 237, 0.5);
            }
        """)
        self.setMinimumHeight(40)


# ---------------------------------------------------------------------------
# Main spotlight window
# ---------------------------------------------------------------------------
class SpotlightWindow(QWidget):
    """
    Floating command-input overlay.

    Signals (safe to connect from any thread via Qt queued connections):
        command_submitted(str)  — emitted on Enter with non-empty text
        abort_requested()       — emitted when Esc is held during execution
    """

    command_submitted = pyqtSignal(str)
    abort_requested = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._bridge = _SignalBridge()
        self._bridge.show_window.connect(self.show_spotlight)
        self._bridge.hide_window.connect(self.hide_spotlight)
        self._bridge.abort.connect(self._on_abort)

        self._executing = False   # True while the coordinator loop is running
        self._hotkey_listener: Optional[pynput_keyboard.GlobalHotKeys] = None
        self._abort_listener: Optional[pynput_keyboard.Listener] = None
        self._esc_press_count = 0
        self._esc_lock = threading.Lock()

        self._build_ui()
        self._start_hotkey_listener()
        logger.info("SpotlightWindow initialised — hotkey: Ctrl+Space")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool          # keeps it out of the taskbar
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(config.SPOTLIGHT_OPACITY)
        self.setFixedSize(config.SPOTLIGHT_WIDTH, config.SPOTLIGHT_HEIGHT + 20)

        # Outer layout adds vertical breathing room for the drop shadow
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        # Pill-shaped container
        self._container = QWidget(self)
        self._container.setObjectName("container")
        self._container.setStyleSheet("""
            QWidget#container {
                background: rgba(28, 28, 30, 0.93);
                border-radius: 14px;
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 160))
        self._container.setGraphicsEffect(shadow)

        inner = QHBoxLayout(self._container)
        inner.setContentsMargins(18, 0, 18, 0)
        inner.setSpacing(10)

        # Magnifying-glass icon
        icon_label = QLabel("⌕")
        icon_label.setFont(QFont("Segoe UI", 18))
        icon_label.setStyleSheet("color: rgba(255,255,255,0.45); padding-top:2px;")
        icon_label.setFixedWidth(28)
        inner.addWidget(icon_label)

        self._input = _SpotlightInput(self._container)
        self._input.returnPressed.connect(self._on_return_pressed)
        inner.addWidget(self._input)

        # Subtle "ESC to cancel" hint shown while executing
        self._esc_hint = QLabel("ESC to stop")
        self._esc_hint.setFont(QFont("Segoe UI", 10))
        self._esc_hint.setStyleSheet("color: rgba(255,255,255,0.30);")
        self._esc_hint.hide()
        inner.addWidget(self._esc_hint)

        outer.addWidget(self._container)
        self._center_on_screen()

    def _center_on_screen(self) -> None:
        screen: QScreen = QApplication.primaryScreen()
        geom = screen.geometry()
        # Position in upper-third, horizontally centred — classic Spotlight placement
        x = (geom.width() - self.width()) // 2
        y = int(geom.height() * 0.28)
        self.move(x, y)

    # ------------------------------------------------------------------
    # pynput listeners — run in daemon threads, never touch Qt directly
    # ------------------------------------------------------------------
    def _start_hotkey_listener(self) -> None:
        def _on_activate() -> None:
            logger.debug("Hotkey fired")
            if self.isVisible():
                self._bridge.hide_window.emit()
            else:
                self._bridge.show_window.emit()

        self._hotkey_listener = pynput_keyboard.GlobalHotKeys(
            {"<ctrl>+<space>": _on_activate}
        )
        self._hotkey_listener.daemon = True
        self._hotkey_listener.start()
        logger.debug("Global hotkey listener started")

    def _start_abort_listener(self) -> None:
        """Start an Esc listener active only during execution."""
        esc_key = pynput_keyboard.Key.esc

        def _on_press(key: pynput_keyboard.Key) -> None:
            if key == esc_key:
                with self._esc_lock:
                    self._esc_press_count += 1
                    count = self._esc_press_count
                if count >= 1:
                    logger.info("Esc pressed — emitting abort signal")
                    self._bridge.abort.emit()

        def _on_release(key: pynput_keyboard.Key) -> None:
            pass

        self._abort_listener = pynput_keyboard.Listener(
            on_press=_on_press, on_release=_on_release
        )
        self._abort_listener.daemon = True
        self._abort_listener.start()
        logger.debug("Abort (Esc) listener started")

    def _stop_abort_listener(self) -> None:
        if self._abort_listener and self._abort_listener.is_alive():
            self._abort_listener.stop()
            self._abort_listener = None
        with self._esc_lock:
            self._esc_press_count = 0

    # ------------------------------------------------------------------
    # Slot: Enter pressed in the input field
    # ------------------------------------------------------------------
    def _on_return_pressed(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        logger.info("Command submitted: %r", text)
        self._input.clear()
        self.hide_spotlight()
        self._set_executing(True)
        self.command_submitted.emit(text)

    # ------------------------------------------------------------------
    # Slots: show / hide (always called on main thread via signal)
    # ------------------------------------------------------------------
    def show_spotlight(self) -> None:
        if self._executing:
            return
        self._center_on_screen()
        self._input.clear()

        self.show()
        self.raise_()

        # Small delay + native focus forcing to beat Windows focus protection
        QTimer.singleShot(50, self._force_native_window_focus)
        logger.debug("Spotlight shown")

    def _force_native_window_focus(self) -> None:
        """Aggressively acquire focus using Win32 APIs."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hwnd = int(self.winId())
            foreground_hwnd = user32.GetForegroundWindow()

            if foreground_hwnd != hwnd:
                foreground_thread = user32.GetWindowThreadProcessId(foreground_hwnd, None)
                current_thread = kernel32.GetCurrentThreadId()

                if foreground_thread != current_thread:
                    user32.AttachThreadInput(current_thread, foreground_thread, True)
                    user32.ShowWindow(hwnd, 5)  # SW_SHOW
                    user32.SetForegroundWindow(hwnd)
                    user32.SetFocus(hwnd)
                    user32.AttachThreadInput(current_thread, foreground_thread, False)
                else:
                    user32.SetForegroundWindow(hwnd)
                    user32.SetFocus(hwnd)

            # Qt reinforcement
            QApplication.setActiveWindow(self)
            self.activateWindow()
            self.raise_()
            self._input.setFocus()
            self._input.activateWindow()
            self._input.selectAll()
            logger.debug("Spotlight focus successfully forced via Win32 API")
        except Exception as e:
            logger.warning(f"Native focus injection failed (non-critical): {e}")
            self.activateWindow()
            self.raise_()
            self._input.setFocus()
            self._input.selectAll()

    def hide_spotlight(self) -> None:
        self.hide()
        logger.debug("Spotlight hidden")

    # ------------------------------------------------------------------
    # Execution state management (called from main thread)
    # ------------------------------------------------------------------
    def _set_executing(self, executing: bool) -> None:
        self._executing = executing
        if executing:
            self._esc_hint.show()
            self._start_abort_listener()
        else:
            self._esc_hint.hide()
            self._stop_abort_listener()

    def mark_execution_complete(self) -> None:
        """Call this (via signal) when the coordinator loop finishes."""
        self._set_executing(False)
        logger.debug("Execution marked complete")

    # ------------------------------------------------------------------
    # Abort slot
    # ------------------------------------------------------------------
    def _on_abort(self) -> None:
        logger.info("Abort requested by user (Esc)")
        self._set_executing(False)
        self.abort_requested.emit()
        # Show the window again so the user can issue a new command
        self.show_spotlight()

    # ------------------------------------------------------------------
    # Qt key events — allow Esc to close from the input field too
    # ------------------------------------------------------------------
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide_spotlight()
        else:
            super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Custom painting — ensures translucency works on Windows
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        super().paintEvent(event)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        logger.info("SpotlightWindow closing — stopping listeners")
        if self._hotkey_listener:
            self._hotkey_listener.stop()
        self._stop_abort_listener()
        super().closeEvent(event)
