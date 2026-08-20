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

Файлы, присланные кандидатом в Telegram напрямую (не ссылкой), реально
прикрепляются в поле "Резюме" через Notion File Upload API - см.
sync_uploaded_files и комментарий у TELEGRAM_MAX_FILE_BYTES ниже про то,
почему это стало возможным и какие есть ограничения.

NOTION_TOKEN и NOTION_DATABASE_ID передаются только через переменные
окружения (config.py) и никогда не хранятся в коде.
"""
import logging
import re
import time

import requests

import storage
from config import BOT_TOKEN, NOTION_TOKEN, NOTION_DATABASE_ID, PROXY_URL

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# File Upload API (file_uploads) - более новая часть Notion API, появившаяся
# уже после версии 2022-06-28. Она обратно совместима, но чтобы не рисковать
# уже проверенным на проде созданием/обновлением страниц (переход на версии
# новее 2025-09-03 - breaking change: parent страницы адресуется через
# data_source_id вместо database_id), используем для этих запросов отдельную,
# более свежую версию, не трогая NOTION_VERSION выше.
NOTION_FILE_UPLOAD_VERSION = "2025-09-03"

TELEGRAM_API_BASE = "https://api.telegram.org"
# Обычный (не self-hosted) Telegram Bot API не отдаёт через getFile файлы
# тяжелее 20 МБ - это ограничение самого Telegram, а не Notion, и обойти его
# без развёртывания своего Bot API сервера нельзя. Совпадает с лимитом
# single-part загрузки в Notion, поэтому multi-part upload не нужен.
TELEGRAM_MAX_FILE_BYTES = 20 * 1024 * 1024

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


def _file_upload_json_headers() -> dict:
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_FILE_UPLOAD_VERSION,
        "Content-Type": "application/json",
    }


def _file_upload_multipart_headers() -> dict:
    # Content-Type сюда специально не добавляем - requests сам проставит
    # multipart/form-data с правильным boundary при передаче files=...
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_FILE_UPLOAD_VERSION,
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


_schema_cache: dict = {"properties": None, "fetched_at": 0.0}
_SCHEMA_CACHE_TTL_SECONDS = 6 * 3600


def _database_property_types() -> dict:
    """Возвращает {имя_поля: тип_поля} реальной схемы базы NOTION_DATABASE_ID,
    а не наши предположения о ней. Раньше расхождение предположения с
    реальным типом поля (например, "Портфолио" оказался url, а не Files &
    media) приводило к 400 validation_error и требовало ручного revert (см.
    историю коммитов) - эта функция позволяет определять тип поля перед
    отправкой запроса вместо угадывания. Кэшируется в процессе на несколько
    часов; при ошибке возвращает последнее известное значение."""
    now = time.time()
    if _schema_cache["properties"] is not None and now - _schema_cache["fetched_at"] < _SCHEMA_CACHE_TTL_SECONDS:
        return _schema_cache["properties"]
    try:
        response = requests.get(
            f"{NOTION_API_BASE}/databases/{NOTION_DATABASE_ID}",
            headers=_headers(),
            timeout=30,
            proxies=_proxies(),
        )
        response.raise_for_status()
        data = response.json()
        types = {name: info.get("type") for name, info in data.get("properties", {}).items()}
        _schema_cache["properties"] = types
        _schema_cache["fetched_at"] = now
        return types
    except requests.RequestException:
        logger.exception("Не удалось получить схему базы Notion, использую предыдущую известную/пустую")
        return _schema_cache["properties"] or {}


def _link_property(value: str | None, prop_name: str, label: str, default_type: str) -> tuple[dict | None, list[str]]:
    """Строит properties-значение для поля со ссылкой (резюме/портфолио),
    выбирая формат по реальному типу поля в Notion (url либо Files & media),
    а не по жёстко зашитому предположению. default_type используется, если
    схему не удалось получить."""
    prop_type = _database_property_types().get(prop_name, default_type)
    if prop_type == "files":
        return _files_property(value, label)
    return _url_property(value)


def _download_telegram_file(file_id: str) -> tuple[bytes, str] | None:
    """Скачивает файл из Telegram по file_id через обычный Bot API (getFile +
    скачивание по file_path). Возвращает (содержимое, имя файла из Telegram)
    либо None, если скачать не удалось - сетевая ошибка или файл тяжелее
    TELEGRAM_MAX_FILE_BYTES (см. комментарий у константы)."""
    if not BOT_TOKEN:
        return None
    try:
        info_response = requests.get(
            f"{TELEGRAM_API_BASE}/bot{BOT_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=30,
            proxies=_proxies(),
        )
        info_response.raise_for_status()
        result = info_response.json().get("result", {})
        file_path = result.get("file_path")
        file_size = result.get("file_size") or 0
        if not file_path:
            logger.warning("Telegram getFile не вернул file_path для file_id=%s", file_id)
            return None
        if file_size and file_size > TELEGRAM_MAX_FILE_BYTES:
            logger.warning(
                "Файл file_id=%s тяжелее лимита Telegram Bot API (%s байт) - не могу скачать для Notion, "
                "останется только в чате рекрутера", file_id, file_size,
            )
            return None
        content_response = requests.get(
            f"{TELEGRAM_API_BASE}/file/bot{BOT_TOKEN}/{file_path}",
            timeout=60,
            proxies=_proxies(),
        )
        content_response.raise_for_status()
        suggested_name = file_path.rsplit("/", 1)[-1]
        return content_response.content, suggested_name
    except requests.RequestException:
        logger.exception("Ошибка скачивания файла из Telegram file_id=%s", file_id)
        return None


def _upload_file_to_notion(content: bytes, filename: str, content_type: str | None) -> str | None:
    """Загружает файл в Notion через File Upload API (создание загрузки +
    отправка содержимого одним куском - single-part, файлы из Telegram не
    превышают 20 МБ, см. TELEGRAM_MAX_FILE_BYTES). Возвращает id завершённой
    загрузки (готов для использования в свойстве files как file_upload),
    либо None при ошибке."""
    try:
        create_payload = {"filename": (filename or "file")[:900]}
        if content_type:
            create_payload["content_type"] = content_type
        create_response = requests.post(
            f"{NOTION_API_BASE}/file_uploads",
            headers=_file_upload_json_headers(),
            json=create_payload,
            timeout=30,
            proxies=_proxies(),
        )
        create_response.raise_for_status()
        upload_id = create_response.json()["id"]

        send_response = requests.post(
            f"{NOTION_API_BASE}/file_uploads/{upload_id}/send",
            headers=_file_upload_multipart_headers(),
            files={"file": (filename or "file", content, content_type or "application/octet-stream")},
            timeout=60,
            proxies=_proxies(),
        )
        send_response.raise_for_status()
        return upload_id
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        detail = f" status={response.status_code} body={response.text[:500]}" if response is not None else " (нет ответа - таймаут/сетевая ошибка)"
        logger.error("Ошибка загрузки файла в Notion filename=%s%s", filename, detail, exc_info=True)
        return None


def _existing_files_property(page_id: str, prop_name: str) -> list:
    """Читает текущее значение files-свойства страницы - нужно, чтобы не
    затереть уже прикреплённые ранее файлы: PATCH заменяет значение
    свойства целиком, а не дописывает в него."""
    try:
        response = requests.get(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=_headers(),
            timeout=30,
            proxies=_proxies(),
        )
        response.raise_for_status()
        prop = response.json().get("properties", {}).get(prop_name) or {}
        return prop.get("files", [])
    except requests.RequestException:
        logger.exception("Не удалось прочитать текущее значение поля '%s' page_id=%s", prop_name, page_id)
        return []


def _attach_files(page_id: str, prop_name: str, new_entries: list) -> bool:
    """Дописывает новые файлы в files-свойство карточки поверх уже
    прикреплённых (см. _existing_files_property)."""
    if not new_entries:
        return True
    merged = _existing_files_property(page_id, prop_name) + new_entries
    try:
        response = requests.patch(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=_headers(),
            json={"properties": {prop_name: {"files": merged}}},
            timeout=30,
            proxies=_proxies(),
        )
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        response = getattr(exc, "response", None)
        detail = f" status={response.status_code} body={response.text[:500]}" if response is not None else " (нет ответа - таймаут/сетевая ошибка)"
        logger.error("Ошибка прикрепления файлов к полю '%s' page_id=%s%s", prop_name, page_id, detail, exc_info=True)
        return False


def sync_uploaded_files(chat_id: int, candidate: dict) -> None:
    """Докачивает файлы, присланные кандидатом в Telegram напрямую (резюме,
    тестовое, доп. документы - в отличие от ссылок, которые уже
    обрабатываются в _build_properties/_link_property), и реально
    прикрепляет их в Notion через File Upload API к файловому полю карточки.

    Notion REST API не принимает бинарное содержимое файла прямо в запросе
    создания/обновления страницы - только ссылку на файл, заранее
    загруженный через отдельный File Upload API (или на внешний URL). Раньше
    в это поле попадала только ссылка, если кандидат присылал резюме текстом
    со ссылкой; если резюме/тестовое присылались файлом Telegram - карточка
    оставалась без вложения, файл уходил только в чат рекрутера (см.
    _send_recruiter_update в bot.py). Эта функция закрывает именно этот
    случай - файлы теперь дополнительно прикрепляются и в саму карточку.

    Карточку сама не создаёт - вызывается только когда она уже существует
    (см. вызовы в bot.py: сразу при получении файла, если карточка уже
    создана, и сразу после создания/обновления карточки - на случай файлов,
    присланных раньше, чем AI отправил итоговую сводку)."""
    if not _configured() or not BOT_TOKEN:
        return
    vacancy = candidate.get("vacancy")
    if not vacancy:
        return
    existing = find_existing_card(chat_id, vacancy)
    if not existing:
        return

    files = candidate.get("uploaded_files") or []
    pending = [f for f in files if isinstance(f, dict) and f.get("file_id") and not f.get("notion_synced")]
    if not pending:
        return

    # "Резюме" - единственное поле типа Files & media в текущей структуре
    # базы (см. PROP_RESUME) - в него попадают все файлы кандидата (резюме,
    # тестовое, доп. документы), т.к. отдельного файлового поля под каждый
    # тип документа в базе нет. Проверяем реальный тип поля на случай, если
    # структура базы поменяется - чтобы не слать туда файлы вслепую.
    schema = _database_property_types()
    if schema.get(PROP_RESUME, "files") != "files":
        logger.warning(
            "Поле '%s' в базе Notion больше не Files & media (тип=%s) - не могу прикрепить файлы кандидата, "
            "они останутся только в чате рекрутера", PROP_RESUME, schema.get(PROP_RESUME),
        )
        return

    new_entries = []
    synced_file_ids = []
    for item in pending:
        downloaded = _download_telegram_file(item["file_id"])
        if not downloaded:
            continue
        content, suggested_name = downloaded
        display_name = item.get("file_name") or suggested_name
        upload_id = _upload_file_to_notion(content, display_name, item.get("mime_type"))
        if not upload_id:
            continue
        new_entries.append({
            "type": "file_upload",
            "file_upload": {"id": upload_id},
            "name": display_name[:100],
        })
        synced_file_ids.append(item["file_id"])

    if not new_entries:
        return

    if _attach_files(existing["notion_page_id"], PROP_RESUME, new_entries):
        storage.mark_uploaded_files_synced(chat_id, synced_file_ids)
        logger.info(
            "Прикреплено %s файл(ов) к карточке Notion chat_id=%s vacancy=%s",
            len(new_entries), chat_id, vacancy,
        )


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

    resume_prop, resume_extra = _link_property(candidate.get("resume_note"), PROP_RESUME, "Резюме", default_type="files")
    if resume_prop:
        props[PROP_RESUME] = resume_prop
        extra_links += [f"Резюме (доп. ссылка): {l}" for l in resume_extra]

    portfolio_prop, portfolio_extra = _link_property(candidate.get("portfolio_link"), PROP_PORTFOLIO, "Портфолио", default_type="url")
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
            "Кандидат также присылал файл(ы) в Telegram (резюме/тестовое/доп. документы). "
            "Они прикрепляются в поле \"Резюме\" этой карточки через Notion File Upload API "
            "(обычно сразу после создания карточки, см. sync_uploaded_files) и дополнительно "
            "пересылаются напрямую в чат рекрутера. Если какой-то файл не прикрепился (например, "
            "он тяжелее 20 МБ), он всё равно останется в чате рекрутера."
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
            if response.status_code == 400 and "archived" in response.text.lower():
                # Карточку могли вручную заархивировать/удалить в Notion -
                # разархивируем её и повторяем запрос, чтобы не плодить
                # дубликаты карточек для одного и того же кандидата.
                logger.warning(
                    "Карточка Notion chat_id=%s vacancy=%s page_id=%s заархивирована, разархивирую",
                    chat_id, vacancy, existing['notion_page_id'],
                )
                unarchive_response = requests.patch(
                    f"{NOTION_API_BASE}/pages/{existing['notion_page_id']}",
                    headers=_headers(),
                    json={"archived": False},
                    timeout=30,
                    proxies=_proxies(),
                )
                unarchive_response.raise_for_status()
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
        prop_type = _database_property_types().get(PROP_TEST_LINK, "url")
        if prop_type == "files":
            # Поле "Тестовое" оказалось Files & media, а не url - прикрепляем
            # через merge (см. _attach_files), а не через properties ниже,
            # чтобы не затереть уже прикреплённые ранее файлы тестового.
            file_prop, _ignored_extra = _files_property(test_link, "Тестовое")
            if file_prop:
                _attach_files(existing["notion_page_id"], PROP_TEST_LINK, file_prop["files"])
        else:
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
