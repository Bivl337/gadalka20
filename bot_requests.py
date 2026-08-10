import csv
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from history_db import DB_PATH, add_message, clear_history, expire_if_inactive, get_history, init_db

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AMVERA_API_TOKEN = os.getenv("AMVERA_API_TOKEN")
AMVERA_API_BASE = os.getenv("AMVERA_API_BASE", "https://inference.waw0.amvera.ru").rstrip("/")
AMVERA_MODEL = os.getenv("AMVERA_MODEL", "deepseek-v3")
LLM_TIMEOUT = 180
LLM_RETRIES = 3
LLM_RETRY_DELAY = 3
MAX_SENTENCES = 7
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_CSV_FILE = os.getenv("LOG_CSV_FILE", "bot.csv")
DEBUG_HTTP = os.getenv("DEBUG_HTTP", "0").strip().lower() in ("1", "true", "yes", "on")
DEBUG_HTTP_BODY_LIMIT = int(os.getenv("DEBUG_HTTP_BODY_LIMIT", "8000"))


def resolve_log_dir() -> Path:
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
TG_GET_UPDATES_URL = TG_URL + "getUpdates"
TG_SEND_MESSAGE_URL = TG_URL + "sendMessage"
LLM_URL = f"{AMVERA_API_BASE}/v1/chat/completions"


def _endpoint(url: str) -> tuple[str, str, str]:
    """host, port, scheme"""
    p = urlparse(url)
    host = p.hostname or ""
    if p.port:
        port = str(p.port)
    elif p.scheme == "https":
        port = "443"
    elif p.scheme == "http":
        port = "80"
    else:
        port = ""
    return host, port, p.scheme or ""


TG_HOST, TG_PORT, TG_SCHEME = _endpoint("https://api.telegram.org/")
LLM_HOST, LLM_PORT, LLM_SCHEME = _endpoint(LLM_URL)

SYSTEM_PROMPT = (
    "Ты таролог и психолог. Отвечай на вопросы пользователя как таролог или психолог в зависимости от полноты контекста. "
    "Будь совсем немного загадочным, но полезным. Если отвечаешь как таролог - отвечай с эмодзи и кратким раскладом. Каждая карта расклада с новой строки и без звездочек.   "
    ""
    f"Отвечай не более чем {MAX_SENTENCES} предложениями. "
    "Учитывай предыдущий диалог, если он есть."
)

SHUFFLING_TEXT = "🃏 Карты тасуются…"
ERROR_TAROT = "🔮 Сейчас тарологу плохо — карты молчат. Попробуйте чуть позже."
SHARE_FOOTER = "\n\n🔮 @Gadalka20_bot <- сделай себе расклад"
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
    "• Я помню несколько последних реплик в этом чате.\n"
    "• Команды: /start — приветствие, /help — справка, /clear — забыть контекст.\n\n"
    "Ответы носят развлекательный характер и не заменяют совет специалиста."
)

CSV_HEADERS = [
    "datetime",
    "method",
    "scheme",
    "host",
    "port",
    "url",
    "from_user",
    "chat_id",
    "content",
    "attempt",
    "status",
    "http",
    "timeout_sec",
    "sec",
    "error",
]

logger = logging.getLogger("tarot_bot")

TELEGRAM_MAX_LENGTH = 4096
SEND_MESSAGE_RETRIES = 10
SHUFFLING_RETRIES = 2
SEND_MESSAGE_RETRY_DELAY = 5


def setup_logging():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        write_header = not LOG_CSV_PATH.exists()
        if write_header:
            with open(LOG_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_HEADERS)
    except OSError as e:
        logger.error("Не удалось подготовить CSV-логи %s: %s", LOG_CSV_PATH, e)

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    if not logger.handlers:
        console = logging.StreamHandler()
        console.setLevel(level)
        console.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(console)


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    out = text
    if TELEGRAM_BOT_TOKEN:
        out = out.replace(TELEGRAM_BOT_TOKEN, "<TELEGRAM_TOKEN>")
    if AMVERA_API_TOKEN:
        out = out.replace(AMVERA_API_TOKEN, "<AMVERA_TOKEN>")
    return out


def _preview(text: str, limit: int = 300) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _redact_headers(headers) -> dict:
    sensitive = {"authorization", "x-auth-token", "x-api-key", "cookie", "set-cookie"}
    out = {}
    for key, value in dict(headers or {}).items():
        if str(key).lower() in sensitive:
            out[str(key)] = "<REDACTED>"
        else:
            out[str(key)] = _redact_secrets(str(value))
    return out


def _clip_body(body: str) -> str:
    body = _redact_secrets(body or "")
    if len(body) > DEBUG_HTTP_BODY_LIMIT:
        cut = len(body) - DEBUG_HTTP_BODY_LIMIT
        return body[:DEBUG_HTTP_BODY_LIMIT] + f"\n... <truncated {cut} chars>"
    return body


