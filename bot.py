import asyncio
import logging

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaDocument, InputMediaPhoto, Message
from aiogram.exceptions import TelegramBadRequest

import storage
import system_prompt
import notion_service
from gigachat_client import GigaChatError, run_turn
from system_prompt import known_test_task_links
from config import (
    BOT_TOKEN,
    RECRUITER_CHAT_ID,
    PROXY_URL,
    HIGH_LOAD_CONCURRENCY_THRESHOLD,
    SILENT_CANDIDATE_HOURS,
    SILENT_CHECK_INTERVAL_SECONDS,
    DIALOG_IDLE_MINUTES,
    DIALOG_IDLE_CHECK_INTERVAL_SECONDS,
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

# Главное меню и статичные тексты (раздел «Хочу в команду» / «О MOVmedia»)
MAIN_MENU_TEXT = (
    "👋 Привет!\n\n"
    "Я — виртуальный ассистент команды рекрутинга MOVmedia.\n\n"
    "Помогу узнать больше о нашей компании, посмотреть открытые вакансии или "
    "оставить информацию о себе.\n\n"
    "Чем могу помочь?"
)

NO_VACANCIES_TEXT = (
    "Сейчас такой открытой вакансии нет.\n\n"
    "Но если вам интересна MOVmedia и вы хотели бы работать у нас в будущем, "
    "отправьте свои данные. Мы сохраним их в базе кандидатов и свяжемся, если "
    "появится подходящая возможность."
)

RESERVE_INTRO_TEXT = (
    "Нам очень приятно, что наша команда привлекла ваше внимание 🩵\n\n"
    "Если сейчас нет подходящей вакансии, вы можете оставить информацию о себе. "
    "Мы сохраним её в базе кандидатов и свяжемся, если появится подходящая "
    "возможность."
)

RESERVE_DONE_TEXT = (
    "Спасибо!\n\n"
    "Я передал ваши данные в базу кандидатов MOVmedia.\n\n"
    "Если появится подходящая возможность, рекрутер обязательно свяжется с вами."
)

FEEDBACK_PROMPT_TEXT = (
    "Если при общении со мной возникли сложности или что-то пошло не так — "
    "расскажите об этом одним сообщением. Я передам его команде напрямую, и "
    "мы постараемся это устранить."
)

FEEDBACK_DONE_TEXT = (
    "Спасибо, что рассказали! Я передал обратную связь команде — обязательно "
    "разберёмся.\n\nЕсли будут ещё вопросы по вакансиям или компании — я на связи."
)

CANDIDATE_REMINDER_TEXT = (
    "Привет! Хочу узнать, как продвигается выполнение тестового задания 🙂\n\n"
    "Если нужно ещё немного времени — не проблема, просто дайте знать. Если по "
    "какой-то причине задание уже не актуально для вас — тоже сообщите, буду "
    "благодарен за обратную связь."
)

# Короткие детерминированные ответы для интерактивного подменю «О MOVmedia».
# Не связаны напрямую с company.md: здесь нет служебных полей (имя ассистента,
# сайт, рекрутер, зарплатная политика, дата основания) — только то, что можно
# показывать кандидату в диалоге.
ABOUT_MENU_TEXT = (
    "🩵 О MOVmedia\n\n"
    "Здесь вы можете узнать больше о нашей компании, культуре и подходе к работе.\n\n"
    "Что вас интересует?"
)

ABOUT_COMPANY_TEXT = (
    "MOVmedia — студия информационного дизайна, которая помогает крупным "
    "компаниям превращать сложную информацию в понятные и визуально сильные "
    "решения.\n\n"
    "Мы создаём бизнес-презентации, инфографику, видеоролики, интерактивные "
    "проекты и другие коммуникационные материалы для крупного бизнеса.\n\n"
    "Среди наших клиентов — Газпром, Сбер, РЖД, X5 Group, Магнит и другие "
    "крупные компании.\n\n"
    "Если хотите, могу рассказать о нашей культуре или показать открытые вакансии."
)

ABOUT_CULTURE_TEXT = (
    "В MOVmedia мы ценим ответственность, инициативность, осознанность и "
    "стремление к развитию.\n\n"
    "Мы строим отношения внутри команды на доверии, партнёрстве и взаимной "
    "ответственности.\n\n"
    "Нам близок подход win-win — когда сотрудники помогают развивать компанию, "
    "а компания создаёт условия для профессионального роста сотрудников.\n\n"
    "Хотите узнать, какой человек обычно чувствует себя комфортно в нашей команде?"
)

ABOUT_WHY_TEXT = (
    "MOVmedia может быть интересна тем, кто хочет работать над сложными "
    "визуальными задачами для крупного бизнеса и видеть реальный результат "
    "своей работы.\n\n"
    "Мы создаём проекты для крупных компаний, помогаем превращать сложную "
    "информацию в понятные и сильные визуальные решения.\n\n"
    "Нам важны самостоятельность, инициативность и желание развиваться. Мы "
    "строим работу на доверии, партнёрстве и ответственности за общий "
    "результат.\n\n"
    "Если хотите, могу подробнее рассказать о наших проектах или культуре команды."
)

ABOUT_TEAM_TEXT = (
    "Мы придерживаемся партнёрского стиля взаимодействия.\n\n"
    "Руководители помогают сотрудникам принимать решения самостоятельно, а не "
    "решают задачи за них.\n\n"
    "Мы поддерживаем инициативу, открытый диалог и конструктивную обратную связь.\n\n"
    "Нам важно, чтобы каждый понимал влияние своей работы на общий результат команды."
)

ABOUT_SECTION_TEXTS = {
    "company": ABOUT_COMPANY_TEXT,
    "culture": ABOUT_CULTURE_TEXT,
    "why": ABOUT_WHY_TEXT,
    "team": ABOUT_TEAM_TEXT,
}

# Ключевые слова для автоматического открытия раздела по свободному тексту
# кандидата, без показа всего меню (например: «какая у вас культура?»).
_ABOUT_SECTION_KEYWORDS = {
    "culture": ["культур", "ценност"],
    "why": ["почему выбирают", "почему выбрать", "почему стоит", "чем вы лучше", "почему movmedia", "почему у вас", "почему мне идти", "преимущества компании", "плюсы работы"],
    "team": ["работа в команде", "стиль управления", "взаимодействи", "руководител", "команда movmedia"],
    "company": ["чем занимается", "чем занимаетесь", "о компании", "про компанию", "что за компания", "расскажи о movmedia", "расскажите о movmedia", "какие у вас клиенты", "какая у вас студия"],
}


def detect_about_section(text: str) -> str | None:
    """Определяет, какой раздел подменю «О MOVmedia» соответствует свободному
    тексту кандидата (например, «какая у вас культура?»), чтобы открыть его
    сразу, не показывая кандидату всё меню целиком."""
    lowered = text.lower()
    for section, keywords in _ABOUT_SECTION_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                return section
    return None

_ABOUT_AFFIRMATIVE_WORDS = {
    "да", "ага", "угу", "конечно", "давай", "давайте", "хочу", "интересно",
    "расскажи", "расскажите", "продолжай", "продолжить", "ок", "окей",
    "хорошо", "yes",
}


def _is_affirmative_reply(text: str) -> bool:
    """Проверяет, что кандидат коротко подтвердил предыдущий вопрос бота
    (например, «да» на «Хотите узнать...?»), а не задал новый вопрос —
    чтобы не спутать подтверждение со свободным вопросом кандидата."""
    normalized = text.strip(" .!?\n").lower()
    return normalized in _ABOUT_AFFIRMATIVE_WORDS


# Если кандидат коротко подтверждает завершающий вопрос раздела подменю
# «О MOVmedia» (например, ABOUT_CULTURE_TEXT заканчивается вопросом «Хотите
# узнать, какой человек обычно чувствует себя комфортно в нашей команде?»),
# показываем именно следующий по смыслу раздел, а не общий обзор компании и
# не отдаём ответ модели «от себя» — модель не отслеживает, на какой именно
# вопрос отвечает кандидат.
_ABOUT_NEXT_SECTION_ON_CONFIRM = {
    "company": "culture",
    "culture": "team",
    "why": "culture",
}


def _next_about_section_after_confirmation(history: list) -> str | None:
    """Смотрит на последнее сообщение бота в истории: если это был текст
    одного из разделов «О MOVmedia» с завершающим вопросом — возвращает
    следующий логичный раздел для короткого подтверждающего ответа."""
    for entry in reversed(history):
        if entry.get("type") != "model_output":
            continue
        content = entry.get("content") or []
        last_text = content[0].get("text") if content else None
        for section_key, section_text in ABOUT_SECTION_TEXTS.items():
            if last_text == section_text:
                return _ABOUT_NEXT_SECTION_ON_CONFIRM.get(section_key)
        return None
    return None


# Ключевые слова для приоритетного безопасного сценария по чувствительным темам
# условий сотрудничества (отпуска, больничные, льготы/компенсации,
# юридические условия). См. ТЗ «Обработка вопросов об условиях
# сотрудничества» — такие вопросы никогда не должны доходить до модели
# или поиска в базе знаний — детали обсуждаются только с рекрутером.
_SENSITIVE_TOPIC_KEYWORDS = {
    "vacation": ["отпуск", "дни отдыха", "сколько дней отдыха", "когда можно уйти в отпуск", "оплачиваемый отпуск"],
    "sick_leave": ["больничный", "больничные", "заболел", "оплата больничного", "оформление больничного"],
    "benefits": ["дмс", "страховк", "льгот", "компенсац", "бонус", "выплат"],
    "legal": ["договор", "оформл", "налог", "официальное трудоустройство", "статус сотрудника"],
}

SENSITIVE_TOPICS_TEXT = (
    "Такие детали обычно обсуждаются непосредственно с рекрутером на следующих этапах собеседования, чтобы вы получили актуальную информацию именно по вашей ситуации.\n\n"
    "Я могу передать ваш вопрос рекрутеру или помочь узнать больше о компании и открытых вакансиях."
)

LEGAL_TOPIC_TEXT = (
        "На старте сотрудничество с MOVmedia оформляется по договору ГПХ. Остальные детали можно будет обсудить на следующих этапах.\n\n"
        "Могу передать ваш вопрос рекрутеру, если хотите уточнить что-то конкретное."
)


def detect_sensitive_topic(text: str) -> str | None:
    """Возвращает категорию чувствительной темы об условиях сотрудничества
    (отпуска, больничные, льготы, юридические условия) в свободном тексте
    кандидата, либо None, если тема не чувствительная. Вызывается в handle_text
    до detect_about_section и до обращения к модели/базе знаний."""
    lowered = text.lower()
    for topic, keywords in _SENSITIVE_TOPIC_KEYWORDS.items():
        for keyword in keywords:
            if keyword in lowered:
                return topic
    return None


TYPING_REFRESH_SECONDS = 4  # Telegram показывает статус "печатает..." около 5 секунд, обновляем чуть чаще

_active_ai_requests = 0
_active_ai_requests_lock = asyncio.Lock()
_main_loop = None

_chat_locks: dict[int, asyncio.Lock] = {}


def _get_chat_lock(chat_id: int) -> asyncio.Lock:
    """Гарантирует, что сообщения одного кандидата обрабатываются строго по
    очереди, а не параллельно. Без этого несколько быстрых сообщений подряд
    от одного кандидата уходят в AI одновременно, и ответы/сохранение профиля
    могут выполниться в непредсказуемом порядке (см. баг с перепутанными
    сообщениями и карточкой Notion без имени кандидата)."""
    lock = _chat_locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _chat_locks[chat_id] = lock
    return lock


class ReserveForm(StatesGroup):
    """FSM сценария «Хочу в команду MOVmedia»: последовательный сбор данных
    кандидата для кадрового резерва. Имя, роль и портфолио — обязательны,
    остальные поля можно пропустить кнопкой."""
    name = State()
    role = State()
    portfolio = State()
    resume = State()
    salary = State()
    about = State()


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🩵 О MOVmedia", callback_data="menu:about")],
        [InlineKeyboardButton(text="💼 Открытые вакансии", callback_data="menu:vacancies")],
        [InlineKeyboardButton(text="🚀 Хочу в команду MOVmedia", callback_data="menu:join")],
        [InlineKeyboardButton(text="💬 Обратная связь о работе бота", callback_data="menu:feedback")],
    ])


