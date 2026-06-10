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
GROK_API_KEY: str = os.environ.get("GROK_API_KEY", "")
GROK_API_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GROK_MODEL: str = "gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Image / coordinate normalisation
# ---------------------------------------------------------------------------
MAX_IMAGE_SIZE: int = 1280          # Maximum dimension (px) sent to Grok
NORMALIZED_GRID: int = 1000         # Grok reasons over a 0-1000 coordinate space
JPEG_QUALITY: int = 85              # Compression quality for API payloads

# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------
MAX_STEPS_PER_COMMAND: int = 12     # Hard limit — prevents runaway loops
UI_HIDE_DELAY_MS: int = 250         # ms to wait after hiding UI before first capture
STEP_DELAY_S: float = 0.5           # Pause between consecutive action steps (seconds)

# ---------------------------------------------------------------------------
# Input emulation timing (Windows-specific)
# ---------------------------------------------------------------------------
CLICK_MOVE_DURATION_S: float = 0.20    # Smooth mouse movement duration
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
LOG_FILE: Path = LOGS_DIR / "agent.log"

# ---------------------------------------------------------------------------
# History context
# ---------------------------------------------------------------------------
MAX_HISTORY_TURNS: int = 6   # Number of past turns injected into each Grok payload
