# Project Implementation Plan: Local Desktop Automation Agent

## Overview
This document provides the definitive, **phase-by-phase implementation plan** for an LLM (like Grok, Claude, or Cursor) to build the production-grade local desktop automation agent described in `hld_local_desktop_agent.md`.

It synthesizes the original modular layout with structural reliability enhancements: a formal **finite-state machine** (`AgentMode`), an **independent vitality watchdog**, **deterministic failure fingerprinting**, **frequency-based oscillation detection**, an **API circuit breaker**, and a **versioned, append-only EventStore** with crash recovery.

The development follows a safety-first, bottom-up + integration approach to minimize bugs and allow early testing.

> ### ⚠ Implementation Status (reconciled 2026-06-17)
>
> This document is the **aspirational** production plan. The shipped codebase
> deliberately tracks a lighter-weight design (see
> `Architecture Clarifications & Reconciliations`), so several hardening
> features below are **deferred, not built**. Statuses are annotated per phase.
> Treat unbuilt items as a future roadmap, not as missing work.
>
> **Built:** DPI bootstrap, Spotlight UI + Status HUD, screen capture, absolute
> coordinate remapping, input emulator, action parser, the capture→reason→act
> coordinator loop, SQLite interaction logging, and a lightweight
> planner / reflection / replanning layer (`Plan`, `Task`, `ReflectionResult`,
> `AgentMode`) with `[TASK_COMPLETE]` task progression.
>
> **Deferred (in plan, not in code):** versioned append-only `EventStore` with
> WAL + `schema_info`, `core/safety.py` (`ApiCircuitBreaker`, threaded
> `Watchdog`), deterministic failure fingerprinting, frequency-based
> oscillation detection, action-dedup guard, `resume_session()` crash recovery,
> and the advanced-safety constants in §3 below.
>
> **Diverged on purpose:** persistence is `core/database.py`
> (`InteractionDatabase`), not `EventStore`; WAL is intentionally *not* used
> (per-operation connections + a write lock serialise access instead); the
> model is `gpt-5.4` via the aicredits gateway, not xAI Grok; `MAX_IMAGE_SIZE`
> is 1920, not 1280.

**Goal**: Create a working Python application that:
- Activates via `Ctrl+Space`
- Shows a floating Spotlight-style input plus a non-intrusive Status HUD
- Captures screen (primary monitor)
- Sends to Grok Vision API with text-only history context
- Parses actions like `[CLICK:x,y]`, `[TYPE:x,y|text]`, `[PRESS:key_name]`, `[SCROLL:direction:amount]`
- Executes mouse/keyboard actions in a **supervised continuous loop** until `[DONE]` or a structural budget / safety trip terminates it

> **Important Coordinate Resolution Note:** To maintain absolute alignment with the visual processing behavior of modern vision models and resolve underlying configuration contradictions, the entire system uses **absolute scaled-image pixel positions** (top-left = `0,0`), which are mapped back onto physical display coordinates by the `ImageProcessor` layer.

---

## Prerequisites & Configuration

### 1. Environment & Administrative Privileges
- Python 3.10+ virtual environment: `python -m venv venv`
- Install core dependencies (expanded per phase):
  ```bash
  pip install pyqt6 pynput mss pillow pyautogui httpx pyperclip
  ```
- The local terminal/runner **must be executed as Administrator** to enable Windows UAC input injection safety.

### 2. API Key
- Obtain the xAI Grok API key and store it securely (e.g., environment variable `GROK_API_KEY`).

### 3. Required Configurations (`config.py`)

> **Status — ⚠ Partial.** Of the thresholds below, only `MAX_ACTIONS`,
> `MAX_REPLANS`, `MAX_CONSECUTIVE_FAILURES`, `MAX_STUCK_TIME`
> (as `MAX_STUCK_TIME_S`) and `MAX_SEMANTIC_STAGNATION_STEPS` exist in the
> current `config.py`. The circuit-breaker, watchdog, oscillation, EventStore,
> runtime/timeout, and escalation-cooldown constants are **not defined** —
> their subsystems are deferred. Note `MAX_IMAGE_SIZE` is **1920** in the code,
> not 1280.

