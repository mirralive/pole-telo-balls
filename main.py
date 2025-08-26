import os
import json
import logging
from datetime import datetime
from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook

# Логирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# Конфиги
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

SHEET_NAME = "challenge-points"

# Google Sheets
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not svc_json_env:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")

try:
    service_account_info = json.loads(svc_json_env)
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
except Exception as e:
    raise RuntimeError(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {e}")

gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1

HEADERS = ["User_id", "Username", "Name", "Points", "Date"]

def ensure_headers(sheet):
    try:
        current = sheet.row_values(1)
        if current[:len(HEADERS)] != HEADERS:
            sheet.update('1:1', [HEADERS])
            logger.info("Обновили заголовки в таблице")
    except Exception as e:
        logger.exception(f"Не удалось проверить/обновить заголовки: {e}")

ensure_headers(sheet)

# Bot
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://example.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

POINTS_PER_TAG = 5
VALID_TAGS = {"#яздесь", "#челлендж1"}

def get_today():
    return datetime.utcnow().strftime("%Y-%m-%d")

# --- Логика работы с таблицей ---
def get_user_points(user_id):
    records = sheet.get_all_records(expected_headers=HEADERS, default_blank="")
    total = 0
    for r in records:
        if str(r["User_id"]) == str(user_id):
            total += int(r["Points"])
    return total

def already_checked_today(user_id):
    today = get_today()
    records = sheet.get_all_records(expected_headers=HEADERS, default_blank="")
    for r in records:
        if str(r["User_id"]) == str(user_id) and str(r["Date"]) == today:
            return True
    return False

def add_points(user: types.User, points):
    today = get_today()
    name = " ".join(filter(None, [user.first_name, user.last_name])) or ""
    row = [user.id, user.username or "", name, points, today]
    sheet.append_row(row)

# --- Handlers ---
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await message.reply("👋 Привет! Используй хештег #яздесь или команду /баланс.")

@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: types.Message):
    total = get_user_points(message.from_user.id)
    await message.reply(f"Ваш баланс: {total} баллов")

@dp.message_handler(lambda m: m.text and any(tag in m.text for tag in VALID_TAGS))
async def handle_hashtag(message: types.Message):
    user = message.from_user
    if already_checked_today(user.id):
        await message.reply("⚠️ Сегодня вы уже отмечались, баллы не начислены.")
        return
    add_points(user, POINTS_PER_TAG)
    total = get_user_points(user.id)
    await message.reply(f"✅ Баллы начислены! Ваш баланс: {total}")

# --- Webhook ---
async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.session.close()
    logger.info("👋 Shutdown complete")

async def handle_webhook(request):
    try:
        data = await request.json()
        update = types.Update.to_object(data)
        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"Ошибка при обработке webhook: {e}")
    return web.Response()

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
