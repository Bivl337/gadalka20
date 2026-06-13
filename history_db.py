import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

MAX_HISTORY_PAIRS = 10
INACTIVITY_DAYS = 10


def resolve_data_dir() -> Path:
    explicit = os.getenv("DATA_DIR")
    if explicit:
        return Path(explicit)
    if Path("/data").is_dir():
        return Path("/data")
    return Path(".")


DB_PATH = resolve_data_dir() / "history.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                last_active_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_chat_id_id
                ON messages(chat_id, id);
            """
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def clear_history(chat_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


def touch_chat(chat_id: int) -> None:
    now = _now_iso()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO chats (chat_id, last_active_at)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_active_at = excluded.last_active_at
            """,
            (chat_id, now),
        )


def expire_if_inactive(chat_id: int) -> bool:
    """
    Если прошло INACTIVITY_DAYS без активности — очищает историю.
    Возвращает True, если контекст был сброшен (нужно уведомить пользователя).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_active_at FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        if row is None:
            return False

        last_active = _parse_iso(row["last_active_at"])
        if datetime.now(timezone.utc) - last_active < timedelta(days=INACTIVITY_DAYS):
            return False

        conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        return True


def get_history(chat_id: int) -> list[dict[str, str]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT role, content
            FROM messages
            WHERE chat_id = ?
            ORDER BY id ASC
            """,
            (chat_id,),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in rows]


def _trim_history(conn: sqlite3.Connection, chat_id: int) -> None:
    max_messages = MAX_HISTORY_PAIRS * 2
    conn.execute(
        """
        DELETE FROM messages
        WHERE chat_id = ?
          AND id NOT IN (
              SELECT id FROM messages
              WHERE chat_id = ?
              ORDER BY id DESC
              LIMIT ?
          )
        """,
        (chat_id, chat_id, max_messages),
    )


def add_message(chat_id: int, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (chat_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, role, content, _now_iso()),
        )
        _trim_history(conn, chat_id)
        conn.execute(
            """
            INSERT INTO chats (chat_id, last_active_at)
            VALUES (?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET last_active_at = excluded.last_active_at
            """,
            (chat_id, _now_iso()),
        )
