# Local Desktop Automation Agent

**A vision-powered desktop automation agent for Windows**

Turn natural language instructions into precise desktop actions using
real-time screen understanding. Trigger with `Ctrl+Space`, describe your
goal, and watch the agent execute multi-step tasks autonomously — or just
ask it a question and get a streamed answer.

------------------------------------------------------------------------

## Features

-   **Hotkey-activated Spotlight UI** -- Clean, floating input window
    (`Ctrl+Space`)
-   **Chat or Automate** -- Each command is classified as `CHAT`
    (answered in the Spotlight panel, streamed token-by-token) or
    `AUTOMATION` (executed on the desktop with a status HUD)
-   **Real-time Vision** -- Captures the primary monitor and sends it to a
    vision model for screen understanding
-   **Intelligent Action Loop** -- Autonomous multi-step execution until
    the task is complete (`[DONE]`) or a budget/safety limit is hit
-   **Lightweight Planning** -- Decomposes a goal into sequential tasks,
    advancing through them via `[TASK_COMPLETE]`, with reflection and
    replanning when progress stalls
-   **Precise Input** -- Absolute scaled-image pixel coordinates remapped
    to physical display pixels; reliable mouse/keyboard control
-   **Conversation History** -- Text-only context (no screenshots) injected
    into each step
-   **Safety-First** -- Background execution, hold-`Esc` abort, clipboard
    backup/restore, and PyAutoGUI failsafe

------------------------------------------------------------------------

## Quick Start

### Prerequisites

-   **Windows 10/11** (primary monitor)
-   Python 3.10+
-   An API key for the inference gateway, set in a `.env` file or
    environment as `AICREDITS_API_KEY` (see [Configuration](#configuration))

### Installation

1.  **Clone or download** the project
2.  Create and activate a virtual environment:

``` bash
python -m venv venv
venv\Scripts\activate
```

3.  Install dependencies:

``` bash
pip install -r requirements.txt
```

4.  Create a `.env` file in the project root:

``` text
AICREDITS_API_KEY=your_key_here
```

5.  Run as Administrator (required for reliable input injection,
    especially into elevated windows)

------------------------------------------------------------------------

## Usage

1.  Run (as Administrator):

``` bash
python main.py
```

2.  Press `Ctrl+Space` to open the input window.
3.  Type your command:
    -   *Automation:* "Open Chrome, go to google.com, and search for
        'weather'"
    -   *Chat:* "What's the capital of France?"
4.  Press Enter. Chat answers stream into the Spotlight panel; automation
    hides the Spotlight and shows a status HUD while it works.
5.  Hold `Esc` at any time to abort a running task.

------------------------------------------------------------------------

## Configuration

Settings live in `config.py`. Common knobs:

| Constant | Default | Purpose |
| --- | --- | --- |
| `GROK_MODEL` | `openai/gpt-5.4` | Inference model id (via the aicredits gateway) |
| `MAX_IMAGE_SIZE` | `1920` | Longest image side (px) sent to the model |
| `MAX_STEPS_PER_COMMAND` | `15` | Soft per-command working budget |
| `MAX_ACTIONS` | `120` | Absolute runaway-protection ceiling |
| `MAX_REPLANS` | `5` | Replanning cycles before failing |
| `HOTKEY_COMBO` | `<ctrl>+<space>` | Global activation hotkey |

The API key is read from `AICREDITS_API_KEY` (see `config.GROK_API_KEY`).

------------------------------------------------------------------------

## Project Structure

``` text
local-desktop-agent/
├── main.py                 # Entry point (DPI bootstrap, wiring, Qt app)
├── config.py               # Settings & constants
├── requirements.txt
├── prompts/
│   └── system_prompt.txt
├── ui/
│   ├── spotlight.py        # Floating input + chat response panel
│   └── hud.py              # Click-through status HUD
├── automation/
│   ├── capture.py          # Primary-monitor screenshot → JPEG bytes
│   └── input_emulator.py   # Click / type / press / scroll
├── core/
│   ├── coordinator.py      # Capture→reason→act loop, planner/reflection
│   └── database.py         # SQLite interaction log
├── utils/
│   ├── image_processor.py  # Resize + absolute coordinate remapping
│   └── parser.py           # Action-tag parser
└── logs/                   # Rotating agent + latency logs
```

------------------------------------------------------------------------

## Key Technical Details

-   **Coordinate System:** The model reports positions in resized-image
    pixel space (longest side capped at `MAX_IMAGE_SIZE`); the
    `ImageProcessor` remaps these back to physical display pixels using the
    stored resize scale. DPI awareness (Per-Monitor v2) is set before any
    Qt or screen-measurement code runs.
-   **Action Tags:** `[CLICK:x,y]`, `[TYPE:x,y|text]`, `[PRESS:key]`,
    `[SCROLL:direction:amount]`, `[TASK_COMPLETE]` (advance to next plan
    task), and `[DONE]` (goal complete).
-   **Threading:** The UI stays responsive; the coordinator runs in a
    background `QThread` and communicates only via Qt signals.
-   **Input Safety:** Clipboard backup/restore with paste delay; physical
    coordinates via `pynput` + `ctypes`; PyAutoGUI failsafe enabled.
-   **Limits:** Soft budget of `MAX_STEPS_PER_COMMAND` (15) per command and
    a hard `MAX_ACTIONS` (120) ceiling; configurable inter-step delays.

------------------------------------------------------------------------

## Development & Customization

See `plan.md` for the phased implementation plan (with current
build-status annotations) and `hld_local_desktop_agent.md` for
architecture and design decisions.

### Important Notes

-   Always run as Administrator.
-   Test on your display scaling (100% / 125% / 150%).
-   DPI awareness is handled automatically.

------------------------------------------------------------------------

## License

MIT License --- feel free to modify and extend.

For issues, suggestions, or contributions, refer to the project files.
