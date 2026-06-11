"""
Automation status HUD — a small click-through pill shown while the agent
controls the desktop.

The spotlight window must hide during automation (it would pollute the
screenshots sent to the vision model), which previously left the user with no
on-screen feedback at all — status messages and the "ESC to stop" hint only
reached the terminal log.  This widget fills that gap:

  - Click-through and non-activating: it never steals focus from the window
    the agent is operating, and injected clicks pass straight through it.
  - Best-effort excluded from screen capture via SetWindowDisplayAffinity
    (WDA_EXCLUDEFROMCAPTURE, Windows 10 2004+) so it does not appear in the
    screenshots sent to the model.
  - Only displays while a task is active (activate() gates show_status), so
    chat commands never flash it.  The final message lingers for
    config.HUD_LINGER_MS, then the pill hides itself.

All slots must be invoked on the Qt main thread — queued signal connections
from the coordinator's worker thread take care of this.
"""

import ctypes
import logging

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

import config

logger = logging.getLogger(__name__)

WDA_EXCLUDEFROMCAPTURE = 0x00000011


class StatusHUD(QWidget):
    """Floating status pill for automation runs.

    Public API (call via queued signals or from the main thread):
        activate()          — arm the HUD and show it ("Working…")
        show_status(msg)    — update the status line (ignored while inactive)
        finish(msg)         — show a final message, then auto-hide
    """

    def __init__(self) -> None:
        super().__init__()
        self._active = False
        self._linger_timer = QTimer(self)
        self._linger_timer.setSingleShot(True)
        self._linger_timer.timeout.connect(self._on_linger_expired)
        self._build_ui()
        logger.info("StatusHUD initialised")

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(config.HUD_WIDTH, config.HUD_HEIGHT + 16)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 10)

        self._container = QWidget(self)
        self._container.setObjectName("hud")
        self._container.setStyleSheet("""
            QWidget#hud {
                background: rgba(28, 28, 30, 0.93);
                border-radius: 12px;
                border: 1px solid rgba(255, 255, 255, 0.12);
            }
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 140))
        self._container.setGraphicsEffect(shadow)

        inner = QHBoxLayout(self._container)
        inner.setContentsMargins(16, 0, 16, 0)
        inner.setSpacing(12)

        self._status_label = QLabel("")
        self._status_label.setFont(QFont("Segoe UI", 10))
        self._status_label.setStyleSheet("color: #E6E6E6;")
        inner.addWidget(self._status_label, stretch=1)

        self._esc_label = QLabel("ESC to stop")
        self._esc_label.setFont(QFont("Segoe UI", 9))
        self._esc_label.setStyleSheet("color: rgba(255,255,255,0.35);")
        inner.addWidget(self._esc_label)

        outer.addWidget(self._container)

    def _position(self) -> None:
        geom = QApplication.primaryScreen().geometry()
        x = (geom.width() - self.width()) // 2
        y = geom.height() - self.height() - config.HUD_MARGIN_BOTTOM
        self.move(x, y)

    # ------------------------------------------------------------------
    # Public slots
    # ------------------------------------------------------------------
    def activate(self) -> None:
        """Arm the HUD at the start of an automation run."""
        self._active = True
        self._linger_timer.stop()
        self._esc_label.show()
        self._set_text("Working…")
        self._position()
        self.show()

    def show_status(self, message: str) -> None:
        """Live progress line; no-op unless a run is active."""
        if not self._active:
            return
        self._linger_timer.stop()
        self._set_text(message)
        if not self.isVisible():
            self._position()
            self.show()

    def finish(self, message: str) -> None:
        """Final status — lingers briefly, then the pill hides itself."""
        if not self._active:
            return
        self._esc_label.hide()
        self._set_text(message)
        self._linger_timer.start(config.HUD_LINGER_MS)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _set_text(self, message: str) -> None:
        metrics = self._status_label.fontMetrics()
        available = config.HUD_WIDTH - 160  # margins + esc hint allowance
        self._status_label.setText(
            metrics.elidedText(message, Qt.TextElideMode.ElideRight, available)
        )

    def _on_linger_expired(self) -> None:
        self._active = False
        self.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._exclude_from_capture()

    def _exclude_from_capture(self) -> None:
        """Keep the HUD out of the screenshots sent to the vision model."""
        try:
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(
                int(self.winId()), WDA_EXCLUDEFROMCAPTURE
            )
            if not ok:
                logger.debug(
                    "SetWindowDisplayAffinity failed — HUD may appear in captures"
                )
        except Exception as exc:
            logger.debug("Capture exclusion unavailable (non-critical): %s", exc)

    # Same translucency workaround as SpotlightWindow — required on Windows.
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 0))
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        super().paintEvent(event)
