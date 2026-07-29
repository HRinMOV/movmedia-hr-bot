import anthropic

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL
from system_prompt import build_system_prompt

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

TOOLS = [
    {
        "name": "notify_recruiter",
        "description": (
            "Отправить рекрутеру Алине сообщение о кандидате. Используй это, когда: "
            "(a) кандидат прислал всё необходимое (вакансия, имя, резюме, телефон, "
            "тестовое если применимо) — пришли полную сводку по кандидату; "
            "(b) кандидат задал вопрос, точного ответа на который нет в базе знаний — "
            "передай вопрос дословно; "
            "(c) кандидат уже второй раз и более просит связать напрямую с рекрутером; "
            "(d) кандидат явно сообщил, что передумал проходить отбор."
        ),
        "input_schema": {
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
        "input_schema": {
            "type": "object",
            "properties": {
                "stage": {
                    "type": "string",
                    "enum": ["test_task_sent", "test_task_submitted", "withdrawn"],
                }
            },
            "required": ["stage"],
        },
    },
]


def run_turn(history: list, tool_executor) -> tuple[str, list]:
    """
    Прогоняет один ход диалога: вызывает Claude, при необходимости выполняет
    tool-calls через tool_executor(name, input) -> str, и возвращает
    (финальный текст для кандидата, обновлённая история сообщений).
    """
    system_prompt = build_system_prompt()
    messages = list(history)

    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            tools=TOOLS,
            messages=messages,
        )

        assistant_content = [block.model_dump() for block in response.content]
        messages.append({"role": "assistant", "content": assistant_content})

        if response.stop_reason != "tool_use":
            final_text = "".join(
                block.text for block in response.content if block.type == "text"
            )
            return final_text, messages

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result_text = tool_executor(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )
        messages.append({"role": "user", "content": tool_results})