Define these exact production thresholds:
```python
# Hard Loop Budgets
MAX_ACTIONS = 120
MAX_REPLANS = 5
MAX_RUNTIME_MINUTES = 30
ACTION_TIMEOUT = 12
MAX_CONSECUTIVE_FAILURES = 5

# Stagnation & Oscillation Failsafes
MAX_STUCK_TIME = 45                  # Max seconds without forward progress
MAX_SEMANTIC_STAGNATION_STEPS = 8    # Max loop steps without state advancement
MAX_OSCILLATION_COUNT = 2            # Max times a distinct failure hash can repeat

# Escalation Control
LOCAL_RETRY_THRESHOLD = 2
REFLECTION_COOLDOWN = 25
MIN_REPLAN_INTERVAL = 35

# Advanced Safety Timing & Sizing
CIRCUIT_BREAKER_FAILURE_THRESHOLD = 3
CIRCUIT_BREAKER_TIMEOUT = 120        # Cooldown duration in seconds
WATCHDOG_TIMEOUT = 60                # Hard timeout for loop heartbeat
MAX_EVENTSTORE_ENTRIES = 500
MAX_SCREENSHOT_HISTORY = 20
STATE_SCHEMA_VERSION = 1
```
Plus existing UI/image constants (e.g., `MAX_IMAGE_SIZE=1280`, `UI_HIDE_DELAY_MS=250`).

---

## Phase 1: Core Architecture & Safety Primitives

> **Status — ⚠ Partial.** Directory structure, `config.py`, and the DPI-aware
> `main.py` bootstrap (step 3) are **built** and treated as protected
> invariants. `pyautogui.FAILSAFE = True` is set. The `ApiCircuitBreaker` and
> threaded `Watchdog` (steps 4–5, `core/safety.py`) are **not implemented** —
> `core/safety.py` does not exist.

**Objective**: Establish the hardened configuration, process environment context, and standalone safety utility layers.

**Steps for LLM**:
1. Create the directory structure (`ui/`, `automation/`, `core/`, `utils/`, `prompts/`).
2. Generate `requirements.txt` and `config.py` with all thresholds above.
3. **DPI-Aware Bootstrapping (`main.py`)**: Declare strict Win32 argument types for `SetProcessDpiAwarenessContext`, passing handle variables explicitly to prevent coordinate mismatching. Set `pyautogui.FAILSAFE = True` purely as a secondary compatibility visual trigger.
4. **Circuit Breaker (`core/safety.py`)**: Implement `ApiCircuitBreaker`, tracking structural network errors inside a sliding time window. On breaching `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, trip into an **OPEN** state for `CIRCUIT_BREAKER_TIMEOUT` (120s) to prevent API burning.
5. **Threaded Watchdog (`core/safety.py`)**: Implement a dedicated, isolated Watchdog thread tracking the primary execution loop's heartbeat.
6. Add basic logging and error-handling setup; `main.py` prints "Agent starting..." for now.

**Deliverable**: Runnable skeleton (`python main.py`, run as Admin) with config, DPI bootstrap, circuit breaker, and watchdog scaffolding.

---

## Phase 2: Atomic Data Layer & Versioned Event Store

> **Status — ⚠ Diverged / Partial.** Persistence exists as
> `core/database.py` (`InteractionDatabase`), a thread-safe SQLite interaction
> log — **not** the versioned append-only `EventStore` this phase specifies.
> No `schema_info` / `STATE_SCHEMA_VERSION`, no FSM `transition_to()` records,
> no `MAX_EVENTSTORE_ENTRIES` / `MAX_SCREENSHOT_HISTORY` bounding. **WAL is
> intentionally not used** — per-operation connections plus a `threading.Lock`
> serialise writes instead (documented in `database.py`). The `AgentMode` FSM
> enum exists in `core/coordinator.py`, but `CRASHED` / `FAILED` are never
> assigned and transitions are not logged.

**Objective**: Build a thread-safe, append-only persistence layer supporting atomic state writes, structural forensics, and crash recovery.

**Steps for LLM**:
1. **Schema Versioning (`core/event_store.py`)**: Implement `EventStore` on SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL;`). Embed a strict internal `schema_info` validation checkpoint tracking `STATE_SCHEMA_VERSION = 1`.
2. **FSM Enforcement**: Structure log interactions to track distinct execution modes (`AgentMode`):
   - **NORMAL** — Standard step cycle
   - **LOCAL_RETRY** — Executing local corrective adjustments
   - **REFLECTING** — Self-diagnosing execution stagnation
   - **REPLANNING** — Re-indexing the active subtask tree layout
   - **FINISHED** — Graceful completion
   - **FAILED** — Exceeded internal structural failure budgets
   - **CRASHED** — Watchdog-terminated lockups
