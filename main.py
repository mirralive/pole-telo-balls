# -*- coding: utf-8 -*-
import os
import re
import json
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone, date

from aiohttp import web
from aiogram import Bot, Dispatcher, types

# =========================
#        CONFIG
# =========================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN is not set in environment!")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
if not WEBHOOK_URL:
    raise SystemExit("❌ WEBHOOK_URL is not set in environment! (e.g. https://<subdomain>.onrender.com/webhook)")

PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

DB_PATH = os.getenv("DB_PATH", "scores.db")
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "0"))  # смещение для "сегодня"

# Правила
CHALLENGE_TAG = "#челлендж1"
CHALLENGE_RE = re.compile(r'(?<!\w)#\s*челлендж1(?!\w)', re.IGNORECASE)

# =========================
#      BOT + DISPATCHER
# =========================
bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# =========================
#         TIME
# =========================
def current_local_date() -> date:
    tz = timezone(timedelta(hours=TZ_OFFSET_HOURS))
    return datetime.now(tz).date()

# =========================
#        DATABASE
# =========================
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            points     INTEGER NOT NULL DEFAULT 0,
            username   TEXT,
            full_name  TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS last_tag (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            day     TEXT NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        conn.commit()

def add_points(chat_id: int, user: types.User, amount: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        with conn:
            conn.execute("""
                INSERT INTO scores(chat_id, user_id, points, username, full_name)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
            """, (chat_id, user.id, user.username or "", user.full_name))
            conn.execute("""
                UPDATE scores
                SET points = points + ?, username = ?, full_name = ?
                WHERE chat_id = ? AND user_id = ?
            """, (amount, user.username or "", user.full_name, chat_id, user.id))
            cur = conn.execute("SELECT points FROM scores WHERE chat_id=? AND user_id=?", (chat_id, user.id))
            row = cur.fetchone()
            return int(row[0]) if row else 0

def get_points(chat_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT points FROM scores WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        return int(row[0]) if row else 0

def get_top(chat_id: int, limit: int = 10):
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            SELECT user_id, points, COALESCE(username,''), COALESCE(full_name,'Без имени')
            FROM scores
            WHERE chat_id=?
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (chat_id, limit))
        return cur.fetchall()

def get_total(chat_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT SUM(points) FROM scores WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] else 0

def can_tag_today(chat_id: int, user_id: int) -> bool:
    """Лимит: 1 хэштег в день на человека (по чату)."""
    today = current_local_date().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT day FROM last_tag WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        if row and row[0] == today:
            return False
        with conn:
            conn.execute("""
                INSERT INTO last_tag(chat_id, user_id, day)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id) DO UPDATE SET day=excluded.day
            """, (chat_id, user_id, today))
        return True

# =========================
#      HELPERS / UI
# =========================
async def reply_autodel(message: types.Message, text: str, delay: int = 5):
    """
    Отправляет ответ бота и удаляет его через delay секунд.
    ВНИМАНИЕ: не используем message.reply() — чтобы не зависеть от aiogram context!
    """
    sent = await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        reply_to_message_id=message.message_id  # визуально как reply
    )
    async def _autodel():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(sent.chat.id, sent.message_id)
        except Exception:
            pass
    asyncio.create_task(_autodel())

async def delete_user_command(message: types.Message):
    """Удаляем САМО сообщение пользователя с командой (нужны права «Удалять сообщения»)."""
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass

def in_group(message: types.Message) -> bool:
    return message.chat and message.chat.type in ("group", "supergroup")

def is_anonymous_admin(msg: types.Message) -> bool:
    # Сообщение «от имени чата»: from_user — бот GroupAnonymousBot, sender_chat — группа/супергруппа
    return (
        msg.from_user is not None
        and msg.from_user.is_bot
        and msg.sender_chat is not None
        and msg.sender_chat.type in ("group", "supergroup")
    )

def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\r", "").replace("\n", " ").strip()
    for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
        s = s.replace(ch, "")
    s = re.sub(r"#\s+", "#", s)
    return s.lower()

def extract_hashtags(text: str, entities):
    if not text or not entities:
        return []
    tags = []
    for ent in entities:
        if ent.type == "hashtag":
            tags.append(clean_text(text[ent.offset: ent.offset + ent.length]))
    return tags

# =========================
#         COMMANDS
# =========================
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я считаю баллы по хэштегам в чате.\n\n"
        "📌 Как это работает:\n"
        "• Напишите <b>#челлендж1</b> — получите <b>+5</b> баллов.\n"
        "  Бот ответит «Поздравляю…» и удалит СВОЙ ответ через 5 секунд. Ваше сообщение остаётся.\n\n"
        "⏳ Лимит: не более <b>1 хэштега в день</b> на человека.\n\n"
        "🔧 Команды (в чате):\n"
        "• /баланс — ваш личный счёт (ответ и ваш запрос удаляются через 5 сек)\n"
        "• /top — топ-10 участников чата (удаляется через 5 сек)\n"
        "• /all — суммарный счёт всех участников (удаляется через 5 сек)\n"
    )
    await reply_autodel(message, text)
    if in_group(message):
        await delete_user_command(message)

@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await reply_autodel(message, f"💰 Ваш баланс: <b>{pts}</b> баллов", delay=5)
    if in_group(message):
        await delete_user_command(message)