def back_to_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:root")],
    ])


def vacancies_list_keyboard(names: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name, callback_data=f"vacancy:{name}")] for name in names]
    rows.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vacancy_card_keyboard(name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Откликнуться", callback_data=f"apply:{name}")],
        [InlineKeyboardButton(text="⬅️ К списку вакансий", callback_data="menu:vacancies")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:root")],
    ])


def join_team_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Хочу в команду MOVmedia", callback_data="menu:join")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:root")],
    ])


def skip_keyboard(field: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data=f"reserve_skip:{field}")],
        [InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="menu:root")],
    ])


def about_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏢 О компании", callback_data="about:company")],
        [InlineKeyboardButton(text="🤝 Культура и ценности", callback_data="about:culture")],
        [InlineKeyboardButton(text="🚀 Почему выбирают MOVmedia", callback_data="about:why")],
        [InlineKeyboardButton(text="👥 Работа в команде", callback_data="about:team")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:root")],
    ])


def about_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:about")],
    ])


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


async def _send_recruiter_update(chat_id: int, text: str, parse_mode: str | None = None) -> None:
    """Отправляет рекрутеру одно сообщение с текстом и всеми файлами, которые
    кандидат успел прислать (резюме, тестовое) - без отдельных дублирующих
    сообщений "Файл от кандидата" сразу при получении. Вся дальнейшая работа
    по кандидату рекрутер ведёт в карточке Notion, поэтому в Telegram нужно
    одно консолидированное уведомление, а не несколько частями."""
    if not RECRUITER_CHAT_ID:
        return
    candidate = storage.get_or_create(chat_id, None)
    files = candidate.get("uploaded_files") or []

    def _file_id(item):
        return item.get("file_id") if isinstance(item, dict) else item

    def _file_type(item):
        return item.get("type", "document") if isinstance(item, dict) else "document"

    if not files:
        await bot.send_message(RECRUITER_CHAT_ID, text, parse_mode=parse_mode)
        return

    files = files[:10]  # ограничение Telegram на размер альбома
    if len(files) == 1:
        item = files[0]
        if _file_type(item) == "photo":
            await bot.send_photo(RECRUITER_CHAT_ID, _file_id(item), caption=text, parse_mode=parse_mode)
        else:
            await bot.send_document(RECRUITER_CHAT_ID, _file_id(item), caption=text, parse_mode=parse_mode)
        return

    media = []
    for i, item in enumerate(files):
        caption = text if i == 0 else None
        item_parse_mode = parse_mode if i == 0 else None
        if _file_type(item) == "photo":
            media.append(InputMediaPhoto(media=_file_id(item), caption=caption, parse_mode=item_parse_mode))
        else:
            media.append(InputMediaDocument(media=_file_id(item), caption=caption, parse_mode=item_parse_mode))
    await bot.send_media_group(RECRUITER_CHAT_ID, media)