3. **Telemetry Tracking**: Force all state transitions through `transition_to(target_mode)`, writing automated transition records directly into the append-only table.
4. Bound retained history with `MAX_EVENTSTORE_ENTRIES` and `MAX_SCREENSHOT_HISTORY`.

**Milestone**: Versioned, append-only event store with FSM-aware transitions and bounded growth.

---

## Phase 3: Modern Spotlight UI & Status HUD

> **Status — ✅ Built.** `ui/spotlight.py` and `ui/hud.py` exist with the
> hotkey-activated Spotlight, `pyqtSignal`-based threading, and the click-through
> HUD. Verify the Win32 focus injection (step 3) and `WDA_EXCLUDEFROMCAPTURE`
> (step 4) details against the current widgets if relying on them.

**Objective**: Implement the hotkey-activated query input and non-intrusive runtime overlay widgets.

**Steps for LLM**:
1. **Spotlight Geometry (`ui/spotlight.py`)**: Construct a borderless, translucent PyQt6 search window. Use a low-level `pynput` daemon thread to wire the global `Ctrl+Space` activation safely. Add emergency abort (hold `Esc`).
2. **Critical**: Use thread-safe `pyqtSignal` for all show/hide and abort operations — never call Qt methods directly from the pynput listener or coordinator thread.
3. **Win32 Focus Injection**: Immediately after layout realization, map native Win32 APIs (`AttachThreadInput`, `SetForegroundWindow`, `SetFocus`) to lock OS input attention and mitigate keyboard focus drop.
4. **Status HUD (`ui/hud.py`)**: Build a click-through, non-activating floating progress widget showing the current `AgentMode` + progress. Enforce `SetWindowDisplayAffinity(handle, WDA_EXCLUDEFROMCAPTURE)` so the HUD is systematically removed from desktop screenshots.
5. On Enter: hide Spotlight, emit command to coordinator to start the loop.

**Milestone**: Standalone UI that sends commands and a HUD that never appears in captures.

---

## Phase 4: OS Automation & Absolute Remapping

> **Status — ✅ Built (one constant differs).** `automation/capture.py`,
> `utils/image_processor.py` (absolute inverse remapping via stored
> `scale_x` / `scale_y`), and `automation/input_emulator.py` (click/type/press/
> scroll, focus-to-type delay, scroll normalisation) are all implemented. The
> resize cap is **1920px** (`MAX_IMAGE_SIZE`), not the 1280px written below.

**Objective**: Implement precision screen interaction utilities working in absolute coordinates.

**Steps for LLM**:
1. **Screen Capture (`automation/capture.py`)**: Extract raw primary monitor frames (Monitor 0) into in-memory arrays via `mss`; return in-memory JPEG bytes.
2. **Absolute Inverse Math (`utils/image_processor.py`)**: Resize maintaining aspect ratio (max 1280px) and store `ProcessedImage.scale_x` / `scale_y`. Write remapping formulas that decode positions from scaled-down screenshots back into real physical display pixels by dividing coordinates against stored scaling values:
   ```math
   scale_x = physical_width  / processed_width
   scale_y = physical_height / processed_height
   final_physical_x = grok_x * scale_x
   final_physical_y = grok_y * scale_y
   ```
