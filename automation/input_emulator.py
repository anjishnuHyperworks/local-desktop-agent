"""
Phase 3: Input Emulator

DPI-aware mouse movement, clicking, keyboard input, scrolling, and diagnostics.

Design notes:
- Physical screen dimensions are read from Win32 (GetSystemMetrics) rather than
  from mss or PyAutoGUI so they are always in true physical pixels, independent
  of any OS scaling factor.
- Cursor positioning uses SetCursorPos (Win32) for the same reason.
- pynput is used exclusively for click/key/scroll events.
- pyperclip backs the clipboard-swap strategy in type_string so we never hold
  an arbitrary string in the clipboard after a paste completes.
"""

import ctypes
import logging
import time
from contextlib import contextmanager
from typing import Generator

import pyperclip
from pynput import keyboard as pynput_keyboard
from pynput import mouse as pynput_mouse
from pynput.keyboard import Key

import config

logger = logging.getLogger(__name__)

_user32 = ctypes.windll.user32

# ---------------------------------------------------------------------------
# Key name → pynput.Key lookup table
# ---------------------------------------------------------------------------
_KEY_MAP: dict[str, Key] = {
    "enter":     Key.enter,
    "tab":       Key.tab,
    "esc":       Key.esc,
    "backspace": Key.backspace,
    "delete":    Key.delete,
    "space":     Key.space,
    "up":        Key.up,
    "down":      Key.down,
    "left":      Key.left,
    "right":     Key.right,
}


