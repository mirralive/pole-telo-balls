import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types

# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# ---------- Конфиги ----------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "challenge-points")

# Таймзона для "сегодня"
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Amsterdam")

# URL хоста (Render подставляет RENDER_EXTERNAL_URL)
# --- Webhook config ---
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # ЯВНО задаём полную ссылку, если хотим
WEBHOOK_PATH = "/webhook"               # роут остаётся
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

POINTS_PER_TAG = int(os.getenv("POINTS_PER_TAG", "5"))
# Поддержим несколько хештегов, регистр игнорируем
VALID_TAGS = {t.strip().lower() for t in os.getenv("VALID_TAGS", "#яздесь,#челлендж1").split(",")}

# ---------- Google Sheets ----------
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not svc_json_env:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON not set")

try:
    service_account_info = json.loads(svc_json_env)
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
except Exception as e:
    raise RuntimeError(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON: {e}")

gc = gspread.authorize(creds)
try:
    sheet = gc.open(SHEET_NAME).sheet1
except Exception as e:
    raise RuntimeError(f"Cannot open sheet '{SHEET_NAME}': {e}")

HEADERS = ["User_id", "Username", "Name", "Points", "Date"]

def ensure_headers():
    """Гарантируем наличие нужной первой строки с заголовками."""
    try:
        values = sheet.get_all_values()
        if not values:
            sheet.update('1:1', [HEADERS])
            logger.info("Создали заголовки в пустой таблице")
            return
        current = values[0] if values else []
        if current[:len(HEADERS)] != HEADERS:
            sheet.update('1:1', [HEADERS])
            logger.info("Обновили строку заголовков")
    except Exception:
        logger.exception("Не удалось проверить/обновить заголовки")

ensure_headers()

# ---------- Вспомогательные ----------
def today_str() -> str:
    tz = ZoneInfo(LOCAL_TZ)
    return datetime.now(tz).date().isoformat()

def get_user_points(user_id: int) -> int:
    try:
        records = sheet.get_all_records(expected_headers=HEADERS, default_blank="")
        return sum(int(r.get("Points") or 0) for r in records if str(r.get("User_id")) == str(user_id))
    except Exception:
        logger.exception("Ошибка при суммировании баллов")
        return 0

def already_checked_today(user_id: int) -> bool:
    try:
        today = today_str()
        records = sheet.get_all_records(expected_headers=HEADERS, default_blank="")
        for r in records:
            if str(r.get("User_id")) == str(user_id) and str(r.get("Date")) == today:
                return True
        return False
    except Exception:
        logger.exception("Ошибка при проверке отметки за сегодня")
        return False

def human_name(u: types.User) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name

def add_points(user: types.User, points: int):
    row = [
        user.id,
        (user.username or "").strip(),
        human_name(user),
        int(points),
        today_str(),
    ]
    sheet.append_row(row)  # RAW достаточно

# ---------- Bot ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await message.reply("👋 Привет! Отмечайся хештегом #яздесь или #челлендж1. Команда: /баланс")

@dp.message_handler(commands=["баланс", "balance"])
async def cmd_balance(message: types.Message):
    total = get_user_points(message.from_user.id)
    await message.reply(f"Ваш баланс: {total} баллов")

@dp.message_handler(lambda m: bool(m.text))
async def handle_text(message: types.Message):
    text = message.text.lower()
    if any(tag in text for tag in VALID_TAGS):
        user = message.from_user
        if already_checked_today(user.id):
            await message.reply("⚠️ Сегодня вы уже отмечались, баллы не начислены.")
            return
        try:
            add_points(user, POINTS_PER_TAG)
            total = get_user_points(user.id)
            await message.reply(f"✅ Баллы начислены! Ваш баланс: {total}")
        except Exception:
            logger.exception("Ошибка при начислении баллов")
            await message.reply("❌ Не удалось записать баллы. Попробуйте позже.")
    # иначе — молчим, бот реагирует только на хештеги и команды

# ---------- Webhook ----------

async def on_startup(app):
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info(f"Webhook установлен: {WEBHOOK_URL}")
        except Exception:
            logger.exception("Не удалось установить webhook — продолжаем без него")
    else:
        logger.warning("WEBHOOK_URL не задан — запускаемся без установки вебхука")

async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except Exception:
        logger.exception("Не удалось удалить webhook")
    await bot.session.close()
    logger.info("👋 Shutdown complete")


async def handle_webhook(request):
    try:
        data = await request.json()
        update = types.Update(**data)  # ВАЖНО: корректно создать Update для aiogram v2
        await dp.process_update(update)
        return web.Response(status=200)
    except Exception:
        logger.exception("Ошибка при обработке webhook")
        return we
