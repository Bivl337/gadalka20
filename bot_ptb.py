import os
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

load_dotenv()
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN в .env")

async def start(update: Update, context):
    await update.message.reply_text(
        "🔮 Добро пожаловать!\n\nЯ гадалка Таро. Скоро я научусь отвечать на вопросы, а пока просто поздороваюсь."
    )

async def help(update: Update, context):
    await update.message.reply_text(
        "📖 Пока просто подождите — скоро появится подключение к GigaChat."
    )

async def echo(update: Update, context):
    # Временно отвечаем заглушкой
    await update.message.reply_text("🔮 Карты ещё не настроены, но я уже здесь! Скоро смогу дать ответ.")

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    
    print("🚀 Бот запущен и готов к работе!")
    app.run_polling()

if __name__ == "__main__":
    main()