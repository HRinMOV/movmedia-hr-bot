import time
import uuid

import requests

from config import GIGACHAT_AUTH_KEY, GIGACHAT_MODEL, GIGACHAT_SCOPE
from system_prompt import build_system_prompt

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
CHAT_URL = "https://api.giga.chat/v1/chat/completions"

_token_cache = {"access_token": None, "expires_at": 0}


def _get_access_token() -> str:
    """Получает (и кэширует) access token GigaChat. Токен живёт 30 минут, обновляем заранее."""
    now_ms = time.time() * 1000
    if _token_cache["access_token"] and _token_cache["expires_at"] - now_ms > 60_000:
        return _token_cache["access_token"]

    response = requests.post(
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
    response.raise_for_status()
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
            "прогресса. Кандидат не видит этот вызов."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["test_task_sent", "test_task_submitted", "withdrawn"],
                },
            },
            "required": ["stage"],
        },
    },
]


def _history_to_messages(history: list) -> list:
    """Преобразует внутреннюю историю кандидата в формат messages GigaChat API."""
    messages = [{"role": "system", "content": build_system_prompt()}]
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
            messages.append({"role": "function", "content": item.get("result", "")})
    return messages


def run_turn(history: list, tool_executor) -> tuple[str, list]:
    """
    Прогоняет один ход диалога через GigaChat Chat Completions API,
    при необходимости выполняет tool-calls через tool_executor(name, input) -> str,
    и возвращает (финальный текст для кандидата, обновлённая история).
    """
    updated_history = list(history)

    while True:
        token = _get_access_token()
        messages = _history_to_messages(updated_history)

        response = requests.post(
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
        response.raise_for_status()
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
        updated_history.append({"type": "function_result", "result": result_text})
