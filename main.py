import logging
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from collections import defaultdict
from datetime import datetime

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# Токен бота
TOKEN = "YOUR_BOT_TOKEN_HERE"

bot = Bot(token=TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# Хранилище баллов
user_points = defaultdict(int)
# Последний день, когда пользователь отправлял челлендж
last_challenge_day = {}

# Хэндлер для #челлендж1
@dp.message_handler(lambda m: m.text and "челлендж1" in m.text.lower())
async def handle_challenge(message: types.Message):
    logger.info(f"CHAT TYPE: {message.chat.type}")  # смотрим тип чата
    user_id = message.from_user.id
    today = datetime.now().date()

    # Проверка лимита — 1 раз в день
    if last_challenge_day.get(user_id) == today:
        reply = await message.reply("⚠️ Сегодня вы уже использовали #челлендж1. Попробуйте завтра!")
        await asyncio.sleep(5)
        await reply.delete()
        return

    # Засчитываем баллы
    user_points[user_id] += 5
    last_challenge_day[user_id] = today

    reply = await message.reply(
        f"🎉 Поздравляю! Вам начислено <b>+5 баллов</b>!\n"
        f"Теперь у вас: <b>{user_points[user_id]} баллов</b> 🌟"
    )
    await asyncio.sleep(5)
    await reply.delete()

# Хэндлер для /баланс
@dp.message_handler(commands=["баланс"])
async def check_balance(message: types.Message):
    logger.info(f"CHAT TYPE: {message.chat.type}")  # логируем тип
    user_id = message.from_user.id
    balance = user_points[user_id]
    reply = await message.reply(f"💰 Ваш баланс: <b>{balance} баллов</b>")
    await asyncio.sleep(5)
    await reply.delete()
    try:
        await message.delete()  # удаляем сам запрос /баланс
    except Exception as e:
        logger.warning(f"Не удалось удалить запрос пользователя: {e}")

# Команда для администратора — посмотреть всех
@dp.message_handler(commands=["все"])
async def show_all(message: types.Message):
    logger.info(f"CHAT TYPE: {message.chat.type}")
    if not user_points:
        await message.reply("Пока нет начисленных баллов.")
        return
    text = "📊 Общий рейтинг:\n"
    for uid, points in user_points.items():
        text += f"👤 {uid}: {points} баллов\n"
    await message.reply(text)

# Старт
if __name__ == "__main__":
    logger.info("Starting bot polling...")
    executor.start_polling(dp, skip_updates=True)
