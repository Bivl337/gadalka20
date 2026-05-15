import asyncio
import logging
import os
import aiohttp
from aiohttp.resolver import AsyncResolver
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.client.session.aiohttp import AiohttpSession
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN в файле .env")

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

dp = Dispatcher()

def extract_user_text(message: Message) -> str | None:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT)

@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)

@dp.message(F.text | F.caption)
async def handle_message(message: Message) -> None:
    user_text = extract_user_text(message)
    if not user_text:
        return
    # Временная заглушка
    await message.answer("🔮 Пока я настраиваю связь с картами. Скоро я смогу гадать!")

@dp.message()
async def handle_unsupported(message: Message) -> None:
    await message.answer("Отправьте текстовый вопрос — карты ответят.")

async def main() -> None:
    global bot
    # Создаём резолвер с DNS-серверами Google и Cloudflare
    resolver = AsyncResolver(nameservers=["8.8.8.8", "1.1.1.1"])
    connector = aiohttp.TCPConnector(resolver=resolver, family=2)  # family=2 принудительно IPv4
    # Создаём клиентскую сессию aiohttp
    client_session = aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=30)
    )
    # Оборачиваем её в AiohttpSession для aiogram
    session = AiohttpSession(session=client_session)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())