3. **Input Emulator (`automation/input_emulator.py`)**: Use **pynput.mouse.Controller** or `ctypes.windll.user32.SetCursorPos` for accurate physical clicks/movement.
   - `click_at(x, y)` with smooth movement (0.2s duration)
   - `type_string(text)`: clipboard context manager with 100ms sleep after paste, using `pynput` for the paste key sequence (not `pyautogui.hotkey`)
   - **Focus-to-Type Delays**: `type_at_coordinates(x, y, text)` clicks to focus, then `time.sleep(0.15)` before pasting so Windows registers active carets
   - `press_key(key_name)` (enter, tab, backspace, escape, etc.)
   - **Scroll Unit Normalization**: values ≤ 10 → direct wheel detents; values > 10 → scaled down via `amount // 50` to match pixel targets safely
4. Add a DPI verification utility (test mouse movement to physical corners).

**Milestone**: Reliable capture and click/type/press/scroll on scaled displays using absolute pixel remapping.

---

## Phase 5: The Cognitive Execution Engine

> **Status — ⚠ Partial.** The parser (step 1, now also `[TASK_COMPLETE]`) and
> multi-turn inference with text history + image only in the final user frame
> (step 5, loading `prompts/system_prompt.txt`) are **built**. Deterministic
> fingerprinting (step 2), frequency-based oscillation tracking (step 3), and
> the action-deduplication guard (step 4) are **not implemented**. In their
> place the coordinator has a lightweight planner / `reflect()` / `replan()`
> recovery layer (from the addendum) driving the `AgentMode` escalation
> NORMAL → LOCAL_RETRY → REFLECTING → REPLANNING.

**Objective**: Construct the unified coordination loop managing inference processing and deterministic error checking.

**Steps for LLM**:
1. **Parser (`utils/parser.py`)**: Regex extractors for `[CLICK:x,y]`, `[TYPE:x,y|text]`, `[PRESS:key_name]`, `[SCROLL:direction:amount]`, and `[DONE]`.
2. **Deterministic Fingerprinting (`core/coordinator.py`)**:
   ```python
   def generate_deterministic_fingerprint(self, failure_summary: str) -> str:
       normalized = failure_summary.lower().strip().encode('utf-8')
       return hashlib.sha256(normalized).hexdigest()
   ```
3. **Frequency-Based Oscillation Tracking**: Store failure hashes in an aging sliding-window queue `deque(maxlen=3)`. Before triggering a cycle, confirm the active signature count is low:
   ```python
   if self.recent_replan_reasons.count(reason_hash) >= config.MAX_OSCILLATION_COUNT:
       self.transition_to(AgentMode.FAILED,
           "Infinite loop oscillation detected via telemetry signature tracking.")
       break
   ```
4. **Action Deduplication Guard**: Cache the hash of the last successfully executed step block (`self.last_executed_hash`). If consecutive cycles yield identical action payloads, drop execution immediately.
5. **Inference Execution (`core/coordinator.py`)**: Build multi-turn system prompts passing conversation logs as **text context**, embedding the desktop image **only in the final user message frame**. Load the system prompt from `prompts/system_prompt.txt`.

**Milestone**: Coordinator processes a command with context, fingerprints failures, detects oscillation, dedups actions, and returns parsed actions.

---

## Phase 6: Full Integration & Watchdog Telemetry

> **Status — ⚠ Partial.** The single-QThread worker lifecycle (step 1), the
> multi-step Capture→reason→execute→log→HUD loop with `[DONE]` / `MAX_ACTIONS` /
> `MAX_STEPS_PER_COMMAND` / abort breaks (step 2), the 250ms post-hide delay
> (step 3), and the dual stagnation check (step 5) are **built**. The watchdog
> telemetry hooks / `handle_watchdog_crash` (step 4) and `resume_session()`
> crash recovery (step 6) are **not implemented** — there is no watchdog and no
> session resume. Note the loop also enforces `MAX_STEPS_PER_COMMAND` (a soft
> per-command budget) not listed in the original step 2.

