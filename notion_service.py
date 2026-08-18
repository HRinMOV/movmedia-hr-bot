"""
notion_service.py

Интеграция с Notion: создание и обновление карточек кандидатов в базе
"Кандидаты (база данных)" при отклике на открытую вакансию через сценарий
"Открытые вакансии" (см. architecture.md). Сценарий "Хочу в команду
MOVmedia" эту базу не использует и не затрагивается этим модулем.

Создание карточки НЕ зависит от названия вакансии - оно происходит для
любой актуальной вакансии, на которую кандидат откликнулся через меню
"Открытые вакансии", в момент, когда AI-модель шлёт итоговую сводку
рекрутеру (notify_recruiter, reason="candidate_summary").

Дедупликация делается локально в SQLite (storage.notion_cards) по паре
(chat_id, vacancy) - один кандидат может иметь отдельные карточки при
отклике на разные вакансии, повторные вызовы не создают дублей.

NOTION_TOKEN и NOTION_DATABASE_ID передаются только через переменные
окружения (config.py) и никогда не хранятся в коде.
"""
import logging
import re

import requests

import storage
from config import NOTION_TOKEN, NOTION_DATABASE_ID, PROXY_URL

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Названия колонок должны точно совпадать с реальными полями в базе
# "Кандидаты (база данных)". Новые поля в Notion самостоятельно не создаются -
# при необходимости расширить список полей нужно отдельное согласование.
PROP_NAME = "ФИО"
PROP_VACANCY = "Вакансия"
PROP_RESUME = "Резюме"
PROP_PORTFOLIO = "Портфолио"
PROP_SALARY = "Финожидания"
PROP_TEST_LINK = "Тестовое"
PROP_TEST_COMMENT = "Комментарий по тестовому"

_URL_RE = re.compile(r"https?://\S+")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _configured() -> bool:
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        logger.warning(
            "NOTION_TOKEN/NOTION_DATABASE_ID не заданы - карточка кандидата в Notion не создаётся"
        )
        return False
    return True


def _proxies() -> dict | None:
    """Прокси для обращений к api.notion.com - тот же PROXY_URL, что уже
    используется для Telegram Bot API (см. bot.py). Notion тоже может быть
    заблокирован на сервере, поэтому используем тот же механизм."""
    if not PROXY_URL:
        return None
    return {"http": PROXY_URL, "https": PROXY_URL}


def _rich_text(value: str) -> list:
    return [{"type": "text", "text": {"content": value[:2000]}}]


def _extract_links(value: str | None) -> list[str]:
    """Резюме/портфолио могут содержать несколько ссылок в одной строке -
    достаём все, чтобы не потерять ни одной (см. ТЗ п.13)."""
    if not value:
        return []
    return _URL_RE.findall(value)


def _url_property(value: str | None) -> tuple[dict | None, list[str]]:
    """Возвращает (свойство для Notion URL-поля с первой ссылкой, список
    оставшихся ссылок, которые нужно дописать в тело карточки)."""
    links = _extract_links(value)
    if not links:
        return None, []
    return {"url": links[0]}, links[1:]

def _files_property(value: str | None, label: str) -> tuple[dict | None, list[str]]:
    """Возвращает свойство для Notion-поля типа "Files & media" со ссылкой на
    внешний файл (без реальной загрузки байтов - Notion принимает такие
    "external"-ссылки как обычные файлы в интерфейсе), плюс список оставшихся
    ссылок, которые нужно дописать в тело карточки.

    Поля "Резюме"/"Портфолио" в базе "Кандидаты (база данных)" настроены как
    Files & media, а не URL - обычное {"url": ...} Notion отклоняет с 400
    validation_error ("... is expected to be files")."""
    links = _extract_links(value)
    if not links:
        return None, []
    return {"files": [{"type": "external", "name": label, "external": {"url": links[0]}}]}, links[1:]


def _build_properties(candidate: dict) -> tuple[dict, list[str]]:
    """Строит properties для создания/обновления страницы. Возвращает также
    список "лишних" ссылок портфолио/резюме, не поместившихся в url-поля."""
    props = {}
    extra_links: list[str] = []

    name = candidate.get("name") or candidate.get("username") or f"Кандидат {candidate.get('chat_id')}"
    props[PROP_NAME] = {"title": _rich_text(name)}

    vacancy = candidate.get("vacancy")
    if vacancy:
        props[PROP_VACANCY] = {"multi_select": [{"name": vacancy[:100]}]}

    resume_prop, resume_extra = _files_property(candidate.get("resume_note"), "Резюме")
    if resume_prop:
        props[PROP_RESUME] = resume_prop
        extra_links += [f"Резюме (доп. ссылка): {l}" for l in resume_extra]

    portfolio_prop, portfolio_extra = _url_property(candidate.get("portfolio_link"))
    if portfolio_prop:
        props[PROP_PORTFOLIO] = portfolio_prop
        extra_links += [f"Портфолио (доп. ссылка): {l}" for l in portfolio_extra]

    salary = candidate.get("salary_expectations")
    if salary:
        props[PROP_SALARY] = {"rich_text": _rich_text(salary)}

    return props, extra_links


