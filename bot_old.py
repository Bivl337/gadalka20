import os
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN в .env")

def start(update: Update, context):
    update.message.reply_text("🔮 Привет! Я гадалка Таро. Скоро подключу GigaChat.")

def help(update: Update, context):
    update.message.reply_text("Пока просто поздоровайся. Я отвечу.")

def echo(update: Update, context):
    update.message.reply_text("🔮 Пока я только учусь. Скоро смогу гадать!")

def main():
    updater = Updater(token=TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, echo))
    updater.start_polling()
    print("Бот запущен. Нажмите Ctrl+C для остановки.")
    updater.idle()

if __name__ == "__main__":
    main()