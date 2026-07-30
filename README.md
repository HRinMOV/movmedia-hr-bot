# movmedia-hr-bot

Telegram-бот на базе Gemini API для первичного отбора кандидатов в MOVmedia: знакомит кандидата с компанией и вакансией, собирает анкету, выдаёт тестовое задание и передаёт кандидата рекрутеру. Подробности архитектуры — в `architecture.md`.

## Установка

```
pip install -r requirements.txt
```

## Переменные окружения

Обязательные:

- `BOT_TOKEN` — токен Telegram-бота от @BotFather
- `GEMINI_API_KEY` — ключ Gemini API (бесплатный тариф, получить в Google AI Studio)
- `RECRUITER_CHAT_ID` — chat_id рекрутера или группы, куда бот шлёт уведомления и файлы

Необязательные (есть значения по умолчанию):

- `GEMINI_MODEL` (по умолчанию `gemini-2.0-flash`)
- `SILENT_CANDIDATE_HOURS` (по умолчанию `72`)
- `SILENT_CHECK_INTERVAL_SECONDS` (по умолчанию `3600`)
- `DB_PATH` (по умолчанию `candidates.db`)

Локально переменные можно положить в файл `.env` (см. `python-dotenv`), на проде — задать через настройки окружения хостинга.

## Запуск

Локально (только бот, без веб-сервера):

```
python bot.py
```

Прод (см. `Procfile`) — `app.py` поднимает Flask health-check endpoint и параллельно запускает Telegram-бота в фоновом потоке:

```
gunicorn app:app --workers 1 --threads 4
```
