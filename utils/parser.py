"""
Phase 4: Action Tag Parser

Extracts structured action descriptors from the free-text responses that Grok
Vision returns.  The convention is that each response contains at most ONE
action tag at its very end (or no tag if the model appended [DONE]).

Supported tags:
    [CLICK:x,y]                  — click at normalised coordinates
    [TYPE:x,y|text_to_type]      — focus field at (x, y) then paste text
    [PRESS:key_name]             — press a named key (enter, tab, esc, …)
    [SCROLL:direction:amount]    — scroll the wheel (down:3, up:300, …)
    [DONE]                       — task complete, no further action needed

Parsing is intentionally strict:
    - Whitespace inside tags is not tolerated (Grok is trained without it).
    - Unknown tag names return None rather than raising.
    - Malformed numeric fields return None rather than raising.
    - Only the FIRST recognised tag in the response is extracted.

The parser does NOT execute actions — it only produces ParsedAction dataclasses.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action type enum
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    CLICK  = "CLICK"
    TYPE   = "TYPE"
    PRESS  = "PRESS"
    SCROLL = "SCROLL"
    DONE   = "DONE"


# ---------------------------------------------------------------------------
# Parsed action container
# ---------------------------------------------------------------------------

@dataclass
class ParsedAction:
    """
    Structured representation of one extracted action tag.

    Fields are populated according to action_type; unused fields are None.

    CLICK:  x, y
    TYPE:   x, y, text
    PRESS:  key
    SCROLL: direction, amount
    DONE:   (no additional fields)
    """

    action_type: ActionType

    # Coordinate fields (CLICK, TYPE)
    x: Optional[int] = field(default=None)
    y: Optional[int] = field(default=None)

    # Text payload (TYPE)
    text: Optional[str] = field(default=None)

    # Key name (PRESS)
    key: Optional[str] = field(default=None)

    # Scroll parameters (SCROLL)
    direction: Optional[str] = field(default=None)
    amount: Optional[int] = field(default=None)

    def __str__(self) -> str:
        if self.action_type == ActionType.CLICK:
            return f"[CLICK:{self.x},{self.y}]"
        if self.action_type == ActionType.TYPE:
            return f"[TYPE:{self.x},{self.y}|{self.text}]"
        if self.action_type == ActionType.PRESS:
            return f"[PRESS:{self.key}]"
        if self.action_type == ActionType.SCROLL:
            return f"[SCROLL:{self.direction}:{self.amount}]"
        return "[DONE]"


# ---------------------------------------------------------------------------
# Regex patterns (compiled once at import time)
# ---------------------------------------------------------------------------

# Capture groups: x, y
_RE_CLICK = re.compile(r"\[CLICK:(\d+),(\d+)\]")

# Capture groups: x, y, text  (text may be empty, may contain | chars internally)
_RE_TYPE = re.compile(r"\[TYPE:(\d+),(\d+)\|([^\]]*)\]")

# Capture group: key_name (letters, digits, underscore, hyphen)
_RE_PRESS = re.compile(r"\[PRESS:([\w\-]+)\]")

# Capture groups: direction, amount
_RE_SCROLL = re.compile(r"\[SCROLL:(up|down|left|right):(\d+)\]", re.IGNORECASE)

# No capture groups
_RE_DONE = re.compile(r"\[DONE\]")

# Ordered list of (pattern, ActionType) for the "find first tag" scan.
_TAG_PATTERNS: list[tuple[re.Pattern, ActionType]] = [
    (_RE_DONE,   ActionType.DONE),
    (_RE_CLICK,  ActionType.CLICK),
    (_RE_TYPE,   ActionType.TYPE),
    (_RE_PRESS,  ActionType.PRESS),
    (_RE_SCROLL, ActionType.SCROLL),
]

# Single pattern that matches ANY known tag (for extract_action_tag / remove_action_tag).
_RE_ANY_TAG = re.compile(
    r"\[(?:DONE|CLICK:\d+,\d+|TYPE:\d+,\d+\|[^\]]*|PRESS:[\w\-]+|SCROLL:(?:up|down|left|right):\d+)\]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------

class ActionParser:
    """
    Stateless parser for Grok Vision action tags.

    All methods are pure functions; the class exists mainly for namespace
    organisation and potential future subclassing.
    """

    # ------------------------------------------------------------------
    # Primary API
    # ------------------------------------------------------------------

    def parse(self, response_text: str) -> Optional[ParsedAction]:
        """
        Extract and decode the first recognised action tag in *response_text*.

        Returns None if no valid tag is found.

        Args:
            response_text: The full text of the AI response.

        Returns:
            ParsedAction or None.
        """
        logger.debug(
            "parse: scanning response (%d chars)", len(response_text)
        )

        tag_text = self.extract_action_tag(response_text)
        if tag_text is None:
            logger.debug("parse: no action tag found")
            return None

        action = self._decode_tag(tag_text)
        if action is None:
            logger.warning("parse: tag found but could not be decoded: %r", tag_text)
        else:
            logger.info("parse: decoded %s", action)

        return action

    def extract_action_tag(self, response_text: str) -> Optional[str]:
        """
        Return the raw tag string (e.g. "[CLICK:500,500]") from *response_text*,
        or None if none is present.

        Only the first tag is returned.

        Args:
            response_text: Full AI response text.

        Returns:
            The matched tag string, or None.
        """
        match = _RE_ANY_TAG.search(response_text)
        if match is None:
            return None
        tag = match.group(0)
        logger.debug("extract_action_tag: found %r", tag)
        return tag

    def remove_action_tag(self, response_text: str) -> str:
        """
        Return *response_text* with the first action tag (and any surrounding
        whitespace) stripped out.

        This yields the "prose" portion of the response, suitable for logging
        or inclusion in history context.

        Args:
            response_text: Full AI response text.

        Returns:
            Text with the first action tag removed and trailing whitespace trimmed.
        """
        cleaned = _RE_ANY_TAG.sub("", response_text, count=1).rstrip()
        logger.debug(
            "remove_action_tag: %d → %d chars", len(response_text), len(cleaned)
        )
        return cleaned

    # ------------------------------------------------------------------
    # Internal decoding
    # ------------------------------------------------------------------

    def _decode_tag(self, tag: str) -> Optional[ParsedAction]:
        """
        Dispatch *tag* to the appropriate handler.

        Returns None if the tag format is invalid despite matching the broad
        _RE_ANY_TAG pattern (e.g. out-of-range numbers).
        """
        tag_upper = tag.upper()

        if tag_upper == "[DONE]":
            return ParsedAction(action_type=ActionType.DONE)

        if tag_upper.startswith("[CLICK:"):
            return self._decode_click(tag)

        if tag_upper.startswith("[TYPE:"):
            return self._decode_type(tag)

        if tag_upper.startswith("[PRESS:"):
            return self._decode_press(tag)

        if tag_upper.startswith("[SCROLL:"):
            return self._decode_scroll(tag)

        logger.warning("_decode_tag: unrecognised tag prefix: %r", tag)
        return None

    def _decode_click(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_CLICK.fullmatch(tag)
        if m is None:
            logger.warning("Malformed CLICK tag: %r", tag)
            return None
        try:
            x, y = int(m.group(1)), int(m.group(2))
        except ValueError:
            logger.warning("Non-integer coordinates in CLICK tag: %r", tag)
            return None
        if not (self._in_grid(x) and self._in_grid(y)):
            logger.warning(
                "CLICK coordinates out of 0-1000 range: (%d, %d)", x, y
            )
            return None
        return ParsedAction(action_type=ActionType.CLICK, x=x, y=y)

    def _decode_type(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_TYPE.fullmatch(tag)
        if m is None:
            logger.warning("Malformed TYPE tag: %r", tag)
            return None
        try:
            x, y = int(m.group(1)), int(m.group(2))
        except ValueError:
            logger.warning("Non-integer coordinates in TYPE tag: %r", tag)
            return None
        if not (self._in_grid(x) and self._in_grid(y)):
            logger.warning(
                "TYPE coordinates out of 0-1000 range: (%d, %d)", x, y
            )
            return None
        text = m.group(3)   # may be empty string; that is valid
        return ParsedAction(action_type=ActionType.TYPE, x=x, y=y, text=text)

    def _decode_press(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_PRESS.fullmatch(tag)
        if m is None:
            logger.warning("Malformed PRESS tag: %r", tag)
            return None
        key = m.group(1).lower()
        return ParsedAction(action_type=ActionType.PRESS, key=key)

    def _decode_scroll(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_SCROLL.fullmatch(tag)
        if m is None:
            logger.warning("Malformed SCROLL tag: %r", tag)
            return None
        direction = m.group(1).lower()
        try:
            amount = int(m.group(2))
        except ValueError:
            logger.warning("Non-integer amount in SCROLL tag: %r", tag)
            return None
        if amount <= 0:
            logger.warning("SCROLL amount must be positive, got: %d", amount)
            return None
        return ParsedAction(
            action_type=ActionType.SCROLL,
            direction=direction,
            amount=amount,
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _in_grid(value: int, lo: int = 0, hi: int = 1000) -> bool:
        return lo <= value <= hi
