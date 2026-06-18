"""
Core Coordinator — capture → reason → act orchestration

The Coordinator is the central orchestrator of the automation loop.  It runs
exclusively inside a dedicated QThread worker and communicates with the UI
through Qt signals only — it never touches widgets directly.

Each command is first classified as CHAT (answered directly, streamed back to
the Spotlight panel) or AUTOMATION.  For automation, a lightweight planner
decomposes the goal into sequential tasks, then the high-frequency
capture → reason → action loop executes them.  When progress stalls the agent
escalates through AgentMode states (NORMAL → LOCAL_RETRY → REFLECTING →
REPLANNING) to recover, advancing the plan via [TASK_COMPLETE] and finishing on
[DONE].

Threading model:
    1. UI thread creates Coordinator and a QThread.
    2. coordinator.moveToThread(worker_thread) — Coordinator lives in that thread.
    3. UI emits command_submitted signal (connected to coordinator.start_command).
    4. Qt's queued-connection mechanism delivers the call into the worker thread.
    5. run_loop() executes entirely in the worker thread.
    6. stop_command() may be called directly from ANY thread — it only writes a
       boolean flag, which is safe.  It does NOT rely on queued signal delivery.

Signal inventory (all emitted from the worker thread):
    status_signal(str)        — progress updates shown in the UI status area
    intent_signal(str)        — "CHAT" or "AUTOMATION", emitted once per command
                                after classification so the UI can pick a flow
    chat_response_signal(str) — the answer text for a pure-chat command
    chat_token_signal(str)    — incremental token chunk for streaming chat
    finished_signal(str)      — task completed normally ([DONE] received)
    error_signal(str)         — task ended due to an error or max-step overflow
    abort_signal()            — task stopped because the user pressed Esc

AI modes:
    use_mock_ai=True replaces API calls with a fixed script that exercises the
    full pipeline — parser, DB, signals, planning/state machine — without
    network access:
        [CLICK:500,500] → [TASK_COMPLETE] → [TYPE:500,500|hello]
        → [TASK_COMPLETE] → [DONE]
    use_mock_ai=False sends real httpx requests to the vision model: the
    screenshot is captured, resized, base64-encoded, and sent alongside the
    plan context, conversation history, and the system prompt.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

_perf = logging.getLogger("perf")

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
# Planning Data Structures
# ---------------------------------------------------------------------------

class TaskStatus(Enum):
    """Status of a task in the plan."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SKIPPED = "skipped"


@dataclass
class Task:
    """A single task in the execution plan."""
    description: str
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class Plan:
    """A sequential plan of tasks for long-horizon execution."""
    goal: str
    tasks: list[Task]
    current_index: int = 0


@dataclass
class ReflectionResult:
    """Structured output from reflection phase."""
    root_cause: str
    failed_strategy: str
    discovered_information: str
    recommendation: str


