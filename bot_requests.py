import csv
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AMVERA_API_TOKEN = os.getenv("AMVERA_API_TOKEN")
AMVERA_API_BASE = os.getenv("AMVERA_API_BASE", "https://inference.waw0.amvera.ru").rstrip("/")
AMVERA_MODEL = os.getenv("AMVERA_MODEL", "deepseek-v3")
LLM_TIMEOUT = 15
MAX_SENTENCES = 7
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_CSV_FILE = os.getenv("LOG_CSV_FILE", "bot.csv")


def resolve_log_dir() -> Path:
    """
    Amvera: постоянный диск обычно смонтирован в /data → logs/bot.csv виден в Data.
    Локально: папка logs/ в каталоге проекта.
    """
    explicit = os.getenv("LOG_DIR")
    if explicit:
        path = Path(explicit)
        if path == Path("logs") and Path("/data").is_dir():
            return Path("/data/logs")
        return path
    if Path("/data").is_dir():
        return Path("/data/logs")
    return Path("logs")


LOG_DIR = resolve_log_dir()
LOG_CSV_PATH = LOG_DIR / LOG_CSV_FILE

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN в .env")
if not AMVERA_API_TOKEN:
    raise ValueError("Задайте AMVERA_API_TOKEN в .env")

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
LLM_URL = f"{AMVERA_API_BASE}/v1/chat/completions"

PROMPT_TEMPLATE = (
    "Ты мистический оракул. Ответь на вопрос пользователя как таролог. "
    "Будь загадочным, но полезным. Ответь с эмодзи и кратким раскладом. "
    "И добавь долю шутки или прикола. Каждая карта с новой строки. "
    f"Ответь не более чем {MAX_SENTENCES} предложениями.\n\n"
    "Вопрос: {user_text}"
)

SHUFFLING_TEXT = "🃏 Карты тасуются…"
ERROR_TAROT = "🔮 Сейчас тарологу плохо — карты молчат. Попробуйте чуть позже."
NO_TEXT_HINT = "✨ Напишите вопрос обычным текстом — и я сделаю расклад."

START_TEXT = (
    "🔮 Добро пожаловать!\n\n"
    "Я гадалка Таро. Задайте вопрос — и карты подскажут ответ.\n\n"
    "Напишите свой вопрос **одним сообщением**, например 'Будем ли с Петром вместе?' или введите /help."
)
HELP_TEXT = (
    "📖 Как пользоваться ботом:\n\n"
    "• Напишите вопрос обычным текстом — я отвечу раскладом.\n"
    "• Можно спрашивать о делах, отношениях, планах и выборе.\n"
    "• Команды: /start — приветствие, /help — эта справка.\n\n"
    "Ответы носят развлекательный характер и не заменяют совет специалиста."
)

CSV_HEADERS = [
    "datetime",
    "level",
    "username",
    "chat_id",
    "incoming_message",
    "llm_duration_sec",
    "http_status",
    "event",
    "error",
]

logger = logging.getLogger("tarot_bot")


