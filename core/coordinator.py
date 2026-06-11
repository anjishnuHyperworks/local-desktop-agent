"""
Phase 5: Core Coordinator — Real Grok Vision API Integration

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

Phase 5:
    use_mock_ai=False sends real httpx requests to the Grok Vision API.
    Screenshot is captured, resized, base64-encoded, and sent alongside
    conversation history and the system prompt.
"""

import base64
import logging
import time
from typing import Optional

import httpx
from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot

import config
from automation.capture import ScreenCapture
from automation.input_emulator import InputEmulator
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
        self._screen_capture = ScreenCapture()
        self._input_emulator = InputEmulator()

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

        # Cache the system prompt text so we read the file once per session.
        self._system_prompt: str = self._load_system_prompt()

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

        self.is_running = True  # lock before classification to block concurrent commands
        self.current_command = command
        self.current_step = 0

        self.status_signal.emit("Analyzing intent...")

        try:
            intent = self.classify_intent(command)

            if intent == "CHAT":
                logger.info("Routing to Pure Chat handler.")
                self.handle_pure_chat(command)
            else:
                logger.info("Routing to Desktop Automation loop.")
                self.status_signal.emit(f"Starting automation: {command}")
                # run_loop() manages is_running internally
                self.run_loop()

        except Exception as exc:
            logger.exception("Fatal error during intent routing: %s", exc)
            self.is_running = False
            self.error_signal.emit(f"Failed to process command: {exc}")
        # No finally: CHAT releases via handle_pure_chat(), AUTOMATION via run_loop()

    def classify_intent(self, command: str) -> str:
        """Determines if the command requires desktop automation or is purely text-based."""
        if self.use_mock_ai:
            return "AUTOMATION"

        classification_prompt = (
            "You are an intent router for a desktop automation agent.\n"
            "Analyze the user's input and classify it into one of two categories:\n"
            "1. AUTOMATION: If the user is asking to control the computer, click something, open an app, scroll, type, or find something on their screen.\n"
            "2. CHAT: If the user is asking a general question, greeting you, asking for calculations, or having a casual conversation that doesn't require looking at their screen.\n\n"
            "Output EXACTLY 'AUTOMATION' or 'CHAT'. Do not include any other text."
        )

        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "messages": [
                            {"role": "system", "content": classification_prompt},
                            {"role": "user", "content": command},
                        ],
                    },
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"].strip().upper()
            return "CHAT" if "CHAT" in result else "AUTOMATION"
        except Exception as exc:
            logger.error("Intent classification failed, defaulting to AUTOMATION: %s", exc)
            return "AUTOMATION"

    def handle_pure_chat(self, command: str) -> None:
        """Answers conversational questions directly without taking a screenshot."""
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "messages": [
                            {"role": "system", "content": "You are a helpful desktop assistant. Answer the user's question concisely."},
                            {"role": "user", "content": command},
                        ],
                    },
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]

            self._db.log_interaction(
                user_command=command,
                assistant_response=text,
                action_tag="[DONE]",
                execution_result="success",
            )
            self.status_signal.emit(text)
            self.finished_signal.emit("Chat complete.")
        except Exception as exc:
            self.error_signal.emit(f"Failed to fetch chat response: {exc}")
        finally:
            self.is_running = False

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
    # Action executor
    # ------------------------------------------------------------------

    def execute_action(self, action: ParsedAction) -> str:
        """Execute parsed action. Returns 'success' or 'error'."""
        if not action or action.action_type == ActionType.DONE:
            return "success"

        try:
            logger.info("Executing action: %s", action)

            if action.action_type == ActionType.CLICK:
                self.status_signal.emit(f"Clicking at ({action.x}, {action.y})")
                self._input_emulator.click_at(action.x, action.y)

            elif action.action_type == ActionType.TYPE:
                self.status_signal.emit(f"Typing at ({action.x}, {action.y})")
                self._input_emulator.type_at_coordinates(action.x, action.y, action.text)

            elif action.action_type == ActionType.PRESS:
                self.status_signal.emit(f"Pressing key: {action.key}")
                self._input_emulator.press_key(action.key)

            elif action.action_type == ActionType.SCROLL:
                self.status_signal.emit(f"Scrolling {action.direction} by {action.amount}")
                self._input_emulator.scroll(action.direction, action.amount)

            return "success"

        except Exception as exc:
            logger.error("Action execution failed: %s - %s", action, exc)
            self.status_signal.emit(f"Action failed: {type(exc).__name__}")
            return "error"

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
        # Allow Spotlight UI to fully hide before first screenshot
        time.sleep(config.UI_HIDE_DELAY_MS / 1000.0)

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

        # 5. Execute the action and record the result.
        action_tag_str = str(action) if action else None
        if action is None:
            execution_result = "skipped"
        elif action.action_type == ActionType.DONE:
            execution_result = "success"
        else:
            execution_result = self.execute_action(action)

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

        Mock mode (use_mock_ai=True):
            Cycles through self._mock_responses in order.  After the list is
            exhausted, returns "[DONE]" for every subsequent call.

        Real mode (use_mock_ai=False):
            Captures a screenshot, resizes it, and sends a multi-turn payload
            to the Grok Vision API.  Returns the model's text reply.

        Returns:
            A string containing exactly one action tag at the end (or [DONE]).
        """
        if self.use_mock_ai:
            idx = self.current_step - 1
            response = (
                self._mock_responses[idx]
                if idx < len(self._mock_responses)
                else "[DONE]"
            )
            logger.info(
                "get_ai_response [MOCK, step=%d]: %r", self.current_step, response
            )
            return response

        return self._call_grok_api()

    def _call_grok_api(self) -> str:
        """
        Perform a synchronous Grok Vision API call and return the response text.

        Captures the screen, resizes the image, builds the message payload with
        conversation history, and calls the API.  All errors are caught and a
        safe fallback string is returned so the caller's loop never crashes.
        """
        fallback = "I couldn't process the screen. [DONE]"

        # 1. Screenshot → optimised JPEG bytes.
        try:
            raw_jpeg = self._screen_capture.capture_jpeg_bytes()
            processed = self._image_processor.resize_for_grok(raw_jpeg)
            b64_image = base64.b64encode(processed.image_bytes).decode("utf-8")
            logger.info(
                "_call_grok_api: image captured — original=%dx%d, "
                "resized=%dx%d, payload_size=%d bytes",
                processed.original_width, processed.original_height,
                processed.resized_width, processed.resized_height,
                len(processed.image_bytes),
            )
        except Exception as exc:
            logger.error("_call_grok_api: screen capture failed: %s", exc)
            return fallback

        # 2. Build message list.
        history_context = self.build_history_context()

        history_note = (
            f"Previous steps summary:\n{history_context}\n\n"
            if history_context
            else ""
        )

        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{history_note}"
                            f"Current command: {self.current_command}"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{b64_image}"
                        },
                    },
                ],
            },
        ]

        payload = {
            "model": config.GROK_MODEL,
            "messages": messages,
        }

        logger.info(
            "_call_grok_api: sending request — model=%s, "
            "history_chars=%d, image attached",
            config.GROK_MODEL,
            len(history_context),
        )

        # 3. HTTP call.
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                )
            response.raise_for_status()
        except httpx.TimeoutException:
            logger.error("_call_grok_api: request timed out (30s)")
            return fallback
        except httpx.HTTPStatusError as exc:
            logger.error(
                "_call_grok_api: HTTP %d — %s",
                exc.response.status_code,
                exc.response.text[:300],
            )
            return fallback
        except Exception as exc:
            logger.error("_call_grok_api: unexpected error: %s", exc)
            return fallback

        # 4. Extract text from response.
        try:
            data = response.json()
            text: str = data["choices"][0]["message"]["content"]
            logger.info(
                "_call_grok_api: received response (%d chars)", len(text)
            )
            return text
        except Exception as exc:
            logger.error(
                "_call_grok_api: failed to parse response body: %s — raw: %s",
                exc,
                response.text[:300],
            )
            return fallback

    # ------------------------------------------------------------------
    # System prompt loader
    # ------------------------------------------------------------------

    @staticmethod
    def _load_system_prompt() -> str:
        """Read the system prompt from disk, returning an empty string on failure."""
        try:
            text = config.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
            logger.info(
                "_load_system_prompt: loaded %d chars from %s",
                len(text),
                config.SYSTEM_PROMPT_PATH,
            )
            return text
        except Exception as exc:
            logger.error(
                "_load_system_prompt: could not read %s: %s",
                config.SYSTEM_PROMPT_PATH,
                exc,
            )
            return ""
