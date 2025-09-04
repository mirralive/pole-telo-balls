import os
import json
import logging
import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import web
import gspread
from google.oauth2.service_account import Credentials
from aiogram import Bot, Dispatcher, types
from aiogram.types.message_entity import MessageEntityType
from aiogram.utils import exceptions as aioexc

# ============== ЛОГИ ==============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")
logging.getLogger("aiogram").setLevel(logging.INFO)

# ============== КОНФИГ (POLLING ONLY) ==============
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
SHEET_NAME     = os.getenv("GOOGLE_SHEET_NAME", "challenge-points")
LOCAL_TZ       = os.getenv("LOCAL_TZ", "Europe/Amsterdam")

POINTS_PER_TAG = int(os.getenv("POINTS_PER_TAG", "5"))
VALID_TAGS     = {t.strip().lower() for t in os.getenv("VALID_TAGS", "#яздесь,#челлендж1").split(",")}

WEBAPP_HOST    = "0.0.0.0"
WEBAPP_PORT    = int(os.getenv("PORT", 10000))

# Раздельные автоудаления
AUTODELETE_SECONDS_PRIVATE      = int(os.getenv("AUTODELETE_SECONDS_PRIVATE", "5"))
AUTODELETE_SECONDS_GROUP_REPLY  = int(os.getenv("AUTODELETE_SECONDS_GROUP_REPLY", "20"))
DELETE_USER_COMMAND_IN_GROUPS   = os.getenv("DELETE_USER_COMMAND_IN_GROUPS", "1") == "1"

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

# ====== Sheets helpers ======
async def _to_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)

def _today_str() -> str:
    return datetime.now(ZoneInfo(LOCAL_TZ)).date().isoformat()

def _safe_int(x) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return 0

def _read_records_sync():
    return sheet.get_all_records(expected_headers=HEADERS, default_blank="")

async def read_records():
    t0 = time.time()
    rows = await _to_thread(_read_records_sync)
    logger.info(f"[sheets] read_records: {len(rows)} rows in {time.time()-t0:.3f}s")
    return rows

async def get_user_points(uid: int) -> int:
    recs = await read_records()
    return sum(_safe_int(r.get("Points")) for r in recs if str(r.get("User_id")) == str(uid))

async def already_checked_today(uid: int) -> bool:
    recs = await read_records()
    t = _today_str()
    return any(str(r.get("User_id")) == str(uid) and str(r.get("Date")) == t for r in recs)

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
    await _to_thread(_append_row_sync, row)

async def get_leaderboard(top_n=15, today_only=False):
    recs = await read_records()
    totals, names, usernames = {}, {}, {}
    t = _today_str()
    for r in recs:
        if today_only and str(r.get("Date")) != t:
            continue
        uid = str(r.get("User_id"))
        pts = _safe_int(r.get("Points"))
        totals[uid] = totals.get(uid, 0) + pts
        nm = (r.get("Name") or "").strip()
        un = (r.get("Username") or "").strip()
        if nm: names[uid] = nm
        if un: usernames[uid] = un
    items = []
    for uid, total in totals.items():
        name = names.get(uid) or (("@" + usernames[uid]) if usernames.get(uid) else uid)
        items.append((total, name, usernames.get(uid, ""), uid))
    items.sort(key=lambda x: (-x[0], x[1].lower()))
    return items[:top_n]

def format_leaderboard(items, title):
    if not items:
        return f"{title}\nПока нет данных."
    lines = [title]
    for i, (total, name, username, uid) in enumerate(items, 1):
        handle = f" (@{username})" if username else ""
        lines.append(f"{i}. {name}{handle} — {total}")
    return "\n".join(lines)

# ====== авто-удаление и отправка в тот же тред ======
def _is_group(chat: types.Chat) -> bool:
    return chat.type in ("group", "supergroup")

async def auto_delete(bot: Bot, chat_id: int, bot_mid: int, user_mid: int | None, delay: int, delete_user: bool):
    if delay <= 0:
        return
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, bot_mid)
    except Exception:
        pass
    if delete_user and user_mid:
        try:
            await bot.delete_message(chat_id, user_mid)
        except Exception:
            pass

async def send_autodel(message: types.Message, text: str, is_command: bool = False):
    """
    Отправляем ответ в тот же комментарный тред (message_thread_id) — только если он есть.
    При ошибке 'Message thread not found' шлём без thread_id в общий чат.
    Разные тайминги автоудаления для лички и групп.
    """
    thread_id = getattr(message, "message_thread_id", None)

    kwargs = {"chat_id": message.chat.id, "text": text}
    if thread_id:
        kwargs["message_thread_id"] = thread_id

    try:
        sent = await bot.send_message(**kwargs)
    except aioexc.BadRequest as e:
        if "Message thread not found" in str(e):
            sent = await bot.send_message(chat_id=message.chat.id, text=text)
        else:
            raise

    if _is_group(message.chat):
        delay = AUTODELETE_SECONDS_GROUP_REPLY
        delete_user = DELETE_USER_COMMAND_IN_GROUPS and is_command
    else:
        delay = AUTODELETE_SECONDS_PRIVATE
        delete_user = False  # в личке нельзя удалять сообщения пользователя

    asyncio.create_task(
        auto_delete(
            bot,
            message.chat.id,
            sent.message_id,
            message.message_id if delete_user else None,
            delay,
            delete_user
        )
    )
    return sent

