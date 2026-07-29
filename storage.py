"""
SQLite-хранилище состояния кандидатов.

ПРИМЕЧАНИЕ: этот файл был восстановлен заново — оригинальное содержимое
storage.py не удалось найти нигде в репозитории (при загрузке файлов их
содержимое перепуталось между собой, а на месте storage.py оказались данные
из faq.json). Реализация ниже основана на интерфейсе, которым пользуются
bot.py и claude_client.py: get_or_create, save_history, update_stage,
find_silent_candidates, mark_flagged_silent.
"""

import json
import sqlite3
import time
from contextlib import contextmanager

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    history TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL DEFAULT 'new',
    stage_updated_at REAL NOT NULL DEFAULT 0,
    flagged_silent INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create(chat_id: int, username: str | None) -> dict:
    """Возвращает запись кандидата, создавая новую при первом обращении."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE chat_id = ?", (chat_id,)
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO candidates (chat_id, username, history, stage, stage_updated_at) "
                "VALUES (?, ?, '[]', 'new', ?)",
                (chat_id, username, time.time()),
            )
            row = conn.execute(
                "SELECT * FROM candidates WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        elif username and row["username"] != username:
            conn.execute(
                "UPDATE candidates SET username = ? WHERE chat_id = ?",
                (username, chat_id),
            )

        return {
            "chat_id": row["chat_id"],
            "username": username or row["username"],
            "history": json.loads(row["history"]),
            "stage": row["stage"],
            "stage_updated_at": row["stage_updated_at"],
            "flagged_silent": bool(row["flagged_silent"]),
        }


def save_history(chat_id: int, history: list) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET history = ? WHERE chat_id = ?",
            (json.dumps(history, ensure_ascii=False), chat_id),
        )


def update_stage(chat_id: int, stage: str) -> None:
    """Обновляет стадию кандидата и сбрасывает пометку 'пропавшего'."""
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET stage = ?, stage_updated_at = ?, flagged_silent = 0 "
            "WHERE chat_id = ?",
            (stage, time.time(), chat_id),
        )


def find_silent_candidates(stage: str, older_than_seconds: int) -> list:
    """Кандидаты в заданной стадии, не обновлявшиеся дольше порога и ещё не помеченные."""
    threshold = time.time() - older_than_seconds
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, username FROM candidates "
            "WHERE stage = ? AND stage_updated_at < ? AND flagged_silent = 0",
            (stage, threshold),
        ).fetchall()
        return [{"chat_id": row["chat_id"], "username": row["username"]} for row in rows]


def mark_flagged_silent(chat_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE candidates SET flagged_silent = 1 WHERE chat_id = ?",
            (chat_id,),
        )
