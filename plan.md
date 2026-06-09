# Project Implementation Plan: Local Desktop Automation Agent

## Overview
This document provides a **step-by-step iterative plan** for an LLM (like Grok, Claude, or Cursor) to build the full local desktop automation agent described in `hld_local_desktop_agent.md`.

The development follows a modular, bottom-up + integration approach to minimize bugs and allow early testing.

**Goal**: Create a working Python application that:
- Activates via `Ctrl+Space`
- Shows a floating Spotlight-style input
- Captures screen (primary monitor)
- Sends to Grok Vision API with history context
- Parses actions like `[CLICK:x,y]`, `[TYPE:x,y|text]`, `[PRESS:key_name]`, `[SCROLL:direction:amount]`
- Executes mouse/keyboard actions in a **continuous loop** until `[DONE]`

---

## Prerequisites

1. **Environment Setup**
   - Python 3.10+
   - Create a virtual environment: `python -m venv venv`
   - Install core dependencies (will be expanded per phase):
     ```bash
     pip install pyqt6 pynput mss pillow pyautogui httpx pyperclip
     ```

2. **API Key**
   - Obtain xAI Grok API key and store it securely (e.g., via environment variable `GROK_API_KEY`).

3. **Windows-Specific**
   - Run the script/terminal **as Administrator** for UAC compatibility.
   - Document DPI awareness and physical coordinate handling.

4. **Project Structure** (to be created in Phase 1)

---

## Phase 1: Project Setup & Structure

**Objective**: Set up the repository skeleton and basic configuration.

**Steps for LLM**:
1. Create the directory structure as previously defined.
2. Generate `requirements.txt` with all necessary packages.
3. Create `config.py` for API keys, constants (e.g., `MAX_IMAGE_SIZE=1280`, `UI_HIDE_DELAY_MS=250`, `MAX_STEPS_PER_COMMAND=12`, `NORMALIZED_GRID=1000`)
4. In `main.py`: Initialize Windows DPI awareness with `ctypes.windll.shcore.SetProcessDpiAwareness(2)` and set `pyautogui.FAILSAFE = True` as a compatibility/visual cue only. The actual emergency abort should come from the `pynput` `Esc` listener.
5. Add basic logging and error handling setup.
6. Write a simple `main.py` that just prints "Agent starting..." for now.

**Deliverable**: Runnable skeleton with `python main.py` working (run as Admin).

---

## Phase 2: UI Layer (Floating Spotlight)

**Objective**: Implement the hotkey-triggered floating input window.

**Steps for LLM**:
1. Implement `ui/spotlight.py` using **PyQt6**:
   - Borderless, semi-transparent window
   - Centered on the primary monitor
   - Text input field
   - Global hotkey listener (`Ctrl+Space`) using `pynput`
   - Add emergency abort (hold `Esc`)
2. **Critical**: Add thread-safe signals (`pyqtSignal`) for showing/hiding — never call Qt methods directly from pynput listener thread. Add abort handler from background thread.
3. Style it to look like Spotlight/Raycast.
4. On Enter: hide window, emit command to coordinator to start loop.

**Milestone**: Standalone UI component that can send text commands.

---

## Phase 3: OS Automation Layer

**Objective**: Screen capture and input execution.

**Steps for LLM**:
1. Implement `automation/capture.py`:
   - Use `mss` for **primary monitor only** (Monitor 0)
   - Return in-memory JPEG bytes
2. Implement `automation/input_emulator.py`:
   - Use **pynput.mouse.Controller** or `ctypes.windll.user32.SetCursorPos` for accurate physical clicks/movement.
   - `click_at(x, y)` with smooth movement (0.2s duration)
   - `type_string(text)`: **Implement clipboard context manager with 100ms sleep** after paste, and use `pynput` for the paste key sequence instead of `pyautogui.hotkey`.
   - `press_key(key_name)` (support common keys like enter, tab, etc.)
   - `scroll(direction: str, amount: int)` implementation
   - Handle **normalized 0-1000 coordinates** scaling to physical screen resolution