def log_http_dump(
    *,
    http_method: str,
    url: str,
    request_headers=None,
    request_params=None,
    request_body: str | None = None,
    response: requests.Response | None = None,
    error: str = "",
    sec: float | str = "",
):
    """Полный дамп HTTP при DEBUG_HTTP=1 (учебный режим)."""
    if not DEBUG_HTTP:
        return
    lines = [
        "----- DEBUG_HTTP BEGIN -----",
        f"HTTP {http_method} {_redact_secrets(url)}",
        f"sec={sec}",
    ]
    if request_headers is not None:
        lines.append(
            "REQUEST HEADERS: "
            + json.dumps(_redact_headers(request_headers), ensure_ascii=False)
        )
    if request_params is not None:
        lines.append(
            "REQUEST PARAMS: "
            + _redact_secrets(json.dumps(request_params, ensure_ascii=False))
        )
    if request_body is not None:
        lines.append("REQUEST BODY:\n" + _clip_body(request_body))
    if response is not None:
        lines.append(f"RESPONSE STATUS: {response.status_code}")
        lines.append(
            "RESPONSE HEADERS: "
            + json.dumps(_redact_headers(response.headers), ensure_ascii=False)
        )
        try:
            lines.append("RESPONSE BODY:\n" + _clip_body(response.text))
        except Exception:
            lines.append("RESPONSE BODY: <unavailable>")
    if error:
        lines.append("ERROR: " + _redact_secrets(error))
    lines.append("----- DEBUG_HTTP END -----")
    logger.info("\n".join(lines))


def log_request(
    method: str,
    *,
    host: str = "",
    port: str = "",
    scheme: str = "https",
    url: str = "",
    from_user: str = "",
    chat_id="",
    content: str = "",
    attempt: str = "1/1",
    status: str = "ok",
    http: str = "",
    timeout_sec: str | int | float = "",
    sec: float | str = "",
    error: str = "",
):
    """Одна строка лога на сетевой запрос (консоль + CSV), с техдеталями."""
    error = _redact_secrets(error)
    content = _preview(content)
    url_safe = _redact_secrets(url)
    sec_str = f"{sec}" if sec != "" else ""
    timeout_str = f"{timeout_sec}" if timeout_sec != "" else ""
    http_str = str(http) if http != "" else "-"

    line = (
        f"{method} → {scheme}://{host}:{port} "
        f"url={url_safe} "
        f"from={from_user or '-'} chat={chat_id or '-'} "
        f"content={content!r} attempt={attempt} "
        f"status={status} http={http_str} timeout={timeout_str} sec={sec_str}"
    )
    if error:
        line += f" error={error}"

    if status == "ok":
        logger.info(line)
    else:
        logger.error(line)

    row = [
        datetime.now(timezone.utc).isoformat(),
        method,
        scheme,
        host,
        port,
        url_safe,
        from_user,
        chat_id,
        content,
        attempt,
        status,
        http_str,
        timeout_str,
        sec_str,
        error,
    ]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(row)
    except Exception as e:
        logger.error("CSV write failed: %s", e)


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
    """Запрашивает у Telegram новые сообщения. Пустые успешные poll не логируем."""
    params = {"timeout": 100, "offset": offset}
    started = time.perf_counter()
    try:
        response = requests.get(TG_GET_UPDATES_URL, params=params, timeout=102)
        sec = round(time.perf_counter() - started, 3)
        response.raise_for_status()
        result = response.json().get("result", [])
        # Полный дамп только если пришли апдейты (не каждый пустой long poll)
        if result:
            log_http_dump(
                http_method="GET",
                url=TG_GET_UPDATES_URL,
                request_params=params,
                response=response,
                sec=sec,
            )
        return result, sec, str(response.status_code)
    except Exception as e:
        sec = round(time.perf_counter() - started, 3)
        http = ""
        resp = getattr(e, "response", None)
        if isinstance(e, requests.HTTPError) and resp is not None:
            http = str(resp.status_code)
        log_http_dump(
            http_method="GET",
            url=TG_GET_UPDATES_URL,
            request_params=params,
            response=resp if isinstance(resp, requests.Response) else None,
            error=str(e),
            sec=sec,
        )
        log_request(
            "getUpdates",
            host=TG_HOST,
            port=TG_PORT,
            scheme=TG_SCHEME,
            url=TG_GET_UPDATES_URL,
            content="poll",
            status="fail",
            http=http,
            timeout_sec=102,
            sec=sec,
            error=str(e),
        )
        return [], sec, http


