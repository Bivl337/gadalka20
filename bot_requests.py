import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise ValueError("Токен не найден!")

URL = f"https://api.telegram.org/bot{TOKEN}/"

def get_updates(offset=None):
    """Запрашивает у Telegram новые сообщения."""
    url = URL + "getUpdates"
    params = {"timeout": 100, "offset": offset}
    try:
        response = requests.get(url, params=params, timeout=102)
        return response.json().get("result", [])
    except Exception as e:
        print(f"Ошибка при получении обновлений: {e}")
        return []

def send_message(chat_id, text):
    """Отправляет сообщение пользователю."""
    url = URL + "sendMessage"
    params = {"chat_id": chat_id, "text": text}
    try:
        requests.get(url, params=params, timeout=10)
    except Exception as e:
        print(f"Ошибка при отправке сообщения: {e}")

def handle_message(message):
    """Обрабатывает текстовые сообщения."""
    text = message.get("text")
    chat_id = message["chat"]["id"]

    if text == "/start":
        send_message(chat_id, START_TEXT)
    elif text == "/help":
        send_message(chat_id, HELP_TEXT)
    else:
        # А это и есть наша заглушка
        send_message(chat_id, "🔮 Екатерина Дмитриевна, не так быстро.Пока я настраиваю связь с картами. Скоро я смогу гадать! А пока просто угадала Ваше имя! Уже неплохо для начала :)")

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