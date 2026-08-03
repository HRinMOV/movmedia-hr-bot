import logging
import time
import uuid

import requests

from config import GIGACHAT_AUTH_KEY, GIGACHAT_MODEL, GIGACHAT_SCOPE
from system_prompt import build_system_prompt

logger = logging.getLogger(__name__)

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://api.giga.chat/v1/chat/completions"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5

_token_cache = {"access_token": None, "expires_at": 0}


class GigaChatError(Exception):
    """Финальный сбой обращения к GigaChat после всех повторных попыток."""


def _request_with_retry(method, url, **kwargs):
    """Выполняет HTTP-запрос с повторными попытками и экспоненциальной паузой.

    Никогда не оставляет кандидата без ответа молча: если все попытки
    исчерпаны, поднимает GigaChatError, которую вызывающий код (bot.py)
    обязан обработать и показать кандидату вежливый fallback-ответ.
    """
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            response = method(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            logger.warning(
                "Запрос к GigaChat (%s) не удался, попытка %s/%s: %s",
                url, attempt, RETRY_ATTEMPTS, exc,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Запрос к GigaChat (%s) окончательно не удался после %s попыток: %s", url, RETRY_ATTEMPTS, last_exc)
    raise GigaChatError(f"Не удалось получить ответ от GigaChat: {last_exc}") from last_exc


def _get_access_token() -> str:
    """Получает (и кэширует) access token GigaChat. Токен живёт 30 минут, обновляем заранее."""
    now_ms = time.time() * 1000
    if _token_cache["access_token"] and _token_cache["expires_at"] - now_ms > 60_000:
        return _token_cache["access_token"]

    response = _request_with_retry(
        requests.post,
        OAUTH_URL,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "RqUID": str(uuid.uuid4()),
            "Authorization": f"Basic {GIGACHAT_AUTH_KEY}",
        },
        data={"scope": GIGACHAT_SCOPE},
        timeout=30,
    )
    data = response.json()
    _token_cache["access_token"] = data["access_token"]
    _token_cache["expires_at"] = data["expires_at"]
    return data["access_token"]


FUNCTIONS = [
    {
        "name": "notify_recruiter",
        "description": (
            "Отправить рекрутеру Алине сообщение о кандидате. Используй это, когда: "
            "(a) кандидат прислал всё необходимое (вакансия, имя, резюме, телефон, "
            "тестовое, если применимо) — пришли полную сводку по кандидату; "
            "(b) кандидат задал вопрос, точного ответа на который нет в базе знаний — "
            "передай вопрос дословно; "
            "(c) кандидат уже второй раз и более просит связать напрямую с рекрутером; "
            "(d) кандидат явно сообщил, что передумал проходить отбор."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": [
                        "candidate_summary",
                        "unknown_question",
                        "connect_request",
                        "withdrawal",
                    ],
                },
                "message": {
                    "type": "string",
                    "description": "Текст сообщения рекрутеру — понятный, структурированный, на русском.",
                },
            },
            "required": ["reason", "message"],
        },
    },
    {
        "name": "update_candidate_stage",
        "description": (
            "Обновить внутреннюю стадию кандидата для технического отслеживания "
            "прогресса в рамках этого чата. Кандидат не видит этот вызов. Остальные "
            "стадии (screening, interview_scheduled, interview_completed, review, "
            "offer, rejected) выставляет рекрутер вручную вне этого диалога — их "
            "вызывать не нужно."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["test_sent", "test_received", "withdrawn"],
                },
            },
            "required": ["stage"],
        },
    },
    {
        "name": "update_candidate_profile",
        "description": (
            "Сохранить/обновить известную информацию о кандидате, чтобы не "
            "переспрашивать её повторно. Вызывай сразу, как только узнал "
            "новое значение любого из полей — не дожидаясь конца диалога. "
            "Указывай только те поля, которые реально стали известны в этом сообщении."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Имя кандидата"},
                "vacancy": {
                    "type": "string",
                    "description": "Вакансия, на которую откликается кандидат (например, 'PR-менеджер')",
                },
                "interview_date": {
                    "type": "string",
                    "description": "Дата/время интервью, если кандидат её называл или подтверждал",
                },
                "salary_expectations": {
                    "type": "string",
                    "description": "Зарплатные ожидания кандидата, если он их называл",
                },
            },
            "required": [],
        },
    },
]


def _history_to_messages(history: list, candidate_profile: dict | None = None) -> list:
    """Преобразует внутреннюю историю кандидата в формат messages GigaChat API."""
    messages = [{"role": "system", "content": build_system_prompt(candidate_profile)}]
    for item in history:
        item_type = item.get("type")
        if item_type == "user_input":
            text = "".join(block.get("text", "") for block in item.get("content", []))
            messages.append({"role": "user", "content": text})
        elif item_type == "model_output":
            text = "".join(block.get("text", "") for block in item.get("content", []))
            messages.append({"role": "assistant", "content": text})
        elif item_type == "function_call":
            messages.append({
                "role": "assistant",
                "content": "",
                "function_call": {"name": item["name"], "arguments": item["arguments"]},
                "functions_state_id": item.get("functions_state_id", ""),
            })
        elif item_type == "function_result":
            messages.append({"role": "function", "content": item.get("result", ""), "name": item.get("name", "")})
    return messages


def run_turn(history: list, tool_executor, candidate_profile: dict | None = None) -> tuple[str, list]:
    """
    Прогоняет один ход диалога через GigaChat Chat Completions API,
    при необходимости выполняет tool-calls через tool_executor(name, input) -> str,
    и возвращает (финальный текст для кандидата, обновлённая история).

    При исчерпании попыток обращения к GigaChat поднимает GigaChatError —
    вызывающий код обязан поймать её и показать кандидату fallback-ответ,
    чтобы сообщение никогда не осталось без реакции.
    """
    updated_history = list(history)

    while True:
        token = _get_access_token()
        messages = _history_to_messages(updated_history, candidate_profile)

        response = _request_with_retry(
            requests.post,
            CHAT_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json={
                "model": GIGACHAT_MODEL,
                "messages": messages,
                "functions": FUNCTIONS,
            },
            timeout=60,
        )
        data = response.json()
        message = data["choices"][0]["message"]

        function_call = message.get("function_call")
        if not function_call:
            reply_text = message.get("content", "")
            updated_history.append({
                "type": "model_output",
                "content": [{"type": "text", "text": reply_text}],
            })
            return reply_text, updated_history

        arguments = function_call.get("arguments") or {}
        updated_history.append({
            "type": "function_call",
            "name": function_call["name"],
            "arguments": arguments,
            "functions_state_id": message.get("functions_state_id", ""),
        })

        result_text = tool_executor(function_call["name"], arguments)
        updated_history.append({"type": "function_result", "result": result_text, "name": function_call["name"]})