3. Add DPI verification utility (test mouse movement to physical corners).
4. Add cursor position logic.

**Milestone**: Ability to capture screen and perform test clicks/types/presses/scrolls reliably on scaled displays.

---

## Phase 4: Core Coordinator & Utilities

**Objective**: Glue logic, image processing, and state management.

**Steps for LLM**:
1. Implement `utils/image_processor.py`:
   - Resize image maintaining aspect ratio (max 1280px)
   - Calculate and store scale_factor for **normalized 0-1000 grid**
2. Implement `core/database.py`:
   - SQLite table for interaction_logs
3. Implement `utils/parser.py`:
   - Regex extractors for updated tags: `[CLICK:x,y]`, `[TYPE:x,y|text]`, `[PRESS:key_name]`, `[SCROLL:direction:amount]`, and `[DONE]`
4. Implement `core/coordinator.py`:
   - Main orchestration class running in **background QThread**.
   - Fetch recent history from DB (text summaries only)
   - Build multi-turn message history for API **with current image only**
   - Enforce `MAX_STEPS_PER_COMMAND`
   - Use the `pynput` `Esc` listener as the primary abort trigger to set `self.is_running = False` and stop the loop. PyAutoGUI failsafe should remain only as a compatibility indicator, not the relied-upon emergency stop for mouse movement driven by `pynput`/`ctypes`.

**Milestone**: Coordinator can process a command with context and return parsed actions in background thread.

---

## Phase 5: AI Engine Integration

**Objective**: Connect to Grok Vision API.

**Steps for LLM**:
1. Create API client in `core/coordinator.py`.
2. Implement payload construction supporting **multi-turn conversation messages**. 
   - Only the **latest user message** includes the current Base64 screenshot.
   - All prior assistant/user messages in history are text-only summaries of actions and results.
3. Use `httpx` for POST to `https://api.x.ai/v1/chat/completions`.
4. Handle response parsing including new action tags (`[TYPE:x,y|text]`).
5. Load system prompt from `prompts/system_prompt.txt`.

**Milestone**: End-to-end call with conversation history context.

---

## Phase 6: Full Integration & Main Loop

**Objective**: Wire everything together with continuous execution.

**Steps for LLM**:
1. Update `main.py` to initialize all components.
2. Implement the **multi-step while loop** inside the background worker thread in coordinator:
   - Capture → Send to Grok with history → Parse → Execute → Log → Repeat
   - Break loop on `[DONE]`, error threshold, max steps, or manual abort (including FailSafe).
3. Add **250ms** delay (configurable) after hiding UI before first capture.
4. **Normalized coordinate scaling** using physical dimensions.
5. Add user feedback (notifications/toasts on actions and completion).
6. **Robust error handling** including full loop termination on failsafe.

**Milestone**: Full working prototype with autonomous multi-step task execution triggered by hotkey and responsive UI.

---

## Phase 7: Prompt Engineering & Refinement

**Objective**: Make AI responses reliable.

(unchanged, with emphasis on new action formats)

---

## Phase 8: Error Handling, Edge Cases & Polish

**Objective**: Production readiness.

**Additional focus**:
- Verify background thread prevents UI freeze.
- Test failsafe abort (mouse to corner immediately stops and restores UI).
- Confirm clipboard paste with delay.
- Test on 100%/125%/150% scaling.

---

## Phase 9: Testing & Iteration

**Test Scenarios**:
- ... (same)
- Specifically test failsafe, threading, clipboard, coordinates.

**Final Steps**:
1. Create a README.md with setup and usage instructions (emphasize Run as Admin).
2. Add `.gitignore`
3. Optional: Packaging (PyInstaller for executable)

---

## Development Workflow Tips for the LLM

- **Iterate Phase by Phase**
- Reference updated HLD for threading, DPI, clipboard fixes.
- **Next Action**: Start with **Phase 1**. Once complete, confirm and proceed to Phase 2.

This plan ensures systematic, low-risk development with the addressed critical fixes.