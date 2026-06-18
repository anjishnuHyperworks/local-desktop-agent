"""
Shared UI design tokens for the Spotlight and HUD windows.

PyQt6 has no CSS-variable system, so this module is the single source of truth
for color, type, radius, and motion — the QSS equivalent of design tokens. Both
windows import from here so the two surfaces stay visually identical and any
future palette change happens in one place.

Conventions:
  - Colors are returned as ready-to-use rgba()/hex strings for QSS, plus QColor
    factories where painting code needs them.
  - Motion durations are milliseconds; easing curves are QEasingCurve types.
  - Respect prefers-reduced-motion via `reduced_motion()`: when True, callers
    should skip animation and snap to the final state.
"""

from __future__ import annotations

from PyQt6.QtCore import QEasingCurve
from PyQt6.QtGui import QColor, QFont, QFontDatabase

# ---------------------------------------------------------------------------
# Palette — a single dark, slightly-cool neutral surface with a cornflower
# accent. Tones are layered (base → raised → border) to build depth instead of
# living on one flat fill.
# ---------------------------------------------------------------------------

# Surface fills (alpha-composited over whatever is behind the translucent window)
SURFACE_BASE = "rgba(24, 24, 27, 0.94)"      # window body
SURFACE_RAISED = "rgba(38, 38, 43, 0.96)"    # answer/input zone lift
SURFACE_SUNKEN = "rgba(18, 18, 21, 0.55)"    # inset wells (working dot track, etc.)

# Hairlines — white at low alpha reads as a crisp edge on the dark surface.
BORDER = "rgba(255, 255, 255, 0.10)"
BORDER_STRONG = "rgba(255, 255, 255, 0.16)"
DIVIDER = "rgba(255, 255, 255, 0.07)"

# Ink ramp. Body text clears 4.5:1 on the surface; muted is reserved for
# secondary affordances only, never body copy.
INK = "#F4F4F5"            # primary text
INK_BODY = "#DCDCE0"       # response body copy (still ≥ 4.5:1)
INK_MUTED = "rgba(244, 244, 245, 0.52)"   # hints, icons — large/secondary only
INK_FAINT = "rgba(244, 244, 245, 0.34)"   # placeholder, decorative

# Accent — cornflower. Used for focus, selection, and the live activity dot.
ACCENT = "#7C9CF2"
ACCENT_DIM = "rgba(124, 156, 242, 0.22)"
SELECTION = "rgba(124, 156, 242, 0.42)"

# Status
DANGER = "#FF9B9B"

# ---------------------------------------------------------------------------
# Radius scale
# ---------------------------------------------------------------------------
RADIUS_WINDOW = 16
RADIUS_HUD = 14
RADIUS_CHIP = 8

# ---------------------------------------------------------------------------
# Motion — "refined & quick": fast ease-out, no bounce. Durations are tuned so
# the entrance feels native (under ~180ms) and never blocks input.
# ---------------------------------------------------------------------------
DUR_ENTER = 170      # window fade+scale in
DUR_EXIT = 120       # fade out
DUR_EXPAND = 200     # response panel height growth
DUR_PULSE = 1100     # working-dot breathing cycle

EASE_OUT = QEasingCurve.Type.OutExpo      # entrances, expansion
EASE_IN = QEasingCurve.Type.InQuad        # exits
EASE_PULSE = QEasingCurve.Type.InOutSine  # looping breath


def reduced_motion() -> bool:
    """Honor the OS 'reduce motion' / 'show animations' preference on Windows.

    Falls back to False (motion enabled) if the setting can't be read, so the
    default experience is the animated one.
    """
    try:
        import ctypes

        SPI_GETCLIENTAREAANIMATION = 0x1042
        enabled = ctypes.c_bool(True)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
        )
        if ok:
            return not enabled.value
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Typography
# ---------------------------------------------------------------------------
_FAMILY_CACHE: str | None = None


def ui_family() -> str:
    """Preferred UI family, with graceful fallback.

    Segoe UI Variable is the modern Windows 11 system face; fall back to Segoe
    UI, then the Qt default, so the app still renders cleanly elsewhere.
    """
    global _FAMILY_CACHE
    if _FAMILY_CACHE is not None:
        return _FAMILY_CACHE
    families = set(QFontDatabase.families())
    for candidate in ("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI"):
        if candidate in families:
            _FAMILY_CACHE = candidate
            break
    else:
        _FAMILY_CACHE = "Segoe UI"
    return _FAMILY_CACHE


def font(size: int, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(ui_family(), size, weight)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


# ---------------------------------------------------------------------------
# QColor factories for painting code
# ---------------------------------------------------------------------------
def accent_color(alpha: int = 255) -> QColor:
    c = QColor(0x7C, 0x9C, 0xF2)
    c.setAlpha(alpha)
    return c


def shadow_color(alpha: int = 165) -> QColor:
    return QColor(0, 0, 0, alpha)