def make_tool_executor(chat_id: int, username: str | None):
    """Замыкание, чтобы tool-executor знал, какому кандидату он служит."""

    def tool_executor(name: str, tool_input: dict) -> str:
        if name == "notify_recruiter":
            reason = tool_input.get("reason", "info")
            message = tool_input.get("message", "")
            who = f"@{username}" if username else f"id {chat_id}"
            notion_line = ""
            if reason == "candidate_summary":
                # Карточка в Notion создаётся только для сценария "Открытые
                # вакансии" (есть выбранная вакансия, это не кадровый резерв)
                # и только в момент, когда AI прислал итоговую сводку по
                # кандидату — не раньше. Название вакансии ни на что не
                # влияет, кроме того, что оно записывается в карточку.
                fresh_candidate = storage.get_or_create(chat_id, username)
                if fresh_candidate.get("vacancy") and not fresh_candidate.get("is_reserve"):
                    try:
                        card = notion_service.create_or_update_candidate_card(
                            fresh_candidate, summary_message=message,
                        )
                    except Exception:
                        logger.exception("Ошибка при создании/обновлении карточки Notion chat_id=%s", chat_id)
                        card = None
                    if card and card.get("url"):
                        notion_line = f"\n\n📇 Карточка в Notion: {card['url']}"
            text = f"🔔 <b>{reason}</b> — кандидат {who}\n\n{message}{notion_line}"
            if RECRUITER_CHAT_ID:
                if _main_loop is not None:
                    if reason == "candidate_summary":
                        coro = _send_recruiter_update(chat_id, text, parse_mode="HTML")
                    else:
                        coro = bot.send_message(RECRUITER_CHAT_ID, text, parse_mode="HTML")
                    asyncio.run_coroutine_threadsafe(
                        coro,
                        _main_loop,
                    )
                else:
                    logger.error("Main event loop is not set, cannot notify recruiter")
            else:
                logger.warning("RECRUITER_CHAT_ID не задан")
            if reason == "unknown_question":
                storage.add_previous_question(chat_id, message)
            storage.mark_summary_notified(chat_id)
            logger.info("notify_recruiter chat_id=%s reason=%s", chat_id, reason)
            return "Уведомление отправлено рекрутеру."

        if name == "update_candidate_stage":
            stage = tool_input.get("stage")
            storage.update_stage(chat_id, stage)
            if stage in ("test_sent", "test_received"):
                fresh_candidate = storage.get_or_create(chat_id, username)
                if fresh_candidate.get("vacancy") and not fresh_candidate.get("is_reserve"):
                    sent_links = fresh_candidate.get("sent_links") or []
                try:
                    notion_service.append_test_task_info(
                        chat_id,
                        fresh_candidate["vacancy"],
                        test_link=(sent_links[-1] if stage == "test_sent" and sent_links else None),
                        comment=("Кандидат прислал выполненное тестовое задание." if stage == "test_received" else None),
                    )
                except Exception:
                    logger.exception("Ошибка при обновлении тестового в Notion chat_id=%s", chat_id)
            logger.info("update_candidate_stage chat_id=%s stage=%s", chat_id, stage)
            return f"Стадия обновлена: {stage}"

        if name == "update_candidate_profile":
            fields = {k: v for k, v in tool_input.items() if v}
            # resume_link — параметр для модели; в памяти кандидата (и в
            # Notion-карточке) резюме хранится в поле resume_note.
            if "resume_link" in fields:
                fields["resume_note"] = fields.pop("resume_link")
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
            try:
                await bot.send_chat_action(chat_id, "typing")
            except asyncio.CancelledError:
                raise
            except Exception:
                # Telegram может временно ограничить частые запросы (flood control) -
                # это не критично, просто пропускаем один цикл обновления статуса.
                logger.debug("Не удалось отправить статус 'печатает' chat_id=%s", chat_id, exc_info=True)
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
    global _main_loop
    _main_loop = asyncio.get_running_loop()
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
    except Exception:
        logger.exception("Непредвиденная ошибка при обработке хода диалога chat_id=%s", chat_id)
        storage.save_history(chat_id, history)
        if RECRUITER_CHAT_ID:
            who = f"@{candidate['username']}" if candidate.get("username") else f"id {chat_id}"
            try:
                await bot.send_message(
                    RECRUITER_CHAT_ID,
                    f"⚠️ Внутренняя ошибка при обработке сообщения кандидата {who} (см. логи). Кандидату отправлен fallback-ответ.",
                )
            except Exception:
                logger.exception("Не удалось уведомить рекрутера о внутренней ошибке chat_id=%s", chat_id)
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
async def cmd_start(message: Message, state: FSMContext):
    """Показывает главное меню сразу, без обращения к модели — оно детерминировано
    и должно быть одинаковым для всех кандидатов (см. ТЗ, раздел «Главное меню»)."""
    await state.clear()
    username = message.from_user.username
    candidate = storage.get_or_create(message.chat.id, username)
    history = candidate["history"]

    logger.info("Входящее сообщение (/start) chat_id=%s", message.chat.id)
    history.append({"type": "user_input", "content": [{"type": "text", "text": "Кандидат запустил диалог (/start)."}]})
    history.append({"type": "model_output", "content": [{"type": "text", "text": MAIN_MENU_TEXT}]})
    storage.save_history(message.chat.id, history)
    await message.answer(MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())


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


