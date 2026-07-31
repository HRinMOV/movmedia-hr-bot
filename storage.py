"""
storage.py

Персистентное хранение состояния кандидатов в SQLite.

Хранит для каждого chat_id: полную историю диалога (для контекста AI),
техническую стадию (stage) и время последнего перехода, а также
"память" о кандидате — чтобы бот никогда не переспрашивал то, что уже
известно: имя, вакансия, дата интервью, ожидания по зарплате,
отправленные ссылки, полученные файлы, предыдущие вопросы.
"""
import json
import logging
import sqlite3
import threading
import time

from config import DB_PATH

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL;")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    chat_id INTEGER PRIMARY KEY,
    username TEXT,
    history TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL DEFAULT 'applied',
    stage_updated_at REAL NOT NULL,
    flagged_silent INTEGER NOT NULL DEFAULT 0,
    name TEXT,
    vacancy TEXT,
    interview_date TEXT,
    salary_expectations TEXT,
    sent_links TEXT NOT NULL DEFAULT '[]',
    uploaded_files TEXT NOT NULL DEFAULT '[]',
    previous_questions TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
"""

with _lock:
    _conn.execute(_SCHEMA)
    _conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "chat_id": row["chat_id"],
        "username": row["username"],
        "history": json.loads(row["history"]),
        "stage": row["stage"],
        "stage_updated_at": row["stage_updated_at"],
        "flagged_silent": bool(row["flagged_silent"]),
        "name": row["name"],
        "vacancy": row["vacancy"],
        "interview_date": row["interview_date"],
        "salary_expectations": row["salary_expectations"],
        "sent_links": json.loads(row["sent_links"]),
        "uploaded_files": json.loads(row["uploaded_files"]),
        "previous_questions": json.loads(row["previous_questions"]),
    }


def get_or_create(chat_id: int, username: str | None) -> dict:
    with _lock:
        cur = _conn.execute("SELECT * FROM candidates WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        if row is None:
            now = time.time()
            _conn.execute(
                "INSERT INTO candidates (chat_id, username, stage_updated_at, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chat_id, username, now, now),
            )
            _conn.commit()
            logger.info("Новый кандидат создан: chat_id=%s username=%s", chat_id, username)
            cur = _conn.execute("SELECT * FROM candidates WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
        elif username and row["username"] != username:
            _conn.execute("UPDATE candidates SET username = ? WHERE chat_id = ?", (username, chat_id))
            _conn.commit()
            cur = _conn.execute("SELECT * FROM candidates WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
        return _row_to_dict(row)


def save_history(chat_id: int, history: list) -> None:
    with _lock:
        _conn.execute(
            "UPDATE candidates SET history = ? WHERE chat_id = ?",
            (json.dumps(history, ensure_ascii=False), chat_id),
        )
        _conn.commit()


def update_stage(chat_id: int, stage: str) -> None:
    with _lock:
        _conn.execute(
            "UPDATE candidates SET stage = ?, stage_updated_at = ?, flagged_silent = 0 "
            "WHERE chat_id = ?",
            (stage, time.time(), chat_id),
        )
        _conn.commit()
    logger.info("Стадия кандидата chat_id=%s изменена на %s", chat_id, stage)


def update_profile(chat_id: int, **fields) -> None:
    """Обновляет память о кандидате: name, vacancy, interview_date,
    salary_expectations. Пустые/None значения игнорируются, чтобы не
    затирать уже известные данные."""
    allowed = {"name", "vacancy", "interview_date", "salary_expectations"}
    updates = {k: v for k, v in fields.items() if k in allowed and v}
    if not updates:
        return
    with _lock:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        _conn.execute(
            f"UPDATE candidates SET {set_clause} WHERE chat_id = ?",
            (*updates.values(), chat_id),
        )
        _conn.commit()
    logger.info("Профиль кандидата chat_id=%s обновлён: %s", chat_id, updates)


def _append_json_list(chat_id: int, column: str, value) -> None:
    with _lock:
        cur = _conn.execute(f"SELECT {column} FROM candidates WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        items = json.loads(row[0]) if row and row[0] else []
        if value not in items:
            items.append(value)
        _conn.execute(
            f"UPDATE candidates SET {column} = ? WHERE chat_id = ?",
            (json.dumps(items, ensure_ascii=False), chat_id),
        )
        _conn.commit()


def add_sent_link(chat_id: int, link: str) -> None:
    _append_json_list(chat_id, "sent_links", link)


def add_uploaded_file(chat_id: int, file_id: str) -> None:
    _append_json_list(chat_id, "uploaded_files", file_id)


def add_previous_question(chat_id: int, question: str) -> None:
    _append_json_list(chat_id, "previous_questions", question)


def mark_flagged_silent(chat_id: int) -> None:
    with _lock:
        _conn.execute("UPDATE candidates SET flagged_silent = 1 WHERE chat_id = ?", (chat_id,))
        _conn.commit()


def find_silent_candidates(stage: str, older_than_seconds: int) -> list:
    threshold = time.time() - older_than_seconds
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM candidates WHERE stage = ? AND stage_updated_at < ? AND flagged_silent = 0",
            (stage, threshold),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]