# ====== ХЭЛПЕРЫ: чат/юзер/хештеги ======
def extract_hashtags(msg: types.Message) -> list[str]:
    tags = []
    if not msg or not msg.entities:
        return tags
    text = msg.text or ""
    for e in msg.entities:
        if e.type == MessageEntityType.HASHTAG:
            tags.append(text[e.offset:e.offset+e.length].lower())
    return tags

def is_valid_chat(message: types.Message) -> bool:
    return message.chat.type in ("private", "group", "supergroup")

def is_valid_user(user: types.User | None) -> bool:
    return bool(user) and not user.is_bot and int(user.id) != 777000

# ====== BOT ======
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)
Bot.set_current(bot); Dispatcher.set_current(dp)

@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    await send_autodel(message, "👋 Привет! Хештеги: #яздесь, #челлендж1.\nКоманды: /баланс, /итоги, /итоги_сегодня, /id", True)

@dp.message_handler(commands=["id"])
async def cmd_id(message: types.Message):
    await send_autodel(message, f"Ваш user_id: {message.from_user.id if message.from_user else None}", True)

@dp.message_handler(commands=["ping"])
async def cmd_ping(message: types.Message):
    await send_autodel(message, "pong", True)

@dp.message_handler(commands=["баланс", "balance", "итого"])
async def cmd_balance(message: types.Message):
    try:
        total = await get_user_points(message.from_user.id)
        await send_autodel(message, f"Ваш баланс: {total} баллов", True)
    except Exception:
        logger.exception("cmd_balance failed")
        await send_autodel(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", True)

@dp.message_handler(commands=["итоги", "leaders", "топ", "top"])
async def cmd_leaders(message: types.Message):
    try:
        items = await get_leaderboard(15, today_only=False)
        await send_autodel(message, format_leaderboard(items, "🏆 Итоги (всего), топ-10"), True)
    except Exception:
        logger.exception("cmd_leaders failed")
        await send_autodel(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", True)

@dp.message_handler(commands=["итоги_сегодня", "leaders_today", "топ_сегодня", "top_today"])
async def cmd_leaders_today(message: types.Message):
    try:
        items = await get_leaderboard(15, today_only=True)
        await send_autodel(message, format_leaderboard(items, f"🌞 Итоги за {_today_str()}, топ-10"), True)
    except Exception:
        logger.exception("cmd_leaders_today failed")
        await send_autodel(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.", True)

@dp.message_handler(lambda m: isinstance(m.text, str) and m.text != "")
async def handle_text(message: types.Message):
    if not is_valid_chat(message) or not is_valid_user(message.from_user):
        return
    tags = extract_hashtags(message)
    if not tags or not any(t in VALID_TAGS for t in tags):
        return
    try:
        if await already_checked_today(message.from_user.id):
            await send_autodel(message, "⚠️ Сегодня вы уже отмечались, баллы не начислены.")
            return
        await add_points(message.from_user, POINTS_PER_TAG)
        total = await get_user_points(message.from_user.id)
        await send_autodel(message, "✅ Баллы начислены!")
        await send_autodel(message, f"Ваш баланс: {total} баллов")
    except Exception:
        logger.exception("handle_text failed")
        await send_autodel(message, "⏳ Сервис временно недоступен. Попробуйте ещё раз.")

# ====== HEALTH + DIAG ======
async def healthcheck(request):
    return web.Response(text=f"ok {datetime.utcnow().isoformat()}Z MODE=polling")

async def getme(request):
    try:
        me = await request.app["bot"].get_me()
        return web.json_response({"ok": True, "id": me.id, "username": me.username})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

# ====== START/STOP (SINGLE POLLING) ======
async def on_startup(app):
    # На всякий — снимаем вебхук (если кто-то где-то включил)
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception:
        pass
    # Запускаем поллинг ОДИН раз; без ретраев, чтобы не ловить "Polling already started"
    asyncio.create_task(dp.start_polling())
    logger.info("Started LONG POLLING (webhook disabled).")

async def on_shutdown(app):
    try:
        session = await bot.get_session()
        await session.close()
    except Exception:
        pass
    logger.info("👋 Shutdown complete")

app = web.Application()
app["bot"] = bot
app.router.add_get("/", healthcheck)
app.router.add_get("/diag/getme", getme)

app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)

if __name__ == "__main__":
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)
