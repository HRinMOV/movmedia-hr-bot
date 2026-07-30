import google.generativeai as genai

from config import GEMINI_API_KEY, GEMINI_MODEL
from system_prompt import build_system_prompt

genai.configure(api_key=GEMINI_API_KEY)

TOOLS = [
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


def _to_gemini_role(role: str) -> str:
    return "model" if role == "assistant" else "user"


def _history_to_contents(history: list) -> list:
    """Приводит внутреннюю историю диалога к формату contents для Gemini API."""
    contents = []
    for message in history:
        contents.append({
            "role": _to_gemini_role(message["role"]),
            "parts": message["content"],
        })
    return contents


def _part_to_content(part) -> dict:
    if getattr(part, "text", None):
        return {"text": part.text}
    if getattr(part, "function_call", None):
        return {
            "function_call": {
                "name": part.function_call.name,
                "args": dict(part.function_call.args),
            }
        }
    return {"text": ""}


def run_turn(history: list, tool_executor) -> tuple[str, list]:
    """
    Прогоняет один ход диалога: вызывает Gemini, при необходимости выполняет
    tool-calls через tool_executor(name, input) -> str, и возвращает
    (финальный текст для кандидата, обновлённая история сообщений).
    """
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        system_instruction=build_system_prompt(),
        tools=[{"function_declarations": TOOLS}],
    )

    messages = list(history)

    while True:
        response = model.generate_content(_history_to_contents(messages))
        parts = response.candidates[0].content.parts

        messages.append({
            "role": "assistant",
            "content": [_part_to_content(part) for part in parts],
        })

        function_calls = [part.function_call for part in parts if getattr(part, "function_call", None)]

        if not function_calls:
            final_text = "".join(part.text for part in parts if getattr(part, "text", None))
            return final_text, messages

        tool_response_parts = []
        for part in parts:
            if getattr(part, "function_call", None):
                fc = part.function_call
                result_text = tool_executor(fc.name, dict(fc.args))
                tool_response_parts.append({
                    "function_response": {
                        "name": fc.name,
                        "response": {"content": result_text},
                    }
                })

        messages.append({"role": "user", "content": tool_response_parts})
