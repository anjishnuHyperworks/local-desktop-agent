"""
Local Desktop Automation Agent — Entry Point

Must be run as Administrator on Windows for UAC-compatible input injection.
"""

import sys
import ctypes
import logging
import logging.handlers
from pathlib import Path

# ---------------------------------------------------------------------------
# DPI Awareness — must be set before any UI or screen-measurement code runs.
# PROCESS_PER_MONITOR_DPI_AWARE (value 2) ensures all coordinate and dimension
# queries return physical pixels regardless of display scaling factor.
# ---------------------------------------------------------------------------
def _init_dpi_awareness() -> None:
    try:
        # SetProcessDpiAwareness(2) = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except AttributeError:
        # shcore not available on very old Windows builds — fall back silently
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_init_dpi_awareness()

# ---------------------------------------------------------------------------
# Now safe to import project modules (they may query screen dimensions)
# ---------------------------------------------------------------------------
import config  # noqa: E402 — must follow DPI init


# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

    # Rotating file handler — keeps last 5 × 2 MB of logs
    file_handler = logging.handlers.RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    return logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------
def _validate_environment(logger: logging.Logger) -> bool:
    ok = True

    if not config.GROK_API_KEY:
        logger.warning(
            "GROK_API_KEY is not set. "
            "Create a .env file or set the environment variable before running the agent."
        )
        ok = False

    if not config.SYSTEM_PROMPT_PATH.exists():
        logger.error("System prompt not found at: %s", config.SYSTEM_PROMPT_PATH)
        ok = False

    return ok


# ---------------------------------------------------------------------------
# PyAutoGUI failsafe — compatibility cue only.
# The primary emergency abort is the pynput Esc listener (implemented in Phase 2+).
# ---------------------------------------------------------------------------
def _init_pyautogui_failsafe() -> None:
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0  # We manage our own delays explicitly
    except ImportError:
        pass  # pyautogui optional at this phase


# ---------------------------------------------------------------------------
# Admin check
# ---------------------------------------------------------------------------
def _warn_if_not_admin(logger: logging.Logger) -> None:
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False
    if not is_admin:
        logger.warning(
            "Not running as Administrator. "
            "Input injection into UAC-elevated windows may fail. "
            "Re-launch the terminal as Administrator for full functionality."
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    logger = _setup_logging()
    logger.info("=" * 60)
    logger.info("Local Desktop Automation Agent — starting up")
    logger.info("Base directory : %s", config.BASE_DIR)
    logger.info("Model          : %s", config.GROK_MODEL)
    logger.info("Max steps      : %d", config.MAX_STEPS_PER_COMMAND)
    logger.info("Normalized grid: 0-%d", config.NORMALIZED_GRID)
    logger.info("=" * 60)

    _warn_if_not_admin(logger)
    _init_pyautogui_failsafe()
    env_ok = _validate_environment(logger)

    if not env_ok:
        logger.warning(
            "Environment validation produced warnings. "
            "The agent will start but may not function correctly until issues are resolved."
        )

    # Phase 1 placeholder — subsequent phases will replace this with the Qt app launch.
    logger.info("Agent skeleton initialised successfully. Phase 1 complete.")
    logger.info("Next: implement UI Layer (Phase 2) in ui/spotlight.py")


if __name__ == "__main__":
    main()
