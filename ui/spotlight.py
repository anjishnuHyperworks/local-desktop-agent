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

import html
import logging
import math
from typing import Optional
import ctypes
import time

from PyQt6.QtCore import Qt, QPoint, pyqtSignal, pyqtSlot, QObject, QTimer
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter, QPainterPath, QScreen
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QTextBrowser,
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
    toggle_window = pyqtSignal()
    abort = pyqtSignal()


# ---------------------------------------------------------------------------
# Styled input field
# ---------------------------------------------------------------------------
class _SpotlightInput(QLineEdit):
    """Single-line input with Spotlight aesthetics."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("Ahoy matey, what be the command?")
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
        self._bridge.toggle_window.connect(self._handle_toggle)
        self._bridge.abort.connect(self._on_abort)

        self._executing = False   # True while the coordinator loop is running
        self._hotkey_listener: Optional[pynput_keyboard.GlobalHotKeys] = None
        self._abort_listener: Optional[pynput_keyboard.Listener] = None

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

        inner = QVBoxLayout(self._container)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(0)

        # --- Input row (always visible) -----------------------------------
        input_row = QWidget(self._container)
        input_row.setFixedHeight(config.SPOTLIGHT_HEIGHT)
        row_layout = QHBoxLayout(input_row)
        row_layout.setContentsMargins(18, 0, 18, 0)
        row_layout.setSpacing(10)

        # Magnifying-glass icon
        icon_label = QLabel("⌕")
        icon_label.setFont(QFont("Segoe UI", 18))
        icon_label.setStyleSheet("color: rgba(255,255,255,0.45); padding-top:2px;")
        icon_label.setFixedWidth(28)
        row_layout.addWidget(icon_label)

        self._input = _SpotlightInput(input_row)
        self._input.returnPressed.connect(self._on_return_pressed)
        row_layout.addWidget(self._input)

        # Subtle "ESC to cancel" hint shown while executing
        self._esc_hint = QLabel("ESC to stop")
        self._esc_hint.setFont(QFont("Segoe UI", 10))
        self._esc_hint.setStyleSheet("color: rgba(255,255,255,0.30);")
        self._esc_hint.hide()
        row_layout.addWidget(self._esc_hint)

        inner.addWidget(input_row)

        # --- Response panel (chat answers; hidden while collapsed) --------
        self._separator = QFrame(self._container)
        self._separator.setFixedHeight(1)
        self._separator.setStyleSheet("background: rgba(255,255,255,0.08); border: none;")
        self._separator.hide()
        inner.addWidget(self._separator)

        self._response_panel = QTextBrowser(self._container)
        self._response_panel.setOpenExternalLinks(True)
        self._response_panel.setFont(QFont("Segoe UI", 11))
        self._response_panel.setFrameShape(QFrame.Shape.NoFrame)
        # Scrollbars are managed explicitly in _fit_window_to_content — the
        # vertical one is enabled only when the answer exceeds the height cap.
        self._response_panel.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._response_panel.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._response_panel.setStyleSheet("""
            QTextBrowser {
                background: transparent;
                border: none;
                color: #E6E6E6;
                padding: 8px 14px;
                selection-background-color: rgba(100, 149, 237, 0.5);
            }
            QScrollBar:vertical {
                background: transparent;
                width: 8px;
                margin: 4px 2px;
            }
            QScrollBar::handle:vertical {
                background: rgba(255, 255, 255, 0.18);
                border-radius: 4px;
                min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self._response_panel.hide()
        inner.addWidget(self._response_panel, stretch=1)

        outer.addWidget(self._container)
        self._center_on_screen()

    def _set_expanded(self, expanded: bool) -> None:
        """Grow the window downward to fit the response panel content, or
        collapse it back to the bare input pill."""
        self._separator.setVisible(expanded)
        self._response_panel.setVisible(expanded)
        if expanded:
            self._fit_window_to_content()
        else:
            self.setFixedSize(config.SPOTLIGHT_WIDTH, config.SPOTLIGHT_HEIGHT + 20)

    # Vertical chrome around the panel text: outer layout margins (8+8) +
    # separator (1) + panel CSS padding (8+8) + document margin slack (6).
    _PANEL_CHROME = 16 + 1 + 16 + 6

    def _fit_window_to_content(self) -> None:
        """Size the window to the answer, capped at the configured max —
        beyond that the panel scrolls."""
        doc = self._response_panel.document()
        # Panel text width: window minus outer margins (20) and CSS side padding (28)
        doc.setTextWidth(config.SPOTLIGHT_WIDTH - 48)
        content_h = math.ceil(doc.size().height())
        needed = config.SPOTLIGHT_HEIGHT + self._PANEL_CHROME + content_h
        capped = needed > config.SPOTLIGHT_MAX_EXPANDED_HEIGHT
        self._response_panel.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if capped
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setFixedSize(
            config.SPOTLIGHT_WIDTH,
            min(needed, config.SPOTLIGHT_MAX_EXPANDED_HEIGHT),
        )

    def _show_panel_html(self, html_text: str) -> None:
        self._response_panel.setHtml(html_text)
        self._set_expanded(True)

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
            self._bridge.toggle_window.emit()

        self._hotkey_listener = pynput_keyboard.GlobalHotKeys(
            {config.HOTKEY_COMBO: _on_activate}
        )
        self._hotkey_listener.daemon = True
        self._hotkey_listener.start()
        logger.debug("Global hotkey listener started")

    def _handle_toggle(self) -> None:
        """Safely check visibility and toggle on the Qt main thread with debouncing."""
        current_time = time.time()

        if not hasattr(self, "_last_toggle_time"):
            self._last_toggle_time = 0.0

        if current_time - self._last_toggle_time < 0.3:
            logger.debug("Hotkey bounce or lingering Ctrl key detected and ignored.")
            return

        self._last_toggle_time = current_time

        if self.isVisible():
            self.hide_spotlight()
        else:
            self.show_spotlight()

    def _start_abort_listener(self) -> None:
        """Start Esc listener - single press aborts."""
        def _on_press(key: pynput_keyboard.Key) -> None:
            if key == pynput_keyboard.Key.esc:
                logger.info("Esc pressed - emitting abort signal")
                self._bridge.abort.emit()

        self._abort_listener = pynput_keyboard.Listener(on_press=_on_press)
        self._abort_listener.daemon = True
        self._abort_listener.start()
        logger.debug("Abort (Esc) listener started")

    def _stop_abort_listener(self) -> None:
        if self._abort_listener and self._abort_listener.is_alive():
            self._abort_listener.stop()
            self._abort_listener = None
    # ------------------------------------------------------------------
    # Slot: Enter pressed in the input field
    # ------------------------------------------------------------------
    def _on_return_pressed(self) -> None:
        if self._executing:
            return
        text = self._input.text().strip()
        if not text:
            return
        logger.info("Command submitted: %r", text)
        # Stay visible in a "thinking" state until the coordinator classifies
        # the intent: chat answers render in the response panel, automation
        # hides the window first (see on_intent_classified).
        self._executing = True
        self._input.setReadOnly(True)
        self._show_panel_html(
            '<i style="color: rgba(255,255,255,0.45);">Thinking…</i>'
        )
        self.command_submitted.emit(text)

    # ------------------------------------------------------------------
    # Slots: coordinator feedback (queued connections from worker thread)
    # ------------------------------------------------------------------
    @pyqtSlot(str)
    def on_intent_classified(self, intent: str) -> None:
        """Pick the UX flow once the coordinator knows what the command is."""
        if intent == "CHAT":
            # Keep the thinking panel up; the answer arrives via
            # show_chat_response shortly.
            return
        # Automation needs the screen clear for screenshots — hide the window
        # and arm the Esc abort listener.
        self._set_expanded(False)
        self.hide_spotlight()
        self._set_executing(True)

    @pyqtSlot(str)
    def show_chat_response(self, text: str) -> None:
        """Render a chat answer in the response panel and hand focus back."""
        self._executing = False
        self._input.setReadOnly(False)
        if not self.isVisible():
            # User dismissed the window while waiting — bring the answer back.
            self._center_on_screen()
            self.show()
            self.raise_()
            QTimer.singleShot(0, self._force_native_window_focus)
        self._response_panel.setMarkdown(text)
        self._set_expanded(True)
        self._input.setFocus()
        self._input.selectAll()

    @pyqtSlot(str)
    def on_task_error(self, msg: str) -> None:
        """Show task errors in the panel when the window is up (chat flow);
        automation errors surface on the HUD instead."""
        self.mark_execution_complete()
        if self.isVisible():
            self._show_panel_html(
                f'<span style="color: #FF9B9B;">{html.escape(msg)}</span>'
            )
            self._input.setFocus()

    # ------------------------------------------------------------------
    # Slots: show / hide (always called on main thread via signal)
    # ------------------------------------------------------------------
    def show_spotlight(self) -> None:
        if self._executing:
            return
        self._set_expanded(False)
        self._center_on_screen()
        self._input.clear()

        self.show()
        self.raise_()

        # 0ms defers to the next event loop tick (after the native handle is realized)
        QTimer.singleShot(0, self._force_native_window_focus)
        logger.debug("Spotlight shown")

    def _force_native_window_focus(self) -> None:
        """Aggressively acquire focus using Win32 APIs."""
        try:
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
        self._input.setReadOnly(False)
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