def setup_logging():
    """Консоль (текст) + CSV-файл без ротации."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not LOG_CSV_PATH.exists():
            with open(LOG_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)
    except OSError as e:
        raise RuntimeError(f"Не удалось создать каталог логов {LOG_DIR}: {e}") from e

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    if logger.handlers:
        return

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(console)
    logger.info("CSV-логи: %s", LOG_CSV_PATH)


def log_event(
    level: str,
    username: str,
    chat_id,
    incoming_message: str,
    *,
    llm_duration_sec: str = "",
    http_status: str = "",
    event: str = "",
    error: str = "",
):
    """Пишет строку в CSV и дублирует кратко в консоль."""
    row = [
        datetime.now(timezone.utc).isoformat(),
        level,
        username,
        chat_id,
        incoming_message,
        llm_duration_sec,
        http_status,
        event,
        error,
    ]
    with open(LOG_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)

    console_msg = (
        f"event={event} user={username} chat={chat_id} "
        f"msg={incoming_message!r} llm_sec={llm_duration_sec} http={http_status}"
    )
    if error:
        console_msg += f" error={error}"
    if level == "ERROR":
        logger.error(console_msg)
    else:
        logger.info(console_msg)


def get_username(message) -> str:
    user = message.get("from") or {}
    if user.get("username"):
        return f"@{user['username']}"
    parts = [user.get("first_name"), user.get("last_name")]
    name = " ".join(p for p in parts if p)
    if name:
        return name
    return str(user.get("id", "unknown"))


def get_updates(offset=None):
    """Запрашивает у Telegram новые сообщения."""
    params = {"timeout": 100, "offset": offset}
    try:
        response = requests.get(TG_URL + "getUpdates", params=params, timeout=102)
        response.raise_for_status()
        return response.json().get("result", [])
    except Exception as e:
        log_event(
            "ERROR",
            "",
            "",
            "",
            event="telegram_get_updates",
            error=str(e),
        )
        return []


def send_message(chat_id, text):
    """Отправляет сообщение пользователю."""
    try:
        response = requests.post(
            TG_URL + "sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as e:
        http_status = ""
        if isinstance(e, requests.HTTPError) and e.response is not None:
            http_status = str(e.response.status_code)
        log_event(
            "ERROR",
            "",
            chat_id,
            text[:200],
            http_status=http_status,
            event="telegram_send_message",
            error=str(e),
        )


def limit_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> str:
    """Обрезает ответ до заданного числа предложений."""
    text = text.strip()
    if not text:
        return text
    parts = re.split(r"(?<=[.!?…])\s+", text)
    if len(parts) <= max_sentences:
        return text
    return " ".join(parts[:max_sentences]).strip()


def ask_deepseek(user_text: str) -> tuple[str | None, float, str, str]:
    """
    Запрос к DeepSeek v3 через Amvera.
    Возвращает: (ответ, длительность_сек, http_status, текст_ошибки).
    """
    prompt = PROMPT_TEMPLATE.format(user_text=user_text)
    headers = {
        "Content-Type": "application/json",
        "X-Auth-Token": f"Bearer {AMVERA_API_TOKEN}",
        "Authorization": f"Bearer {AMVERA_API_TOKEN}",
    }
    payload = {
        "model": AMVERA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    started = time.perf_counter()
    http_status = ""
    error_text = ""
    try:
        response = requests.post(
            LLM_URL,
            headers=headers,
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        http_status = str(response.status_code)
        response.raise_for_status()
        data = response.json()
        content = limit_sentences(data["choices"][0]["message"]["content"].strip())
        duration = round(time.perf_counter() - started, 3)
        return content, duration, http_status, ""
    except requests.RequestException as e:
        duration = round(time.perf_counter() - started, 3)
        error_text = str(e)
        if getattr(e, "response", None) is not None:
            http_status = str(e.response.status_code)
            error_text = f"{e} | body={e.response.text[:500]}"
        return None, duration, http_status, error_text


def handle_message(message):
    """Обрабатывает входящие сообщения."""
    chat_id = message["chat"]["id"]
    username = get_username(message)
    text = message.get("text")

    if not text:
        send_message(chat_id, NO_TEXT_HINT)
        log_event(
            "INFO",
            username,
            chat_id,
            "",
            event="no_text",
        )
        return

    if text in ("/start", "/help"):
        send_message(chat_id, START_TEXT if text == "/start" else HELP_TEXT)
        log_event(
            "INFO",
            username,
            chat_id,
            text,
            event="command",
        )
        return

    send_message(chat_id, SHUFFLING_TEXT)
    answer, llm_sec, http_status, error_text = ask_deepseek(text)

    if answer:
        send_message(chat_id, answer)
        log_event(
            "INFO",
            username,
            chat_id,
            text,
            llm_duration_sec=str(llm_sec),
            http_status=http_status,
            event="llm_ok",
        )
    else:
        send_message(chat_id, ERROR_TAROT)
        log_event(
            "ERROR",
            username,
            chat_id,
            text,
            llm_duration_sec=str(llm_sec),
            http_status=http_status,
            event="llm_fail",
            error=error_text,
        )


def main():
    setup_logging()
    logger.info("Бот запущен")
    log_event("INFO", "", "", "", event="bot_start")
    last_update_id = 0
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            if "message" in update:
                handle_message(update["message"])
        time.sleep(1)


if __name__ == "__main__":
    main()
