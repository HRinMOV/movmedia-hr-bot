import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# chat_id Алины (или группы рекрутинга) — куда бот шлёт уведомления и файлы
RECRUITER_CHAT_ID = os.getenv("RECRUITER_CHAT_ID")

# Модель Gemini для ведения диалога (бесплатный тариф Google AI Studio)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Через сколько часов без ответа кандидата после отправки тестового считать его "пропавшим"
SILENT_CANDIDATE_HOURS = int(os.getenv("SILENT_CANDIDATE_HOURS", "72"))

# Как часто (в секундах) проверять базу на "пропавших" кандидатов
SILENT_CHECK_INTERVAL_SECONDS = int(os.getenv("SILENT_CHECK_INTERVAL_SECONDS", "3600"))

DB_PATH = os.getenv("DB_PATH", "candidates.db")
