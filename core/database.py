"""
Phase 4: Interaction Database

Provides thread-safe SQLite persistence for interaction logs.

Threading model:
    The coordinator runs in a QThread worker.  To avoid cross-thread cursor
    sharing we use a per-operation connection strategy: every public method
    opens its own connection, completes its work, and closes immediately.
    A threading.Lock serialises concurrent writes so WAL mode is not required.

Schema:
    interaction_logs
        id                INTEGER PRIMARY KEY AUTOINCREMENT
        timestamp         TEXT NOT NULL          (ISO-8601, UTC)
        user_command      TEXT                   (original command text)
        assistant_response TEXT                  (Grok's text, minus action tag)
        action_tag        TEXT                   (e.g. "[CLICK:500,500]")
        execution_result  TEXT                   (success/error/skipped)

Design constraints:
    - Screenshots and Base64 payloads are NEVER stored here.
    - All queries use parameterised SQL to prevent injection.
    - Logging wraps every DB operation.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Row type alias (matches schema column order for history queries)
# ---------------------------------------------------------------------------

class InteractionRecord:
    """Lightweight container for a single history row."""

    __slots__ = (
        "id",
        "timestamp",
        "user_command",
        "assistant_response",
        "action_tag",
        "execution_result",
    )

    def __init__(
        self,
        id: int,
        timestamp: str,
        user_command: Optional[str],
        assistant_response: Optional[str],
        action_tag: Optional[str],
        execution_result: Optional[str],
    ) -> None:
        self.id = id
        self.timestamp = timestamp
        self.user_command = user_command
        self.assistant_response = assistant_response
        self.action_tag = action_tag
        self.execution_result = execution_result

    def __repr__(self) -> str:
        return (
            f"InteractionRecord(id={self.id}, "
            f"user_command={self.user_command!r}, "
            f"action_tag={self.action_tag!r})"
        )


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class InteractionDatabase:
    """
    Thread-safe SQLite store for interaction history.

    Usage:
        db = InteractionDatabase(config.DB_PATH)
        db.initialize()
        db.log_interaction(
            user_command="Open Chrome",
            assistant_response="Clicking Chrome icon",
            action_tag="[CLICK:120,540]",
            execution_result="success",
        )
        history = db.get_recent_history(limit=6)
        db.close()
    """

    def __init__(self, db_path: Path = config.DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        logger.info("InteractionDatabase configured — path: %s", self._db_path)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """
        Create the database file and schema if they do not exist.

        Safe to call multiple times (uses CREATE TABLE IF NOT EXISTS).
        """
        logger.info("Initialising database at: %s", self._db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            conn = self._open()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS interaction_logs (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp           TEXT    NOT NULL,
                        user_command        TEXT,
                        assistant_response  TEXT,
                        action_tag          TEXT,
                        execution_result    TEXT
                    )
                """)
                conn.commit()
                logger.info("Schema ready — table: interaction_logs")
            except sqlite3.Error as exc:
                logger.error("Schema creation failed: %s", exc)
                raise
            finally:
                conn.close()

    def close(self) -> None:
        """No persistent connection to close; provided for symmetry / future use."""
        logger.info("InteractionDatabase closed (no persistent connection)")

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def log_interaction(
        self,
        user_command: Optional[str] = None,
        assistant_response: Optional[str] = None,
        action_tag: Optional[str] = None,
        execution_result: Optional[str] = None,
    ) -> int:
        """
        Insert one row into interaction_logs and return the new row id.

        Args:
            user_command:       The original natural-language command from the user.
            assistant_response: The text portion of Grok's response (no image data).
            action_tag:         The parsed action tag string, e.g. "[CLICK:500,500]".
            execution_result:   Outcome string such as "success", "error", "skipped".

        Returns:
            The ROWID of the newly inserted row.
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        logger.debug(
            "log_interaction: command=%r tag=%r result=%r",
            user_command, action_tag, execution_result,
        )

        with self._lock:
            conn = self._open()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO interaction_logs
                        (timestamp, user_command, assistant_response,
                         action_tag, execution_result)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        user_command,
                        assistant_response,
                        action_tag,
                        execution_result,
                    ),
                )
                conn.commit()
                row_id: int = cursor.lastrowid  # type: ignore[assignment]
                logger.info(
                    "Logged interaction id=%d tag=%r result=%r",
                    row_id, action_tag, execution_result,
                )
                return row_id
            except sqlite3.Error as exc:
                logger.error("log_interaction failed: %s", exc)
                raise
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_recent_history(self, limit: int = 10) -> list[InteractionRecord]:
        """
        Return the most recent *limit* rows in chronological order (oldest first).

        The coordinator calls this to build the history context block for each
        Grok payload.  Only text fields are returned — no screenshots or Base64.

        Args:
            limit: Maximum number of rows to return.

        Returns:
            List of InteractionRecord, oldest-first.
        """
        logger.debug("get_recent_history: limit=%d", limit)

        with self._lock:
            conn = self._open()
            try:
                cursor = conn.execute(
                    """
                    SELECT id, timestamp, user_command, assistant_response,
                           action_tag, execution_result
                    FROM interaction_logs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
                rows = cursor.fetchall()
            except sqlite3.Error as exc:
                logger.error("get_recent_history failed: %s", exc)
                raise
            finally:
                conn.close()

        # Reverse so the result is chronological (oldest first).
        records = [
            InteractionRecord(
                id=row[0],
                timestamp=row[1],
                user_command=row[2],
                assistant_response=row[3],
                action_tag=row[4],
                execution_result=row[5],
            )
            for row in reversed(rows)
        ]

        logger.debug(
            "get_recent_history: returned %d record(s)", len(records)
        )
        return records

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """
        Open a short-lived connection.

        check_same_thread=False is safe here because all callers hold
        self._lock before entering and close the connection before releasing.
        """
        return sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
        )
