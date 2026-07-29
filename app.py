import asyncio
import logging
import threading
import time

from flask import Flask

import bot as bot_module

app = Flask(__name__)

_started = False
_lock = threading.Lock()


def _run_bot_forever():
    while True:
        try:
            asyncio.run(bot_module.main())
        except Exception as e:
            logging.error(f"Бот упал с ошибкой: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)


def start_bot_once():
    global _started
    with _lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=_run_bot_forever, daemon=True)
        thread.start()
        logging.info("Telegram-бот запущен в фоновом потоке")


# Запускаем бота сразу при импорте приложения — так делает и gunicorn, и flask run
start_bot_once()


@app.route("/")
def health_check():
    # Эта страница не имеет отношения к боту — она нужна только для того,
    # чтобы App Platform видел, что приложение отвечает и считал его "живым"
    return "MOVmedia HR bot is running"