@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    rows = get_top(message.chat.id, limit=10)
    if not rows:
        await reply_autodel(message, "📭 Пока пусто. Начните с <b>#челлендж1</b>!")
        if in_group(message):
            await delete_user_command(message)
        return
    lines = ["🏆 <b>Топ-10 этого чата</b>"]
    for i, (user_id, pts, username, full_name) in enumerate(rows, start=1):
        name = f"@{username}" if username else f'<a href="tg://user?id={user_id}">{full_name}</a>'
        lines.append(f"{i}. {name} — <b>{pts}</b> баллов")
    await reply_autodel(message, "\n".join(lines))
    if in_group(message):
        await delete_user_command(message)

@dp.message_handler(commands=["all", "общий"])
async def cmd_all(message: types.Message):
    total = get_total(message.chat.id)
    await reply_autodel(message, f"🌍 Общий счёт всех участников: <b>{total}</b> баллов")
    if in_group(message):
        await delete_user_command(message)

# =========================
#     GROUP TEXT / MEDIA
# =========================
@dp.message_handler(content_types=types.ContentType.TEXT)
async def on_text(message: types.Message):
    if not in_group(message):
        return
    if is_anonymous_admin(message):
        await reply_autodel(
            message,
            "ℹ️ Сообщение отправлено от имени чата.\n"
            "Чтобы получить баллы, напишите хэштег <b>от своего имени</b>.",
            delay=5
        )
        return
    if message.from_user and message.from_user.is_bot:
        return

    raw = message.text or ""
    cleaned = clean_text(raw)
    tags = set(extract_hashtags(message.text, message.entities))
    logger.info("DEBUG(text) chat=%s type=%s cleaned=%r tags=%r",
                message.chat.id, message.chat.type, cleaned, tags)

    is_challenge = (
        (CHALLENGE_TAG in tags) or
        bool(CHALLENGE_RE.search(cleaned)) or
        ("челлендж1" in cleaned and "#" in cleaned)
    )
    if not is_challenge:
        return

    if not can_tag_today(message.chat.id, message.from_user.id):
        await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
        return

    new_total = add_points(message.chat.id, message.from_user, 5)
    await reply_autodel(
        message,
        f"🎉 Поздравляю! Вам засчитано <b>+5</b> баллов!\n"
        f"💰 Ваш баланс: <b>{new_total}</b>",
        delay=5
    )

@dp.message_handler(content_types=[
    types.ContentType.PHOTO,
    types.ContentType.VIDEO,
    types.ContentType.ANIMATION,
    types.ContentType.DOCUMENT,
    types.ContentType.AUDIO,
    types.ContentType.VOICE,
    types.ContentType.VIDEO_NOTE,
])
async def on_media(message: types.Message):
    if not in_group(message):
        return
    if is_anonymous_admin(message):
        await reply_autodel(
            message,
            "ℹ️ Сообщение отправлено от имени чата.\n"
            "Чтобы получить баллы, прикрепите медиа и хэштег <b>от своего имени</b>.",
            delay=5
        )
        return
    if message.from_user and message.from_user.is_bot:
        return

    caption = message.caption or ""
    cleaned = clean_text(caption)
    tags = set(extract_hashtags(message.caption, message.caption_entities))
    logger.info("DEBUG(media) chat=%s type=%s cleaned=%r tags=%r",
                message.chat.id, message.chat.type, cleaned, tags)

    is_challenge = (
        (CHALLENGE_TAG in tags) or
        bool(CHALLENGE_RE.search(cleaned)) or
        ("челлендж1" in cleaned and "#" in cleaned)
    )
    if not is_challenge:
        return

    if not can_tag_today(message.chat.id, message.from_user.id):
        await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
        return

    new_total = add_points(message.chat.id, message.from_user, 5)
    await reply_autodel(
        message,
        f"🎉 Поздравляю! Вам засчитано <b>+5</b> баллов!\n"
        f"💰 Ваш баланс: <b>{new_total}</b>",
        delay=5
    )

# =========================
#  STARTUP / SHUTDOWN
# =========================
async def on_startup(app: web.Application):
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")

    init_db()
    me = await bot.get_me()
    logger.info(f"🤖 Authorized as @{me.username} (id={me.id})")

async def on_shutdown(app: web.Application):
    # 1) снимаем вебхук (после него запроса к API больше не будет)
    try:
        await bot.delete_webhook()
    except Exception:
        pass

    # 2) закрываем storage (если когда-нибудь добавишь FSM)
    try:
        storage = getattr(dp, "storage", None)
        if storage is not None:
            await storage.close()
            await storage.wait_closed()
    except Exception:
        pass

    # 3) корректно закрываем самого бота (внутри закроется ClientSession)
    try:
        await bot.close()          # предпочтительнее, чем напрямую session.close()
    except Exception:
        pass

    # 4) даём event loop «тик», чтобы aiohttp успел закрыть коннекторы
    await asyncio.sleep(0)

    logger.info("👋 Shutdown complete")


# =========================
#  AIOHTTP APP & RAW WEBHOOK
# =========================
async def webhook_handler(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except Exception:
        data = {}
    logger.info("RAW JSON: %s", json.dumps(data, ensure_ascii=False))
    try:
        update = types.Update.to_object(data)
        await dp.process_update(update)
    except Exception as e:
        logger.exception("Failed to process update: %s", e)
    return web.Response(text="ok")

async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")

def create_app() -> web.Application:
    from urllib.parse import urlparse
    parsed = urlparse(WEBHOOK_URL)
    webhook_path = parsed.path or "/webhook"

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    app.router.add_get("/", health)
    app.router.add_post(webhook_path, webhook_handler)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host=HOST, port=PORT)
