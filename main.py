import os
import json
import logging
import asyncio
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Полный URL, путь берем как есть
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", 10000))

# ================== GOOGLE SHEETS ==================
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

def _safe_int(x) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return 0

def read_records():
    try:
        return sheet.get_all_records(expected_headers=HEADERS, default_blank="")
    except Exception:
        logger.exception("Не удалось прочитать таблицу")
        return []

def get_user_points(user_id: int) -> int:
    records = read_records()
    return sum(
        _safe_int(r.get("Points"))
        for r in records
        if str(r.get("User_id")) == str(user_id)
    )

def already_checked_today(user_id: int) -> bool:
    records = read_records()
    today = today_str()
    for r in records:
        if str(r.get("User_id")) == str(user_id) and str(r.get("Date")) == today:
            return True
    return False

def human_name(u: types.User) -> str:
    parts = [u.first_name or "", u.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (("@" + u.username) if u.username else str(u.id))

def add_points(user: types.User, points: int):
    row = [
        user.id,
        (user.username or "").strip(),
        human_name(user),
        int(points),
        today_str(),
    ]
    sheet.append_row(row)

def get_leaderboard(top_n: int = 10, today_only: bool = False):
    """Возвращает список кортежей (total, name, username, user_id)."""
    records = read_records()
    totals = {}  # user_id -> total
    names = {}   # user_id -> display name (последняя известная)
    usernames = {}

    today = today_str()
    for r in records:
        if today_only and str(r.get("Date")) != today:
            continue
        uid = str(r.get("User_id"))
        pts = _safe_int(r.get("Points"))
        totals[uid] = totals.get(uid, 0) + pts
        nm = str(r.get("Name") or "").strip()
        un = str(r.get("Username") or "").strip()
        if nm:
            names[uid] = nm
        if un:
            usernames[uid] = un

    items = []
    for uid, total in totals.items():
        name = names.get(uid) or (("@" + usernames[uid]) if usernames.get(uid) else uid)
        items.append((total, name, usernames.get(uid, ""), uid))

    items.sort(key=lambda x: (-x[0], x[1].lower()))
    return items[:top_n]

def format_leaderboard(items, title="🏆 Топ-10"):
    if not items:
        return f"{title}\nПока нет данных."
    lines = [title]
    for idx, (total, name, username, uid) in enumerate(items, start=1):
        handle = f" (@{username})" if username else ""
        lines.append(f"{idx}. {name}{handle} — {total}")
    return "\n".join(lines)

async def auto_delete(bot: Bot, chat_id: int, bot_message_id: int, user_message_id: int | None = None, delay: int = 5):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, bot_message_id)
    except Exception:
        pass
    if user_message_id:
        try:
            await bot.delete_message(chat_id, user_message_id)
        except Exception:
            pass

# ================== BOT (aiogram v2) ==================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot)
Dispatcher.set_current(dp)

@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    sent = await message.answer("👋 Привет! Отмечайся хештегом #яздесь или #челлендж1.\nКоманды: /баланс, /итоги, /итоги_сегодня")
    asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id, user_message_id=message.message_id))

@dp.message_handler(commands=["баланс", "balance", "итого"])
async def cmd_balance(message: types.Message):
    total = get_user_points(message.from_user.id)
    sent = await message.answer(f"Ваш баланс: {total} баллов")
    asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id, user_message_id=message.message_id))

@dp.message_handler(commands=["итоги", "leaders", "топ", "top"])
async def cmd_leaders(message: types.Message):
    items = get_leaderboard(top_n=10, today_only=False)
    text = format_leaderboard(items, title="🏆 Итоги (всего), топ-10")
    sent = await message.answer(text)
    asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id, user_message_id=message.message_id))

@dp.message_handler(commands=["итоги_сегодня", "leaders_today", "топ_сегодня", "top_today"])
async def cmd_leaders_today(message: types.Message):
    items = get_leaderboard(top_n=10, today_only=True)
    text = format_leaderboard(items, title=f"🌞 Итоги за {today_str()}, топ-10")
    sent = await message.answer(text)
    asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id, user_message_id=message.message_id))

@dp.message_handler(lambda m: bool(m.text))
async def handle_text(message: types.Message):
    text = message.text.lower()
    if any(tag in text for tag in VALID_TAGS):
        user = message.from_user
        if already_checked_today(user.id):
            sent = await message.answer("⚠️ Сегодня вы уже отмечались, баллы не начислены.")
            asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id))
            return
        try:
            add_points(user, POINTS_PER_TAG)
            total = get_user_points(user.id)
            sent1 = await message.answer("✅ Баллы начислены!")
            sent2 = await message.answer(f"Ваш баланс: {total} баллов")
            asyncio.create_task(auto_delete(bot, message.chat.id, sent1.message_id))
            asyncio.create_task(auto_delete(bot, message.chat.id, sent2.message_id))
        except Exception:
            logger.exception("Ошибка при начислении баллов")
            sent = await message.answer("❌ Не удалось записать баллы. Попробуйте позже.")
            asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id))
    # иначе молчим

# ================== WEBHOOK / AIOHTTP ==================
def _path_from_webhook_url(default_path="/webhook"):
    if WEBHOOK_URL:
        try:
            parsed = urlparse(WEBHOOK_URL)
            return parsed.path or "/"
        except Exception:
            logger.exception("Не удалось распарсить WEBHOOK_URL; используем дефолтный путь")
    return default_path

WEBHOOK_PATH = _path_from_webhook_url("/webhook")

async def on_startup(app):
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
    try:
        session = await bot.get_session()
        await session.close()
    except Exception:
        pass
    logger.info("👋 Shutdown complete")

async def handle_webhook(request):
    try:
        data = await request.json()
        update = types.Update(**data)
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
app.router.add_get("/", healthcheck)
app.router.add_post(WEBHOOK_PATH, handle_webhook)
if WEBHOOK_PATH.endswith("/"):
    app.router.add_post(WEBHOOK_PATH.rstrip("/"), handle_webhook)
else:
    app.router.add_post(WEBHOOK_PATH + "/", handle_webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
