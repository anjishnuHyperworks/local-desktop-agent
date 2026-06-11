"""
Local Desktop Automation Agent — Entry Point

Must be run as Administrator on Windows for UAC-compatible input injection.
"""

import sys
import os
import ctypes
import logging
import logging.handlers
from pathlib import Path

# ---------------------------------------------------------------------------
# Silence Qt's internal complaints about duplicate DPI initialization
# ---------------------------------------------------------------------------
os.environ["QT_LOGGING_RULES"] = "qt.qpa.window=false"

# ---------------------------------------------------------------------------
# DPI Awareness — must be set before any UI or screen-measurement code runs.
# Forces modern Per-Monitor v2 context layer (bypasses shell environment locks).
# Ensures all coordinate queries return true physical pixels regardless of scaling.
# ---------------------------------------------------------------------------
def _init_dpi_awareness() -> None:
    try:
        # Force modern Windows 10 Creators Update context layer context first
        # -4 corresponds to DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
        return
    except Exception:
        pass

    try:
        # Fallback to older shcore API if context switching is unavailable
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
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

    # ------------------------------------------------------------------
    # Phase 2 + 4: Qt application + Spotlight UI + Coordinator
    # ------------------------------------------------------------------
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QThread
    from ui.spotlight import SpotlightWindow
    from core.database import InteractionDatabase
    from core.coordinator import Coordinator

    # Must be called before QApplication() is constructed
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)   # keep alive after window hides

    # ---- Database -----------------------------------------------------------
    db = InteractionDatabase(config.DB_PATH)
    db.initialize()

    # ---- Coordinator + worker thread ----------------------------------------
    # The Coordinator must NOT inherit QThread.  Instead, it is a QObject moved
    # into a dedicated worker thread so all its slot invocations run there.
    coordinator = Coordinator(db=db, use_mock_ai=False)

    worker_thread = QThread()
    worker_thread.setObjectName("CoordinatorThread")
    coordinator.moveToThread(worker_thread)
    worker_thread.start()
    logger.info("Coordinator worker thread started")

    # ---- UI window ----------------------------------------------------------
    window = SpotlightWindow()

    # ---- UI → Coordinator (queued, runs in worker thread) -------------------
    window.command_submitted.connect(coordinator.start_command)

    # abort_requested is emitted by the Esc listener thread.  stop_command()
    # only writes a boolean so a direct connection is safe across threads.
    window.abort_requested.connect(coordinator.stop_command)

    # ---- Coordinator → UI (queued, runs in main/UI thread) ------------------
    coordinator.finished_signal.connect(
        lambda msg: (
            logger.info("Task finished: %s", msg),
            window.mark_execution_complete(),
        )
    )
    coordinator.error_signal.connect(
        lambda msg: (
            logger.error("Task error: %s", msg),
            window.mark_execution_complete(),
        )
    )
    coordinator.abort_signal.connect(window.mark_execution_complete)
    coordinator.status_signal.connect(
        lambda msg: logger.info("Status: %s", msg)
    )

    # ---- Graceful teardown on app exit --------------------------------------
    def _shutdown() -> None:
        logger.info("Shutting down — stopping coordinator worker thread")
        coordinator.stop_command()
        worker_thread.quit()
        if not worker_thread.wait(3000):
            logger.warning("Worker thread did not stop within 3 s; terminating")
            worker_thread.terminate()
        db.close()
        logger.info("Shutdown complete")

    app.aboutToQuit.connect(_shutdown)

    logger.info(
        "Phase 5 ready — Coordinator wired up (use_mock_ai=False). "
        "Press Ctrl+Space to open the spotlight."
    )
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
