"""
Action Tag Parser

Extracts structured action descriptors from the free-text responses that Grok
Vision returns.  The convention is that each response contains at most ONE
action tag at its very end (or no tag if the model appended [DONE]).

Supported tags:
    [CLICK:x,y]                  — click at resized-image pixel coordinates
    [TYPE:x,y|text_to_type]      — focus field at (x, y) then paste text
    [PRESS:key_name]             — press a named key (enter, tab, esc, …)
    [SCROLL:direction:amount]    — scroll the wheel (down:3, up:300, …)
    [TASK_COMPLETE]              — current plan task finished, advance to next task
    [DONE]                       — whole goal complete, no further action needed

Parsing is lenient about the bracket wrapper but strict about the payload:
    - Smaller models frequently drop the opening "[" and/or closing "]"
      (e.g. "Action: CLICK:150,45]"), so brackets are optional and modest
      whitespace around delimiters is tolerated.
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
    CLICK         = "CLICK"
    TYPE          = "TYPE"
    PRESS         = "PRESS"
    SCROLL        = "SCROLL"
    WAIT          = "WAIT"
    NEW_TAB       = "NEW_TAB"
    LAUNCH        = "LAUNCH"
    OPEN_URL      = "OPEN_URL"
    TASK_COMPLETE = "TASK_COMPLETE"
    DONE          = "DONE"


# ---------------------------------------------------------------------------
# Parsed action container
# ---------------------------------------------------------------------------

@dataclass
class ParsedAction:
    """
    Structured representation of one extracted action tag.

    Fields are populated according to action_type; unused fields are None.

    CLICK:    x, y
    TYPE:     x, y, text
    PRESS:    key
    SCROLL:   direction, amount
    WAIT:     seconds (optional)
    LAUNCH:   text  (the app name)
    OPEN_URL: text  (the URL)
    DONE:     (no additional fields)
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

    # Wait duration in seconds (WAIT)
    seconds: Optional[float] = field(default=None)

    def __str__(self) -> str:
        if self.action_type == ActionType.CLICK:
            return f"[CLICK:{self.x},{self.y}]"
        if self.action_type == ActionType.TYPE:
            return f"[TYPE:{self.x},{self.y}|{self.text}]"
        if self.action_type == ActionType.PRESS:
            return f"[PRESS:{self.key}]"
        if self.action_type == ActionType.SCROLL:
            return f"[SCROLL:{self.direction}:{self.amount}]"
        if self.action_type == ActionType.WAIT:
            return f"[WAIT:{self.seconds:g}]" if self.seconds is not None else "[WAIT]"
        if self.action_type == ActionType.NEW_TAB:
            return "[NEW_TAB]"
        if self.action_type == ActionType.LAUNCH:
            return f"[LAUNCH:{self.text}]"
        if self.action_type == ActionType.OPEN_URL:
            return f"[OPEN_URL:{self.text}]"
        if self.action_type == ActionType.TASK_COMPLETE:
            return "[TASK_COMPLETE]"
        return "[DONE]"


# ---------------------------------------------------------------------------
# Regex patterns (compiled once at import time)
# ---------------------------------------------------------------------------

# Payload patterns (the part between the brackets). The full per-tag regexes
# and the catch-all _RE_ANY_TAG are both composed from these strings so the
# two can never drift out of sync.
_P_CLICK  = r"CLICK:\s*(\d+)\s*,\s*(\d+)"
# TYPE text may legitimately contain newlines (multi-line code/snippets) and
# square brackets (e.g. a Python list literal), so the payload capture must be
# greedy to the end and span newlines. The opening/closing wrapper brackets are
# matched as a balanced pair (see _lenient_type) so a trailing "]" that belongs
# to the typed code is preserved while the wrapper "]" is not captured.
_P_TYPE   = r"TYPE:\s*(\d+)\s*,\s*(\d+)\s*\|"
_P_PRESS  = r"PRESS:\s*([\w\-]+)"
_P_SCROLL = r"SCROLL:\s*(up|down|left|right)\s*:\s*(\d+)"
# LAUNCH app name: greedy run of everything that is not "]" or a line break, up
# to the closing bracket / EOL. Greedy (not lazy) so multi-word names like
# "google chrome" are captured whole rather than collapsing to one char when the
# trailing "]" is optional. Trailing whitespace is trimmed in _decode_launch.
_P_LAUNCH = r"LAUNCH:\s*([^\]\r\n]+)"
# OPEN_URL: everything up to a closing bracket or whitespace/EOL. URLs contain
# ":/.?=&%#" etc. but never a literal "]" or whitespace, so stop at those.
_P_OPENURL = r"OPEN_URL:\s*([^\]\s]+)"
# WAIT takes an optional ":seconds" argument (e.g. [WAIT] or [WAIT:2.5]).
_P_WAIT   = r"WAIT(?:\s*:\s*(\d+(?:\.\d+)?))?"
# Stricter WAIT form used only by the combined any-tag scanner: a bare,
# unbracketed "wait" is an ordinary English word, so the scanner must see
# either a bracket ([WAIT]) or a ":seconds" argument (WAIT:3) to treat it as
# a tag. The lenient _P_WAIT is still used to decode an already-extracted tag.
_P_WAIT_STRICT = r"\[\s*WAIT(?:\s*:\s*\d+(?:\.\d+)?)?\s*\]|\bWAIT\s*:\s*\d+(?:\.\d+)?\s*\]?"


