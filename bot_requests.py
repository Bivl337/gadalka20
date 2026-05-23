import os
import re
import time

import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AMVERA_API_TOKEN = os.getenv("AMVERA_API_TOKEN")
AMVERA_API_BASE = os.getenv("AMVERA_API_BASE", "https://inference.waw0.amvera.ru").rstrip("/")
AMVERA_MODEL = os.getenv("AMVERA_MODEL", "deepseek-v3")
LLM_TIMEOUT = 15
MAX_SENTENCES = 7

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN в .env")
if not AMVERA_API_TOKEN:
    raise ValueError("Задайте AMVERA_API_TOKEN в .env")

TG_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/"
LLM_URL = f"{AMVERA_API_BASE}/v1/chat/completions"

PROMPT_TEMPLATE = (
    "Ты мистический оракул. Ответь на вопрос пользователя как таролог. "
    "Будь загадочным, но полезным. Ответь с эмодзи и кратким раскладом. И добавь долю шутки или прикола "
    f"Ответь не более чем {MAX_SENTENCES} предложениями.\n\n"
    "Вопрос: {user_text}"
)

SHUFFLING_TEXT = "🃏 Карты тасуются…"
ERROR_TAROT = "🔮 Сейчас тарологу плохо — карты молчат. Попробуйте чуть позже."
NO_TEXT_HINT = "✨ Напишите вопрос обычным текстом — и я сделаю расклад."

START_TEXT = (
    "🔮 Добро пожаловать!\n\n"
    "Я гадалка Таро. Задайте вопрос — и карты подскажут ответ.\n\n"
    "Напишите свой вопрос одним сообщением или введите /help."
)
HELP_TEXT = (
    "📖 Как пользоваться ботом:\n\n"
    "• Напишите вопрос обычным текстом — я отвечу раскладом.\n"
    "• Можно спрашивать о делах, отношениях, планах и выборе.\n"
    "• Команды: /start — приветствие, /help — эта справка.\n\n"
    "Ответы носят развлекательный характер и не заменяют совет специалиста."
)


def get_updates(offset=None):
    """Запрашивает у Telegram новые сообщения."""
    params = {"timeout": 100, "offset": offset}
    try:
        response = requests.get(TG_URL + "getUpdates", params=params, timeout=102)
        response.raise_for_status()
        return response.json().get("result", [])
    except Exception as e:
        print(f"Ошибка при получении обновлений: {e}")
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
        print(f"Ошибка при отправке сообщения: {e}")


def limit_sentences(text: str, max_sentences: int = MAX_SENTENCES) -> str:
    """Обрезает ответ до заданного числа предложений."""
    text = text.strip()
    if not text:
        return text
    parts = re.split(r"(?<=[.!?…])\s+", text)
    if len(parts) <= max_sentences:
        return text
    return " ".join(parts[:max_sentences]).strip()


def ask_deepseek(user_text: str) -> str | None:
    """Запрос к DeepSeek v3 через Amvera Inference API."""
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
    try:
        response = requests.post(
            LLM_URL,
            headers=headers,
            json=payload,
            timeout=LLM_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        return limit_sentences(content.strip())
    except requests.RequestException as e:
        print(f"Ошибка Amvera/DeepSeek: {e}")
        if getattr(e, "response", None) is not None:
            print(e.response.text)
        return None


def handle_message(message):
    """Обрабатывает входящие сообщения."""
    chat_id = message["chat"]["id"]
    text = message.get("text")

    if not text:
        send_message(chat_id, NO_TEXT_HINT)
        return

    if text == "/start":
        send_message(chat_id, START_TEXT)
        return
    if text == "/help":
        send_message(chat_id, HELP_TEXT)
        return

    send_message(chat_id, SHUFFLING_TEXT)
    answer = ask_deepseek(text)
    if answer:
        send_message(chat_id, answer)
    else:
        send_message(chat_id, ERROR_TAROT)


def main():
    print("🚀 Бот запущен!")
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