def log_incoming_update(update: dict, poll_sec: float = 0, http: str = "200") -> None:
    """Лог входящего update из getUpdates (только когда есть сообщение)."""
    common = dict(
        host=TG_HOST,
        port=TG_PORT,
        scheme=TG_SCHEME,
        url=TG_GET_UPDATES_URL,
        http=http or "200",
        timeout_sec=100,
        sec=poll_sec,
        status="ok",
    )
    msg = update.get("message")
    if not msg:
        other = next((k for k in update if k != "update_id"), "unknown")
        log_request(
            "getUpdates",
            content=f"update_id={update.get('update_id')} type={other}",
            **common,
        )
        return

    chat_id = msg.get("chat", {}).get("id", "")
    username = get_username(msg)
    text = msg.get("text")
    if text is None:
        if msg.get("caption") is not None:
            text = f"<caption> {msg.get('caption')}"
        elif "photo" in msg:
            text = "<photo>"
        elif "voice" in msg:
            text = "<voice>"
        elif "document" in msg:
            text = "<document>"
        else:
            text = "<non-text>"

    tg_date = msg.get("date", "")
    if tg_date:
        try:
            tg_date = datetime.fromtimestamp(int(tg_date), tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            tg_date = str(msg.get("date"))

    content = (
        f"update_id={update.get('update_id')} "
        f"message_id={msg.get('message_id')} "
        f"tg_date={tg_date} | {text}"
    )
    log_request(
        "getUpdates",
        from_user=username,
        chat_id=chat_id,
        content=content,
        **common,
    )


def send_message(
    chat_id,
    text,
    *,
    retries: int = SEND_MESSAGE_RETRIES,
    kind: str = "message",
    from_user: str = "",
) -> bool:
    """Отправляет сообщение. В лог — итог (с номером успешной/последней попытки)."""
    if len(text) > TELEGRAM_MAX_LENGTH:
        text = text[: TELEGRAM_MAX_LENGTH - 3] + "..."

    last_error = ""
    last_http = ""
    started = time.perf_counter()
    used_attempt = 1

    for attempt in range(1, retries + 1):
        used_attempt = attempt
        attempt_started = time.perf_counter()
        try:
            response = requests.post(
                TG_SEND_MESSAGE_URL,
                json={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            last_http = str(response.status_code)
            data = response.json()
            req_body = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False)
            if data.get("ok"):
                log_http_dump(
                    http_method="POST",
                    url=TG_SEND_MESSAGE_URL,
                    request_headers={"Content-Type": "application/json"},
                    request_body=req_body,
                    response=response,
                    sec=round(time.perf_counter() - attempt_started, 3),
                )
                log_request(
                    "sendMessage",
                    host=TG_HOST,
                    port=TG_PORT,
                    scheme=TG_SCHEME,
                    url=TG_SEND_MESSAGE_URL,
                    from_user=from_user,
                    chat_id=chat_id,
                    content=kind,
                    attempt=f"{attempt}/{retries}",
                    status="ok",
                    http=last_http,
                    timeout_sec=10,
                    sec=round(time.perf_counter() - attempt_started, 3),
                )
                return True
            last_error = data.get("description", "Telegram API error")
            log_http_dump(
                http_method="POST",
                url=TG_SEND_MESSAGE_URL,
                request_headers={"Content-Type": "application/json"},
                request_body=req_body,
                response=response,
                error=last_error,
                sec=round(time.perf_counter() - attempt_started, 3),
            )
        except Exception as e:
            last_error = str(e)
            req_body = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False)
            if isinstance(e, requests.HTTPError) and e.response is not None:
                last_http = str(e.response.status_code)
                log_http_dump(
                    http_method="POST",
                    url=TG_SEND_MESSAGE_URL,
                    request_body=req_body,
                    response=e.response,
                    error=last_error,
                    sec=round(time.perf_counter() - attempt_started, 3),
                )
            else:
                log_http_dump(
                    http_method="POST",
                    url=TG_SEND_MESSAGE_URL,
                    request_body=req_body,
                    error=last_error,
                    sec=round(time.perf_counter() - attempt_started, 3),
                )

        if attempt < retries:
            time.sleep(SEND_MESSAGE_RETRY_DELAY)

    log_request(
        "sendMessage",
        host=TG_HOST,
        port=TG_PORT,
        scheme=TG_SCHEME,
        url=TG_SEND_MESSAGE_URL,
        from_user=from_user,
        chat_id=chat_id,
        content=kind,
        attempt=f"{used_attempt}/{retries}",
        status="fail",
        http=last_http,
        timeout_sec=10,
        sec=round(time.perf_counter() - started, 3),
        error=last_error,
    )
    return False


def save_history(chat_id: int, user_text: str, answer: str | None = None) -> None:
    try:
        add_message(chat_id, "user", user_text)
        if answer:
            add_message(chat_id, "assistant", answer)
    except Exception as e:
        logger.error("history_save fail chat=%s error=%s", chat_id, e)


def limit_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> str:
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
    *,
    from_user: str = "",
    chat_id="",
) -> tuple[str | None, float, str, str]:
    """
    Запрос к DeepSeek. Логирует один итог по всем попыткам.
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
    used_attempt = 1

    for attempt in range(1, LLM_RETRIES + 1):
        used_attempt = attempt
        try:
            response = requests.post(
                LLM_URL,
                headers=headers,
                json=payload,
                timeout=LLM_TIMEOUT,
            )
            http_status = str(response.status_code)
            duration = round(time.perf_counter() - started, 3)
            response.raise_for_status()
            data = response.json()
            content = limit_sentences(data["choices"][0]["message"]["content"].strip())
            log_http_dump(
                http_method="POST",
                url=LLM_URL,
                request_headers=headers,
                request_body=json.dumps(payload, ensure_ascii=False),
                response=response,
                sec=duration,
            )
            log_request(
                "chat/completions",
                host=LLM_HOST,
                port=LLM_PORT,
                scheme=LLM_SCHEME,
                url=LLM_URL,
                from_user=from_user,
                chat_id=chat_id,
                content=user_text,
                attempt=f"{attempt}/{LLM_RETRIES}",
                status="ok",
                http=http_status,
                timeout_sec=LLM_TIMEOUT,
                sec=duration,
            )
            return content, duration, http_status, ""
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as e:
            duration = round(time.perf_counter() - started, 3)
            error_text = _redact_secrets(str(e))
            resp = getattr(e, "response", None)
            if resp is not None:
                http_status = str(resp.status_code)
                error_text = _redact_secrets(f"{e} | body={resp.text[:300]}")
            log_http_dump(
                http_method="POST",
                url=LLM_URL,
                request_headers=headers,
                request_body=json.dumps(payload, ensure_ascii=False),
                response=resp if isinstance(resp, requests.Response) else None,
                error=error_text,
                sec=duration,
            )
            if http_status and http_status.startswith("4") and http_status not in ("408", "429"):
                break
            if attempt < LLM_RETRIES:
                time.sleep(LLM_RETRY_DELAY)

    duration = round(time.perf_counter() - started, 3)
    log_request(
        "chat/completions",
        host=LLM_HOST,
        port=LLM_PORT,
        scheme=LLM_SCHEME,
        url=LLM_URL,
        from_user=from_user,
        chat_id=chat_id,
        content=user_text,
        attempt=f"{used_attempt}/{LLM_RETRIES}",
        status="fail",
        http=http_status,
        timeout_sec=LLM_TIMEOUT,
        sec=duration,
        error=error_text,
    )
    return None, duration, http_status, error_text


def handle_message(message):
    chat_id = message["chat"]["id"]
    username = get_username(message)
    text = message.get("text")

    if not text:
        send_message(chat_id, NO_TEXT_HINT, kind="hint_no_text", from_user=username)
        return

    if text in ("/start", "/help"):
        send_message(
            chat_id,
            START_TEXT if text == "/start" else HELP_TEXT,
            kind=f"command:{text}",
            from_user=username,
        )
        return

    if text == "/clear":
        clear_history(chat_id)
        send_message(chat_id, CLEAR_TEXT, kind="command:/clear", from_user=username)
        return

    send_message(
        chat_id,
        SHUFFLING_TEXT,
        retries=SHUFFLING_RETRIES,
        kind="shuffle",
        from_user=username,
    )

    if expire_if_inactive(chat_id):
        send_message(chat_id, EXPIRY_NOTICE, kind="expiry_notice", from_user=username)

    history = get_history(chat_id)
    answer, llm_sec, http_status, error_text = ask_deepseek(
        text,
        history,
        from_user=username,
        chat_id=chat_id,
    )

    if answer:
        sent = send_message(
            chat_id,
            answer + SHARE_FOOTER,
            kind="answer",
            from_user=username,
        )
        if sent:
            save_history(chat_id, text, answer)
        else:
            save_history(chat_id, text)
            send_message(chat_id, ERROR_TAROT, kind="error_tarot", from_user=username)
    else:
        save_history(chat_id, text)
        send_message(chat_id, ERROR_TAROT, kind="error_tarot", from_user=username)


def main():
    setup_logging()
    init_db()
    logger.info(
        "bot_start history=%s logs=%s DEBUG_HTTP=%s",
        DB_PATH,
        LOG_CSV_PATH,
        DEBUG_HTTP,
    )
    last_update_id = 0
    while True:
        updates, poll_sec, poll_http = get_updates(offset=last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            log_incoming_update(update, poll_sec=poll_sec, http=poll_http)
            if "message" in update:
                try:
                    handle_message(update["message"])
                except Exception as e:
                    logger.exception("Ошибка обработки сообщения: %s", e)
        time.sleep(1)


if __name__ == "__main__":
    main()
