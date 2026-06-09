# Local Desktop Automation Agent

**A Grok Vision-powered desktop automation agent for Windows**

Turn natural language instructions into precise desktop actions using
real-time screen understanding. Trigger with `Ctrl+Space`, describe your
goal, and watch the agent execute multi-step tasks autonomously.

------------------------------------------------------------------------

## Features

-   **Hotkey-activated Spotlight UI** -- Clean, floating input window
    (`Ctrl+Space`)
-   **Real-time Vision** -- Captures primary monitor and sends to Grok
    Vision API
-   **Intelligent Action Loop** -- Autonomous multi-step execution until
    task completion
-   **Precise Input** -- Normalized coordinate system + reliable
    mouse/keyboard control
-   **Conversation History** -- Maintains context across actions
-   **Safety-First** -- Background execution, Esc abort, clipboard
    protection, failsafe handling

------------------------------------------------------------------------

## Quick Start

### Prerequisites

-   **Windows 10/11** (single monitor)
-   Python 3.10+
-   xAI Grok API key (set as environment variable `GROK_API_KEY`)

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

4.  Run as Administrator (required for reliable input simulation)

------------------------------------------------------------------------

## Usage

1.  Run:

``` bash
python main.py
```

(as Administrator)

2.  Press `Ctrl+Space` to open the input window.
3.  Type your command (for example: *"Open Chrome, go to google.com, and
    search for 'weather'"*).
4.  Press Enter --- the agent will begin autonomous execution.
5.  Hold `Esc` at any time to abort.

------------------------------------------------------------------------

## Project Structure

``` text
local-desktop-agent/
├── main.py                 # Entry point
├── config.py               # Settings & constants
├── requirements.txt
├── prompts/
│   └── system_prompt.txt
├── ui/
│   └── spotlight.py
├── automation/
│   ├── capture.py
│   └── input_emulator.py
├── core/
│   ├── coordinator.py
│   └── database.py
├── utils/
│   ├── image_processor.py
│   └── parser.py
└── logs/                   # Interaction history
```

------------------------------------------------------------------------

## Key Technical Details

-   **Coordinate System:** Normalized 0--1000 grid (independent of
    resolution/DPI)
-   **Threading:** UI remains responsive; execution runs in a background
    QThread
-   **Input Safety:** Clipboard backup/restore with delay; physical
    coordinates via `pynput` + `ctypes`
-   **Limits:** Maximum 12 steps per command; configurable delays

------------------------------------------------------------------------

## Development & Customization

See `plan.md` for phased implementation details and
`hld_local_desktop_agent.md` for architecture and design decisions.

### Important Notes

-   Always run as Administrator.
-   Test on your display scaling (100% / 125% / 150%).
-   DPI awareness is handled automatically.

------------------------------------------------------------------------

## License

MIT License --- feel free to modify and extend.

Built with Grok Vision --- Powered by xAI.

For issues, suggestions, or contributions, refer to the project files.
