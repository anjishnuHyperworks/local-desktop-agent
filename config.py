"""
Central configuration for the Local Desktop Automation Agent.
All tuneable constants live here; import this module everywhere else.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROMPTS_DIR = BASE_DIR / "prompts"
LOGS_DIR = BASE_DIR / "logs"
DB_PATH = BASE_DIR / "agent_history.db"
SYSTEM_PROMPT_PATH = PROMPTS_DIR / "system_prompt.txt"

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
GROK_API_KEY: str = os.environ.get("AICREDITS_API_KEY", "")
GROK_API_URL: str = "https://api.aicredits.in/v1/chat/completions"
GROK_MODEL: str = "openai/gpt-5.4"

# ---------------------------------------------------------------------------
# Image / coordinate normalisation
# ---------------------------------------------------------------------------
MAX_IMAGE_SIZE: int = 1920          # Maximum dimension (px) sent to the model.
                                    # The model reports coordinates in this
                                    # resized-image pixel space; the coordinator
                                    # converts them back to physical screen px.
JPEG_QUALITY: int = 85              # Compression quality for API payloads

# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------
MAX_STEPS_PER_COMMAND: int = 15    # Soft working budget — protects latency, responsiveness, API cost
MAX_ACTIONS: int = 120              # Absolute runaway-protection ceiling — structural safety
UI_HIDE_DELAY_MS: int = 250         # ms to wait after hiding UI before first capture
STEP_DELAY_S: float = 0.5           # Pause between consecutive action steps (seconds)

# ---------------------------------------------------------------------------
# Long-horizon planning / reflection / replanning
# ---------------------------------------------------------------------------
MAX_REPLANS: int = 5                       # Hard ceiling on replanning cycles before failing
MAX_STUCK_TIME_S: float = 45.0             # Temporal stagnation: no successful action for this long
MAX_SEMANTIC_STAGNATION_STEPS: int = 8     # Semantic stagnation: steps without state advancement
MAX_CONSECUTIVE_FAILURES: int = 5          # Consecutive failed actions before recovery

# ---------------------------------------------------------------------------
# Input emulation timing (Windows-specific)
# ---------------------------------------------------------------------------
CLICK_MOVE_DURATION_S: float = 0.30    # Smooth mouse movement duration
FOCUS_REGISTRATION_DELAY_S: float = 0.15  # Delay after click before typing
CLIPBOARD_PASTE_DELAY_S: float = 0.10    # Delay after Ctrl+V for Windows paste

# ---------------------------------------------------------------------------
# Scroll unit normalisation
# ---------------------------------------------------------------------------
# Grok may emit large pixel values (e.g. 300) or small page-unit values (e.g. 3).
# Values ≤ SCROLL_UNIT_THRESHOLD are treated as direct wheel clicks;
# values above are divided by SCROLL_PIXEL_DIVISOR to convert to wheel clicks.
SCROLL_UNIT_THRESHOLD: int = 10
SCROLL_PIXEL_DIVISOR: int = 50

# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
HOTKEY_COMBO: str = "<ctrl>+<space>"   # pynput key combo string
SPOTLIGHT_WIDTH: int = 680
SPOTLIGHT_HEIGHT: int = 56
SPOTLIGHT_OPACITY: float = 0.92
SPOTLIGHT_MAX_EXPANDED_HEIGHT: int = 440   # cap for the chat response panel —
                                           # shorter answers shrink to fit,
                                           # longer ones scroll inside the panel

# Automation status HUD (small click-through pill shown while the agent works)
HUD_WIDTH: int = 480
HUD_HEIGHT: int = 44
HUD_MARGIN_BOTTOM: int = 60      # distance from the bottom edge of the screen
HUD_LINGER_MS: int = 3000        # final status stays visible this long before hiding

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE: Path = LOGS_DIR / "agent.log"

# ---------------------------------------------------------------------------
# History context
# ---------------------------------------------------------------------------
MAX_HISTORY_TURNS: int = 6   # Number of past turns injected into each Grok payload