def _lenient(payload: str) -> str:
    """Wrap a payload pattern in optional square brackets.

    Smaller models frequently drop the opening "[" and/or the closing "]"
    (e.g. "Action: CLICK:150,45]"); the \\b stops a bare keyword from matching
    inside a longer word when the bracket is absent.
    """
    return r"\[?\b" + payload + r"\s*\]?"


def _lenient_type(payload: str) -> str:
    """Lenient wrapper specialised for the multi-line TYPE payload.

    The captured text (group 3) spans to the end of the string under DOTALL.
    Two balanced forms are offered so a wrapper "]" is excluded while a "]"
    that is part of the typed code (e.g. a list literal) is preserved:

      * bracketed   "[TYPE:..|<text>]"  → text = everything up to the FINAL "]"
      * unbracketed "TYPE:..|<text>"    → text = everything to end of string

    The unbracketed form (the leading "[" absent) covers models that drop the
    wrapper entirely. Trailing whitespace after the closing bracket is ignored.
    Requires re.DOTALL on the compiled regex.
    """
    # The payload (_P_TYPE) carries the x,y coordinate groups; it appears in
    # both alternatives, so name them distinctly per branch to read back
    # unambiguously in _decode_type regardless of which branch matched.
    bracketed = payload.replace(r"(\d+)", r"(?P<bx>\d+)", 1).replace(r"(\d+)", r"(?P<by>\d+)", 1)
    unbrack = payload.replace(r"(\d+)", r"(?P<ux>\d+)", 1).replace(r"(\d+)", r"(?P<uy>\d+)", 1)
    return (
        r"(?:\[\b" + bracketed + r"(?P<typed>.*)\]\s*$"      # fully bracketed: text before final ]
        r"|\[?\b" + unbrack + r"(?P<typed_nb>.*?)\s*$)"      # missing closing ]: text to end
    )


# Capture groups: x, y
_RE_CLICK = re.compile(_lenient(_P_CLICK), re.IGNORECASE)

# Capture groups: x, y, text. The text may be empty, may contain "|", "]" and
# newlines (multi-line code). DOTALL lets the greedy ".*" span line breaks.
_RE_TYPE = re.compile(_lenient_type(_P_TYPE), re.IGNORECASE | re.DOTALL)

# Capture group: key_name (letters, digits, underscore, hyphen)
_RE_PRESS = re.compile(_lenient(_P_PRESS), re.IGNORECASE)

# Capture groups: direction, amount
_RE_SCROLL = re.compile(_lenient(_P_SCROLL), re.IGNORECASE)

# Capture group: seconds (optional)
_RE_WAIT = re.compile(_lenient(_P_WAIT), re.IGNORECASE)

# Capture group: app name / URL
_RE_LAUNCH = re.compile(_lenient(_P_LAUNCH), re.IGNORECASE)
_RE_OPENURL = re.compile(_lenient(_P_OPENURL), re.IGNORECASE)

