import os
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types

# ================== ЛОГИ ==================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

# ================== КОНФИГ ==================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

# Google Sheets
SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")  # можно не задавать, если открываем по имени
SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "challenge-points")

# Таймзона для «сегодня»
LOCAL_TZ = os.getenv("LOCAL_TZ", "Europe/Amsterdam")

# Баллы/теги
POINTS_PER_TAG = int(os.getenv("POINTS_PER_TAG", "5"))
VALID_TAGS = {t.strip().lower() for t in os.getenv("VALID_TAGS", "#яздесь,#челлендж1").split(",")}

# Вебхук и сервер
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Можешь задать ЛЮБОЙ полный URL. Мы возьмём путь как есть.
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

# ================== GOOGLE SHEETS ==================
# ТРЕБУЕШЬ DRIVE — ОК: добавляю drive.readonly
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
    if SPREADSHEET_ID:
        sh = gc.open_by_key(SPREADSHEET_ID)
    else:
        # Открываем по имени — требует drive.readonly (что мы и включили)
        sh = gc.open(SHEET_NAME)
    sheet = sh.sheet1
except Exception as e:
    raise RuntimeError(f"Cannot open sheet (id='{SPREADSHEET_ID}' name='{SHEET_NAME}'): {e}")

HEADERS = ["User_id", "Username", "Name", "Points", "Date"]

def ensure_headers():
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

# ================== ВСПОМОГАТЕЛЬНЫЕ ==================
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
    return " ".join(p for p in parts if p).strip()

def add_points(user: types.User, points: int):
    row = [
        user.id,
        (user.username or "").strip(),
        human_name(user),
        int(points),
        today_str(),
    ]
    sheet.append_row(row)

# ================== BOT (aiogram v2) ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ВАЖНО: починим контекст, чтобы message.answer() работал
Bot.set_current(bot)
Dispatcher.set_current(dp)

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
            await message.answer("✅ Баллы начислены!")
            await message.reply(f"Ваш баланс: {total} баллов")
        except Exception:
            logger.exception("Ошибка при начислении баллов")
            await message.reply("❌ Не удалось записать баллы. Попробуйте позже.")

# ================== WEBHOOK / AIOHTTP ==================
def _path_from_webhook_url(default_path="/webhook"):
    """
    Берём путь из WEBHOOK_URL как есть (если задан).
    Если не задан — используем default_path.
    Добавим дубли с/без завершающего слеша для надёжности.
    """
    if WEBHOOK_URL:
        try:
            parsed = urlparse(WEBHOOK_URL)
            path = parsed.path or "/"
            return path
        except Exception:
            logger.exception("Не удалось распарсить WEBHOOK_URL; используем дефолтный путь")
    return default_path

WEBHOOK_PATH = _path_from_webhook_url("/webhook")

async def on_startup(app):
    # мягкая установка вебхука — не валим процесс, если не получилось
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info(f"Webhook установлен: {WEBHOOK_URL}")
        except Exception:
            logger.exception("Не удалось установить webhook — продолжаем без него")
    else:
        logger.warning("WEBHOOK_URL не задан — сервер поднимется, но Telegram не будет знать куда слать апдейты.")

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
        update = types.Update(**data)

        # страховка контекста (на некоторых хостингах это помогает)
        Bot.set_current(bot)
        Dispatcher.set_current(dp)

        await dp.process_update(update)
        return web.Response(status=200)
    except Exception:
        logger.exception("Ошибка при обработке webhook")
        return web.Response(status=200)

async def healthcheck(request):
    return web.Response(text="ok")

app = web.Application()
# Health
app.router.add_get("/", healthcheck)

# Регистрируем роут ровно по твоему пути И дубликат с противоположным слешем на конце
app.router.add_post(WEBHOOK_PATH, handle_webhook)
if WEBHOOK_PATH.endswith("/"):
    app.router.add_post(WEBHOOK_PATH.rstrip("/"), handle_webhook)
else:
    app.router.add_post(WEBHOOK_PATH + "/", handle_webhook)

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