async def safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Обновляет текущее сообщение кандидата новым текстом/клавиатурой, а если
    это невозможно — отправляет новое сообщение. Если Telegram отвечает, что
    текст не изменился (кандидат повторно открыл тот же пункт меню), просто
    ничего не переотправляем — иначе кандидат получил бы дублирующееся
    сообщение с той же информацией."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        await callback.message.answer(text, reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(text, reply_markup=reply_markup)


def _log_menu_shown(callback: CallbackQuery, text: str) -> None:
    """Записывает показанный кандидату текст меню/раздела в историю диалога.

    Без этого AI и логика короткого подтверждения (см.
    _next_about_section_after_confirmation) не видят, какой экран кандидат
    только что открыл кнопкой — цепочка ломалась именно тогда, когда раздел
    «О MOVmedia» открывали через инлайн-кнопки, а не свободным текстом."""
    username = callback.from_user.username
    candidate = storage.get_or_create(callback.message.chat.id, username)
    history = candidate["history"]
    history.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    storage.save_history(callback.message.chat.id, history)


# --- Главное меню и разделы (раздел «О MOVmedia» / «Открытые вакансии») ---

@router.callback_query(F.data == "menu:root")
async def cb_menu_root(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    _log_menu_shown(callback, MAIN_MENU_TEXT)
    await safe_edit_or_send(callback, MAIN_MENU_TEXT, reply_markup=main_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:about")
async def cb_menu_about(callback: CallbackQuery, state: FSMContext):
    """Показывает интерактивное подменю «О MOVmedia» с коротким приветствием —
    вместо вывода всей базы знаний целиком."""
    _log_menu_shown(callback, ABOUT_MENU_TEXT)
    await safe_edit_or_send(callback, ABOUT_MENU_TEXT, reply_markup=about_menu_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("about:"))
async def cb_about_section(callback: CallbackQuery, state: FSMContext):
    """Открывает один из 4 коротких разделов подменю «О MOVmedia» (см.
    ABOUT_SECTION_TEXTS) — короткий, готовый к чтению в Telegram ответ,
    без служебных полей и без дампа knowledge/company.md."""
    section = callback.data.split("about:", 1)[1]
    text = ABOUT_SECTION_TEXTS.get(section)
    if not text:
        await callback.answer()
        return
    _log_menu_shown(callback, text)
    await safe_edit_or_send(callback, text, reply_markup=about_section_keyboard())
    await callback.answer()


@router.callback_query(F.data == "menu:vacancies")
async def cb_menu_vacancies(callback: CallbackQuery, state: FSMContext):
    names = system_prompt.list_vacancy_names()
    if not names:
        text = NO_VACANCIES_TEXT
        markup = join_team_keyboard()
    else:
        text = "💼 Открытые вакансии:\n\nВыберите вакансию, чтобы узнать подробности."
        markup = vacancies_list_keyboard(names)
    await safe_edit_or_send(callback, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("vacancy:"))
async def cb_vacancy_card(callback: CallbackQuery, state: FSMContext):
    name = callback.data.split("vacancy:", 1)[1]
    section = system_prompt.get_vacancy_section(name)
    if not section:
        await callback.answer("Эта вакансия уже неактуальна, попробуйте посмотреть список ещё раз.", show_alert=True)
        return
    text = f"💼 {name}\n\n{section}"
    await safe_edit_or_send(callback, text, reply_markup=vacancy_card_keyboard(name))
    await callback.answer()


@router.callback_query(F.data.startswith("apply:"))
async def cb_apply_vacancy(callback: CallbackQuery, state: FSMContext):
    """Кандидат нажал «Откликнуться» на карточке вакансии — передаём диалог
    в уже проверенный AI-сценарий сбора анкеты/резюме/тестового (run_and_reply),
    не дублируя эту логику в коде меню."""
    name = callback.data.split("apply:", 1)[1]
    chat_id = callback.message.chat.id
    username = callback.from_user.username
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer()
    async with _get_chat_lock(chat_id):
        candidate = storage.get_or_create(chat_id, username)
        storage.update_profile(chat_id, vacancy=name)
        candidate["vacancy"] = name
        history = candidate["history"]
        history.append({
            "type": "user_input",
            "content": [{"type": "text", "text": f"Кандидат нажал «Откликнуться» на вакансию «{name}» в меню бота."}],
        })
        executor = make_tool_executor(chat_id, username)
        await run_and_reply(callback.message, candidate, history, executor)


# --- Обратная связь о работе бота (не отзыв о компании) ---

class FeedbackForm(StatesGroup):
    """FSM короткого сценария обратной связи о работе самого бота-ассистента.
    Кандидат присылает одно сообщение, которое напрямую пересылается
    рекрутеру, чтобы команда могла оперативно исправить проблему."""
    text = State()


@router.callback_query(F.data == "menu:feedback")
async def cb_menu_feedback(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackForm.text)
    await safe_edit_or_send(callback, FEEDBACK_PROMPT_TEXT, reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(FeedbackForm.text, F.text)
async def feedback_got_text(message: Message, state: FSMContext):
    await state.clear()
    username = message.from_user.username
    who = f"@{username}" if username else f"id {message.chat.id}"
    if RECRUITER_CHAT_ID:
        try:
            await bot.send_message(
                RECRUITER_CHAT_ID,
                f"💬 Обратная связь о работе бота от {who}:\n\n{message.text}",
            )
        except Exception:
            logger.exception("Не удалось переслать обратную связь о боте chat_id=%s", message.chat.id)
    await message.answer(FEEDBACK_DONE_TEXT, reply_markup=main_menu_keyboard())


# --- Сценарий «Хочу в команду MOVmedia» (кадровый резерв) ---

@router.callback_query(F.data == "menu:join")
async def cb_menu_join(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(ReserveForm.name)
    text = RESERVE_INTRO_TEXT + "\n\n" + "Как вас зовут? Напишите, пожалуйста, имя и фамилию."
    await safe_edit_or_send(callback, text, reply_markup=back_to_menu_keyboard())
    await callback.answer()


@router.message(ReserveForm.name, F.text)
async def reserve_got_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await state.set_state(ReserveForm.role)
    await message.answer(
        "Какая роль вам интересна? Напишите желаемую должность или направление.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(ReserveForm.role, F.text)
async def reserve_got_role(message: Message, state: FSMContext):
    await state.update_data(role=message.text.strip())
    await state.set_state(ReserveForm.portfolio)
    await message.answer(
        "Пришлите, пожалуйста, ссылку на портфолио.",
        reply_markup=back_to_menu_keyboard(),
    )


@router.message(ReserveForm.portfolio, F.text)
async def reserve_got_portfolio(message: Message, state: FSMContext):
    await state.update_data(portfolio=message.text.strip())
    await state.set_state(ReserveForm.resume)
    await message.answer(
        "Если хотите, пришлите резюме файлом или ссылкой — это необязательно.",
        reply_markup=skip_keyboard("resume"),
    )


@router.message(ReserveForm.resume, F.document | F.photo)
async def reserve_got_resume_file(message: Message, state: FSMContext):
    if message.document:
        storage.add_uploaded_file(message.chat.id, message.document.file_id, "document")
    elif message.photo:
        storage.add_uploaded_file(message.chat.id, message.photo[-1].file_id, "photo")
    await state.update_data(resume="резюме отправлено файлом")
    await state.set_state(ReserveForm.salary)
    await message.answer(
        "Какой желаемый уровень дохода? Можно пропустить этот шаг.",
        reply_markup=skip_keyboard("salary"),
    )


@router.message(ReserveForm.resume, F.text)
async def reserve_got_resume_text(message: Message, state: FSMContext):
    await state.update_data(resume=message.text.strip())
    await state.set_state(ReserveForm.salary)
    await message.answer(
        "Какой желаемый уровень дохода? Можно пропустить этот шаг.",
        reply_markup=skip_keyboard("salary"),
    )


@router.message(ReserveForm.salary, F.text)
async def reserve_got_salary(message: Message, state: FSMContext):
    await state.update_data(salary=message.text.strip())
    await state.set_state(ReserveForm.about)
    await message.answer(
        "Расскажите коротко о себе, если хотите — это необязательно.",
        reply_markup=skip_keyboard("about"),
    )


@router.message(ReserveForm.about, F.text)
async def reserve_got_about(message: Message, state: FSMContext):
    await state.update_data(about=message.text.strip())
    await _finish_reserve(message.chat.id, message.from_user.username, state, message)


@router.callback_query(F.data.startswith("reserve_skip:"))
async def cb_reserve_skip(callback: CallbackQuery, state: FSMContext):
    field = callback.data.split("reserve_skip:", 1)[1]
    await callback.answer()
    if field == "resume":
        await state.set_state(ReserveForm.salary)
        text = "Какой желаемый уровень дохода? Можно пропустить этот шаг."
        markup = skip_keyboard("salary")
        await safe_edit_or_send(callback, text, reply_markup=markup)
    elif field == "salary":
        await state.set_state(ReserveForm.about)
        text = "Расскажите коротко о себе, если хотите — это необязательно."
        markup = skip_keyboard("about")
        await safe_edit_or_send(callback, text, reply_markup=markup)
    elif field == "about":
        await _finish_reserve(callback.message.chat.id, callback.from_user.username, state, callback.message)


async def _finish_reserve(chat_id: int, username: str | None, state: FSMContext, reply_target: Message) -> None:
    """Сохраняет собранные данные кандидата резерва, уведомляет рекрутера
    отдельной сводкой (не полным чатом) и подтверждает кандидату получение."""
    data = await state.get_data()
    storage.update_profile(
        chat_id,
        name=data.get("name"),
        desired_role=data.get("role"),
        portfolio_link=data.get("portfolio"),
        resume_note=data.get("resume"),
        salary_expectations=data.get("salary"),
        about_me=data.get("about"),
    )
    storage.mark_reserve(chat_id)
    await state.clear()

    who = f"@{username}" if username else f"id {chat_id}"
    summary_lines = [f"🚀 Новый кандидат в кадровом резерве — {who}"]
    if data.get("name"):
        summary_lines.append(f"Имя: {data['name']}")
    if data.get("role"):
        summary_lines.append(f"Желаемая роль: {data['role']}")
    if data.get("portfolio"):
        summary_lines.append(f"Портфолио: {data['portfolio']}")
    if data.get("resume"):
        summary_lines.append(f"Резюме: {data['resume']}")
    if data.get("salary"):
        summary_lines.append(f"Желаемый доход: {data['salary']}")
    if data.get("about"):
        summary_lines.append(f"О себе: {data['about']}")
    if RECRUITER_CHAT_ID:
        try:
            await _send_recruiter_update(chat_id, "\n".join(summary_lines))
        except Exception:
            logger.exception("Не удалось уведомить рекрутера о новом кандидате резерва chat_id=%s", chat_id)

    await reply_target.answer(RESERVE_DONE_TEXT, reply_markup=main_menu_keyboard())


@router.message(F.document | F.photo)
async def handle_file(message: Message):
    """Файлы (резюме, тестовое) сохраняются и пересылаются рекрутеру одним
    сообщением вместе со сводкой по кандидату (см. notify_recruiter,
    _send_recruiter_update) - без отдельного дублирующего сообщения "Файл от
    кандидата" сразу при получении."""
    username = message.from_user.username

    if message.document:
        storage.add_uploaded_file(message.chat.id, message.document.file_id, "document")
    elif message.photo:
        storage.add_uploaded_file(message.chat.id, message.photo[-1].file_id, "photo")

    # Модель тоже должна знать, что файл получен, чтобы отреагировать текстом
    marker = "[Кандидат прислал файл — резюме или выполненное тестовое задание.]"
    if message.caption:
        marker += f" Подпись к файлу: {message.caption}"
    await process_text_turn(message, username, marker)


@router.message(F.text)
async def handle_text(message: Message):
    """Если кандидат в свободной форме спрашивает про компанию/культуру/команду —
    сразу открываем соответствующий короткий раздел подменю «О MOVmedia»,
    не отправляя вопрос модели и не показывая всё меню целиком."""
    topic = detect_sensitive_topic(message.text)
    if topic:
        reply_text = LEGAL_TOPIC_TEXT if topic == "legal" else SENSITIVE_TOPICS_TEXT
        username = message.from_user.username
        candidate = storage.get_or_create(message.chat.id, username)
        history = candidate["history"]
        history.append({"type": "user_input", "content": [{"type": "text", "text": message.text}]})
        history.append({"type": "model_output", "content": [{"type": "text", "text": reply_text}]})
        storage.save_history(message.chat.id, history)
        await message.answer(reply_text)
        return
    
    section = detect_about_section(message.text)
    if not section and _is_affirmative_reply(message.text):
        username = message.from_user.username
        candidate = storage.get_or_create(message.chat.id, username)
        section = _next_about_section_after_confirmation(candidate["history"])
    if section:
        username = message.from_user.username
        candidate = storage.get_or_create(message.chat.id, username)
        history = candidate["history"]
        history.append({"type": "user_input", "content": [{"type": "text", "text": message.text}]})
        text = ABOUT_SECTION_TEXTS[section]
        history.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
        storage.save_history(message.chat.id, history)
        await message.answer(text, reply_markup=about_section_keyboard())
        return
    await process_text_turn(message, message.from_user.username, message.text)


async def process_text_turn(message: Message, username: str | None, user_text: str):
    """Обрабатывает сообщение кандидата строго по очереди (см. _get_chat_lock) -
    иначе несколько быстрых сообщений подряд уходят в AI параллельно, и ответы
    или сохранение профиля могут произойти в перепутанном порядке."""
    async with _get_chat_lock(message.chat.id):
        candidate = storage.get_or_create(message.chat.id, username)
        history = candidate["history"]
        storage.update_last_activity(message.chat.id)

        logger.info("Входящее сообщение chat_id=%s: %s", message.chat.id, user_text[:200])
        history.append({"type": "user_input", "content": [{"type": "text", "text": user_text}]})

        executor = make_tool_executor(message.chat.id, username)
        await run_and_reply(message, candidate, history, executor)


async def check_silent_candidates():
    """Фоновая задача: раз в SILENT_CHECK_INTERVAL_SECONDS ищет кандидатов,
    которым выдали тестовое, но они не выходят на связь дольше порога.

    Помимо уведомления рекрутера, кандидату автоматически отправляется одно
    напоминание (см. ТЗ, раздел «Автоматизация»: через 3 дня — одно
    напоминание). Повторно оно не отправляется благодаря flagged_silent."""
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
                    f"Отправил кандидату одно автоматическое напоминание.",
                )
            try:
                await bot.send_message(candidate["chat_id"], CANDIDATE_REMINDER_TEXT)
                history = candidate["history"]
                history.append({
                    "type": "model_output",
                    "content": [{"type": "text", "text": CANDIDATE_REMINDER_TEXT}],
                })
                storage.save_history(candidate["chat_id"], history)
            except Exception:
                logger.exception("Не удалось отправить напоминание кандидату chat_id=%s", candidate["chat_id"])
            storage.mark_flagged_silent(candidate["chat_id"])

async def check_finished_dialogs():
    """Фоновая задача: раз в DIALOG_IDLE_CHECK_INTERVAL_SECONDS ищет кандидатов,
    которые писали боту, но не получили ни одного апдейта у рекрутера (ни через
    notify_recruiter, ни автоматически) и не выходят на связь дольше
    DIALOG_IDLE_MINUTES - считаем, что диалог завершён, и шлём рекрутеру
    короткий апдейт, чтобы ни один отклик не остался без внимания, даже если
    AI-модель не решила прислать полную сводку."""
    while True:
        await asyncio.sleep(DIALOG_IDLE_CHECK_INTERVAL_SECONDS)
        finished = storage.find_finished_dialog_candidates(
            older_than_seconds=DIALOG_IDLE_MINUTES * 60,
        )
        for candidate in finished:
            who = f"@{candidate['username']}" if candidate.get("username") else f"id {candidate['chat_id']}"
            lines = [f"💬 Короткий апдейт по кандидату {who} (диалог завершён)"]
            if candidate.get("vacancy"):
                lines.append(f"Вакансия: {candidate['vacancy']}")
            if candidate.get("name"):
                lines.append(f"Имя: {candidate['name']}")
            if candidate.get("resume_note"):
                lines.append(f"Резюме: {candidate['resume_note']}")
            if candidate.get("salary_expectations"):
                lines.append(f"Зарплатные ожидания: {candidate['salary_expectations']}")
            lines.append(f"Стадия: {candidate.get('stage')}")
            if RECRUITER_CHAT_ID:
                try:
                    await bot.send_message(RECRUITER_CHAT_ID, "\n".join(lines))
                except Exception:
                    logger.exception("Не удалось отправить автоматический апдейт chat_id=%s", candidate["chat_id"])
                    continue
            storage.mark_summary_notified(candidate["chat_id"])


dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)


async def main():
    global bot
    session = AiohttpSession(timeout=60, proxy=PROXY_URL)
    bot = Bot(token=BOT_TOKEN, session=session)

    asyncio.create_task(check_silent_candidates())
    asyncio.create_task(check_finished_dialogs())
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
