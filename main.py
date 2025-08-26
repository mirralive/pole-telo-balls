import os
import json
import logging
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types
from aiogram.types.message_entity import MessageEntityType

# ============== ЛОГИ ==============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ============== КОНФИГ ==============
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SHEET_NAME     = os.getenv("GOOGLE_SHEET_NAME", "challenge-points")
LOCAL_TZ       = os.getenv("LOCAL_TZ", "Europe/Amsterdam")

POINTS_PER_TAG = int(os.getenv("POINTS_PER_TAG", "5"))
VALID_TAGS     = {t.strip().lower() for t in os.getenv("VALID_TAGS", "#яздесь,#челлендж1").split(",")}

WEBHOOK_URL    = os.getenv("WEBHOOK_URL")  # Полный URL вебхука (желательно с путём не "/")
WEBAPP_HOST    = "0.0.0.0"
WEBAPP_PORT    = int(os.getenv("PORT", 10000))

AUTODELETE_SECONDS = int(os.getenv("AUTODELETE_SECONDS", "5"))

# ============== GOOGLE SHEETS ==============
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

def ensure_headers_sync():
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

ensure_headers_sync()

# ====== Sheets в отдельном потоке ======
async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

def _today_str() -> str:
    tz = ZoneInfo(LOCAL_TZ)
    return datetime.now(tz).date().isoformat()

def _safe_int(x) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return 0

def _read_records_sync():
    return sheet.get_all_records(expected_headers=HEADERS, default_blank="")

async def read_records():
    t0 = time.time()
    records = await _to_thread(_read_records_sync)
    logger.info(f"[sheets] read_records: {len(records)} rows in {time.time()-t0:.3f}s")
    return records

async def get_user_points(user_id: int) -> int:
    records = await read_records()
    total = sum(
        _safe_int(r.get("Points"))
        for r in records
        if str(r.get("User_id")) == str(user_id)
    )
    logger.info(f"[logic] get_user_points uid={user_id} -> {total}")
    return total

async def already_checked_today(user_id: int) -> bool:
    records = await read_records()
    today = _today_str()
    res = any(str(r.get("User_id")) == str(user_id) and str(r.get("Date")) == today for r in records)
    logger.info(f"[logic] already_checked_today uid={user_id} -> {res}")
    return res

def _append_row_sync(row):
    sheet.append_row(row)

async def add_points(user: types.User, points: int):
    row = [
        user.id,
        (user.username or "").strip(),
        " ".join(p for p in [(user.first_name or ""), (user.last_name or "")] if p).strip(),
        int(points),
        _today_str(),
    ]
    t0 = time.time()
    await _to_thread(_append_row_sync, row)
    logger.info(f"[sheets] append_row for uid={user.id} in {time.time()-t0:.3f}s")

async def get_leaderboard(top_n: int = 10, today_only: bool = False):
    records = await read_records()
    totals, names, usernames = {}, {}, {}
    today = _today_str()
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

# ====== авто-удаление ======
async def auto_delete(bot: Bot, chat_id: int, bot_message_id: int, user_message_id: int | None = None):
    delay = AUTODELETE_SECONDS
    if delay <= 0:
        return
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

# ============== ХЭЛПЕРЫ: чат/юзер/хештеги ==============
def extract_hashtags(msg: types.Message) -> list[str]:
    tags = []
    if not msg or not msg.entities:
        return tags
    text = msg.text or ""
    for e in msg.entities:
        if e.type == MessageEntityType.HASHTAG:
            tag = text[e.offset:e.offset + e.length]
            tags.append(tag.lower())
    return tags

def is_valid_chat(message: types.Message) -> bool:
    return message.chat.type in ("private", "group", "supergroup")

def is_valid_user(user: types.User | None) -> bool:
    if not user or user.is_bot or int(user.id) == 777000:
        return False
    return True

# ============== BOT (aiogram v2) ==============
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot)
Dispatcher.set_current(dp)

async def send_and_autodelete(message: types.Message, text: str, delete_user: bool = False):
    logger.info(f"[send] -> chat={message.chat.id}, text={text[:60]!r}...")
    sent = await message.answer(text)
    asyncio.create_task(auto_delete(bot, message.chat.id, sent.message_id, user_message_id=message.message_id if delete_user else None))
    return sent

@dp.message_handler(commands=["id"])
async def cmd_id(message: types.Message):
    uid = message.from_user.id if message.from_user else None
    await send_and_autodelete(message, f"Ваш user_id: {uid}", delete_user=True)

@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await send_and_autodelete(message, "pong", delete_user=True)

@dp.message_handler(commands=["debug"])
async def cmd_debug(message: types.Message):
    path = (urlparse(WEBHOOK_URL).path or "/tg") if WEBHOOK_URL else "/tg"
    await send_and_autodelete(message, f"✅ Бот жив.\nWEBHOOK_PATH: {path}\nTZ: {LOCAL_TZ}", delete_user=True)

