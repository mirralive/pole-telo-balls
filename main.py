import logging
import os
import json
from datetime import datetime

from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook

import gspread
from google.oauth2.service_account import Credentials

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# -------------------
# CONFIG
# -------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан!")

WEBHOOK_PATH = "/webhook"
WEBHOOK_BASE = os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL")
if not WEBHOOK_BASE:
    raise RuntimeError("WEBHOOK_URL или RENDER_EXTERNAL_URL не задан")

WEBHOOK_URL = WEBHOOK_BASE.rstrip("/") + WEBHOOK_PATH + "/"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "10000"))

SHEET_NAME = "challenge-points"

# -------------------
# GOOGLE SHEETS AUTH
# -------------------
svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not svc_json_env:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не задан")

try:
    service_account_info = json.loads(svc_json_env)
except Exception:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON содержит некорректный JSON")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)

try:
    sheet = gc.open(SHEET_NAME).sheet1
except Exception as e:
    raise RuntimeError(f"Не удалось открыть таблицу {SHEET_NAME}: {e}")

# -------------------
# BOT
# -------------------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)


def user_today_key(user_id: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return f"{user_id}:{today}"


def add_points(user_id: int, username: str, points: int = 5):
    """Начисляем очки в таблицу"""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    values = sheet.get_all_records()

    # ищем пользователя
    found_row = None
    for idx, row in enumerate(values, start=2):  # начиная со второй строки
        if str(row.get("user_id")) == str(user_id):
            found_row = idx
            break

    if found_row:
        current_points = int(sheet.cell(found_row, 3).value or 0)
        sheet.update_cell(found_row, 2, username)
        sheet.update_cell(found_row, 3, current_points + points)
        sheet.update_cell(found_row, 4, today)
    else:
        sheet.append_row([str(user_id), username, points, today])


def get_balance(user_id: int) -> int:
    values = sheet.get_all_records()
    for row in values:
        if str(row.get("user_id")) == str(user_id):
            return int(row.get("points", 0))
    return 0


@dp.message_handler(commands=["баланс"])
async def balance_cmd(message: types.Message):
    balance = get_balance(message.from_user.id)
    await message.answer(f"Ваш баланс: {balance} баллов")


@dp.message_handler(lambda msg: msg.text and "#яздесь" in msg.text.lower())
async def handle_hashtag(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    key = user_today_key(user_id)

    # Проверка на повтор
    values = sheet.get_all_records()
    for row in values:
        if str(row.get("user_id")) == str(user_id) and row.get("last_date") == datetime.utcnow().strftime("%Y-%m-%d"):
            await message.answer("Сегодня вы уже отмечались. Баллы не начислены.")
            return

    add_points(user_id, username, points=5)
    await message.answer("✅ Баллы начислены!")


# -------------------
# AIOHTTP WEBHOOK APP
# -------------------
app = web.Application()


async def handle_webhook(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad json", status=400)
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response(text="ok")


async def health(request: web.Request):
    return web.json_response({"ok": True})


# маршруты
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.router.add_post(WEBHOOK_PATH + "/", handle_webhook)
app.router.add_get("/", health)


async def on_startup(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.set_webhook(WEBHOOK_URL)
    logger.info("✅ Webhook set to: %s", WEBHOOK_URL)


async def on_shutdown(app: web.Application):
    await bot.session.close()
    logger.info("👋 Shutdown complete")


app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

# -------------------
# ENTRY
# -------------------
if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
