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
    # IMPORTANT: SetProcessDpiAwarenessContext takes a HANDLE (pointer-width)
    # argument. Passing a bare Python int marshals as a 32-bit value, which the
    # API rejects on 64-bit Windows — the call returns 0 (failure) and the
    # process stays DPI-UNAWARE, so GetSystemMetrics reports *logical* pixels
    # (e.g. 1536x864 at 125%) while mss captures *physical* pixels (1920x1080).
    # That mismatch silently corrupts every coordinate conversion. Declaring the
    # correct argtypes makes the call succeed and metrics return true pixels.
    from ctypes import wintypes

    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [wintypes.HANDLE]
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        # -4 == DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if user32.SetProcessDpiAwarenessContext(wintypes.HANDLE(-4)):
            return
    except Exception:
        pass

    try:
        # Fallback to older shcore API if context switching is unavailable
        # (Windows 8.1 .. 10 pre-1607). 2 == PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass

    try:
        # Last-resort system-DPI awareness (Vista .. Windows 8).
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

    # Dedicated latency log — all [latency] entries go here for easy analysis
    perf_handler = logging.handlers.RotatingFileHandler(
        config.LOGS_DIR / "latency.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    perf_handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    perf_logger = logging.getLogger("perf")
    perf_logger.setLevel(logging.INFO)
    perf_logger.addHandler(perf_handler)
    perf_logger.propagate = False   # keep perf entries out of the main log

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
# The primary emergency abort is the pynput Esc listener in the Spotlight UI.
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
    logger.info("Max image size : %dpx (longest side)", config.MAX_IMAGE_SIZE)
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
    # Qt application + Spotlight UI + HUD + Coordinator
    # ------------------------------------------------------------------
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt, QThread
    from ui.spotlight import SpotlightWindow
    from ui.hud import StatusHUD
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

    # ---- UI windows ---------------------------------------------------------
    window = SpotlightWindow()
    hud = StatusHUD()

    # ---- UI → Coordinator (queued, runs in worker thread) -------------------
    window.command_submitted.connect(coordinator.start_command)

    # abort_requested is emitted by the Esc listener thread.  stop_command()
    # only writes a boolean so a direct connection is safe across threads.
    window.abort_requested.connect(coordinator.stop_command)

    # ---- Coordinator → UI (queued, runs in main/UI thread) ------------------
    # Intent decides the flow: chat keeps the spotlight open (answer renders in
    # its response panel); automation hides it and shows the HUD pill instead.
    coordinator.intent_signal.connect(window.on_intent_classified)
    coordinator.intent_signal.connect(
        lambda intent: hud.activate() if intent == "AUTOMATION" else None
    )
    coordinator.chat_response_signal.connect(window.show_chat_response)
    coordinator.chat_token_signal.connect(window.append_chat_token)

    coordinator.finished_signal.connect(
        lambda msg: (
            logger.info("Task finished: %s", msg),
            window.mark_execution_complete(),
            hud.finish(msg),
        )
    )
    coordinator.error_signal.connect(
        lambda msg: (
            logger.error("Task error: %s", msg),
            window.on_task_error(msg),
            hud.finish(msg),
        )
    )
    coordinator.abort_signal.connect(
        lambda: (
            window.mark_execution_complete(),
            hud.finish("Stopped by user."),
        )
    )
    coordinator.status_signal.connect(
        lambda msg: (
            logger.info("Status: %s", msg),
            hud.show_status(msg),
        )
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
        "Ready — Coordinator wired up (use_mock_ai=False). "
        "Press Ctrl+Space to open the spotlight."
    )
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