class AgentMode(Enum):
    """Execution mode of the agent."""
    NORMAL = "normal"
    LOCAL_RETRY = "local_retry"
    REFLECTING = "reflecting"
    REPLANNING = "replanning"
    FINISHED = "finished"
    FAILED = "failed"
    CRASHED = "crashed"


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

    finished_signal      = pyqtSignal(str)   # Task completed successfully
    error_signal         = pyqtSignal(str)   # Fatal error or max-step exceeded
    abort_signal         = pyqtSignal()      # User-requested abort (Esc)
    status_signal        = pyqtSignal(str)   # Live progress messages
    intent_signal        = pyqtSignal(str)   # "CHAT" | "AUTOMATION" per command
    chat_response_signal = pyqtSignal(str)   # Answer text for pure-chat commands (non-streaming fallback)
    chat_token_signal    = pyqtSignal(str)   # Incremental token chunk for streaming chat

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
        self.total_actions: int = 0  # Tracks total actions across all commands
        self.current_mode: AgentMode = AgentMode.NORMAL
        self.current_plan: Optional[Plan] = None
        self.current_reflection: Optional[ReflectionResult] = None
        self.replan_count: int = 0

        # -- Last-command memory (drives context-aware intent routing) --------
        # A follow-up like "why didn't it open?" / "try again" is only
        # interpretable relative to the command that just ran. We remember the
        # previous automation command and how it ended so classify_intent can
        # treat a vague follow-up as a continuation of that task rather than as
        # context-free trivia.
        self._last_automation_command: Optional[str] = None
        self._last_automation_outcome: Optional[str] = None  # "finished" | "error" | "aborted"
        self._automation_succeeded: bool = False
        self._aborted_by_user: bool = False

        # Step budget for the current command. Derived from the plan size at
        # runtime (see _compute_step_budget); defaults to the static fallback
        # until a plan exists. MAX_ACTIONS is the separate, fixed runaway guard.
        self._step_budget: int = config.MAX_STEPS_PER_COMMAND

        # Consecutive [WAIT] guard — prevents the model from stalling forever
        # by waiting on a page that never changes.
        self._consecutive_waits: int = 0

        # Resize scale of the most recently sent screenshot. The model emits
        # coordinates in resized-image pixels; dividing by these recovers
        # physical screen pixels. 1.0 means "image not resized / identity".
        self._last_scale_x: float = 1.0
        self._last_scale_y: float = 1.0

        # -- Mock AI script ---------------------------------------------------
        # Cycled through in order; after exhaustion [DONE] is returned.
        self._mock_responses: list[str] = [
            "[CLICK:500,500]",
            "[TASK_COMPLETE]",
            "[TYPE:500,500|hello]",
            "[TASK_COMPLETE]",
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
    # Planner
    # ------------------------------------------------------------------

    def _build_prior_task_context(self) -> Optional[str]:
        """
        Summarise the immediately-preceding automation for the planner, so a
        follow-up command can be planned as a recovery/continuation.

        Returns None when there is no useful prior context — i.e. no automation
        has run yet, or the last one finished cleanly (a follow-up after success
        is almost always a fresh task, and stale context would only mislead the
        planner). Returns a short note for unfinished outcomes (error / aborted /
        still in_progress) since those are exactly the cases a follow-up tends to
        be about.
        """
        command = self._last_automation_command
        outcome = self._last_automation_outcome
        if not command or outcome in (None, "finished"):
            return None

        outcome_phrase = {
            "error": "did NOT complete — it failed or got stuck",
            "aborted": "was stopped by the user before completing",
            "in_progress": "was still running and did not reach completion",
        }.get(outcome, f"ended with outcome '{outcome}'")

        return (
            "Context — the user's previous desktop automation task "
            f"{outcome_phrase}:\n"
            f"  previous command: {command}\n"
            "The new goal below is most likely a FOLLOW-UP about that attempt "
            "(e.g. a correction, a request to retry, or to fix what went wrong). "
            "Plan the steps needed to recover and achieve the original intent on "
            "the CURRENT screen — do not assume a clean starting state, and check "
            "what is actually visible before acting."
        )

    def create_plan(self, goal: str, prior_context: Optional[str] = None) -> Plan:
        """
        Decompose the user's objective into a sequential list of tasks.

        This runs once at the beginning of a command. The planner is
        infrequent and therefore does not significantly affect latency.

        Args:
            goal: The user's command/objective
            prior_context: Optional summary of a desktop task that was just
                attempted (its command and outcome). Supplied when the new goal
                is a follow-up ("why didn't it open?", "fix the error", "try
                again") so the planner can plan a recovery/continuation instead
                of starting from a blank slate.

        Returns:
            A Plan with a list of tasks to accomplish the goal
        """
        if self.use_mock_ai:
            # Mock planner returns a simple 3-step plan
            tasks = [
                Task(description="Analyze the current screen state"),
                Task(description="Execute the requested action"),
                Task(description="Verify completion"),
            ]
            return Plan(goal=goal, tasks=tasks, current_index=0)

        # When this goal is a follow-up to a just-attempted task, give the
        # planner that context so a vague correction ("fix it", "try again")
        # becomes a concrete recovery plan rather than an empty/literal one.
        prior_note = (
            f"{prior_context}\n\n" if prior_context else ""
        )

        # Real planner calls the AI to decompose the goal
        planner_prompt = (
            "You are a task planner for a desktop automation agent.\n"
            "Decompose the user's goal into a sequential list of specific, actionable tasks.\n"
            "Each task should be a single step that can be accomplished by observing the screen "
            "and taking one or more actions (click, type, scroll, press).\n"
            "Do NOT create standalone tasks for waiting, loading, or confirming that a page "
            "has appeared — the executor handles waiting on its own. Plan only the actions that "
            "advance the goal, and keep the list as short as the goal allows.\n\n"
            f"{prior_note}"
            f"User Goal: {goal}\n\n"
            "Output your response as a numbered list of tasks, one per line. "
            "Be specific but concise. Example:\n"
            "1. Open Chrome browser\n"
            "2. Navigate to Google Sheets\n"
            "3. Inspect the spreadsheet\n"
        )

        # The planner runs once per command and its decomposition drives the
        # executor's step budget, so a transient timeout collapsing it to a
        # single-task fallback is expensive — it starves multi-step goals. Retry
        # on transient failures before giving up; only fall back when every
        # attempt fails.
        last_exc: Optional[Exception] = None
        for attempt in range(1, config.PLANNER_MAX_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=config.PLANNER_TIMEOUT_S) as client:
                    response = client.post(
                        config.GROK_API_URL,
                        json={
                            "model": config.GROK_MODEL,
                            "messages": [
                                {"role": "system", "content": planner_prompt},
                                {"role": "user", "content": goal},
                            ],
                        },
                        headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                    )
                response.raise_for_status()
                result = response.json()["choices"][0]["message"]["content"].strip()

                # Parse the numbered list into tasks
                tasks = []
                for line in result.split('\n'):
                    line = line.strip()
                    if line and (line[0].isdigit() or line.startswith('-')):
                        # Remove the number/bullet and any leading punctuation
                        task_desc = line.split('.', 1)[-1].split('-', 1)[-1].strip()
                        if task_desc:
                            tasks.append(Task(description=task_desc))

                if not tasks:
                    # Fallback if parsing failed
                    tasks = [Task(description=goal)]

                logger.info(
                    "Planner created %d tasks for goal (attempt %d/%d): %s",
                    len(tasks), attempt, config.PLANNER_MAX_ATTEMPTS, goal,
                )
                return Plan(goal=goal, tasks=tasks, current_index=0)

            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Planner attempt %d/%d failed: %s",
                    attempt, config.PLANNER_MAX_ATTEMPTS, exc,
                )

        logger.error(
            "Planner failed after %d attempt(s), using single-task fallback: %s",
            config.PLANNER_MAX_ATTEMPTS, last_exc,
        )
        # Fallback: single task with the original goal
        tasks = [Task(description=goal)]
        return Plan(goal=goal, tasks=tasks, current_index=0)

    # ------------------------------------------------------------------
    # Reflection
    # ------------------------------------------------------------------

    def reflect(self, recent_history: str) -> ReflectionResult:
        """
        Perform self-diagnosis when progress has stalled.

        This is triggered when the agent enters REFLECTING mode. It produces
        a structured ReflectionResult that will be used by the replanner.

        Args:
            recent_history: Recent execution history for context

        Returns:
            A ReflectionResult with structured diagnosis
        """
        if self.use_mock_ai:
            # Mock reflection returns a simple diagnosis
            return ReflectionResult(
                root_cause="Task execution stalled",
                failed_strategy="Direct action execution",
                discovered_information="Screen state did not change as expected",
                recommendation="Try a different approach or break down the task further"
            )

        reflection_prompt = (
            "You are a desktop automation agent performing self-diagnosis.\n"
            "Your previous approach has stalled and you need to understand why.\n\n"
            f"Current Goal: {self.current_command}\n"
            f"Recent Execution History:\n{recent_history}\n\n"
            "Analyze the situation and provide:\n"
            "1. Root cause: Why did progress stall?\n"
            "2. Failed strategy: What approach didn't work?\n"
            "3. Discovered information: What did you learn?\n"
            "4. Recommendation: What should you try instead?\n\n"
            "Format your response as a JSON object with these four keys:\n"
            "{\n"
            '  "root_cause": "...",\n'
            '  "failed_strategy": "...",\n'
            '  "discovered_information": "...",\n'
            '  "recommendation": "..."\n'
            "}\n"
        )

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "messages": [
                            {"role": "system", "content": reflection_prompt},
                            {"role": "user", "content": recent_history},
                        ],
                    },
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"].strip()

            # Parse JSON response
            try:
                import re
                # Extract JSON from response (in case there's surrounding text)
                json_match = re.search(r'\{.*\}', result, re.DOTALL)
                if json_match:
                    result = json_match.group(0)

                reflection_data = json.loads(result)
                return ReflectionResult(
                    root_cause=reflection_data.get("root_cause", "Unknown cause"),
                    failed_strategy=reflection_data.get("failed_strategy", "Unknown strategy"),
                    discovered_information=reflection_data.get("discovered_information", "No new information"),
                    recommendation=reflection_data.get("recommendation", "Try a different approach")
                )
            except json.JSONDecodeError:
                logger.error("Failed to parse reflection JSON, using fallback")
                return ReflectionResult(
                    root_cause="Parsing error",
                    failed_strategy="Reflection parsing",
                    discovered_information="Could not parse reflection output",
                    recommendation="Proceed with generic recovery strategy"
                )

        except Exception as exc:
            logger.error("Reflection failed, using fallback: %s", exc)
            return ReflectionResult(
                root_cause="Reflection API error",
                failed_strategy="Reflection call",
                discovered_information=f"Error: {exc}",
                recommendation="Attempt to continue with current strategy"
            )

    # ------------------------------------------------------------------
    # Replanning
    # ------------------------------------------------------------------

    def replan(self, reflection: ReflectionResult) -> Plan:
        """
        Revise the remaining tasks based on reflection results.

        This is triggered when the agent enters REPLANNING mode. It modifies
        only the remaining tasks in the plan, using the reflection to guide
        the revision.

        Args:
            reflection: The structured reflection result from the reflect() call

        Returns:
            An updated Plan with revised remaining tasks
        """
        if self.current_plan is None:
            logger.warning("Replan called with no current plan, creating new plan")
            return self.create_plan(self.current_command)

        # Get remaining tasks
        remaining_tasks = self.current_plan.tasks[self.current_plan.current_index:]
        remaining_descriptions = [task.description for task in remaining_tasks]

        if self.use_mock_ai:
            # Mock replanner adds a diagnostic step before remaining tasks
            new_tasks = [
                Task(description=f"Address issue: {reflection.root_cause}"),
                Task(description=f"Try alternative: {reflection.recommendation}"),
                *remaining_tasks
            ]
            updated_plan = Plan(
                goal=self.current_plan.goal,
                tasks=new_tasks,
                current_index=0
            )
            logger.info("Mock replanner revised plan with %d tasks", len(new_tasks))
            return updated_plan

        # Real replanner uses reflection to revise remaining tasks
        replan_prompt = (
            "You are a task replanner for a desktop automation agent.\n"
            "Your previous approach has stalled and you need to revise the remaining tasks.\n\n"
            f"Current Goal: {self.current_plan.goal}\n"
            f"Remaining Tasks:\n" + "\n".join(f"{i+1}. {desc}" for i, desc in enumerate(remaining_descriptions)) + "\n\n"
            f"Reflection Summary:\n"
            f"Root Cause: {reflection.root_cause}\n"
            f"Failed Strategy: {reflection.failed_strategy}\n"
            f"Discovered Information: {reflection.discovered_information}\n"
            f"Recommendation: {reflection.recommendation}\n\n"
            "Revise only the remaining tasks. Avoid repeating the failed approach. "
            "Output your response as a numbered list of revised tasks, one per line.\n"
        )

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "messages": [
                            {"role": "system", "content": replan_prompt},
                            {"role": "user", "content": f"Revise these tasks:\n" + "\n".join(remaining_descriptions)},
                        ],
                    },
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                )
            response.raise_for_status()
            result = response.json()["choices"][0]["message"]["content"].strip()

            # Parse the numbered list into new tasks
            new_tasks = []
            for line in result.split('\n'):
                line = line.strip()
                if line and (line[0].isdigit() or line.startswith('-')):
                    # Remove the number/bullet and any leading punctuation
                    task_desc = line.split('.', 1)[-1].split('-', 1)[-1].strip()
                    if task_desc:
                        new_tasks.append(Task(description=task_desc))

            if not new_tasks:
                # Fallback if parsing failed - keep original remaining tasks
                logger.warning("Replanner parsing failed, keeping original remaining tasks")
                new_tasks = remaining_tasks

            # Combine completed tasks with new remaining tasks
            completed_tasks = self.current_plan.tasks[:self.current_plan.current_index]
            updated_plan = Plan(
                goal=self.current_plan.goal,
                tasks=completed_tasks + new_tasks,
                current_index=len(completed_tasks)
            )

            self.replan_count += 1
            logger.info("Replanner revised plan with %d new tasks (replan #%d)", len(new_tasks), self.replan_count)
            return updated_plan

        except Exception as exc:
            logger.error("Replanner failed, keeping original plan: %s", exc)
            # Fallback: keep the original plan
            return self.current_plan

    # ------------------------------------------------------------------
    # Plan progression
    # ------------------------------------------------------------------

    def _advance_task(self) -> bool:
        """
        Mark the current plan task COMPLETED and advance to the next one.

        Returns:
            True if there are still pending tasks after advancing,
            False if the plan is exhausted (all tasks done).
        """
        plan = self.current_plan
        if plan is None or plan.current_index >= len(plan.tasks):
            return False

        plan.tasks[plan.current_index].status = TaskStatus.COMPLETED
        plan.current_index += 1

        if plan.current_index < len(plan.tasks):
            next_task = plan.tasks[plan.current_index]
            next_task.status = TaskStatus.IN_PROGRESS
            logger.info(
                "Advanced to task %d/%d: %s",
                plan.current_index + 1, len(plan.tasks), next_task.description,
            )
            self.status_signal.emit(
                f"Task {plan.current_index + 1}/{len(plan.tasks)}: {next_task.description}"
            )
            return True

        logger.info("All %d plan tasks completed", len(plan.tasks))
        return False

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
        self.current_mode = AgentMode.NORMAL
        self.replan_count = 0

        self.status_signal.emit("Analyzing intent...")

        try:
            _t0_command = time.perf_counter()

            _t0 = time.perf_counter()
            intent = self.classify_intent(command)
            _perf.info("[latency] intent_classification=%.3fs", time.perf_counter() - _t0)

            self.intent_signal.emit(intent)
            self._command_start_time = _t0_command

            if intent == "CHAT":
                logger.info("Routing to Pure Chat handler.")
                self.handle_pure_chat(command)
            else:
                logger.info("Routing to Desktop Automation loop.")
                self.status_signal.emit(f"Starting automation: {command}")

                # Build follow-up context from the PREVIOUS automation (if any)
                # before we overwrite the memory below. This lets the planner
                # turn a vague correction into a concrete recovery plan.
                prior_context = self._build_prior_task_context()

                # Create plan at the beginning of automation
                _t0 = time.perf_counter()
                self.current_plan = self.create_plan(command, prior_context=prior_context)
                if self.current_plan.tasks:
                    self.current_plan.tasks[0].status = TaskStatus.IN_PROGRESS
                self._step_budget = self._compute_step_budget()
                _perf.info("[latency] planning=%.3fs", time.perf_counter() - _t0)

                # Remember this as the most recent automation so a follow-up
                # ("why didn't it work?", "try again") routes back to AUTOMATION.
                # The outcome is filled in when run_loop terminates.
                self._last_automation_command = command
                self._last_automation_outcome = "in_progress"

                # run_loop() manages is_running internally
                self.run_loop()

        except Exception as exc:
            logger.exception("Fatal error during intent routing: %s", exc)
            self.is_running = False
            self.error_signal.emit(f"Failed to process command: {exc}")
        # No finally: CHAT releases via handle_pure_chat(), AUTOMATION via run_loop()

    def classify_intent(self, command: str) -> str:
        """
        Determine whether the command requires desktop automation or is a pure
        chat/knowledge question.

        Context-aware: if a desktop automation command just ran (and especially
        if it stalled or errored), a vague follow-up such as "why didn't it
        open?", "that didn't work", "try again", or "fix the error" is a
        continuation of that task — it must route to AUTOMATION, not be answered
        as standalone trivia. We pass the previous command and its outcome to the
        router so it can resolve those references instead of seeing a
        context-free question.
        """
        if self.use_mock_ai:
            return "AUTOMATION"

        classification_prompt = (
            "You are an intent router for a desktop automation agent.\n"
            "Analyze the user's input and classify it into one of two categories:\n"
            "1. AUTOMATION: If the user is asking to control the computer (click, type, scroll, open an app), OR asking about what is currently on their screen (e.g. 'what's on the screen?', 'what do you see?', 'read the screen', 'what's open?', 'describe the screen').\n"
            "2. CHAT: If the user is asking a general knowledge question, greeting you, asking for calculations, or having a casual conversation that has nothing to do with their current screen or computer state.\n\n"
            "IMPORTANT — follow-up rule: If a desktop automation task was just attempted, a follow-up that refers to it implicitly is a CONTINUATION and must be AUTOMATION. This includes messages like 'why didn't it work?', 'that didn't open', 'try again', 'fix the error', 'it failed', 'do it now', 'continue', or any complaint/correction about the previous attempt. Only classify such a follow-up as CHAT if it is CLEARLY unrelated general knowledge.\n\n"
            "Output EXACTLY 'AUTOMATION' or 'CHAT'. Do not include any other text."
        )

        # Build the prior-task context, if any. An automation that ended in
        # error/abort makes a follow-up far more likely to be a continuation.
        if self._last_automation_command:
            outcome = self._last_automation_outcome or "unknown"
            context_note = (
                "Context — a desktop automation task was just attempted:\n"
                f"  previous command: {self._last_automation_command}\n"
                f"  outcome: {outcome}\n\n"
                "Now classify the user's NEW input below, applying the follow-up rule.\n\n"
                f"User input: {command}"
            )
        else:
            context_note = command

        try:
            with httpx.Client(timeout=5.0) as client:
                response = client.post(
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "messages": [
                            {"role": "system", "content": classification_prompt},
                            {"role": "user", "content": context_note},
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
        """Streams a chat answer token-by-token via chat_token_signal."""
        _t0 = time.perf_counter()
        accumulated: list[str] = []
        try:
            with httpx.Client(timeout=30.0) as client:
                with client.stream(
                    "POST",
                    config.GROK_API_URL,
                    json={
                        "model": config.GROK_MODEL,
                        "stream": True,
                        "messages": [
                            {"role": "system", "content": "You are a helpful desktop assistant. Answer the user's question concisely."},
                            {"role": "user", "content": command},
                        ],
                    },
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                ) as response:
                    response.raise_for_status()
                    _perf.info("[latency] chat_first_byte=%.3fs", time.perf_counter() - _t0)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                            token = chunk["choices"][0]["delta"].get("content", "")
                            if token:
                                accumulated.append(token)
                                self.chat_token_signal.emit(token)
                        except Exception:
                            pass

            full_text = "".join(accumulated)
            _perf.info("[latency] chat_api_call=%.3fs", time.perf_counter() - _t0)
            self._db.log_interaction(
                user_command=command,
                assistant_response=full_text,
                action_tag="[DONE]",
                execution_result="success",
            )
            total = time.perf_counter() - getattr(self, "_command_start_time", _t0)
            _perf.info("[latency] total_chat_command=%.3fs", total)
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
        self._aborted_by_user = True
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
            _t0 = time.perf_counter()

            if action.action_type == ActionType.CLICK:
                px, py = self._image_processor.image_to_physical(
                    action.x, action.y, self._last_scale_x, self._last_scale_y
                )
                self.status_signal.emit(f"Clicking at ({px}, {py})")
                self._input_emulator.click_at(px, py)

            elif action.action_type == ActionType.TYPE:
                px, py = self._image_processor.image_to_physical(
                    action.x, action.y, self._last_scale_x, self._last_scale_y
                )
                self.status_signal.emit(f"Typing at ({px}, {py})")
                self._input_emulator.type_at_coordinates(px, py, action.text)

            elif action.action_type == ActionType.PRESS:
                self.status_signal.emit(f"Pressing key: {action.key}")
                self._input_emulator.press_key(action.key)

            elif action.action_type == ActionType.SCROLL:
                self.status_signal.emit(f"Scrolling {action.direction} by {action.amount}")
                self._input_emulator.scroll(action.direction, action.amount)

            elif action.action_type == ActionType.WAIT:
                secs = action.seconds if action.seconds is not None else config.WAIT_DEFAULT_S
                secs = max(0.0, min(secs, config.WAIT_MAX_S))
                self.status_signal.emit(f"Waiting {secs:g}s for the screen to settle...")
                time.sleep(secs)

            elif action.action_type == ActionType.NEW_TAB:
                self.status_signal.emit("Opening a new browser tab...")
                self._input_emulator.open_new_browser_tab()

            _perf.info("[latency] action_execution(%s)=%.3fs", action.action_type.name, time.perf_counter() - _t0)
            return "success"

        except Exception as exc:
            logger.error("Action execution failed: %s - %s", action, exc)
            self.status_signal.emit(f"Action failed: {type(exc).__name__}")
            self._consecutive_failures += 1
            self._steps_without_progress += 1
            return "error"

    def _check_stagnation(self) -> bool:
        """
        Check if execution has stalled and requires recovery.

        Returns True if stagnation is detected, False otherwise.
        """
        # Check temporal stagnation (no progress for too long)
        time_since_success = time.perf_counter() - self._last_success_time
        if time_since_success > config.MAX_STUCK_TIME_S:
            logger.warning("Stagnation detected: no progress for %.1fs", time_since_success)
            return True

        # Check semantic stagnation (too many steps without state advancement)
        if self._steps_without_progress > config.MAX_SEMANTIC_STAGNATION_STEPS:
            logger.warning("Stagnation detected: %d steps without progress", self._steps_without_progress)
            return True

        # Check consecutive failures
        if self._consecutive_failures >= config.MAX_CONSECUTIVE_FAILURES:
            logger.warning("Stagnation detected: %d consecutive failures", self._consecutive_failures)
            return True

        return False

    def _compute_step_budget(self) -> int:
        """
        Derive the soft step budget for the current command from the plan size.

        Each planned task needs at least one step, plus retries, waits and the
        occasional confirmation step — so a flat ceiling that ignores task count
        (the old behaviour) kills multi-task plans before they can finish. We
        grant BASE_STEPS of fixed headroom plus STEPS_PER_TASK per task, capped
        by MAX_ACTIONS so the absolute runaway guard is never exceeded.

        Falls back to MAX_STEPS_PER_COMMAND when there is no plan.
        """
        if not self.current_plan or not self.current_plan.tasks:
            return config.MAX_STEPS_PER_COMMAND

        num_tasks = len(self.current_plan.tasks)
        budget = config.BASE_STEPS + config.STEPS_PER_TASK * num_tasks
        budget = min(budget, config.MAX_ACTIONS)
        logger.info(
            "_compute_step_budget: %d tasks → step budget %d (cap %d)",
            num_tasks, budget, config.MAX_ACTIONS,
        )
        return budget

    # ------------------------------------------------------------------
    # Main execution loop (runs entirely in the worker thread)
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        """
        The main capture-reason-execute cycle with planning and reflection.

        Termination conditions (deterministic, mutually exclusive):
            DONE tag received      → finished_signal emitted
            Max steps exceeded     → error_signal emitted
            Max actions exceeded   → error_signal emitted
            stop_command() called  → abort_signal emitted (and nothing else)
            Unhandled exception    → error_signal emitted

        The loop emits status_signal at the start of each step so the UI can
        display progress in real time.
        """
        # Allow Spotlight UI to fully hide before first screenshot
        time.sleep(config.UI_HIDE_DELAY_MS / 1000.0)

        _loop_start = time.perf_counter()
        logger.info(
            "run_loop started — command=%r, step_budget=%d, max_actions=%d, mock=%s",
            self.current_command,
            self._step_budget,
            config.MAX_ACTIONS,
            self.use_mock_ai,
        )

        # Stagnation tracking
        self._last_success_time = time.perf_counter()
        self._consecutive_failures = 0
        self._steps_without_progress = 0
        self._consecutive_waits = 0
        self._retry_attempted = False   # escalation stage: LOCAL_RETRY → REFLECTING

        # Outcome marker for last-command memory. _handle_step_outcome flips this
        # to True on genuine completion; any other exit (budget, abort, error,
        # exception) leaves it False and is resolved in the finally below.
        self._automation_succeeded = False
        self._aborted_by_user = False

        try:
            while (
                self.is_running
                and self.current_step < self._step_budget
                and self.total_actions < config.MAX_ACTIONS
            ):
                self.current_step += 1
                self.total_actions += 1
                logger.info(
                    "Loop iteration %d/%d (total actions: %d/%d, mode: %s)",
                    self.current_step,
                    self._step_budget,
                    self.total_actions,
                    config.MAX_ACTIONS,
                    self.current_mode.value,
                )

                # Emit step progress to the UI.
                if self.current_plan and self.current_plan.current_index < len(self.current_plan.tasks):
                    current_task = self.current_plan.tasks[self.current_plan.current_index]
                    task_info = f"Task {self.current_plan.current_index + 1}/{len(self.current_plan.tasks)}: {current_task.description}"
                else:
                    task_info = "Executing..."
                self.status_signal.emit(
                    f"Step {self.current_step}/{self._step_budget} — {task_info}"
                )

                # Execute one reasoning + action cycle based on current mode
                if self.current_mode == AgentMode.NORMAL:
                    if self._handle_step_outcome(self.process_single_step(), _loop_start):
                        return
                elif self.current_mode == AgentMode.REFLECTING:
                    # Perform reflection
                    self.status_signal.emit("Reflecting on stalled progress...")
                    recent_history = self.build_history_context()
                    self.current_reflection = self.reflect(recent_history)
                    logger.info(
                        "Reflection result: root_cause=%s, recommendation=%s",
                        self.current_reflection.root_cause,
                        self.current_reflection.recommendation,
                    )
                    # Transition to REPLANNING
                    self.current_mode = AgentMode.REPLANNING
                    continue
                elif self.current_mode == AgentMode.REPLANNING:
                    # Perform replanning
                    if self.replan_count >= config.MAX_REPLANS:
                        logger.warning("Max replans reached, failing")
                        self.is_running = False
                        self.error_signal.emit("Task failed: Maximum replans exceeded.")
                        return
                    self.status_signal.emit("Replanning based on reflection...")
                    self.current_plan = self.replan(self.current_reflection)
                    # A replan may add tasks; extend the budget to fit them
                    # (never shrink — steps already spent must still count).
                    self._step_budget = max(
                        self._step_budget, self._compute_step_budget()
                    )
                    # Transition back to NORMAL with a clean recovery slate so the
                    # next stall (if any) re-escalates from LOCAL_RETRY.
                    self.current_mode = AgentMode.NORMAL
                    self._consecutive_failures = 0
                    self._steps_without_progress = 0
                    self._last_success_time = time.perf_counter()
                    self._retry_attempted = False
                    continue
                elif self.current_mode == AgentMode.LOCAL_RETRY:
                    # Execute a retry step
                    outcome = self.process_single_step()
                    if self._handle_step_outcome(outcome, _loop_start):
                        return
                    if outcome == ActionType.TASK_COMPLETE:
                        # Retry succeeded in finishing the task — back to NORMAL.
                        self.current_mode = AgentMode.NORMAL
                        continue
                    # If retry produced no progress, transition to REFLECTING.
                    self.current_mode = AgentMode.REFLECTING
                    continue

                # Check for stagnation and trigger state transitions.
                # Linear escalation per the plan: a fresh stall first attempts a
                # cheap LOCAL_RETRY; if stagnation is still present afterwards we
                # escalate to REFLECTING. _retry_attempted tracks the stage so it
                # is not conflated with the _consecutive_failures tally.
                if self._check_stagnation():
                    if not self._retry_attempted:
                        logger.info("Stagnation detected, entering LOCAL_RETRY")
                        self.current_mode = AgentMode.LOCAL_RETRY
                        self._retry_attempted = True
                    else:
                        logger.info("Stagnation persists, entering REFLECTING")
                        self.current_mode = AgentMode.REFLECTING
                    continue

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
            if self.current_step >= self._step_budget:
                logger.warning(
                    "run_loop: step budget (%d) exceeded — aborting",
                    self._step_budget,
                )
                self.is_running = False
                self.error_signal.emit(
                    "Task aborted: Maximum execution steps exceeded."
                )
            elif self.total_actions >= config.MAX_ACTIONS:
                logger.warning(
                    "run_loop: max actions (%d) exceeded — aborting",
                    config.MAX_ACTIONS,
                )
                self.is_running = False
                self.error_signal.emit(
                    "Task aborted: Maximum actions exceeded."
                )

        except Exception as exc:
            logger.exception("run_loop: fatal unhandled exception: %s", exc)
            self.is_running = False
            self.error_signal.emit(f"Fatal error encountered: {exc}")

        finally:
            # Resolve last-command memory exactly once, regardless of which exit
            # path the loop took. A vague follow-up after a non-"finished"
            # outcome ("why didn't it work?") will then route back to AUTOMATION.
            if self._automation_succeeded:
                self._last_automation_outcome = "finished"
            elif self._aborted_by_user:
                self._last_automation_outcome = "aborted"
            else:
                self._last_automation_outcome = "error"
            logger.info(
                "run_loop: outcome recorded for follow-up routing → %s",
                self._last_automation_outcome,
            )

    def _handle_step_outcome(
        self, outcome: Optional[ActionType], loop_start: float
    ) -> bool:
        """
        Interpret a control outcome from process_single_step().

        DONE finishes the goal — but only when the plan has no pending tasks.
        Models often emit [DONE] after finishing a single subtask (meaning "this
        task is done") instead of the correct [TASK_COMPLETE]. When that happens
        mid-plan we treat [DONE] as [TASK_COMPLETE] so the remaining tasks aren't
        abandoned. TASK_COMPLETE advances the plan to the next task; if that was
        the last task, the goal is also finished.

        Returns:
            True if the goal is complete and run_loop should return,
            False to keep iterating.
        """
        if outcome == ActionType.DONE:
            plan = self.current_plan
            mid_plan = (
                plan is not None
                and plan.current_index < len(plan.tasks) - 1
            )
            if mid_plan:
                logger.info(
                    "run_loop: [DONE] received mid-plan (task %d/%d) — "
                    "treating as [TASK_COMPLETE] to avoid abandoning "
                    "remaining tasks",
                    plan.current_index + 1, len(plan.tasks),
                )
                self._advance_task()
                return False

            logger.info("run_loop: [DONE] received — finishing normally")
            _perf.info(
                "[latency] total_automation_loop=%.3fs steps=%d",
                time.perf_counter() - loop_start,
                self.current_step,
            )
            self.is_running = False
            self._automation_succeeded = True
            self.finished_signal.emit("Task completed successfully.")
            return True

        if outcome == ActionType.TASK_COMPLETE:
            has_more = self._advance_task()
            if not has_more:
                # Final task done — treat as goal completion.
                logger.info("run_loop: final task complete — finishing")
                _perf.info(
                    "[latency] total_automation_loop=%.3fs steps=%d",
                    time.perf_counter() - loop_start,
                    self.current_step,
                )
                self.is_running = False
                self._automation_succeeded = True
                self.finished_signal.emit("Task completed successfully.")
                return True

        return False

    # ------------------------------------------------------------------
    # Single iteration: fetch AI response → parse → log → return done flag
    # ------------------------------------------------------------------

    def process_single_step(self) -> Optional[ActionType]:
        """
        Execute one complete step of the reasoning loop.

        Sequence:
            1. Fetch history context from the database.
            2. Obtain AI response (mock or real).
            3. Parse the response for an action tag.
            4. Emit status update with the parsed action.
            5. Log the interaction to the database.

        Returns:
            ActionType.DONE          if the goal is complete,
            ActionType.TASK_COMPLETE if the current plan task is complete,
            None                     to keep iterating.
        """
        _t0_step = time.perf_counter()

        # 1. Load conversation history (text summaries only).
        _t0 = time.perf_counter()
        history = self.build_history_context()
        _perf.info("[latency] step=%d db_history_fetch=%.3fs", self.current_step, time.perf_counter() - _t0)
        logger.debug(
            "process_single_step: history context length=%d chars", len(history)
        )

        # 2. Get AI response (mock or real).
        _t0 = time.perf_counter()
        raw_response = self.get_ai_response()
        _perf.info("[latency] step=%d ai_response=%.3fs", self.current_step, time.perf_counter() - _t0)
        logger.info("AI response (step %d): %r", self.current_step, raw_response)

        # 3. Parse for action tag.
        action: Optional[ParsedAction] = self._parser.parse(raw_response)
        prose = self._parser.remove_action_tag(raw_response)

        # 4. Emit human-readable status.
        if action is None:
            status_msg = f"AI responded (no action tag): {prose[:80]}"
        elif action.action_type == ActionType.DONE:
            status_msg = "AI signalled goal complete."
        elif action.action_type == ActionType.TASK_COMPLETE:
            status_msg = "AI signalled current task complete."
        else:
            status_msg = f"Action → {action}"

        self.status_signal.emit(status_msg)
        logger.info("process_single_step: %s", status_msg)

        # 5. Execute the action and record the result.
        action_tag_str = str(action) if action else None
        if action is None:
            execution_result = "skipped"
            self._consecutive_waits = 0
        elif action.action_type in (ActionType.DONE, ActionType.TASK_COMPLETE):
            execution_result = "success"
            self._consecutive_waits = 0
            # A control tag is genuine progress — reset stagnation counters so a
            # multi-task goal isn't flagged stuck just because individual tasks
            # complete without a coordinate action on that step.
            self._last_success_time = time.perf_counter()
            self._consecutive_failures = 0
            self._steps_without_progress = 0
            self._retry_attempted = False
        elif action.action_type == ActionType.WAIT:
            # WAIT just sleeps and re-observes (e.g. while a page loads). It is
            # not real work, so refund the step it would otherwise consume —
            # unless the model has been waiting repeatedly, in which case we let
            # it count toward the budget so a stuck page can't loop forever.
            self._consecutive_waits += 1
            execution_result = self.execute_action(action)
            if self._consecutive_waits <= config.MAX_CONSECUTIVE_WAITS:
                self.current_step -= 1
                self.total_actions -= 1
            else:
                logger.warning(
                    "process_single_step: %d consecutive WAITs — no longer "
                    "refunding the step budget",
                    self._consecutive_waits,
                )
        else:
            execution_result = self.execute_action(action)
            self._consecutive_waits = 0
            # Reset stagnation counters on successful action execution
            if execution_result == "success":
                self._last_success_time = time.perf_counter()
                self._consecutive_failures = 0
                self._steps_without_progress = 0
                self._retry_attempted = False

        _t0 = time.perf_counter()
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
        _perf.info("[latency] step=%d db_log=%.3fs", self.current_step, time.perf_counter() - _t0)
        _perf.info("[latency] step=%d total_step=%.3fs", self.current_step, time.perf_counter() - _t0_step)

        # Surface control outcomes (DONE / TASK_COMPLETE) so run_loop can either
        # finish the goal or advance the plan to the next task.
        if action is not None and action.action_type in (
            ActionType.DONE, ActionType.TASK_COMPLETE
        ):
            return action.action_type
        return None

    # ------------------------------------------------------------------
    # History context builder
    # ------------------------------------------------------------------

    def build_history_context(self) -> str:
        """
        Fetch recent interactions from the DB and format them as an explicit,
        text-only world-state summary suitable for injection into an AI prompt.

        Rather than flat "User/Assistant/Action" prose (which forces the model to
        re-derive what it tried and whether it worked on every step), this emits a
        numbered step log carrying each step's reasoning, the exact action tag,
        and its execution result — plus a "Current state" block summarising the
        most recent attempt and how many times the identical action has been
        repeated. That gives the model the last_action / result / attempt_count
        framing it needs to notice a stuck approach and change tack.

        Format example:
            Recent steps (oldest first):
              1. action=[CLICK:1022,970] result=success
                 reasoning: Clicking the Chrome taskbar icon.
              2. action=[CLICK:965,970] result=success
                 reasoning: Trying the taskbar Search box instead.

            Current state:
              last_action: [CLICK:965,970]
              last_result: success
              repeated_action_count: 2 (this same action has been issued 2 step(s) in a row)
              note: The last action executed but the goal is not yet confirmed
                    complete — verify on the screenshot whether it had the
                    intended effect before repeating it.

        Constraints:
            - No screenshots, no image bytes, no Base64 strings.
            - Limited to config.MAX_HISTORY_TURNS most-recent turns.
        """
        try:
            records = self._db.get_recent_history(limit=config.MAX_HISTORY_TURNS)
        except Exception as exc:
            logger.error("build_history_context: DB read failed: %s", exc)
            return ""

        # Keep only records that represent an actual agent step (they carry an
        # action tag). The very first row of a command also holds the user
        # command with no action tag; surface that as the goal line.
        step_records = [r for r in records if r.action_tag]
        if not step_records:
            logger.debug("build_history_context: no step history available")
            return ""

        lines: list[str] = ["Recent steps (oldest first):"]
        for i, record in enumerate(step_records, start=1):
            result = record.execution_result or "unknown"
            lines.append(f"  {i}. action={record.action_tag} result={result}")
            if record.assistant_response:
                # Collapse the model's reasoning to a single compact line.
                reasoning = " ".join(record.assistant_response.split())
                if len(reasoning) > 200:
                    reasoning = reasoning[:197] + "..."
                lines.append(f"     reasoning: {reasoning}")

        # -- Explicit current-state block ------------------------------------
        last = step_records[-1]
        last_result = last.execution_result or "unknown"

        # How many times the identical action tag has been issued at the tail —
        # a high count is the signal that the current approach is stuck.
        repeated = 0
        for record in reversed(step_records):
            if record.action_tag == last.action_tag:
                repeated += 1
            else:
                break

        lines.append("")
        lines.append("Current state:")
        lines.append(f"  last_action: {last.action_tag}")
        lines.append(f"  last_result: {last_result}")
        lines.append(
            f"  repeated_action_count: {repeated} "
            f"(this same action has been issued {repeated} step(s) in a row)"
        )
        if last_result == "error":
            lines.append(
                "  note: The last action failed to execute. Do not repeat it "
                "verbatim — pick a different target or approach."
            )
        elif repeated >= 2:
            lines.append(
                "  note: You have issued the SAME action repeatedly without "
                "confirmed progress. The previous attempts likely did not have "
                "the intended effect (e.g. the click missed its target or focus "
                "went elsewhere). Change approach — re-check the element's "
                "coordinates on the screenshot or try a different element."
            )
        else:
            lines.append(
                "  note: The last action executed, but success is not confirmed. "
                "Verify on the screenshot whether it had the intended effect "
                "before continuing."
            )

        context = "\n".join(lines).strip()
        logger.debug(
            "build_history_context: %d step record(s) → %d chars",
            len(step_records), len(context),
        )
        return context

    # ------------------------------------------------------------------
    # AI response provider (mock script or real vision API)
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

    def _build_plan_context(self) -> str:
        """
        Render the current plan (goal, task checklist with the active task
        marked) plus the [TASK_COMPLETE] instruction and any reflection hint,
        for injection into the per-step API request.

        Returns an empty string if there is no plan.
        """
        plan = self.current_plan
        if plan is None or not plan.tasks:
            return ""

        lines = ["Plan progress:"]
        for i, task in enumerate(plan.tasks):
            marker = "→" if i == plan.current_index else (
                "✓" if i < plan.current_index else " "
            )
            lines.append(f"  {marker} {i + 1}. {task.description}")

        if plan.current_index < len(plan.tasks):
            current = plan.tasks[plan.current_index].description
            lines.append(f"\nFocus on the current task (marked →): {current}")
            lines.append(
                "When this specific task is finished, emit [TASK_COMPLETE] to "
                "advance to the next task. Emit [DONE] only when the entire goal "
                "is achieved."
            )

        if self.current_reflection is not None:
            lines.append(
                f"\nNote from self-diagnosis — avoid repeating the failed "
                f"approach ({self.current_reflection.failed_strategy}). "
                f"Recommended: {self.current_reflection.recommendation}"
            )

        return "\n".join(lines) + "\n\n"

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
            _t0 = time.perf_counter()
            raw_jpeg = self._screen_capture.capture_jpeg_bytes()
            _perf.info("[latency] screenshot_capture=%.3fs", time.perf_counter() - _t0)

            _t0 = time.perf_counter()
            processed = self._image_processor.resize_for_grok(raw_jpeg)
            b64_image = base64.b64encode(processed.image_bytes).decode("utf-8")
            _perf.info("[latency] image_resize_encode=%.3fs", time.perf_counter() - _t0)

            # Remember the resize scale so execute_action can map the model's
            # image-pixel coordinates back to physical screen pixels.
            self._last_scale_x = processed.scale_x
            self._last_scale_y = processed.scale_y

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

        plan_note = self._build_plan_context()

        messages = [
            {"role": "system", "content": self._system_prompt},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"{history_note}"
                            f"{plan_note}"
                            f"Overall goal: {self.current_command}"
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

        # 3. Streaming HTTP call — accumulate SSE chunks.
        payload["stream"] = True
        accumulated: list[str] = []
        try:
            _t0 = time.perf_counter()
            with httpx.Client(timeout=30.0) as client:
                with client.stream(
                    "POST",
                    config.GROK_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {config.GROK_API_KEY}"},
                ) as response:
                    response.raise_for_status()
                    _perf.info("[latency] grok_first_byte=%.3fs", time.perf_counter() - _t0)
                    for line in response.iter_lines():
                        if not line.startswith("data:"):
                            continue
                        payload_str = line[5:].strip()
                        if payload_str == "[DONE]":
                            break
                        try:
                            token = json.loads(payload_str)["choices"][0]["delta"].get("content", "")
                            if token:
                                accumulated.append(token)
                        except Exception:
                            pass
            _perf.info("[latency] grok_http_call=%.3fs", time.perf_counter() - _t0)
        except httpx.TimeoutException:
            logger.error("_call_grok_api: request timed out (30s)")
            return fallback
        except httpx.HTTPStatusError as exc:
            # The response is streamed, so its body isn't read yet. We must
            # read() it before touching .text, or httpx raises ResponseNotRead.
            try:
                exc.response.read()
                body = exc.response.text[:300]
            except Exception:
                body = "<unreadable response body>"
            logger.error(
                "_call_grok_api: HTTP %d — %s",
                exc.response.status_code,
                body,
            )
            return fallback
        except Exception as exc:
            logger.error("_call_grok_api: unexpected error: %s", exc)
            return fallback

        text = "".join(accumulated)
        if not text:
            logger.error("_call_grok_api: empty response from stream")
            return fallback
        logger.info("_call_grok_api: received response (%d chars)", len(text))
        return text

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