class InputEmulator:
    """
    Emulates mouse movements, clicks, keyboard input, and scroll events using
    DPI-aware physical-pixel coordinates throughout.

    Coordinate space:
        Grok reasons over a 0–{NORMALIZED_GRID} grid (default 0–1000).
        normalized_to_physical() maps that grid to the physical display
        dimensions obtained from Win32, so the translation is correct even
        on high-DPI / scaled displays.
    """

    def __init__(self) -> None:
        self._mouse = pynput_mouse.Controller()
        self._keyboard = pynput_keyboard.Controller()

        # Cache physical screen size at construction time.
        self._screen_width: int = _user32.GetSystemMetrics(0)
        self._screen_height: int = _user32.GetSystemMetrics(1)

        logger.info(
            "InputEmulator ready — physical display: %dx%d, grid: 0-%d",
            self._screen_width,
            self._screen_height,
            config.NORMALIZED_GRID,
        )

    # ------------------------------------------------------------------
    # Coordinate conversion
    # ------------------------------------------------------------------

    def normalized_to_physical(self, x: int, y: int) -> tuple[int, int]:
        """
        Map a point in Grok's normalised 0-{NORMALIZED_GRID} grid to physical
        screen pixels.

            physical_x = x * screen_width  / NORMALIZED_GRID
            physical_y = y * screen_height / NORMALIZED_GRID
        """
        px = int(x * self._screen_width  / config.NORMALIZED_GRID)
        py = int(y * self._screen_height / config.NORMALIZED_GRID)
        logger.debug(
            "Coordinate conversion: (%d, %d) → (%d, %d)", x, y, px, py
        )
        return px, py

    # ------------------------------------------------------------------
    # Mouse movement
    # ------------------------------------------------------------------

    def move_to(self, physical_x: int, physical_y: int) -> None:
        """
        Smoothly interpolate the cursor from its current position to
        (physical_x, physical_y) over CLICK_MOVE_DURATION_S seconds.
        Uses SetCursorPos for DPI-correct placement.
        """
        # Read current cursor position via Win32 POINT struct.
        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        start_x, start_y = pt.x, pt.y

        duration = config.CLICK_MOVE_DURATION_S
        step_delay = 0.01
        steps = max(1, int(duration / step_delay))

        logger.debug(
            "move_to: (%d, %d) → (%d, %d) over %.2fs (%d steps)",
            start_x, start_y, physical_x, physical_y, duration, steps,
        )

        for i in range(1, steps + 1):
            t = i / steps
            cx = int(start_x + (physical_x - start_x) * t)
            cy = int(start_y + (physical_y - start_y) * t)
            _user32.SetCursorPos(cx, cy)
            time.sleep(step_delay)

    def click_at(self, x: int, y: int) -> None:
        """
        Accept normalised coordinates, convert to physical pixels, smoothly
        move there, then execute a left click.
        """
        px, py = self.normalized_to_physical(x, y)
        logger.info("click_at: normalised (%d, %d) → physical (%d, %d)", x, y, px, py)
        self.move_to(px, py)
        self._mouse.click(pynput_mouse.Button.left)

    # ------------------------------------------------------------------
    # Typing
    # ------------------------------------------------------------------

    @contextmanager
    def _clipboard_swap(self, text: str) -> Generator[None, None, None]:
        """Context manager: backup clipboard → set text → yield → restore."""
        try:
            original = pyperclip.paste()
        except Exception:
            original = ""

        try:
            pyperclip.copy(text)
            yield
        finally:
            try:
                pyperclip.copy(original)
            except Exception:
                logger.warning("Failed to restore clipboard contents")

    def type_string(self, text: str) -> None:
        """
        Paste text via a clipboard-swap so individual keystrokes are never
        emitted (avoids IME and key-mapping issues on Windows).

        Sequence: backup clipboard → copy text → Ctrl+V → wait → restore.
        """
        logger.debug("type_string: %d characters", len(text))
        with self._clipboard_swap(text):
            with self._keyboard.pressed(Key.ctrl):
                self._keyboard.press("v")
                self._keyboard.release("v")
            time.sleep(config.CLIPBOARD_PASTE_DELAY_S)

    def type_at_coordinates(self, x: int, y: int, text: str) -> None:
        """
        Click to focus, wait for Windows to register the focus event, then
        paste text.  The focus delay is mandatory — omitting it causes Windows
        to drop the first few characters of the paste on slower machines.
        """
        logger.info(
            "type_at_coordinates: (%d, %d), text length=%d", x, y, len(text)
        )
        self.click_at(x, y)
        time.sleep(config.FOCUS_REGISTRATION_DELAY_S)
        self.type_string(text)

    # ------------------------------------------------------------------
    # Key presses
    # ------------------------------------------------------------------

    def press_key(self, key_name: str) -> None:
        """
        Press and release a single named key.  Accepts string names defined in
        _KEY_MAP; raises ValueError for unrecognised names.
        """
        key_name_lower = key_name.lower()
        key = _KEY_MAP.get(key_name_lower)
        if key is None:
            raise ValueError(
                f"Unrecognised key name {key_name!r}. "
                f"Valid keys: {sorted(_KEY_MAP)}"
            )
        logger.debug("press_key: %r → %r", key_name, key)
        self._keyboard.press(key)
        self._keyboard.release(key)

    # ------------------------------------------------------------------
    # Scrolling
    # ------------------------------------------------------------------

    def scroll(self, direction: str, amount: int) -> None:
        """
        Scroll the mouse wheel.

        If amount <= SCROLL_UNIT_THRESHOLD it is treated as direct wheel clicks.
        If amount >  SCROLL_UNIT_THRESHOLD it is treated as pixels and divided
        by SCROLL_PIXEL_DIVISOR to arrive at wheel clicks.

        direction: "up" (positive scroll) or "down" (negative scroll).
        """
        if amount <= config.SCROLL_UNIT_THRESHOLD:
            steps = amount
        else:
            steps = amount // config.SCROLL_PIXEL_DIVISOR

        if direction.lower() == "down":
            steps = -steps

        logger.info(
            "scroll: direction=%s amount=%d → %d wheel clicks",
            direction, amount, steps,
        )
        self._mouse.scroll(0, steps)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def get_cursor_position(self) -> tuple[int, int]:
        """Return the current cursor position in physical pixels via Win32."""
        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = _POINT()
        _user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def run_dpi_test(self) -> None:
        """
        Move the cursor to five cardinal positions on the physical display,
        logging the reported position at each stop.  Use this to verify that
        SetCursorPos coordinates match the physical screen geometry.
        """
        w, h = self._screen_width, self._screen_height
        landmarks: list[tuple[str, int, int]] = [
            ("top-left",     0,         0        ),
            ("top-right",    w - 1,     0        ),
            ("bottom-right", w - 1,     h - 1    ),
            ("bottom-left",  0,         h - 1    ),
            ("center",       w // 2,    h // 2   ),
        ]

        logger.info(
            "DPI test — physical screen: %dx%d, grid: 0-%d",
            w, h, config.NORMALIZED_GRID,
        )

        for name, tx, ty in landmarks:
            _user32.SetCursorPos(tx, ty)
            time.sleep(0.15)    # let SetCursorPos settle before reading back
            rx, ry = self.get_cursor_position()
            match = "OK" if (rx == tx and ry == ty) else "MISMATCH"
            logger.info(
                "  %-14s  target=(%4d, %4d)  reported=(%4d, %4d)  %s",
                name, tx, ty, rx, ry, match,
            )

        logger.info("DPI test complete")