# Single pattern that matches ANY known tag (for extract_action_tag /
# remove_action_tag). DONE keeps its stricter form: brackets required unless
# it is the bare final word of the response. TYPE is anchored to the end of the
# string (_lenient_type) so its greedy, newline-spanning text capture does not
# swallow other tags; it is therefore tried last in the alternation.
_RE_ANY_TAG = re.compile(
    "|".join(
        [_lenient(p) for p in (_P_CLICK, _P_PRESS, _P_SCROLL, _P_OPENURL, _P_LAUNCH)]
        + [_P_WAIT_STRICT]
        + [r"\[\s*NEW_TAB\s*\]", r"\[TASK_COMPLETE\]", r"\[DONE\]", r"\bDONE\s*$"]
        + [_lenient_type(_P_TYPE)]
    ),
    re.IGNORECASE | re.DOTALL,
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
        # The opening bracket may be absent (lenient extraction); strip it so
        # the prefix dispatch below works either way.
        tag_upper = tag.upper().strip().lstrip("[")

        if "NEW_TAB" in tag_upper:
            return ParsedAction(action_type=ActionType.NEW_TAB)

        if "TASK_COMPLETE" in tag_upper:
            return ParsedAction(action_type=ActionType.TASK_COMPLETE)

        if "DONE" in tag_upper:
            return ParsedAction(action_type=ActionType.DONE)

        if tag_upper.startswith("CLICK:"):
            return self._decode_click(tag)

        if tag_upper.startswith("TYPE:"):
            return self._decode_type(tag)

        if tag_upper.startswith("PRESS:"):
            return self._decode_press(tag)

        if tag_upper.startswith("SCROLL:"):
            return self._decode_scroll(tag)

        if tag_upper.startswith("WAIT"):
            return self._decode_wait(tag)

        if tag_upper.startswith("OPEN_URL:"):
            return self._decode_open_url(tag)

        if tag_upper.startswith("LAUNCH:"):
            return self._decode_launch(tag)

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
        if not (self._in_pixel_range(x) and self._in_pixel_range(y)):
            logger.warning(
                "CLICK coordinates out of pixel range: (%d, %d)", x, y
            )
            return None
        return ParsedAction(action_type=ActionType.CLICK, x=x, y=y)

    def _decode_type(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_TYPE.fullmatch(tag)
        if m is None:
            logger.warning("Malformed TYPE tag: %r", tag)
            return None

        # _lenient_type has two alternatives (bracketed / unbracketed), each
        # carrying its own x,y coordinate groups and a named text group. Exactly
        # one branch matches; pick whichever coordinate pair is populated.
        groups = m.groupdict()
        if groups.get("typed") is not None:
            text = groups["typed"]
            xs, ys = groups["bx"], groups["by"]        # bracketed branch coords
        else:
            text = groups.get("typed_nb") or ""
            xs, ys = groups["ux"], groups["uy"]        # unbracketed branch coords

        try:
            x, y = int(xs), int(ys)
        except (TypeError, ValueError):
            logger.warning("Non-integer coordinates in TYPE tag: %r", tag)
            return None
        if not (self._in_pixel_range(x) and self._in_pixel_range(y)):
            logger.warning(
                "TYPE coordinates out of pixel range: (%d, %d)", x, y
            )
            return None

        text = self._unescape_text(text)   # may be empty string; that is valid
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

    def _decode_wait(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_WAIT.fullmatch(tag)
        if m is None:
            logger.warning("Malformed WAIT tag: %r", tag)
            return None
        seconds: Optional[float] = None
        if m.group(1) is not None:
            try:
                seconds = float(m.group(1))
            except ValueError:
                logger.warning("Non-numeric duration in WAIT tag: %r", tag)
                seconds = None
        return ParsedAction(action_type=ActionType.WAIT, seconds=seconds)

    def _decode_launch(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_LAUNCH.fullmatch(tag)
        if m is None:
            logger.warning("Malformed LAUNCH tag: %r", tag)
            return None
        name = m.group(1).strip()
        if not name:
            logger.warning("Empty app name in LAUNCH tag: %r", tag)
            return None
        return ParsedAction(action_type=ActionType.LAUNCH, text=name)

    def _decode_open_url(self, tag: str) -> Optional[ParsedAction]:
        m = _RE_OPENURL.fullmatch(tag)
        if m is None:
            logger.warning("Malformed OPEN_URL tag: %r", tag)
            return None
        url = m.group(1).strip()
        if not url:
            logger.warning("Empty URL in OPEN_URL tag: %r", tag)
            return None
        return ParsedAction(action_type=ActionType.OPEN_URL, text=url)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    # Escape sequences the model writes as literal characters inside a TYPE
    # payload (a plain-text response, not JSON, so the runtime never decodes
    # them). Each maps the two-character source sequence to the real character
    # that should be typed. Without this, code pasted into an editor arrives as
    # one unbroken line full of literal "\n" tokens — exactly the Colab failure
    # this fixes. Backslash itself is restored last so "\\n" stays a literal
    # backslash-n rather than collapsing to a newline.
    _ESCAPE_MAP: tuple[tuple[str, str], ...] = (
        (r"\n", "\n"),
        (r"\t", "\t"),
        (r"\r", "\r"),
    )

    @classmethod
    def _unescape_text(cls, text: str) -> str:
        r"""Convert literal escape sequences (\n, \t, \r, \\) in TYPE text.

        The model emits these as two literal characters; this turns them into
        the characters they represent so multi-line code is typed with real
        line breaks. A doubled backslash (\\) is preserved as a single literal
        backslash and is NOT treated as the start of an escape.
        """
        if "\\" not in text:
            return text

        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "\\" and i + 1 < n:
                pair = text[i:i + 2]
                if pair == "\\\\":
                    out.append("\\")
                    i += 2
                    continue
                replaced = False
                for src, dst in cls._ESCAPE_MAP:
                    if pair == src:
                        out.append(dst)
                        i += 2
                        replaced = True
                        break
                if replaced:
                    continue
            out.append(text[i])
            i += 1
        return "".join(out)

    @staticmethod
    def _in_pixel_range(value: int, lo: int = 0, hi: int = 10000) -> bool:
        """Sanity bound for image-pixel coordinates.

        The model emits pixels in the resized-image space (longest side capped
        at MAX_IMAGE_SIZE). The parser does not know the exact image dimensions,
        so this is a loose ceiling that rejects only clearly garbage values
        (negatives, or absurd numbers from a malformed tag). The InputEmulator
        clamps the converted physical coordinate to the real screen bounds.
        """
        return lo <= value <= hi
