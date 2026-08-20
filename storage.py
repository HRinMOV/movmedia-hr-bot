"""
storage.py

Персистентное хранение состояния кандидатов в SQLite.

Хранит для каждого chat_id: полную историю диалога (для контекста AI),
техническую стадию (stage) и время последнего перехода, а также
"память" о кандидате — чтобы бот никогда не переспрашивал то, что уже
известно: имя, вакансия, дата интервью, ожидания по зарплате,
отправленные ссылки, полученные файлы, предыдущие вопросы, а также
данные кадрового резерва (желаемая роль, портфолио, резюме, о себе).
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
    desired_role TEXT,
    portfolio_link TEXT,
    resume_note TEXT,
    about_me TEXT,
    is_reserve INTEGER NOT NULL DEFAULT 0,
            last_activity_at REAL,
                    summary_notified INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""

# Колонки, добавленные уже после первого релиза — накатываем через ALTER TABLE
# на случай, если в базе уже есть таблица candidates старой структуры.
_NEW_COLUMNS = {
    "desired_role": "TEXT",
    "portfolio_link": "TEXT",
    "resume_note": "TEXT",
    "about_me": "TEXT",
    "is_reserve": "INTEGER NOT NULL DEFAULT 0",
        "last_activity_at": "REAL",
        "summary_notified": "INTEGER NOT NULL DEFAULT 0",
}

with _lock:
    _conn.execute(_SCHEMA)
    _conn.commit()
    for column, col_type in _NEW_COLUMNS.items():
        try:
            _conn.execute(f"ALTER TABLE candidates ADD COLUMN {column} {col_type}")
            _conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует




# Связь кандидат+вакансия -> карточка Notion. Используется исключительно для
# дедупликации (чтобы повторное сообщение/сбой/повторный запуск не создавали
# вторую карточку) и чтобы во всех дальнейших уведомлениях рекрутеру можно
# было подставить ссылку на уже созданную карточку. Ничего общего со
# сценарием "Хочу в команду MOVmedia" не имеет - туда карточки не создаются.
_NOTION_CARDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS notion_cards (
    chat_id INTEGER NOT NULL,
    vacancy TEXT NOT NULL,
    notion_page_id TEXT NOT NULL,
    notion_page_url TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (chat_id, vacancy)
);
"""

with _lock:
    _conn.execute(_NOTION_CARDS_SCHEMA)
    _conn.commit()


