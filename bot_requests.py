import csv
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from history_db import DB_PATH, add_message, clear_history, expire_if_inactive, get_history, init_db

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

SYSTEM_PROMPT = (
    "Ты таролог и психолог. Отвечай на вопросы пользователя как таролог или психолог в зависимости от полноты контекста. "
    "Будь совсем немного загадочным, но полезным. Если отвечаешь как таролог - отвечай с эмодзи и кратким раскладом. Каждая карта расклада с новой строки и без звездочек.   "
    ""
    f"Отвечай не более чем {MAX_SENTENCES} предложениями. "
    "Учитывай предыдущий диалог, если он есть."
)

SHUFFLING_TEXT = "🃏 Карты тасуются…"
ERROR_TAROT = "🔮 Сейчас тарологу плохо — карты молчат. Попробуйте чуть позже."
NO_TEXT_HINT = "✨ Напишите вопрос обычным текстом — и я сделаю расклад."
EXPIRY_NOTICE = (
    "🌙 Прошло 10 дней тишины — оракул забыл прежний контекст. "
    "Начнём с чистого расклада."
)
CLEAR_TEXT = "🕯️ Контекст очищен. Оракул не помнит прошлых вопросов — спрашивайте заново."

START_TEXT = (
    "🔮 Добро пожаловать!\n\n"
    "Я гадалка Таро. Задайте вопрос — и карты подскажут ответ.\n\n"
    "Напишите свой вопрос **одним сообщением**, например 'Будем ли с Петром вместе?' или введите /help."
)
HELP_TEXT = (
    "📖 Как пользоваться ботом:\n\n"
    "• Напишите вопрос обычным текстом — я отвечу раскладом.\n"
    "• Можно спрашивать о делах, отношениях, планах и выборе.\n"
    "• Я помню до 10 последних вопросов в этом чате.\n"
    "• Команды: /start — приветствие, /help — справка, /clear — забыть контекст.\n\n"
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

TELEGRAM_MAX_LENGTH = 4096
SEND_MESSAGE_RETRIES = 5
SEND_MESSAGE_RETRY_DELAY = 5  # секунды между попытками

# #region agent log
DEBUG_LOG_PATH = Path(__file__).resolve().parent / "debug-843ab9.log"


def _agent_dbg(hypothesis_id: str, location: str, message: str, data: dict | None = None) -> None:
    import json
    import sys

    payload = {
        "sessionId": "843ab9",
        "runId": "post-fix",
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data or {},
        "timestamp": int(time.time() * 1000),
    }
    line = json.dumps(payload, ensure_ascii=False)
    for path in {DEBUG_LOG_PATH, LOG_DIR / "debug-843ab9.log"}:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    # print+flush: Amvera иногда не показывает INFO из logging
    print(f"DBG [{hypothesis_id}] {message} {data or {}}", flush=True, file=sys.stderr)
    try:
        logger.error("DBG [%s] %s %s", hypothesis_id, message, data or {})
    except Exception:
        pass
# #endregion


def setup_logging():
    """Консоль (текст) + CSV-файл без ротации."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not LOG_CSV_PATH.exists():
            with open(LOG_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)
    except OSError as e:
        # Не роняем бота из‑за CSV: консольные логи важнее
        print(f"Не удалось подготовить CSV-логи {LOG_CSV_PATH}: {e}", flush=True)

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    if not logger.handlers:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(console)
    logger.info("CSV-логи: %s", LOG_CSV_PATH)


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    if TELEGRAM_BOT_TOKEN:
        out = out.replace(TELEGRAM_BOT_TOKEN, "<TELEGRAM_TOKEN>")
    if AMVERA_API_TOKEN:
        out = out.replace(AMVERA_API_TOKEN, "<AMVERA_TOKEN>")
    return out


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
    """Пишет в консоль сразу, CSV — отдельно (сбой CSV не глушит логи)."""
    error = _redact_secrets(error)
    # #region agent log
    _agent_dbg(
        "A",
        "bot_requests.py:log_event:entry",
        "log_event called",
        {"event": event, "level": level, "chat_id": str(chat_id), "csv_path": str(LOG_CSV_PATH)},
    )
    # #endregion

    console_msg = (
        f"event={event} user={username} chat={chat_id} "
        f"msg={incoming_message!r} llm_sec={llm_duration_sec} http={http_status}"
    )
    if error:
        console_msg += f" error={error}"
    # Консоль ПЕРВОЙ — иначе падение CSV оставляло Amvera без логов
    if level == "ERROR":
        logger.error(console_msg)
    else:
        logger.info(console_msg)
    print(console_msg, flush=True)

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
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
        # #region agent log
        _agent_dbg("A", "bot_requests.py:log_event:csv_ok", "csv write ok", {"event": event})
        # #endregion
    except Exception as e:
        # #region agent log
        _agent_dbg(
            "A",
            "bot_requests.py:log_event:csv_fail",
            "csv write failed",
            {"event": event, "error": str(e)},
        )
        # #endregion
        logger.error("CSV log write failed event=%s: %s", event, e)


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


def send_message(chat_id, text) -> bool:
    """Отправляет сообщение пользователю. До 5 попыток с паузой 5 сек. Возвращает True при успехе."""
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[: TELEGRAM_MAX_LENGTH - 3] + "..."

    last_error = ""
    last_http_status = ""

    for attempt in range(1, SEND_MESSAGE_RETRIES + 1):
        try:
            response = requests.post(
                TG_URL + "sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            data = response.json()
            if data.get("ok"):
                if attempt > 1:
                    logger.info(
                        "sendMessage успех с попытки %s/%s chat=%s",
                        attempt,
                        SEND_MESSAGE_RETRIES,
                        chat_id,
                    )
                return True

            last_http_status = str(response.status_code)
            last_error = data.get("description", "Telegram API error")
        except Exception as e:
            last_http_status = ""
            if isinstance(e, requests.HTTPError) and e.response is not None:
                last_http_status = str(e.response.status_code)
            last_error = str(e)

        logger.warning(
            "sendMessage попытка %s/%s не удалась chat=%s: %s",
            attempt,
            SEND_MESSAGE_RETRIES,
            chat_id,
            last_error,
        )
        if attempt < SEND_MESSAGE_RETRIES:
            time.sleep(SEND_MESSAGE_RETRY_DELAY)

    log_event(
        "ERROR",
        "",
        chat_id,
        "",
        http_status=last_http_status,
        event="telegram_send_message",
        error=f"после {SEND_MESSAGE_RETRIES} попыток: {last_error}",
    )
    return False


def save_history(chat_id: int, user_text: str, answer: str | None = None) -> None:
    """Сохраняет историю; ошибки БД не блокируют ответ в Telegram."""
    try:
        add_message(chat_id, "user", user_text)
        if answer:
            add_message(chat_id, "assistant", answer)
    except Exception as e:
        logger.error("Не удалось сохранить историю chat=%s: %s", chat_id, e)
        log_event(
            "ERROR",
            "",
            chat_id,
            user_text,
            event="history_save_fail",
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


def ask_deepseek(
    user_text: str,
    history: list[dict[str, str]],
) -> tuple[str | None, float, str, str]:
    """
    Запрос к DeepSeek v3 через Amvera с учётом истории диалога.
    Возвращает: (ответ, длительность_сек, http_status, текст_ошибки).
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    headers = {
        "Content-Type": "application/json",
        "X-Auth-Token": f"Bearer {AMVERA_API_TOKEN}",
        "Authorization": f"Bearer {AMVERA_API_TOKEN}",
    }
    payload = {
        "model": AMVERA_MODEL,
        "messages": messages,
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
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
        duration = round(time.perf_counter() - started, 3)
        error_text = _redact_secrets(str(e))
        if getattr(e, "response", None) is not None:
            http_status = str(e.response.status_code)
            error_text = _redact_secrets(f"{e} | body={e.response.text[:500]}")
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

    if text == "/clear":
        clear_history(chat_id)
        send_message(chat_id, CLEAR_TEXT)
        log_event(
            "INFO",
            username,
            chat_id,
            text,
            event="history_clear",
        )
        return

    if expire_if_inactive(chat_id):
        send_message(chat_id, EXPIRY_NOTICE)

    history = get_history(chat_id)
    # #region agent log
    _agent_dbg(
        "B",
        "bot_requests.py:handle_message:before_llm",
        "about to call llm",
        {"chat_id": chat_id, "history_len": len(history), "text_len": len(text)},
    )
    # #endregion
    send_message(chat_id, SHUFFLING_TEXT)
    answer, llm_sec, http_status, error_text = ask_deepseek(text, history)
    # #region agent log
    _agent_dbg(
        "B",
        "bot_requests.py:handle_message:after_llm",
        "llm finished",
        {
            "chat_id": chat_id,
            "has_answer": bool(answer),
            "llm_sec": llm_sec,
            "http_status": http_status,
            "error_preview": (error_text or "")[:200],
        },
    )
    # #endregion

    if answer:
        sent = send_message(chat_id, answer)
        # #region agent log
        _agent_dbg(
            "C",
            "bot_requests.py:handle_message:answer_send",
            "oracle answer send result",
            {"chat_id": chat_id, "sent": sent, "answer_len": len(answer)},
        )
        # #endregion
        if sent:
            save_history(chat_id, text, answer)
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
            save_history(chat_id, text)
            send_message(chat_id, ERROR_TAROT)
            log_event(
                "ERROR",
                username,
                chat_id,
                text,
                llm_duration_sec=str(llm_sec),
                http_status=http_status,
                event="telegram_answer_fail",
                error="Не удалось отправить ответ в Telegram",
            )
    else:
        # #region agent log
        _agent_dbg(
            "B",
            "bot_requests.py:handle_message:llm_fail_branch",
            "entering llm_fail branch",
            {"chat_id": chat_id, "http_status": http_status},
        )
        # #endregion
        save_history(chat_id, text)
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
    init_db()
    logger.info("Бот запущен")
    logger.info("История диалогов: %s", DB_PATH)
    log_event("INFO", "", "", "", event="bot_start")
    last_update_id = 0
    while True:
        updates = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            if "message" in update:
                try:
                    handle_message(update["message"])
                except Exception as e:
                    # #region agent log
                    _agent_dbg(
                        "D",
                        "bot_requests.py:main:handle_exception",
                        "unhandled exception in handle_message",
                        {"error": str(e)},
                    )
                    # #endregion
                    logger.exception("Ошибка обработки сообщения: %s", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
