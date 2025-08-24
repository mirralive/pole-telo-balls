# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import logging
import asyncio
from datetime import datetime, timedelta, timezone, date
from contextlib import closing
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, executor, types
from aiogram.dispatcher.middlewares import BaseMiddleware

# =========================
#        CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN).")

DB_PATH = os.getenv("DB_PATH", "scores.db")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # если указано — вебхуки; иначе polling
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

# Часовой пояс для "сегодня"
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "0"))

# Паттерны
PLUS_ONE_PATTERN = re.compile(
    r"(?i)(^|\s)#\s*\+\s*1(\s|$)|(^|\s)#балл(ы)?(\s|$)|(^|\s)#очки(\s|$)|(^|\s)#score(\s|$)|(^|\s)#points?(\s|$)"
)
CHALLENGE_CANON = "#челлендж1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# =========================
#   RAW UPDATE LOGGER
# =========================
class UpdateLogger(BaseMiddleware):
    async def on_pre_process_update(self, update: types.Update, data: dict):
        # Выведем, что именно пришло (тип апдейта)
        ut = []
        if update.message: ut.append("message")
        if update.edited_message: ut.append("edited_message")
        if update.channel_post: ut.append("channel_post")
        if update.edited_channel_post: ut.append("edited_channel_post")
        if update.callback_query: ut.append("callback_query")
        if update.inline_query: ut.append("inline_query")
        if update.chosen_inline_result: ut.append("chosen_inline_result")
        if update.shipping_query: ut.append("shipping_query")
        if update.pre_checkout_query: ut.append("pre_checkout_query")
        if update.poll: ut.append("poll")
        if update.poll_answer: ut.append("poll_answer")
        if update.my_chat_member: ut.append("my_chat_member")
        if update.chat_member: ut.append("chat_member")
        if update.chat_join_request: ut.append("chat_join_request")
        logger.info("RAW UPDATE TYPES: %s", ",".join(ut) or "UNKNOWN")

dp.middleware.setup(UpdateLogger())

# =========================
#       TIME HELPERS
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

def add_point(chat_id: int, user: types.User, amount: int = 1) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        with closing(conn.cursor()) as cur:
            cur.execute("""
                INSERT INTO scores(chat_id, user_id, points, username, full_name)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
            """, (chat_id, user.id, user.username or "", f"{user.full_name}"))
            cur.execute("""
                UPDATE scores
                SET points = points + ?, username = ?, full_name = ?
                WHERE chat_id = ? AND user_id = ?
            """, (amount, user.username or "", f"{user.full_name}", chat_id, user.id))
            conn.commit()
            cur.execute("SELECT points FROM scores WHERE chat_id = ? AND user_id = ?", (chat_id, user.id))
            row = cur.fetchone()
            return int(row[0]) if row else 0

