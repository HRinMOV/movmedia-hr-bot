# movmedia-hr-bot

Telegram-бот на базе GigaChat API для первичного отбора кандидатов в MOVmedia: знакомит кандидата с компанией и вакансией, собирает анкету, выдаёт тестовое задание и передаёт кандидата рекрутеру. Пока готовится ответ, кандидат видит статус «печатает...», а при повышенной нагрузке — предупреждение, что ответ может занять чуть больше времени. Подробности архитектуры — в `architecture.md`.

## Установка

```
pip install -r requirements.txt
```

## Переменные окружения

Обязательные:

- `BOT_TOKEN` — токен Telegram-бота от @BotFather
- `GIGACHAT_AUTH_KEY` — Authorization key GigaChat API (личный кабинет developers.sber.ru, base64 Client ID:Client Secret)
- `RECRUITER_CHAT_ID` — chat_id рекрутера или группы, куда бот шлёт уведомления и файлы

Необязательные (есть значения по умолчанию):

- `GIGACHAT_MODEL` (по умолчанию `GigaChat-2-Max`)
- `GIGACHAT_SCOPE` (по умолчанию `GIGACHAT_API_PERS`)
- `PROXY_URL` — прокси/VPN для доступа к Telegram API, если он заблокирован на сервере (адрес вида `socks5://host:port` или `http://user:pass@host:port`); без переменной бот работает напрямую
- `HIGH_LOAD_CONCURRENCY_THRESHOLD` (по умолчанию `3`) — сколько одновременных обращений к AI считается повышенной нагрузкой; при превышении кандидату отправляется предупреждение, что ответ займёт больше времени
- `SILENT_CANDIDATE_HOURS` (по умолчанию `72`)
- `SILENT_CHECK_INTERVAL_SECONDS` (по умолчанию `3600`)
- `DB_PATH` (по умолчанию `candidates.db`)

Notion-интеграция (создание карточек кандидатов при отклике на вакансию; если не заданы — бот работает как раньше, просто без создания карточек):

- `NOTION_TOKEN` — секретный токен интеграции Notion (создаётся в notion.so/my-integrations и подключается к базе "Кандидаты (база данных)" через Share → Connections)
- `NOTION_DATABASE_ID` — ID базы "Кандидаты (база данных)" в Notion

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