**Objective**: Combine subsystems into a robust execution loop that can recover from crashes.

**Steps for LLM**:
1. **Single Worker Lifecycle (`core/coordinator.py`)**: Launch the primary coordinator loop on an independent `QThread`. Avoid distributed executors; rely on cooperative `self.is_running` monitoring to prevent thread leaks.
2. Implement the **multi-step loop**: Capture → remap → send to Grok with text history → parse → fingerprint/dedup/oscillation check → execute → log → update HUD → repeat. Break on `[DONE]`, hard budgets (`MAX_ACTIONS`, `MAX_RUNTIME_MINUTES`, `MAX_CONSECUTIVE_FAILURES`), oscillation, or manual `Esc` abort.
3. Add **250ms** delay (configurable) after hiding the UI before the first capture.
4. **Watchdog Telemetry Hooks**: If the coordinator hits an un-alertable OS wait or locks up, the external Watchdog triggers an override:
   ```python
   def handle_watchdog_crash(self, diagnostic_reason: str):
       self.is_running = False
       self.transition_to(AgentMode.CRASHED, context=diagnostic_reason)
       self._event_store.log_event(self.current_mode, "WATCHDOG_TERMINATION",
           {"error": "Vitality heartbeat failed.", "trace": diagnostic_reason})
       # Fire emergency UI state restorations...
   ```
5. **Dual Stagnation Check**: Track elapsed time since last success alongside structural loop iterations. If `MAX_STUCK_TIME` (temporal) or `MAX_SEMANTIC_STAGNATION_STEPS` (semantic) is crossed, force a `REFLECTING` sequence.
6. **Crash Recovery Validation (`resume_session()`)**: On startup, parse the persistent EventStore. If the last recorded index reads `CRASHED`, pull cached subtask parameters to safely restore agent state.

**Milestone**: Full working prototype with autonomous, watchdog-supervised multi-step execution and crash recovery.

---

## Phase 7: Production Rigor & Verification Scenarios

> **Status — ⚠ Deferred.** Most scenarios below exercise subsystems that are
> not built (circuit breaker, watchdog, oscillation, dedup, crash resume,
> `WDA_EXCLUDEFROMCAPTURE`) and are therefore **not yet applicable**. The
> coordinate-accuracy, clipboard/focus/scroll, and `Esc`-abort scenarios *are*
> relevant to the current build. The README exists but is **outdated** — it
> predates the model switch (aicredits `gpt-5.4`), the planner layer, and the
> 1920px image cap; refresh it before relying on it.

**Objective**: Validate system performance under real-world operating conditions and failure states.

**Test Scenarios**:
- Coordinate accuracy on 100% / 125% / 150% scaling (absolute remapping).
- Circuit breaker trips after repeated API errors and recovers after cooldown.
- Watchdog terminates a simulated lockup and recovery restores UI/state.
- Oscillation detection breaks an induced repeating-failure loop.
- Action dedup guard drops identical consecutive payloads.
- Crash resume (`resume_session()`) restores from a `CRASHED` record.
- Clipboard backup/restore with delay; click-to-type focus delay; scroll normalization.
- HUD confirmed absent from screenshots (`WDA_EXCLUDEFROMCAPTURE`).
- `Esc` abort immediately stops the loop and restores the UI.

**Final Steps**:
1. Create a `README.md` with setup and usage instructions (emphasize Run as Admin).
2. Add `.gitignore`.
3. Optional: Packaging (PyInstaller for executable).

---

## Development Workflow Tips for the LLM

- **Iterate Phase by Phase** — safety primitives and persistence (Phases 1–2) come before UI and automation.
- Reference the updated HLD for the FSM, watchdog, circuit breaker, absolute coordinate remapping, EventStore, and HUD-exclusion details.
- **Next Action**: Start with **Phase 1**. Once complete, confirm and proceed to Phase 2.

This plan ensures systematic, low-risk development toward a hardened, self-recovering production agent.