def get_points(chat_id: int, user_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT points FROM scores WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        row = cur.fetchone()
        return int(row[0]) if row else 0

def get_top(chat_id: int, limit: int = 10):
    with sqlite3.connect(DB_PATH) as conn, closing(conn.cursor()) as cur:
        cur.execute("""
            SELECT user_id, points, COALESCE(username, ''), COALESCE(full_name, 'Без имени')
            FROM scores
            WHERE chat_id = ?
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (chat_id, limit))
        return cur.fetchall()

def get_total(chat_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT SUM(points) FROM scores WHERE chat_id = ?", (chat_id,))
        row = cur.fetchone()
        return int(row[0]) if row and row[0] else 0

def can_post_tag(chat_id: int, user_id: int) -> bool:
    """Лимит: 1 хэштег в день на человека (в группах/супергруппах)."""
    today = current_local_date().isoformat()
    with sqlite3.connect(DB_PATH) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT day FROM last_tag WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        if row and row[0] == today:
            return False
        cur.execute("""
            INSERT INTO last_tag(chat_id, user_id, day) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET day=excluded.day
        """, (chat_id, user_id, today))
        conn.commit()
        return True

# =========================
#     TEXT NORMALIZATION
# =========================
ZERO_WIDTH = "".join(["\u200b", "\u200c", "\u200d", "\ufeff"])

def clean_text(s: str) -> str:
    """чистим невидимые символы/переводы строк, убираем пробелы после '#', -> lower()."""
    if not s:
        return ""
    s = s.replace("\r", "").replace("\n", " ").strip()
    for ch in ZERO_WIDTH:
        s = s.replace(ch, "")
    s = re.sub(r"#\s+", "#", s)
    return s.lower()

def extract_hashtags_from(text: str, entities) -> list:
    """берём хэштеги из entities/caption_entities и прогоняем через clean_text."""
    tags = []
    if not text or not entities:
        return tags
    for ent in entities:
        if ent.type == "hashtag":
            tag = text[ent.offset: ent.offset + ent.length]
            tags.append(clean_text(tag))
    return tags

# =========================
#         UTILS
# =========================
async def reply_autodel(message: types.Message, text: str, delay: int = 5):
    """Ответ бота, который удалится через delay секунд."""
    sent = await message.reply(text)
    async def _autodelete():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(sent.chat.id, sent.message_id)
        except Exception:
            pass
    asyncio.create_task(_autodelete())

async def delete_user_command_if_group(message: types.Message):
    """Удаляем САМО сообщение пользователя с командой (нужны права «Удалять сообщения»)."""
    if message.chat and message.chat.type in ("group", "supergroup"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

def is_group(message: types.Message) -> bool:
    return message.chat and message.chat.type in ("group", "supergroup")

# =========================
#        COMMANDS
# =========================
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я считаю баллы по хэштегам в чатах.\n\n"
        "📌 Хэштеги:\n"
        "• <b>#челлендж1</b> → <b>+5</b> баллов (ответ бота исчезнет через 5 сек)\n"
        "• <b>#балл</b>, <b>#+1</b> и т.п. → <b>+1</b> балл (ответ остаётся)\n\n"
        "⏳ Лимит: <b>1 хэштег в день</b> на человека.\n\n"
        "🔧 Команды:\n"
        "• /баланс — твой личный счёт\n"
        "• /top — топ-10 участников\n"
        "• /all — общий счёт всех участников\n"
    )
    await reply_autodel(message, text)
    await delete_user_command_if_group(message)

@dp.message_handler(commands=["баланс", "моибаллы", "my", "me"])
async def cmd_balance(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await reply_autodel(message, f"💰 Ваш баланс: <b>{pts}</b> баллов")
    await delete_user_command_if_group(message)

@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    rows = get_top(message.chat.id, limit=10)
    if not rows:
        await reply_autodel(message, "📭 Пока пусто. Начните с <b>#балл</b> или <b>#челлендж1</b>!")
        await delete_user_command_if_group(message)
        return
    lines = ["🏆 <b>Топ-10 этого чата</b>"]
    for i, (user_id, pts, username, full_name) in enumerate(rows, start=1):
        name = f"@{username}" if username else f'<a href="tg://user?id={user_id}">{full_name}</a>'
        lines.append(f"{i}. {name} — <b>{pts}</b> баллов")
    await reply_autodel(message, "\n".join(lines))
    await delete_user_command_if_group(message)

@dp.message_handler(commands=["all", "общий"])
async def cmd_all(message: types.Message):
    total = get_total(message.chat.id)
    await reply_autodel(message, f"🌍 Общий счёт всех участников: <b>{total}</b> баллов")
    await delete_user_command_if_group(message)

# =========================
#   GROUP TEXT HANDLER
# =========================
@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_group_text(message: types.Message):
    if not is_group(message):
        return
    if message.from_user and message.from_user.is_bot:
        return

    raw = message.text or ""
    cleaned = clean_text(raw)
    tags = set(extract_hashtags_from(message.text, message.entities))

    logger.info("DEBUG(group-text) chat=%s user=%s text=%r cleaned=%r tags=%r",
                message.chat.id, message.from_user.id if message.from_user else None, raw, cleaned, tags)

    # CHALLENGE +5
    if (CHALLENGE_CANON in tags) or re.search(r'(?<!\w)#челлендж1(?!\w)', cleaned):
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=5)
        await reply_autodel(message, f"🎉 Поздравляю! Вам засчитано <b>+5</b> баллов.\n💰 Ваш баланс: <b>{new_points}</b>")
        return

    # PLUS ONE +1
    if any(t in ("#балл", "#баллы", "#очки", "#score", "#point", "#points", "#+1") for t in tags) \
       or PLUS_ONE_PATTERN.search(cleaned):
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=1)
        await message.reply(f"✅ Балл засчитан! Теперь у вас <b>{new_points}</b>.")

# =========================
#   GROUP MEDIA (caption)
# =========================
@dp.message_handler(content_types=[
    types.ContentType.PHOTO,
    types.ContentType.VIDEO,
    types.ContentType.ANIMATION,
    types.ContentType.DOCUMENT,
    types.ContentType.AUDIO,
    types.ContentType.VOICE,
    types.ContentType.VIDEO_NOTE,
])
async def handle_group_media(message: types.Message):
    if not is_group(message):
        return
    if message.from_user and message.from_user.is_bot:
        return

    caption = message.caption or ""
    cleaned = clean_text(caption)
    tags = set(extract_hashtags_from(message.caption, message.caption_entities))

    logger.info("DEBUG(group-media) chat=%s user=%s caption=%r cleaned=%r tags=%r",
                message.chat.id, message.from_user.id if message.from_user else None, caption, cleaned, tags)

    # CHALLENGE +5
    if (CHALLENGE_CANON in tags) or re.search(r'(?<!\w)#челлендж1(?!\w)', cleaned):
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=5)
        await reply_autodel(message, f"🎉 Поздравляю! Вам засчитано <b>+5</b> баллов.\n💰 Ваш баланс: <b>{new_points}</b>")
        return

    # PLUS ONE +1
    if any(t in ("#балл", "#баллы", "#очки", "#score", "#point", "#points", "#+1") for t in tags) \
       or PLUS_ONE_PATTERN.search(cleaned):
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=1)
        await message.reply(f"✅ Балл засчитан! Теперь у вас <b>{new_points}</b>.")

# =========================
#   CATCH-ALL DEBUG (group)
# =========================
@dp.message_handler(content_types=types.ContentType.ANY)
async def catch_all_group(message: types.Message):
    """Ловим вообще всё в группе — чтобы увидеть, что доезжает."""
    if not is_group(message):
        return
    logger.info("CATCH-ALL(group) type=%s text=%r caption=%r entities=%r caption_entities=%r",
                message.content_type,
                getattr(message, "text", None),
                getattr(message, "caption", None),
                getattr(message, "entities", None),
                getattr(message, "caption_entities", None))

# =========================
#        STARTUP
# =========================
async def startup_common():
    me = await bot.get_me()
    logger.info(f"Authorized as @{me.username} (id={me.id})")
    if WEBHOOK_URL:
        # ни на что не «фильтруем» allowed_updates — пусть тг шлёт всё
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted. Using polling.")

def main():
    init_db()
    if WEBHOOK_URL:
        parsed = urlparse(WEBHOOK_URL)
        webhook_path = parsed.path or "/webhook"
        executor.start_webhook(
            dispatcher=dp,
            webhook_path=webhook_path,
            on_startup=lambda _: asyncio.get_event_loop().create_task(startup_common()),
            skip_updates=True,
            host=HOST,
            port=PORT,
        )
    else:
        executor.start_polling(
            dp,
            skip_updates=True,
            on_startup=lambda _: asyncio.get_event_loop().create_task(startup_common()),
        )

if __name__ == "__main__":
    main()
