from google import genai

from config import GEMINI_API_KEY, GEMINI_MODEL
from system_prompt import build_system_prompt

client = genai.Client(api_key=GEMINI_API_KEY)

TOOLS = [
    {
        "type": "function",
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
        "type": "function",
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


def _text_from_steps(steps) -> str:
    """Собирает финальный текст ответа модели из шагов взаимодействия."""
    chunks = []
    for step in steps:
        if getattr(step, "type", None) == "model_output":
            for block in step.content or []:
                if getattr(block, "type", None) == "text":
                    chunks.append(block.text)
    return "".join(chunks)


def run_turn(history: list, tool_executor) -> tuple[str, list]:
    """
    Прогоняет один ход диалога: вызывает Gemini через Interactions API,
    при необходимости выполняет tool-calls через tool_executor(name, input) -> str,
    и возвращает (финальный текст для кандидата, обновлённая история шагов).
    """
    messages = list(history)

    while True:
        interaction = client.interactions.create(
            model=GEMINI_MODEL,
            system_instruction=build_system_prompt(),
            input=messages,
            tools=TOOLS,
            store=False,
        )

        for step in interaction.steps:
            messages.append(step.model_dump())

        function_calls = [s for s in interaction.steps if s.type == "function_call"]

        if not function_calls:
            return _text_from_steps(interaction.steps), messages

        for fc in function_calls:
            result_text = tool_executor(fc.name, dict(fc.arguments))
            messages.append({
                "type": "function_result",
                "name": fc.name,
                "call_id": fc.id,
                "result": [{"type": "text", "text": result_text}],
            })
