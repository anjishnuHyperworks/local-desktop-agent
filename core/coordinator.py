"""
Phase 4: Core Coordinator

The Coordinator is the central orchestrator of the automation loop.  It runs
exclusively inside a dedicated QThread worker and communicates with the UI
through Qt signals only — it never touches widgets directly.

Threading model (mandatory for Phase 4+):
    1. UI thread creates Coordinator and a QThread.
    2. coordinator.moveToThread(worker_thread) — Coordinator lives in that thread.
    3. UI emits command_submitted signal (connected to coordinator.start_command).
    4. Qt's queued-connection mechanism delivers the call into the worker thread.
    5. run_loop() executes entirely in the worker thread.
    6. stop_command() may be called directly from ANY thread — it only writes a
       boolean flag, which is safe.  It does NOT rely on queued signal delivery.

Signal inventory (all emitted from the worker thread):
    status_signal(str)   — progress updates shown in the UI status area
    finished_signal(str) — task completed normally ([DONE] received)
    error_signal(str)    — task ended due to an error or max-step overflow
    abort_signal()       — task stopped because the user pressed Esc

Mock AI mode (Phase 4):
    use_mock_ai=True replaces Grok API calls with a fixed script:
        Step 1 → [CLICK:500,500]
        Step 2 → [TYPE:500,500|hello]
        Step 3 → [DONE]
    This lets the entire pipeline — parser, DB, signals, state machine — be
    validated end-to-end without network access.

Phase 5 upgrade path:
    Replace get_ai_response() internals with real httpx calls.  Everything
    else (loop skeleton, parser integration, DB logging, signals) stays unchanged.
"""

import logging
import time
from typing import Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

import config
from core.database import InteractionDatabase
from utils.image_processor import ImageProcessor
from utils.parser import ActionParser, ActionType, ParsedAction

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------