def _row_to_dict(row: sqlite3.Row) -> dict:
    keys = row.keys()
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
        "desired_role": row["desired_role"] if "desired_role" in keys else None,
        "portfolio_link": row["portfolio_link"] if "portfolio_link" in keys else None,
        "resume_note": row["resume_note"] if "resume_note" in keys else None,
        "about_me": row["about_me"] if "about_me" in keys else None,
        "is_reserve": bool(row["is_reserve"]) if "is_reserve" in keys else False,
                "last_activity_at": row["last_activity_at"] if "last_activity_at" in keys else None,
                "summary_notified": bool(row["summary_notified"]) if "summary_notified" in keys else False,
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
    salary_expectations, desired_role, portfolio_link, resume_note, about_me.
    Пустые/None значения игнорируются, чтобы не затирать уже известные данные."""
    allowed = {
        "name", "vacancy", "interview_date", "salary_expectations",
        "desired_role", "portfolio_link", "resume_note", "about_me",
    }
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


def mark_reserve(chat_id: int) -> None:
    """Помечает кандидата как добавленного в кадровый резерв."""
    with _lock:
        _conn.execute("UPDATE candidates SET is_reserve = 1 WHERE chat_id = ?", (chat_id,))
        _conn.commit()
    logger.info("Кандидат chat_id=%s добавлен в кадровый резерв", chat_id)


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


def add_uploaded_file(chat_id: int, file_id: str, file_type: str = "document",
                       file_name: str | None = None, mime_type: str | None = None) -> None:
    """Сохраняет присланный кандидатом файл. notion_synced=False по умолчанию -
    отмечается True функцией mark_uploaded_files_synced после того, как файл
    реально прикреплён к карточке в Notion (см. notion_service.sync_uploaded_files),
    чтобы не скачивать и не загружать его туда повторно при каждом новом
    сообщении кандидата."""
    entry = {"file_id": file_id, "type": file_type, "notion_synced": False}
    if file_name:
        entry["file_name"] = file_name
    if mime_type:
        entry["mime_type"] = mime_type
    _append_json_list(chat_id, "uploaded_files", entry)


def mark_uploaded_files_synced(chat_id: int, file_ids: list) -> None:
    """Отмечает файлы кандидата как уже прикреплённые к карточке Notion -
    см. add_uploaded_file и notion_service.sync_uploaded_files."""
    if not file_ids:
        return
    with _lock:
        cur = _conn.execute("SELECT uploaded_files FROM candidates WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        items = json.loads(row[0]) if row and row[0] else []
        changed = False
        for item in items:
            if isinstance(item, dict) and item.get("file_id") in file_ids:
                item["notion_synced"] = True
                changed = True
        if changed:
            _conn.execute(
                "UPDATE candidates SET uploaded_files = ? WHERE chat_id = ?",
                (json.dumps(items, ensure_ascii=False), chat_id),
            )
            _conn.commit()


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


def get_notion_card(chat_id: int, vacancy: str) -> dict | None:
    """Возвращает уже существующую карточку Notion для пары (chat_id, vacancy),
    если она была создана ранее. Используется для дедупликации: одна и та же
    пара кандидат+вакансия никогда не должна получить вторую карточку, но
    один кандидат может иметь отдельные карточки при отклике на разные
    вакансии."""
    with _lock:
        cur = _conn.execute(
            "SELECT notion_page_id, notion_page_url FROM notion_cards WHERE chat_id = ? AND vacancy = ?",
            (chat_id, vacancy),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"notion_page_id": row["notion_page_id"], "notion_page_url": row["notion_page_url"]}


def save_notion_card(chat_id: int, vacancy: str, notion_page_id: str, notion_page_url: str) -> None:
    """Запоминает связь кандидат+вакансия -> карточка Notion сразу после её
    создания, чтобы дальнейшие обновления шли в ту же карточку."""
    with _lock:
        _conn.execute(
            "INSERT OR REPLACE INTO notion_cards (chat_id, vacancy, notion_page_id, notion_page_url, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, vacancy, notion_page_id, notion_page_url, time.time()),
        )
        _conn.commit()
    logger.info("Сохранена связь с карточкой Notion chat_id=%s vacancy=%s page_id=%s", chat_id, vacancy, notion_page_id)


def update_last_activity(chat_id: int) -> None:
    """Отмечает время последнего сообщения от кандидата. Используется фоновой
    задачей check_finished_dialogs в bot.py, чтобы понять, что кандидат
    закончил переписку с ботом и по нему пора отправить рекрутеру короткий
    апдейт (если он ещё не был отправлен ни автоматически, ни через
    notify_recruiter)."""
    with _lock:
        _conn.execute(
            "UPDATE candidates SET last_activity_at = ? WHERE chat_id = ?",
            (time.time(), chat_id),
        )
        _conn.commit()

def mark_summary_notified(chat_id: int) -> None:
    """Отмечает, что рекрутер уже получил апдейт по этому кандидату - вручную
    (через notify_recruiter) или автоматически по завершению диалога, — чтобы
    не отправлять дублирующий автоматический апдейт."""
    with _lock:
        _conn.execute(
            "UPDATE candidates SET summary_notified = 1 WHERE chat_id = ?",
            (chat_id,),
        )
        _conn.commit()

def find_finished_dialog_candidates(older_than_seconds: int) -> list:
    """Возвращает кандидатов сценария «Открытые вакансии» (is_reserve = 0),
    которые писали боту (last_activity_at задан), не выходили на связь дольше
    порога и по которым ещё не отправлялся ни один апдейт рекрутеру - ни
    ручной через notify_recruiter, ни автоматический."""
    threshold = time.time() - older_than_seconds
    with _lock:
        cur = _conn.execute(
            "SELECT * FROM candidates WHERE last_activity_at IS NOT NULL "
            "AND last_activity_at < ? AND summary_notified = 0 AND is_reserve = 0",
            (threshold,),
        )
        rows = cur.fetchall()
    return [_row_to_dict(r) for r in rows]
