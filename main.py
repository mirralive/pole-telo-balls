# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import logging
from contextlib import closing

from aiogram import Bot, Dispatcher, executor, types

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN environment variable with your bot token.")

DB_PATH = os.getenv("DB_PATH", "scores.db")
HASHTAG_PATTERN = re.compile(r"(?i)(#\\s*\\+\\s*1|#балл(ы)?|#очки|#score|#point|#points)\\b")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# --- Database helpers ---
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS scores (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            points  INTEGER NOT NULL DEFAULT 0,
            username TEXT,
            full_name TEXT,
            PRIMARY KEY (chat_id, user_id)
        )
        """)
        conn.commit()

def add_point(chat_id: int, user: types.User) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        with closing(conn.cursor()) as cur:
            cur.execute("""
                INSERT INTO scores(chat_id, user_id, points, username, full_name)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
            """, (chat_id, user.id, user.username or "", f"{user.full_name}"))
            cur.execute("""
                UPDATE scores SET points = points + 1, username = ?, full_name = ?
                WHERE chat_id = ? AND user_id = ?
            """, (user.username or "", f"{user.full_name}", chat_id, user.id))
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
            FROM scores WHERE chat_id = ?
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (chat_id, limit))
        return cur.fetchall()

# --- Handlers ---
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "Привет! Я считаю баллы по хэштегам в комментариях.\n\n"
        "Пиши в комментариях к постам канала/в группе: <b>#балл</b> или <b>#+1</b>, и я добавлю балл.\n\n"
        "Команды:\n"
        "• /моибаллы — показать твои баллы\n"
        "• /top — топ-10 чата\n"
        "• /help — справка\n\n"
        "<i>Важно: меня надо добавить в привязанную к каналу группу-комментарии и отключить Privacy в @BotFather.</i>"
    )
    await message.reply(text)

@dp.message_handler(commands=["моибаллы", "my", "me", "moi", "moibal"])
async def cmd_my(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await message.reply(f"Твои баллы: <b>{pts}</b>")

@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    rows = get_top(message.chat.id, limit=10)
    if not rows:
        await message.reply("Пока пусто. Напиши #балл в этом чате, чтобы начать.")
        return
    lines = ["🏆 <b>Топ этого чата</b>"]
    place = 1
    for user_id, pts, username, full_name in rows:
        name = f"@{username}" if username else f'<a href="tg://user?id={user_id}">{full_name}</a>'
        lines.append(f"{place}. {name} — <b>{pts}</b>")
        place += 1
    await message.reply("\n".join(lines), disable_web_page_preview=True)

@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_text(message: types.Message):
    # Only count in groups/supergroups (i.e., comment groups)
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return

    text = message.text.strip()
    if HASHTAG_PATTERN.search(text):
        new_points = add_point(message.chat.id, message.from_user)
        name = message.from_user.first_name or "Игрок"
        await message.reply(f"✅ Балл засчитан, {name}! Теперь у тебя <b>{new_points}</b>.")

def main():
    init_db()
    logger.info("Starting bot polling...")
    executor.start_polling(dp, skip_updates=True)

if __name__ == "__main__":
    main()
