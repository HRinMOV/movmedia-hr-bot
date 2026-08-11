import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GIGACHAT_AUTH_KEY = os.getenv("GIGACHAT_AUTH_KEY")

# chat_id Алины (или группы рекрутинга) — куда бот шлёт уведомления и файлы
RECRUITER_CHAT_ID = os.getenv("RECRUITER_CHAT_ID")

# Модель и параметры GigaChat (https://developers.sber.ru/docs/ru/gigachat/api/overview)
GIGACHAT_MODEL = os.getenv("GIGACHAT_MODEL", "GigaChat-2-Max")
GIGACHAT_SCOPE = os.getenv("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")

# Прокси/VPN для доступа к Telegram API, если он заблокирован на сервере
# (обычный proxy URL вида socks5://host:port или http://user:pass@host:port)
PROXY_URL = os.getenv("PROXY_URL")

# Сколько одновременных обращений к AI считаем "повышенной нагрузкой" — при
# превышении кандидату отправляется предупреждение, что ответ может занять больше времени
HIGH_LOAD_CONCURRENCY_THRESHOLD = int(os.getenv("HIGH_LOAD_CONCURRENCY_THRESHOLD", "3"))

# Через сколько часов без ответа кандидата после отправки тестового считать его "пропавшим"
SILENT_CANDIDATE_HOURS = int(os.getenv("SILENT_CANDIDATE_HOURS", "72"))

# Как часто (в секундах) проверять базу на "пропавших" кандидатов
SILENT_CHECK_INTERVAL_SECONDS = int(os.getenv("SILENT_CHECK_INTERVAL_SECONDS", "3600"))

DB_PATH = os.getenv("DB_PATH", "candidates.db")

# Интеграция с Notion (создание карточек кандидатов при отклике на вакансию).
# Если не заданы — notion_service просто пропускает создание карточки и пишет
# предупреждение в лог, остальная работа бота не нарушается.
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