@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await send_and_autodelete(
        message,
        "👋 Привет! Хештеги: #яздесь, #челлендж1.\nКоманды: /баланс, /итоги, /итоги_сегодня, /id",
        delete_user=True
    )

@dp.message_handler(commands=["баланс", "balance", "итого"])
async def cmd_balance(message: types.Message):
    try:
        total = await get_user_points(message.from_user.id)
        await send_and_autodelete(message, f"Ваш баланс: {total} баллов", delete_user=True)
    except Exception:
        logger.exception("cmd_balance failed")
        await send_and_autodelete(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", delete_user=True)

@dp.message_handler(commands=["итоги", "leaders", "топ", "top"])
async def cmd_leaders(message: types.Message):
    try:
        items = await get_leaderboard(top_n=10, today_only=False)
        text = format_leaderboard(items, title="🏆 Итоги (всего), топ-10")
        await send_and_autodelete(message, text, delete_user=True)
    except Exception:
        logger.exception("cmd_leaders failed")
        await send_and_autodelete(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", delete_user=True)

@dp.message_handler(commands=["итоги_сегодня", "leaders_today", "топ_сегодня", "top_today"])
async def cmd_leaders_today(message: types.Message):
    try:
        items = await get_leaderboard(top_n=10, today_only=True)
        text = format_leaderboard(items, title=f"🌞 Итоги за {_today_str()}, топ-10")
        await send_and_autodelete(message, text, delete_user=True)
    except Exception:
        logger.exception("cmd_leaders_today failed")
        await send_and_autodelete(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", delete_user=True)

@dp.message_handler(lambda m: isinstance(m.text, str) and m.text != "")
async def handle_text(message: types.Message):
    try:
        if not is_valid_chat(message):
            logger.info(f"[skip] chat_type={message.chat.type}")
            return
        if not is_valid_user(message.from_user):
            logger.info(f"[skip] invalid user: {message.from_user}")
            return

        tags = extract_hashtags(message)
        logger.info(f"[in] chat={message.chat.id} uid={message.from_user.id} tags={tags}")
        if not tags or not any(tag in VALID_TAGS for tag in tags):
            return

        if await already_checked_today(message.from_user.id):
            await send_and_autodelete(message, "⚠️ Сегодня вы уже отмечались, баллы не начислены.")
            return

        await add_points(message.from_user, POINTS_PER_TAG)
        total = await get_user_points(message.from_user.id)
        await send_and_autodelete(message, "✅ Баллы начислены!")
        await send_and_autodelete(message, f"Ваш баланс: {total} баллов")
        logger.info(f"[ok] points added uid={message.from_user.id} total={total}")

    except Exception:
        logger.exception("handle_text failed")
        await send_and_autodelete(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.")

# ============== WEBHOOK / AIOHTTP ==============
def _path_from_webhook_url(default_path="/tg"):
    if WEBHOOK_URL:
        try:
            parsed = urlparse(WEBHOOK_URL)
            return parsed.path or default_path
        except Exception:
            logger.exception("Не удалось распарсить WEBHOOK_URL; используем дефолтный путь")
    return default_path

WEBHOOK_PATH = _path_from_webhook_url("/tg")

async def on_startup(app):
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info(f"Webhook установлен: {WEBHOOK_URL}")
        except Exception:
            logger.exception("Не удалось установить webhook — продолжаем без него")
    else:
        logger.warning("WEBHOOK_URL не задан — сервер поднят, но Telegram не знает, куда слать апдейты.")

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
        upd_keys = ", ".join(data.keys())
        logger.info(f"[update] keys: {upd_keys}")
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

async def set_webhook(request):
    if not WEBHOOK_URL:
        return web.Response(text="WEBHOOK_URL env is empty", status=400)
    try:
        await bot.set_webhook(WEBHOOK_URL)
        return web.Response(text=f"Webhook set: ok\n{WEBHOOK_URL}")
    except Exception as e:
        logger.exception("set_webhook failed")
        return web.Response(text=f"Webhook set: failed\n{e}", status=500)

app = web.Application()
app.router.add_get("/", healthcheck)
app.router.add_post(WEBHOOK_PATH, handle_webhook)
# зеркальный маршрут с/без слеша
if WEBHOOK_PATH.endswith("/"):
    app.router.add_post(WEBHOOK_PATH.rstrip("/"), handle_webhook)
else:
    app.router.add_post(WEBHOOK_PATH + "/", handle_webhook)
# ручной ребайнд вебхука
app.router.add_get("/set-webhook", set_webhook)
app.router.add_post("/", handle_webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
