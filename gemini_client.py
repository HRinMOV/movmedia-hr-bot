import logging
import time

import requests

from config import GEMINI_API_KEY, GEMINI_MODEL, PROXY_URL
from system_prompt import build_system_prompt

logger = logging.getLogger(__name__)

GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.5
PROXY_KWARGS = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


class GeminiError(Exception):
    """Финальный сбой при обращении к Gemini после всех повторных попыток."""


def _request_with_retry(method, url, **kwargs):
    """Выполняет HTTP-запрос с повторными попытками и экспоненциальной паузой.

    Никогда не оставляет кандидата без ответа молча: если все попытки
    исчерпаны, поднимает GeminiError, которую вызывающий код (bot.py)
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
            body = getattr(exc.response, "text", None)
            if body:
                logger.warning("Тело ответа Gemini: %s", body[:1000])
            logger.warning(
                "Запрос к Gemini (%s) не удался, попытка %s/%s: %s",
                url, attempt, RETRY_ATTEMPTS, exc,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_BACKOFF_SECONDS * attempt)
    logger.error("Запрос к Gemini (%s) окончательно не удался после %s попыток: %s", url, RETRY_ATTEMPTS, last_exc)
    raise GeminiError(f"Не удалось получить ответ от Gemini: {last_exc}") from last_exc


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
            "type": "OBJECT",
            "properties": {
                "reason": {
                    "type": "STRING",
                    "enum": [
                        "candidate_summary",
                        "unknown_question",
                        "connect_request",
                        "withdrawal",
                    ],
                },
                "message": {
                    "type": "STRING",
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
            "type": "OBJECT",
            "properties": {
                "stage": {
                    "type": "STRING",
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
            "type": "OBJECT",
            "properties": {
                "name": {"type": "STRING", "description": "Имя кандидата"},
                "vacancy": {
                    "type": "STRING",
                    "description": "Вакансия, на которую откликается кандидат (например, «PR-менеджер»)",
                },
                "interview_date": {
                    "type": "STRING",
                    "description": "Дата/время интервью, если кандидат её называл или подтверждал",
                },
                "salary_expectations": {
                    "type": "STRING",
                    "description": "Зарплатные ожидания кандидата, если он их называл",
                },
            },
            "required": [],
        },
    },
]


def _history_to_contents(history: list) -> list:
    """Преобразует внутреннюю историю кандидата в формат contents Gemini API."""
    contents = []
    for item in history:
        item_type = item.get("type")
        if item_type == "user_input":
            text = "".join(block.get("text", "") for block in item.get("content", []))
            contents.append({"role": "user", "parts": [{"text": text}]})
        elif item_type == "model_output":
            text = "".join(block.get("text", "") for block in item.get("content", []))
            contents.append({"role": "model", "parts": [{"text": text}]})
        elif item_type == "function_call":
            contents.append({
                "role": "model",
                "parts": [{"functionCall": {"name": item["name"], "args": item.get("arguments", {})}}],
            })
        elif item_type == "function_result":
            contents.append({
                "role": "function",
                "parts": [{"functionResponse": {
                    "name": item.get("name", ""),
                    "response": {"result": item.get("result", "")},
                }}],
            })
    return contents


def run_turn(history: list, tool_executor, candidate_profile: dict | None = None) -> tuple[str, list]:
    """
    Прогоняет один ход диалога через Gemini API (generateContent),
    при необходимости выполняет tool-calls через tool_executor(name, input) -> str,
    и возвращает (финальный текст для кандидата, обновлённая история).

    При исчерпании попыток обращения к Gemini поднимает GeminiError —
    вызывающий код обязан поймать её и показать кандидату fallback-ответ,
    чтобы сообщение никогда не осталось без реакции.
    """
    updated_history = list(history)
    url = GENERATE_URL.format(model=GEMINI_MODEL)

    while True:
        contents = _history_to_contents(updated_history)

        response = _request_with_retry(
            requests.post,
            url,
            params={"key": GEMINI_API_KEY},
            headers={"Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": build_system_prompt(candidate_profile)}]},
                "contents": contents,
                "tools": [{"functionDeclarations": FUNCTIONS}],
            },
            timeout=60,
            proxies=PROXY_KWARGS,
        )
        data = response.json()
        parts = data["candidates"][0].get("content", {}).get("parts", [])

        function_calls = [p["functionCall"] for p in parts if "functionCall" in p]

        if not function_calls:
            reply_text = "".join(p.get("text", "") for p in parts)
            updated_history.append({
                "type": "model_output",
                "content": [{"type": "text", "text": reply_text}],
            })
            return reply_text, updated_history

        for call in function_calls:
            arguments = call.get("args") or {}
            updated_history.append({
                "type": "function_call",
                "name": call["name"],
                "arguments": arguments,
            })

            result_text = tool_executor(call["name"], arguments)
            updated_history.append({"type": "function_result", "result": result_text, "name": call["name"]})
