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
import math

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint, pyqtProperty
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QWidget,
)

import config
from ui import theme

logger = logging.getLogger(__name__)

WDA_EXCLUDEFROMCAPTURE = 0x00000011


class _PulseDot(QWidget):
    """A single accent dot that breathes while a task runs — the HUD's
    'something is happening' tell. Goes solid under reduced motion."""

    _SIZE = 9

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(self._SIZE + 4, self._SIZE + 4)
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._advance)

    def start(self) -> None:
        if theme.reduced_motion():
            self._phase = 0.25  # static, near-full brightness
            self.update()
            return
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _advance(self) -> None:
        self._phase = (self._phase + 0.018) % 1.0
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        wave = (math.sin(self._phase * 2 * math.pi) + 1) / 2  # 0..1
        cx = self.width() / 2
        cy = self.height() / 2

        # Soft halo that swells with the breath.
        halo_r = self._SIZE / 2 + 2 + wave * 3
        p.setBrush(theme.accent_color(int(40 + wave * 50)))
        p.drawEllipse(
            int(cx - halo_r), int(cy - halo_r), int(halo_r * 2), int(halo_r * 2)
        )

        # Solid core.
        core_r = self._SIZE / 2
        p.setBrush(theme.accent_color(int(180 + wave * 75)))
        p.drawEllipse(
            int(cx - core_r), int(cy - core_r), int(core_r * 2), int(core_r * 2)
        )


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
        self._container.setStyleSheet(f"""
            QWidget#hud {{
                background: {theme.SURFACE_BASE};
                border-radius: {theme.RADIUS_HUD}px;
                border: 1px solid {theme.BORDER};
            }}
        """)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(theme.shadow_color(150))
        self._container.setGraphicsEffect(shadow)

        inner = QHBoxLayout(self._container)
        inner.setContentsMargins(14, 0, 14, 0)
        inner.setSpacing(11)

        # Live activity dot — the first thing the eye lands on.
        self._pulse = _PulseDot(self._container)
        inner.addWidget(self._pulse)

        self._status_label = QLabel("")
        self._status_label.setFont(theme.font(10))
        self._status_label.setStyleSheet(f"color: {theme.INK};")
        inner.addWidget(self._status_label, stretch=1)

        self._esc_label = QLabel("esc to stop")
        self._esc_label.setFont(theme.font(9, QFont.Weight.DemiBold))
        self._esc_label.setStyleSheet(f"""
            color: {theme.INK_MUTED};
            background: {theme.SURFACE_SUNKEN};
            border: 1px solid {theme.BORDER};
            border-radius: {theme.RADIUS_CHIP}px;
            padding: 2px 8px;
        """)
        inner.addWidget(self._esc_label)

        outer.addWidget(self._container)
        self._build_animations()

    def _build_animations(self) -> None:
        """Entrance fade + slide-up. The HUD is non-activating, so animating
        windowOpacity/pos never steals focus from the app being automated."""
        self._fade_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade_anim.setDuration(theme.DUR_ENTER)
        self._fade_anim.setEasingCurve(theme.EASE_OUT)

        self._slide_anim = QPropertyAnimation(self, b"pos", self)
        self._slide_anim.setDuration(theme.DUR_ENTER)
        self._slide_anim.setEasingCurve(theme.EASE_OUT)

        self._exit_anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._exit_anim.setDuration(theme.DUR_EXIT)
        self._exit_anim.setEasingCurve(theme.EASE_IN)
        self._exit_anim.finished.connect(self._after_fade_out)

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
        self._exit_anim.stop()
        self._esc_label.show()
        self._set_text("Working…")
        self._pulse.start()
        self._reveal()

    def show_status(self, message: str) -> None:
        """Live progress line; no-op unless a run is active."""
        if not self._active:
            return
        self._linger_timer.stop()
        self._set_text(message)
        if not self.isVisible():
            self._reveal()

    def finish(self, message: str) -> None:
        """Final status — lingers briefly, then the pill fades out."""
        if not self._active:
            return
        self._esc_label.hide()
        self._pulse.stop()
        self._set_text(message)
        self._linger_timer.start(config.HUD_LINGER_MS)

    # ------------------------------------------------------------------
    # Entrance / exit animation
    # ------------------------------------------------------------------
    def _reveal(self) -> None:
        """Fade + slide the pill up into its resting position. Snaps under
        reduced motion."""
        self._position()
        rest = self.pos()
        if theme.reduced_motion():
            self.setWindowOpacity(1.0)
            self.show()
            return
        self._fade_anim.stop()
        self._slide_anim.stop()
        self.setWindowOpacity(0.0)
        self.move(rest.x(), rest.y() + 14)  # start a touch lower
        self.show()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._slide_anim.setStartValue(self.pos())
        self._slide_anim.setEndValue(rest)
        self._fade_anim.start()
        self._slide_anim.start()

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
        """Fade the pill out once its final message has lingered."""
        self._active = False
        if theme.reduced_motion() or not self.isVisible():
            self.hide()
            return
        self._exit_anim.stop()
        self._exit_anim.setStartValue(self.windowOpacity())
        self._exit_anim.setEndValue(0.0)
        self._exit_anim.start()

    def _after_fade_out(self) -> None:
        # Re-activation may have happened mid-fade; only hide if still idle.
        if not self._active:
            self.hide()
            self.setWindowOpacity(1.0)

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
