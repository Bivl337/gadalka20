import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GIGACHAT_CREDENTIALS = os.getenv("GIGACHAT_CREDENTIALS")

if not TELEGRAM_BOT_TOKEN or not GIGACHAT_CREDENTIALS:
    raise ValueError("Задайте TELEGRAM_BOT_TOKEN и GIGACHAT_CREDENTIALS в файле .env")

SYSTEM_PROMPT = "Ты гадалка Таро. Ответь на вопрос пользователя"

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

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


def extract_user_text(message: Message) -> str | None:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    return None


def ask_tarot_sync(question: str) -> str:
    with GigaChat(
        credentials=GIGACHAT_CREDENTIALS,
        verify_ssl_certs=False,
    ) as giga:
        response = giga.chat(
            Chat(
                messages=[
                    Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
                    Messages(role=MessagesRole.USER, content=question),
                ],
            )
        )
    return response.choices[0].message.content or "Карты молчат..."


async def ask_tarot(question: str) -> str:
    return await asyncio.to_thread(ask_tarot_sync, question)


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

    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        answer = await ask_tarot(user_text)
        await message.answer(answer)
    except Exception:
        logger.exception("Ошибка при обращении к GigaChat")
        await message.answer("Связь с картами прервалась. Попробуйте позже.")


@dp.message()
async def handle_unsupported(message: Message) -> None:
    await message.answer("Отправьте текстовый вопрос — карты ответят.")


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
