# movmedia-hr-bot

Telegram-бот на базе Gemini API для первичного отбора кандидатов в MOVmedia: знакомит кандидата с компанией и вакансией, собирает анкету, выдаёт тестовое задание и передаёт кандидата рекрутеру. Подробности архитектуры — в `architecture.md`.

## Установка

```
pip install -r requirements.txt
```

## Переменные окружения

Обязательные:

- `BOT_TOKEN` — токен Telegram-бота от @BotFather
- `GEMINI_API_KEY` — ключ авторизации Gemini API (Google AI Studio)
- `RECRUITER_CHAT_ID` — chat_id рекрутера или группы, куда бот шлёт уведомления и файлы

Необязательные (есть значения по умолчанию):

- `GEMINI_MODEL` (по умолчанию `gemini-3.6-flash`)
- `PROXY_URL` — прокси/VPN для доступа к Telegram API, если он заблокирован на сервере (адрес вида `socks5://host:port` или `http://user:pass@host:port`); без переменной бот работает напрямую
- `SILENT_CANDIDATE_HOURS` (по умолчанию `72`)
- `SILENT_CHECK_INTERVAL_SECONDS` (по умолчанию `3600`)
- `DB_PATH` (по умолчанию `candidates.db`)

Локально переменные можно положить в файл `.env` (см. `python-dotenv`), на проде — задать через настройки окружения хостинга.

## Запуск

Локально (только бот, без веб-сервера):

```
python bot.py
```

Прод (см. Procfile) — `app.py` поднимает Flask health-check endpoint и параллельно запускает Telegram-бота в фоновом потоке:

```
gunicorn app:app --workers 1 --threads 4
```

## Команда рекрутера /status

В чате рекрутера (RECRUITER_CHAT_ID) доступна команда для ручного перевода кандидата на стадии, которые происходят вне диалога с ботом:

```
/status <chat_id> <stage> [комментарий]
```

Доступные stage: `applied`, `screening`, `test_sent`, `test_received`, `interview_scheduled`, `interview_completed`, `review`, `offer`, `rejected`, `withdrawn`. Кандидату автоматически отправляется шаблонное уведомление о смене стадии.
