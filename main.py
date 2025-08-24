import logging
import os
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

# ==============================
# ЛОГИ
# ==============================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# ==============================
# ТОКЕН (берём из переменной окружения, а если нет — используем твой напрямую)
# ==============================
TOKEN = os.getenv("BOT_TOKEN", "8427853720:AAGpXCbp-VHpsRxlTvu2DXgqAD7C2rZ_SSM")

bot = Bot(token=TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# ==============================
# ПРИМЕР: реакция на текст
# ==============================
@dp.message_handler(lambda message: message.text and "#челлендж1" in message.text)
async def handle_challenge(message: types.Message):
    await message.reply("🎉 Засчитано +5 баллов!")

# ==============================
# ПРИМЕР: команда /баланс
# ==============================
@dp.message_handler(commands=["баланс"])
async def handle_balance(message: types.Message):
    await message.reply("Ваш баланс: 0 (тестовая версия)")

# ==============================
# СТАРТ
# ==============================
if __name__ == "__main__":
    logger.info("Starting bot polling...")
    executor.start_polling(dp, skip_updates=True)
