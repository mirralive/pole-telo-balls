import logging
import os
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.utils.executor import start_webhook

import gspread
from google.oauth2.service_account import Credentials

# --- НАСТРОЙКИ ---
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHEET_NAME = "ЯЗДЕСЬ"

WEBHOOK_HOST = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

# --- ЛОГИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# --- BOT ---
bot = Bot(token=API_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# --- GOOGLE SHEETS ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1  # первая вкладка

# создаём заголовки, если пусто
if not sheet.row_values(1):
    sheet.append_row(["UserID", "Username", "Points", "LastDate"])


# --- УТИЛИТЫ ---
def get_user_row(user_id: int):
    """ищет строку пользователя по user_id"""
    records = sheet.get_all_records()
    for i, r in enumerate(records, start=2):  # первая строка — заголовки
        if str(r["UserID"]) == str(user_id):
            return i, r
    return None, None


def update_points(user: types.User, add_points: int):
    """обновляет баллы пользователя в таблице"""
    today = datetime.utcnow().date()

    row, record = get_user_row(user.id)
    if record:
        last_date = record["LastDate"]
        if str(last_date) == str(today):  # уже играл сегодня
            return False, record["Points"]
        new_points = record["Points"] + add_points
        sheet.update_cell(row, 3, new_points)
        sheet.update_cell(row, 4, str(today))
        return True, new_points
    else:
        sheet.append_row([user.id, user.username or user.full_name, add_points, str(today)])
        return True, add_points


def get_balance(user_id: int):
    _, record = get_user_row(user_id)
    return record["Points"] if record else 0


def get_top(n=10):
    records = sheet.get_all_records()
    sorted_users = sorted(records, key=lambda x: x["Points"], reverse=True)
    return sorted_users[:n]


async def reply_autodel(message: Message, text: str, delay: int = 5):
    sent = await message.reply(text)
    await asyncio.sleep(delay)
    try:
        await sent.delete()
    except Exception:
        pass


# --- ХЭНДЛЕРЫ ---

@dp.message_handler(commands=["start"])
async def cmd_start(message: Message):
    await message.reply("👋 Привет! Пиши <b>#яздесь</b> один раз в день и получай +5 баллов!\n"
                        "Посмотреть свой баланс: /баланс\n"
                        "Топ участников: /топ")


@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: Message):
    balance = get_balance(message.from_user.id)
    await reply_autodel(message, f"📊 Ваш баланс: <b>{balance}</b> баллов")


@dp.message_handler(commands=["топ"])
async def cmd_top(message: Message):
    top_users = get_top()
    if not top_users:
        await message.reply("Пока нет участников.")
        return
    text = "🏆 <b>ТОП участников</b>\n\n"
    for i, user in enumerate(top_users, start=1):
        name = user['Username'] or f"id{user['UserID']}"
        text += f"{i}. {name} — {user['Points']} баллов\n"
    await message.reply(text)


@dp.message_handler(lambda m: m.text and "#яздесь" in m.text.lower())
async def hashtag_handler(message: Message):
    ok, points = update_points(message.from_user, 5)
    if ok:
        await reply_autodel(message,
                            f"🎉 Поздравляю, {message.from_user.first_name}!\n"
                            f"Вам начислено <b>+5</b> баллов.\n"
                            f"Теперь у вас <b>{points}</b> 💎")
    else:
        await reply_autodel(message, "⏳ Сегодня вы уже отмечались! Приходите завтра 😉")


# --- WEBHOOK ---
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook set to {WEBHOOK_URL}")


async def on_shutdown(dp):
    logger.info("👋 Shutdown complete")
    await bot.delete_webhook()
    await bot.session.close()


if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