class Coordinator(QObject):
    """
    Orchestrates the capture → AI → parse → execute loop in a background thread.

    Instantiate on the main thread, then move to a QThread before starting.

    Example wiring (in main.py or similar):
        coordinator = Coordinator(db)
        worker_thread = QThread()
        coordinator.moveToThread(worker_thread)
        worker_thread.start()

        # Connect UI → Coordinator (queued, safe across threads)
        spotlight.command_submitted.connect(coordinator.start_command)
        spotlight.abort_requested.connect(coordinator.stop_command)

        # Connect Coordinator → UI (queued, safe across threads)
        coordinator.finished_signal.connect(spotlight.mark_execution_complete)
        coordinator.error_signal.connect(spotlight.mark_execution_complete)
        coordinator.abort_signal.connect(spotlight.mark_execution_complete)
        coordinator.status_signal.connect(your_status_slot)
    """

    # ------------------------------------------------------------------
    # Public signals (emitted from worker thread → received on UI thread)
    # ------------------------------------------------------------------

    finished_signal = pyqtSignal(str)   # Task completed successfully
    error_signal    = pyqtSignal(str)   # Fatal error or max-step exceeded
    abort_signal    = pyqtSignal()      # User-requested abort (Esc)
    status_signal   = pyqtSignal(str)   # Live progress messages

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        db: Optional[InteractionDatabase] = None,
        use_mock_ai: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        Args:
            db:           Initialised InteractionDatabase instance.  If None, a
                          default instance is created using config.DB_PATH.
            use_mock_ai:  When True, get_ai_response() returns scripted mock
                          responses instead of calling the Grok API.
            parent:       Optional Qt parent (usually None for worker objects).
        """
        super().__init__(parent)

        self._db = db if db is not None else InteractionDatabase(config.DB_PATH)
        self._image_processor = ImageProcessor()
        self._parser = ActionParser()

        # -- Execution state --------------------------------------------------
        self.is_running: bool = False
        self.current_step: int = 0
        self.current_command: str = ""
        self.use_mock_ai: bool = use_mock_ai

        # -- Mock AI script ---------------------------------------------------
        # Cycled through in order; after exhaustion [DONE] is returned.
        self._mock_responses: list[str] = [
            "[CLICK:500,500]",
            "[TYPE:500,500|hello]",
            "[DONE]",
        ]

        logger.info(
            "Coordinator created — use_mock_ai=%s, db=%s",
            self.use_mock_ai,
            self._db._db_path,
        )

    # ------------------------------------------------------------------
    # Slot: start execution (called via queued connection from UI thread)
    # ------------------------------------------------------------------

    @pyqtSlot(str)
    def start_command(self, command: str) -> None:
        """
        Entry point for a new user command.

        This slot is connected to spotlight.command_submitted via a queued
        connection so Qt delivers it into the worker thread automatically.
        Calling it directly from the main thread is therefore safe — the
        call is marshalled across thread boundaries by Qt.

        Guards:
            - Rejects concurrent invocations (only one loop at a time).
        """
        if self.is_running:
            logger.warning(
                "start_command rejected — execution already in progress "
                "(command=%r ignored)", command
            )
            return

        logger.info(
            "start_command: received %r (thread=%s)",
            command,
            QThread.currentThread().objectName() or "unnamed",
        )

        self.current_command = command
        self.current_step = 0
        self.is_running = True

        self.status_signal.emit(f"Starting: {command}")
        self.run_loop()

    # ------------------------------------------------------------------
    # Slot: abort (called directly from UI/abort thread — flag write only)
    # ------------------------------------------------------------------

    @pyqtSlot()
    def stop_command(self) -> None:
        """
        Signal the loop to stop on its next iteration check.

        DESIGN NOTE: This method is intentionally minimal.  The worker loop
        may be blocking in time.sleep() when this is called from the UI thread.
        Because the loop is not processing Qt events during sleep, a queued
        signal would not be delivered until after the sleep completes anyway.
        Writing a plain Python bool is atomic at the CPython level and visible
        to any thread, so the loop will observe is_running=False at the next
        check point.

        Only performs:
            - Flag update (self.is_running = False)
            - Logging

        Must NOT:
            - Emit signals
            - Touch UI objects
            - Modify any other coordinator state
        """
        logger.info("stop_command called — setting is_running=False")
        self.is_running = False

    # ------------------------------------------------------------------
    # Main execution loop (runs entirely in the worker thread)
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        """
        The main capture-reason-execute cycle.

        Termination conditions (deterministic, mutually exclusive):
            DONE tag received      → finished_signal emitted
            Max steps exceeded     → error_signal emitted
            stop_command() called  → abort_signal emitted (and nothing else)
            Unhandled exception    → error_signal emitted

        The loop emits status_signal at the start of each step so the UI can
        display progress in real time.
        """
        logger.info(
            "run_loop started — command=%r, max_steps=%d, mock=%s",
            self.current_command,
            config.MAX_STEPS_PER_COMMAND,
            self.use_mock_ai,
        )

        try:
            while (
                self.is_running
                and self.current_step < config.MAX_STEPS_PER_COMMAND
            ):
                self.current_step += 1
                logger.info(
                    "Loop iteration %d/%d",
                    self.current_step,
                    config.MAX_STEPS_PER_COMMAND,
                )

                # Emit step progress to the UI.
                self.status_signal.emit(
                    f"Step {self.current_step}/{config.MAX_STEPS_PER_COMMAND} …"
                )

                # Execute one reasoning + action cycle.
                done = self.process_single_step()

                if done:
                    # [DONE] received — normal completion.
                    logger.info("run_loop: [DONE] received — finishing normally")
                    self.is_running = False
                    self.finished_signal.emit("Task completed successfully.")
                    return

                # Check abort flag between iterations (stop_command may have
                # been called while process_single_step was executing).
                if not self.is_running:
                    break

                # Pause between steps to avoid hammering the API and to give
                # the OS time to settle after an injected action.
                logger.debug(
                    "Sleeping %.1fs before next step", config.STEP_DELAY_S
                )
                time.sleep(config.STEP_DELAY_S)

            # -- Post-loop evaluation ------------------------------------------

            if not self.is_running:
                # Loop exited because stop_command() set the flag.
                logger.info("run_loop: terminated by user abort")
                self.abort_signal.emit()
                return

            # Loop exited because step limit was reached.
            logger.warning(
                "run_loop: max steps (%d) exceeded — aborting",
                config.MAX_STEPS_PER_COMMAND,
            )
            self.is_running = False
            self.error_signal.emit(
                "Task aborted: Maximum execution steps exceeded."
            )

        except Exception as exc:
            logger.exception("run_loop: fatal unhandled exception: %s", exc)
            self.is_running = False
            self.error_signal.emit(f"Fatal error encountered: {exc}")

    # ------------------------------------------------------------------
    # Single iteration: fetch AI response → parse → log → return done flag
    # ------------------------------------------------------------------

    def process_single_step(self) -> bool:
        """
        Execute one complete step of the reasoning loop.

        Sequence:
            1. Fetch history context from the database.
            2. Obtain AI response (mock or real).
            3. Parse the response for an action tag.
            4. Emit status update with the parsed action.
            5. Log the interaction to the database.

        Returns:
            True if the response contained [DONE], False otherwise.
        """
        # 1. Load conversation history (text summaries only).
        history = self.build_history_context()
        logger.debug(
            "process_single_step: history context length=%d chars", len(history)
        )

        # 2. Get AI response (mock for Phase 4).
        raw_response = self.get_ai_response()
        logger.info("AI response (step %d): %r", self.current_step, raw_response)

        # 3. Parse for action tag.
        action: Optional[ParsedAction] = self._parser.parse(raw_response)
        prose = self._parser.remove_action_tag(raw_response)

        # 4. Emit human-readable status.
        if action is None:
            status_msg = f"AI responded (no action tag): {prose[:80]}"
        elif action.action_type == ActionType.DONE:
            status_msg = "AI signalled task complete."
        else:
            status_msg = f"Action → {action}"

        self.status_signal.emit(status_msg)
        logger.info("process_single_step: %s", status_msg)

        # 5. Persist interaction record.
        action_tag_str = str(action) if action else None
        execution_result = "pending"   # Phase 5+ will update this after execution.

        try:
            self._db.log_interaction(
                user_command=self.current_command if self.current_step == 1 else None,
                assistant_response=prose or None,
                action_tag=action_tag_str,
                execution_result=execution_result,
            )
        except Exception as db_exc:
            # DB failure must not kill the loop.
            logger.error(
                "process_single_step: DB log failed (non-fatal): %s", db_exc
            )

        # Return True on DONE, False to continue iterating.
        return action is not None and action.action_type == ActionType.DONE

    # ------------------------------------------------------------------
    # History context builder
    # ------------------------------------------------------------------

    def build_history_context(self) -> str:
        """
        Fetch recent interactions from the DB and format them as a compact,
        text-only summary suitable for injection into an AI prompt.

        Format example:
            User: Open Chrome
            Assistant: Clicking Chrome icon
            Action: CLICK

            User: Search weather
            Assistant: Typing search query
            Action: TYPE

        Constraints:
            - No screenshots, no image bytes, no Base64 strings.
            - Limited to config.MAX_HISTORY_TURNS most-recent turns.
        """
        try:
            records = self._db.get_recent_history(limit=config.MAX_HISTORY_TURNS)
        except Exception as exc:
            logger.error("build_history_context: DB read failed: %s", exc)
            return ""

        if not records:
            logger.debug("build_history_context: no history available")
            return ""

        lines: list[str] = []
        for record in records:
            if record.user_command:
                lines.append(f"User: {record.user_command}")
            if record.assistant_response:
                lines.append(f"Assistant: {record.assistant_response}")
            if record.action_tag:
                # Extract bare action type name for compactness.
                action_type = record.action_tag.lstrip("[").split(":")[0]
                lines.append(f"Action: {action_type}")
            if lines and lines[-1] != "":
                lines.append("")    # blank separator between turns

        context = "\n".join(lines).strip()
        logger.debug(
            "build_history_context: %d records → %d chars",
            len(records), len(context),
        )
        return context

    # ------------------------------------------------------------------
    # AI response provider (mock for Phase 4, real in Phase 5)
    # ------------------------------------------------------------------

    def get_ai_response(self) -> str:
        """
        Return the next AI response string.

        Phase 4 — mock mode:
            Cycles through self._mock_responses in order.  After the list is
            exhausted, returns "[DONE]" for every subsequent call, so the loop
            always terminates cleanly.

        Phase 5 upgrade:
            Replace the mock branch with an httpx call to the Grok Vision API.
            The method signature and return type stay the same.

        Returns:
            A string in the format Grok would return, containing exactly one
            action tag at the end (or [DONE]).
        """
        if self.use_mock_ai:
            # Zero-based step index: current_step was already incremented.
            idx = self.current_step - 1
            if idx < len(self._mock_responses):
                response = self._mock_responses[idx]
            else:
                response = "[DONE]"

            logger.info(
                "get_ai_response [MOCK, step=%d]: %r", self.current_step, response
            )
            return response

        # Phase 5 placeholder — should not be reached in Phase 4.
        logger.error(
            "get_ai_response: use_mock_ai=False but real API not implemented yet"
        )
        return "[DONE]"
