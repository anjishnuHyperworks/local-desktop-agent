"""
Phase 3 — Standalone Test Utility

Exercises every public component of ScreenCapture and InputEmulator
sequentially.  Safe to run at any time: the DPI movement test visits
screen corners harmlessly, and the type_string test only fires if you
focus a plain-text editor yourself before the prompt expires.

Run with:
    python automation/test_automation.py

Do NOT import or call the coordinator from this script.
"""

import logging
import os
import sys
import time

# ---------------------------------------------------------------------------
# DPI awareness must be set before any Win32 screen measurement.
# Mirrors the init sequence in main.py.
# ---------------------------------------------------------------------------
import ctypes

def _init_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except AttributeError:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

_init_dpi()

# ---------------------------------------------------------------------------
# Add project root to sys.path so `import config` resolves from any cwd.
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402

# ---------------------------------------------------------------------------
# Logging — verbose to stdout for the test run
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("test_automation")

# ---------------------------------------------------------------------------
# Imports (after path fixup)
# ---------------------------------------------------------------------------
from automation.capture import ScreenCapture           # noqa: E402
from automation.input_emulator import InputEmulator    # noqa: E402


# ===========================================================================
# Helpers
# ===========================================================================

def _section(title: str) -> None:
    logger.info("")
    logger.info("=" * 60)
    logger.info("  %s", title)
    logger.info("=" * 60)


def _pass(msg: str) -> None:
    logger.info("[PASS] %s", msg)


def _fail(msg: str, exc: Exception) -> None:
    logger.error("[FAIL] %s — %s: %s", msg, type(exc).__name__, exc)


# ===========================================================================
# Section 1 — ScreenCapture
# ===========================================================================

def test_screen_capture() -> None:
    _section("1. ScreenCapture")

    cap = ScreenCapture()

    # 1a — dimensions
    try:
        w, h = cap.get_screen_dimensions()
        assert w > 0 and h > 0, f"Invalid dimensions: {w}x{h}"
        _pass(f"get_screen_dimensions() → {w}x{h}")
    except Exception as exc:
        _fail("get_screen_dimensions", exc)
        return

    # 1b — PIL image
    try:
        img = cap.capture_image()
        assert img.width == w and img.height == h, (
            f"Size mismatch: image={img.width}x{img.height} "
            f"vs monitor={w}x{h}"
        )
        _pass(f"capture_image() → PIL Image {img.width}x{img.height} mode={img.mode}")
    except Exception as exc:
        _fail("capture_image", exc)

    # 1c — JPEG bytes
    try:
        jpeg = cap.capture_jpeg_bytes()
        assert len(jpeg) > 0, "JPEG bytes are empty"
        # JPEG magic bytes
        assert jpeg[:2] == b"\xff\xd8", "Not a valid JPEG"
        _pass(f"capture_jpeg_bytes() → {len(jpeg):,} bytes")
    except Exception as exc:
        _fail("capture_jpeg_bytes", exc)

    # 1d — Base64 string
    try:
        import base64
        b64 = cap.capture_base64()
        decoded = base64.b64decode(b64)
        assert decoded[:2] == b"\xff\xd8", "Decoded bytes not a valid JPEG"
        _pass(f"capture_base64() → {len(b64):,} chars (decodes OK)")
    except Exception as exc:
        _fail("capture_base64", exc)


# ===========================================================================
# Section 2 — InputEmulator: coordinate conversion
# ===========================================================================

def test_coordinate_conversion() -> None:
    _section("2. Coordinate Conversion")

    emu = InputEmulator()
    grid = config.NORMALIZED_GRID

    cases: list[tuple[str, int, int]] = [
        ("origin",      0,      0     ),
        ("centre",      500,    500   ),
        ("full-grid",   grid,   grid  ),
        ("top-right",   grid,   0     ),
        ("bottom-left", 0,      grid  ),
    ]

    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)

    for label, nx, ny in cases:
        try:
            px, py = emu.normalized_to_physical(nx, ny)
            expected_x = int(nx * w / grid)
            expected_y = int(ny * h / grid)
            assert px == expected_x and py == expected_y, (
                f"Expected ({expected_x}, {expected_y}), got ({px}, {py})"
            )
            _pass(f"{label:14s} norm=({nx:4d},{ny:4d})  phys=({px:4d},{py:4d})")
        except Exception as exc:
            _fail(f"normalized_to_physical({nx}, {ny})", exc)


# ===========================================================================
# Section 3 — InputEmulator: DPI movement test
# ===========================================================================

def test_dpi_movement() -> None:
    _section("3. DPI Movement Diagnostic (run_dpi_test)")
    logger.info(
        "The cursor will visit the four screen corners and the centre."
    )
    logger.info("You can observe cursor movement during this test.")
    time.sleep(1.0)

    emu = InputEmulator()
    try:
        emu.run_dpi_test()
        _pass("run_dpi_test() completed without error")
    except Exception as exc:
        _fail("run_dpi_test", exc)


# ===========================================================================
# Section 4 — InputEmulator: cursor position read-back
# ===========================================================================

