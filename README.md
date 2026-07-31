# movmedia-hr-bot

Telegram-бот на базе GigaChat API для первичного отбора кандидатов в MOVmedia: знакомит кандидата с компанией и вакансией, собирает анкету, выдаёт тестовое задание и передаёт кандидата рекрутеру. Подробности архитектуры — в `architecture.md`.

## Установка

```
pip install -r requirements.txt
```

## Переменные окружения

Обязательные:

- `BOT_TOKEN` — токен Telegram-бота от @BotFather
- `GIGACHAT_AUTH_KEY` — ключ авторизации GigaChat API (Authorization key из личного кабинета GigaChat API)
- `RECRUITER_CHAT_ID` — chat_id рекрутера или группы, куда бот шлёт уведомления и файлы

Необязательные (есть значения по умолчанию):

- `GIGACHAT_MODEL` (по умолчанию `GigaChat-2-Max`)
- `GIGACHAT_SCOPE` (по умолчанию `GIGACHAT_API_PERS`)
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
