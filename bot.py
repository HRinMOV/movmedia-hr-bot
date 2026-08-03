import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

import storage
from gigachat_client import GigaChatError, run_turn
from system_prompt import known_test_task_links
from config import (
    BOT_TOKEN,
    RECRUITER_CHAT_ID,
    PROXY_URL,
    HIGH_LOAD_CONCURRENCY_THRESHOLD,
    SILENT_CANDIDATE_HOURS,
    SILENT_CHECK_INTERVAL_SECONDS,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hr_bot")
router = Router()

bot: Bot | None = None  # инициализируется в main(), используется в tool_executor

FALLBACK_MESSAGE = (
    "Прошу прощения, у меня сейчас небольшие технические сложности с обработкой "
    "сообщения. Я уже сообщил об этом рекрутеру, и мы вам ответим напрямую в "
    "ближайшее время. Можно также попробовать написать ещё раз чуть позже — "
    "обычно всё быстро восстанавливается. Если появятся вопросы — пишите, я на связи."
)

HIGH_LOAD_MESSAGE = (
    "Сейчас чуть больше обращений, чем обычно, поэтому отвечаю немного дольше — "
    "спасибо за терпение, я уже готовлю ответ!"
)

TYPING_REFRESH_SECONDS = 4  # Telegram показывает статус "печатает..." около 5 секунд, обновляем чуть чаще

_active_ai_requests = 0
_active_ai_requests_lock = asyncio.Lock()

# Стадии, которые кандидат чаще всего проходит вне этого диалога и которые
# рекрутер выставляет вручную командой /status. Для каждой — короткая база
# шаблона (подтверждение + что дальше); срок и приглашение задать вопрос
# добавляются автоматически в build_status_message.
STATUS_MESSAGES = {
    "applied": "Ваша заявка принята в работу. Рекрутер Алина рассмотрит анкету — обычно это занимает 1-2 рабочих дня.",
    "screening": "Ваша анкета сейчас на проверке у рекрутера. Как только будет решение о следующем шаге — обязательно сообщим, обычно в течение нескольких рабочих дней.",
    "test_sent": "Вам отправлено тестовое задание. Не спешите — если по ходу возникнут вопросы, смело пишите прямо здесь.",
    "test_received": "Спасибо! Мы получили ваше тестовое задание и передали его на проверку. Обычно это занимает несколько рабочих дней — как только будет решение, сразу сообщим.",
    "interview_scheduled": "Для вас запланировано интервью.",
    "interview_completed": "Спасибо, что прошли интервью! Сейчас команда обсуждает решение по следующему шагу, обычно это занимает несколько рабочих дней — обязательно дадим знать.",
    "review": "Ваша анкета и результаты сейчас на финальном рассмотрении у команды. Сообщим о решении в ближайшее время.",
    "offer": "У нас отличные новости по вашему отклику!",
    "rejected": "Спасибо большое за уделённое время и интерес к MOVmedia.",
    "withdrawn": "Спасибо, что сообщили. Будем рады видеть вас среди кандидатов в будущем.",
}


def build_status_message(stage: str, extra: str | None) -> str | None:
    base = STATUS_MESSAGES.get(stage)
    if not base:
        return None
    parts = [base]
    if extra:
        parts.append(extra)
    parts.append("Если появятся вопросы — обязательно пишите, всегда на связи!")
    return " ".join(parts)


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
                logger.warning("RECRUITER_CHAT_ID не задан")
            if reason == "unknown_question":
                storage.add_previous_question(chat_id, message)
            logger.info("notify_recruiter chat_id=%s reason=%s", chat_id, reason)
            return "Уведомление отправлено рекрутеру."

        if name == "update_candidate_stage":
            stage = tool_input.get("stage")
            storage.update_stage(chat_id, stage)
            logger.info("update_candidate_stage chat_id=%s stage=%s", chat_id, stage)
            return f"Стадия обновлена: {stage}"

        if name == "update_candidate_profile":
            fields = {k: v for k, v in tool_input.items() if v}
            storage.update_profile(chat_id, **fields)
            logger.info("update_candidate_profile chat_id=%s fields=%s", chat_id, list(fields))
            return "Профиль кандидата обновлён."

        logger.warning("Неизвестный инструмент вызван моделью: %s", name)
        return "Неизвестный инструмент"

    return tool_executor


async def _keep_typing(chat_id: int) -> None:
    """Периодически отправляет статус "печатает...", пока бот готовит ответ через AI."""
    try:
        while True:
            await bot.send_chat_action(chat_id, "typing")
            await asyncio.sleep(TYPING_REFRESH_SECONDS)
    except asyncio.CancelledError:
        pass


async def run_and_reply(message: Message, candidate: dict, history: list, executor) -> None:
    """Обращается к AI и гарантирует, что кандидат получит ответ.

    Повторные попытки уже сделаны внутри run_turn (gigachat_client). Если
    все попытки исчерпаны — не оставляем кандидата в тишине, а отвечаем
    вежливым fallback-сообщением и уведомляем рекрутера о сбое.

    Пока готовится ответ, кандидату показывается статус "печатает...", а при
    повышенной нагрузке (много одновременных обращений к AI) — предупреждение,
    что ответ может занять больше времени."""
    global _active_ai_requests
    chat_id = message.chat.id
    profile = {
        "name": candidate.get("name"),
        "vacancy": candidate.get("vacancy"),
        "interview_date": candidate.get("interview_date"),
        "salary_expectations": candidate.get("salary_expectations"),
    }

    async with _active_ai_requests_lock:
        _active_ai_requests += 1
        current_load = _active_ai_requests

    if current_load > HIGH_LOAD_CONCURRENCY_THRESHOLD:
        logger.info(
            "Повышенная нагрузка (%s одновременных запросов к AI), предупреждаю кандидата chat_id=%s",
            current_load, chat_id,
        )
        await message.answer(HIGH_LOAD_MESSAGE)

    typing_task = asyncio.create_task(_keep_typing(chat_id))
    try:
        logger.info("Отправка запроса в GigaChat chat_id=%s", chat_id)
        reply_text, updated_history = await asyncio.to_thread(run_turn, history, executor, profile)
    except GigaChatError:
        logger.error("AI недоступен после всех попыток, использую fallback chat_id=%s", chat_id)
        storage.save_history(chat_id, history)
        if RECRUITER_CHAT_ID:
            who = f"@{candidate['username']}" if candidate.get("username") else f"id {chat_id}"
            try:
                await bot.send_message(
                    RECRUITER_CHAT_ID,
                    f"⚠️ AI недоступен, кандидату {who} отправлен fallback-ответ. Загляните в диалог вручную.",
                )
            except Exception:
                logger.exception("Не удалось уведомить рекрутера о сбое AI chat_id=%s", chat_id)
        await message.answer(FALLBACK_MESSAGE)
        return
    finally:
        typing_task.cancel()
        async with _active_ai_requests_lock:
            _active_ai_requests -= 1

    storage.save_history(chat_id, updated_history)
    for link in known_test_task_links():
        if link in reply_text:
            storage.add_sent_link(chat_id, link)
    await message.answer(reply_text)


@router.message(CommandStart())
async def cmd_start(message: Message):
    username = message.from_user.username
    candidate = storage.get_or_create(message.chat.id, username)
    history = candidate["history"]

    logger.info("Входящее сообщение (/start) chat_id=%s", message.chat.id)
    history.append({"type": "user_input", "content": [{"type": "text", "text": "Кандидат запустил диалог (/start)."}]})
    executor = make_tool_executor(message.chat.id, username)
    await run_and_reply(message, candidate, history, executor)


@router.message(Command("status"))
async def cmd_status(message: Message):
    """Команда рекрутера: /status <chat_id> <stage> [комментарий].

    Позволяет вручную перевести кандидата на стадию, которая происходит вне
    этого чата (screening, interview_scheduled, interview_completed, review,
    offer, rejected), и сразу отправляет кандидату шаблонное уведомление —
    подтверждение, что дальше и приглашение задать вопросы."""
    if not RECRUITER_CHAT_ID or str(message.chat.id) != str(RECRUITER_CHAT_ID):
        return

    parts = (message.text or "").split(maxsplit=3)
    if len(parts) < 3:
        await message.answer(
            "Формат: /status <chat_id> <stage> [комментарий]\n"
            f"Доступные stage: {', '.join(STATUS_MESSAGES)}"
        )
        return

    _, target_chat_id_raw, stage, *rest = parts
    extra = rest[0] if rest else None

    if stage not in STATUS_MESSAGES:
        await message.answer(f"Неизвестный stage '{stage}'. Доступные: {', '.join(STATUS_MESSAGES)}")
        return

    try:
        target_chat_id = int(target_chat_id_raw)
    except ValueError:
        await message.answer("chat_id должен быть числом.")
        return

    storage.update_stage(target_chat_id, stage)
    logger.info("Рекрутер вручную перевёл chat_id=%s на стадию %s", target_chat_id, stage)
    text = build_status_message(stage, extra)
    try:
        await bot.send_message(target_chat_id, text)
    except Exception:
        logger.exception("Не удалось отправить статус кандидату chat_id=%s", target_chat_id)
        await message.answer("Стадия обновлена в базе, но отправить сообщение кандидату не удалось (см. логи).")
        return

    await message.answer(f"Готово: кандидату {target_chat_id} отправлено уведомление о стадии '{stage}'.")


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
            storage.add_uploaded_file(message.chat.id, message.document.file_id)
        elif message.photo:
            await bot.send_photo(
                RECRUITER_CHAT_ID, message.photo[-1].file_id,
                caption=f"Файл от кандидата {who}",
            )
            storage.add_uploaded_file(message.chat.id, message.photo[-1].file_id)

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

    logger.info("Входящее сообщение chat_id=%s: %s", message.chat.id, user_text[:200])
    history.append({"type": "user_input", "content": [{"type": "text", "text": user_text}]})

    executor = make_tool_executor(message.chat.id, username)
    await run_and_reply(message, candidate, history, executor)


async def check_silent_candidates():
    """Фоновая задача: раз в SILENT_CHECK_INTERVAL_SECONDS ищет кандидатов,
    которым выдали тестовое, но они не выходят на связь дольше порога."""
    while True:
        await asyncio.sleep(SILENT_CHECK_INTERVAL_SECONDS)
        silent = storage.find_silent_candidates(
            stage="test_sent",
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


dp = Dispatcher()
dp.include_router(router)


async def main():
    global bot
    session = AiohttpSession(timeout=60, proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)

    asyncio.create_task(check_silent_candidates())
    await dp.start_polling(bot, handle_signals=False)


async def run_forever():
    # Держит бота в одном event loop; retry делаем через while,
    # а не через новый asyncio.run(), чтобы не плодить event loop-ы.
    while True:
        try:
            await main()
        except Exception:
            logging.exception("Бот упал с ошибкой, перезапуск через 5 секунд...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(run_forever())