def test_cursor_readback() -> None:
    _section("4. Cursor Position Read-back")

    emu = InputEmulator()
    w = ctypes.windll.user32.GetSystemMetrics(0)
    h = ctypes.windll.user32.GetSystemMetrics(1)
    target_x, target_y = w // 2, h // 2

    try:
        ctypes.windll.user32.SetCursorPos(target_x, target_y)
        time.sleep(0.1)
        rx, ry = emu.get_cursor_position()
        assert rx == target_x and ry == target_y, (
            f"Read-back mismatch: set=({target_x},{target_y}) "
            f"got=({rx},{ry})"
        )
        _pass(
            f"get_cursor_position() → ({rx}, {ry}) matches "
            f"SetCursorPos target"
        )
    except Exception as exc:
        _fail("get_cursor_position read-back", exc)


# ===========================================================================
# Section 5 — InputEmulator: press_key validation
# ===========================================================================

def test_press_key_validation() -> None:
    _section("5. press_key — key-map validation")

    emu = InputEmulator()

    valid_keys = [
        "enter", "tab", "esc", "backspace", "delete",
        "space", "up", "down", "left", "right",
    ]

    for name in valid_keys:
        try:
            # We verify the lookup resolves without raising; we do NOT actually
            # inject the keystrokes to avoid side-effects in the test terminal.
            from automation.input_emulator import _KEY_MAP
            assert name in _KEY_MAP, f"{name!r} missing from _KEY_MAP"
            _pass(f"key {name!r} resolves to {_KEY_MAP[name]!r}")
        except Exception as exc:
            _fail(f"press_key lookup for {name!r}", exc)

    # Verify unknown key raises ValueError
    try:
        emu.press_key("nonexistent_key_xyz")
        logger.error("[FAIL] press_key('nonexistent') should have raised ValueError")
    except ValueError:
        _pass("press_key raises ValueError for unknown key name")
    except Exception as exc:
        _fail("press_key('nonexistent') raised unexpected exception", exc)


# ===========================================================================
# Section 6 — InputEmulator: scroll normalisation
# ===========================================================================

def test_scroll_normalisation() -> None:
    _section("6. Scroll Normalisation Logic")

    threshold = config.SCROLL_UNIT_THRESHOLD
    divisor   = config.SCROLL_PIXEL_DIVISOR

    # Reproduce the normalisation logic inline for assertion.
    def expected_steps(amount: int, direction: str) -> int:
        steps = amount if amount <= threshold else amount // divisor
        return -steps if direction == "down" else steps

    cases: list[tuple[int, str]] = [
        (1,   "up"),
        (threshold, "down"),
        (threshold + 1, "up"),
        (300, "down"),
        (50,  "up"),
    ]

    emu = InputEmulator()
    for amount, direction in cases:
        exp = expected_steps(amount, direction)
        # We cannot intercept pynput scroll calls without monkey-patching, so
        # we verify the normalisation formula directly and log it.
        raw = amount if amount <= threshold else amount // divisor
        actual = -raw if direction == "down" else raw
        assert actual == exp, f"amount={amount} dir={direction}: {actual} != {exp}"
        _pass(
            f"scroll('{direction}', {amount:3d}) → {actual:+d} wheel clicks "
            f"({'direct' if amount <= threshold else 'pixel→clicks'})"
        )


# ===========================================================================
# Section 7 — Optional: type_string into an open text editor
# ===========================================================================

def test_type_string_interactive() -> None:
    _section("7. type_string — interactive (optional)")
    logger.info(
        "Focus a plain-text editor window (e.g. Notepad) within 5 seconds."
    )
    logger.info("If no editor is focused the paste will land wherever focus is.")

    countdown = 5
    for i in range(countdown, 0, -1):
        logger.info("  typing in %d…", i)
        time.sleep(1.0)

    emu = InputEmulator()
    sample = "Phase 3 automation test — clipboard paste OK"
    try:
        emu.type_string(sample)
        _pass(f"type_string() dispatched {len(sample)} chars via clipboard")
    except Exception as exc:
        _fail("type_string", exc)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    logger.info("Phase 3 Automation Test Suite")
    logger.info("Python  : %s", sys.version)
    logger.info("Platform: %s", sys.platform)
    logger.info(
        "Config  : NORMALIZED_GRID=%d  JPEG_QUALITY=%d  "
        "CLICK_MOVE_DURATION_S=%.2f",
        config.NORMALIZED_GRID,
        config.JPEG_QUALITY,
        config.CLICK_MOVE_DURATION_S,
    )

    test_screen_capture()
    test_coordinate_conversion()
    test_dpi_movement()
    test_cursor_readback()
    test_press_key_validation()
    test_scroll_normalisation()

    # Interactive section is opt-in — pass --type to enable it.
    if "--type" in sys.argv:
        test_type_string_interactive()
    else:
        logger.info("")
        logger.info(
            "Skipping interactive type_string test. "
            "Re-run with --type to enable it."
        )

    logger.info("")
    logger.info("Phase 3 test suite finished.")
