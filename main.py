import logging
import os
import json
import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook
from google.oauth2.service_account import Credentials
import gspread

# --- Логирование ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# --- Конфигурация Telegram ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

bot = Bot(token=TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# --- Конфигурация Google Sheets ---
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
if not svc_json_env:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set")

try:
    data = json.loads(svc_json_env)
except json.JSONDecodeError:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON содержит некорректный JSON")

# фиксируем ключ (переводим \\n → \n)
if "private_key" in data and "\\n" in data["private_key"]:
    data["private_key"] = data["private_key"].replace("\\n", "\n")

creds = Credentials.from_service_account_info(data, scopes=SCOPES)
gc = gspread.authorize(creds)

SHEET_NAME = "challenge-points"
try:
    sheet = gc.open(SHEET_NAME).sheet1
except Exception as e:
    raise RuntimeError(f"Не удалось открыть таблицу {SHEET_NAME}: {e}")

# --- Настройки челленджа ---
CHALLENGE_TAG = "#яздесь"
POINTS_PER_DAY = 5

# --- Вспомогательные функции работы с таблицей ---
def get_or_create_user_row(user_id: int, username: str, full_name: str):
    """Находит или создаёт строку для пользователя в таблице"""
    try:
        records = sheet.get_all_records()
    except Exception as e:
        logger.error(f"Ошибка чтения таблицы: {e}")
        return None

    for idx, row in enumerate(records, start=2):  # первая строка — заголовки
        if str(row.get("user_id")) == str(user_id):
            return idx

    # создаём новую строку
    sheet.append_row([str(user_id), username, full_name, 0, ""])  # баллы = 0, последняя дата пустая
    return len(records) + 2

def add_points(user_id: int, username: str, full_name: str):
    row = get_or_create_user_row(user_id, username, full_name)
    if not row:
        return 0, False

    current_date = datetime.utcnow().date().isoformat()
    values = sheet.row_values(row)

    try:
        points = int(values[3]) if len(values) >= 4 else 0
        last_date = values[4] if len(values) >= 5 else ""
    except Exception:
        points, last_date = 0, ""

    if last_date == current_date:
        return points, False  # уже получал сегодня

    points += POINTS_PER_DAY
    sheet.update_cell(row, 4, points)
    sheet.update_cell(row, 5, current_date)
    return points, True

def get_points(user_id: int):
    records = sheet.get_all_records()
    for row in records:
        if str(row.get("user_id")) == str(user_id):
            return int(row.get("points", 0))
    return 0

# --- Вспомогательная функция авто-удаления сообщений ---
async def reply_autodel(message: types.Message, text: str, delay: int = 5):
    sent = await message.reply(text)
    await asyncio.sleep(delay)
    try:
        await sent.delete()
    except Exception:
        pass
    try:
        await message.delete()
    except Exception:
        pass

# --- Хэндлеры ---
@dp.message_handler(lambda m: m.text and m.text.startswith(CHALLENGE_TAG))
async def handle_challenge(message: types.Message):
    user = message.from_user
    points, added = add_points(user.id, user.username or "", f"{user.first_name or ''} {user.last_name or ''}".strip())

    if added:
        text = f"🎉 {user.first_name}, вы получили <b>+{POINTS_PER_DAY} баллов</b>!\n✨ Ваш текущий счёт: <b>{points}</b>"
    else:
        text = f"⚡ {user.first_name}, вы уже отмечались сегодня!\nВаш счёт: <b>{points}</b>"

    await reply_autodel(message, text, delay=5)

@dp.message_handler(commands=["balance"])
async def cmd_balance(message: types.Message):
    user = message.from_user
    points = get_points(user.id)
    text = f"📊 {user.first_name}, у вас <b>{points}</b> баллов"
    await reply_autodel(message, text, delay=5)

# --- Webhook конфигурация ---
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = WEBHOOK_HOST + WEBHOOK_PATH
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "10000"))

async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown(dp):
    logger.info("👋 Shutdown complete")

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