def _body_children(candidate: dict, summary_message: str | None, extra_links: list[str]) -> list:
    """Информация, для которой в базе нет отдельных полей, - добавляется в
    тело карточки, а не в виде новых свойств Notion."""

    def para(text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich_text(text)}}

    blocks = []
    who = f"@{candidate['username']}" if candidate.get("username") else f"id {candidate.get('chat_id')}"
    blocks.append(para(f"Источник: Telegram Bot. Кандидат: {who} (Telegram ID {candidate.get('chat_id')})."))

    if summary_message:
        blocks.append(para(f"Сводка от AI-ассистента: {summary_message}"))

    for link in extra_links:
        blocks.append(para(link))

    if candidate.get("uploaded_files"):
        blocks.append(para(
            "Кандидат также присылал файл(ы) в Telegram (резюме/тестовое). Notion API "
            "не поддерживает загрузку бинарных файлов в поля этой базы, поэтому файлы "
            "переданы напрямую в чат рекрутера."
        ))

    return blocks


def find_existing_card(chat_id: int, vacancy: str) -> dict | None:
    return storage.get_notion_card(chat_id, vacancy)


def create_or_update_candidate_card(candidate: dict, summary_message: str | None = None) -> dict | None:
    """Создаёт карточку кандидата в Notion при первом отклике на вакансию,
    либо обновляет уже существующую карточку той же пары (chat_id, vacancy).
    Никогда не создаёт вторую карточку для одной и той же пары.

    Возвращает {"id": notion_page_id, "url": notion_page_url} либо None, если
    интеграция не настроена или запрос к API не удался."""
    if not _configured():
        return None

    chat_id = candidate.get("chat_id")
    vacancy = candidate.get("vacancy")
    if not vacancy:
        logger.warning("create_or_update_candidate_card вызван без вакансии, chat_id=%s", chat_id)
        return None

    existing = find_existing_card(chat_id, vacancy)
    properties, extra_links = _build_properties(candidate)

    try:
        if existing:
            response = requests.patch(
                f"{NOTION_API_BASE}/pages/{existing['notion_page_id']}",
                headers=_headers(),
                json={"properties": properties},
                timeout=30,
                proxies=_proxies(),
            )
            response.raise_for_status()
            page_id = existing["notion_page_id"]
            page_url = existing["notion_page_url"]
            logger.info("Обновлена карточка Notion chat_id=%s vacancy=%s page_id=%s", chat_id, vacancy, page_id)
            return {"id": page_id, "url": page_url}
        else:
            response = requests.post(
                f"{NOTION_API_BASE}/pages",
                headers=_headers(),
                json={
                    "parent": {"database_id": NOTION_DATABASE_ID},
                    "properties": properties,
                    "children": _body_children(candidate, summary_message, extra_links),
                },
                timeout=30,
                proxies=_proxies(),
            )
            response.raise_for_status()
            data = response.json()
            page_id = data["id"]
            page_url = data.get("url", "")
            storage.save_notion_card(chat_id, vacancy, page_id, page_url)
            logger.info("Создана карточка Notion chat_id=%s vacancy=%s page_id=%s", chat_id, vacancy, page_id)
            return {"id": page_id, "url": page_url}
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        detail = f" status={response.status_code} body={response.text[:500]}" if response is not None else " (нет ответа - таймаут/сетевая ошибка)"
        logger.error("Ошибка обращения к Notion API chat_id=%s vacancy=%s%s", chat_id, vacancy, detail, exc_info=True)
        return None


def append_test_task_info(chat_id: int, vacancy: str, test_link: str | None = None,
                           comment: str | None = None) -> None:
    """Дополняет уже существующую карточку информацией о тестовом задании
    (не создаёт новую карточку, если она ещё не существует - это означало бы,
    что candidate_summary ещё не отправлялся)."""
    if not _configured():
        return
    existing = find_existing_card(chat_id, vacancy)
    if not existing:
        logger.info("append_test_task_info: карточка ещё не создана, chat_id=%s vacancy=%s", chat_id, vacancy)
        return

    properties = {}
    if test_link:
        properties[PROP_TEST_LINK] = {"url": test_link}
    if comment:
        properties[PROP_TEST_COMMENT] = {"rich_text": _rich_text(comment)}
    if not properties:
        return

    try:
        response = requests.patch(
            f"{NOTION_API_BASE}/pages/{existing['notion_page_id']}",
            headers=_headers(),
            json={"properties": properties},
            timeout=30,
            proxies=_proxies(),
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        detail = f" status={response.status_code} body={response.text[:500]}" if response is not None else " (нет ответа - таймаут/сетевая ошибка)"
        logger.error("Ошибка обновления тестового задания в Notion chat_id=%s vacancy=%s%s", chat_id, vacancy, detail, exc_info=True)
