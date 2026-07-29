import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

import storage
from claude_client import run_turn
from config import (
    BOT_TOKEN,
    RECRUITER_CHAT_ID,
    SILENT_CANDIDATE_HOURS,
    SILENT_CHECK_INTERVAL_SECONDS,
)

logging.basicConfig(level=logging.INFO)
router = Router()

bot: Bot | None = None  # инициализируется в main(), используется в tool_executor


def make_tool_executor(chat_id: int, username: str | None):
    """Замыкание, чтобы tool-executor знал, какому кандидату он служит."""

    def tool_executor(name: str, tool_input: dict) -> str:
        if name == "notify_recruiter":
            reason = tool_input.get("reason", "info")
            message = tool_input.get("message", "")
            who = f"@{username}" if username else f"id {chat_id}"
            text = f"🔔 <b>{reason}</b> — кандидат {who}\n\n{message}"
            if RECRUITER_CHAT_ID:
                asyncio.create_task(
                    bot.send_message(RECRUITER_CHAT_ID, text, parse_mode="HTML")
                )
            else:
                logging.warning("RECRUITER_CHAT_ID не задан")
            return "Уведомление отправлено рекрутеру."

        if name == "update_candidate_stage":
            stage = tool_input.get("stage")
            storage.update_stage(chat_id, stage)
            return f"Стадия обновлена: {stage}"

        return "Неизвестный инструмент"

    return tool_executor


@router.message(CommandStart())
async def cmd_start(message: Message):
    username = message.from_user.username
    candidate = storage.get_or_create(message.chat.id, username)
    history = candidate["history"]

    history.append({"role": "user", "content": "Кандидат запустил диалог (/start)."})
    executor = make_tool_executor(message.chat.id, username)
    reply_text, updated_history = run_turn(history, executor)

    storage.save_history(message.chat.id, updated_history)
    await message.answer(reply_text)


@router.message(F.document | F.photo)
async def handle_file(message: Message):
    """Файлы (резюме, тестовое) сразу дублируются рекрутеру напрямую, в обход модели."""
    username = message.from_user.username
    who = f"@{username}" if username else f"id {message.chat.id}"

    if RECRUITER_CHAT_ID:
        if message.document:
            await bot.send_document(
                RECRUITER_CHAT_ID, message.document.file_id,
                caption=f"Файл от кандидата {who}",
            )
        elif message.photo:
            await bot.send_photo(
                RECRUITER_CHAT_ID, message.photo[-1].file_id,
                caption=f"Файл от кандидата {who}",
            )

    # Модель тоже должна знать, что файл получен, чтобы отреагировать текстом
    marker = "[Кандидат прислал файл — резюме или выполненное тестовое задание.]"
    if message.caption:
        marker += f" Подпись к файлу: {message.caption}"
    await process_text_turn(message, username, marker)


@router.message(F.text)
async def handle_text(message: Message):
    await process_text_turn(message, message.from_user.username, message.text)


async def process_text_turn(message: Message, username: str | None, user_text: str):
    candidate = storage.get_or_create(message.chat.id, username)
    history = candidate["history"]
    history.append({"role": "user", "content": user_text})

    executor = make_tool_executor(message.chat.id, username)
    reply_text, updated_history = run_turn(history, executor)

    storage.save_history(message.chat.id, updated_history)
    await message.answer(reply_text)


async def check_silent_candidates():
    """Фоновая задача: раз в SILENT_CHECK_INTERVAL_SECONDS ищет кандидатов,
    которым выдали тестовое, но они не выходят на связь дольше порога."""
    while True:
        await asyncio.sleep(SILENT_CHECK_INTERVAL_SECONDS)
        silent = storage.find_silent_candidates(
            stage="test_task_sent",
            older_than_seconds=SILENT_CANDIDATE_HOURS * 3600,
        )
        for candidate in silent:
            who = f"@{candidate['username']}" if candidate["username"] else f"id {candidate['chat_id']}"
            if RECRUITER_CHAT_ID:
                await bot.send_message(
                    RECRUITER_CHAT_ID,
                    f"⏰ Кандидат {who} получил тестовое задание более "
                    f"{SILENT_CANDIDATE_HOURS} ч. назад и до сих пор не прислал результат. "
                    f"Решение о напоминании — на ваше усмотрение.",
                )
            storage.mark_flagged_silent(candidate["chat_id"])


async def main():
    global bot
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(check_silent_candidates())
    await dp.start_polling(bot, handle_signals=False)


if __name__ == "__main__":
    asyncio.run(main())
