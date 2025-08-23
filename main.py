# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import logging
import asyncio
from contextlib import closing
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import ConflictError

# --- Config ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN).")

DB_PATH = os.getenv("DB_PATH", "scores.db")

# Webhook config (optional). If WEBHOOK_URL is set -> webhook mode
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))  # Render sets PORT automatically

# Normal +1 hashtags
HASHTAG_PATTERN = re.compile(r"(?i)(#\s*\+\s*1|#балл(ы)?|#очки|#score|#point|#points)\b")
# Challenge +5
CHALLENGE_TAG = "#челлендж1"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# --- DB helpers ---
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

def add_point(chat_id: int, user: types.User, amount: int = 1) -> int:
    if amount == 0:
        return get_points(chat_id, user.id)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        with closing(conn.cursor()) as cur:
            cur.execute("""
                INSERT INTO scores(chat_id, user_id, points, username, full_name)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
            """, (chat_id, user.id, user.username or "", f"{user.full_name}"))
            cur.execute("""
                UPDATE scores SET points = points + ?, username = ?, full_name = ?
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
            FROM scores WHERE chat_id = ?
            ORDER BY points DESC, user_id ASC
            LIMIT ?
        """, (chat_id, limit))
        return cur.fetchall()

# --- Commands ---
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "Привет! Я считаю баллы по хэштегам в комментариях (группа обсуждений).\n\n"
        "Как набрать очки:\n"
        "• <b>#челлендж1</b> — <b>+5</b> баллов.\n"
        "• <b>#балл</b> или <b>#+1</b> — <b>+1</b> балл.\n\n"
        "Команды:\n"
        "• /баланс или /моибаллы — твой баланс\n"
        "• /top — топ-10 по чату\n"
    )
    await message.reply(text)

@dp.message_handler(commands=["моибаллы", "my", "me", "moi", "moibal", "баланс"])
async def cmd_balance(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await message.reply(f"Ваш баланс: <b>{pts}</b>")

@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    rows = get_top(message.chat.id, limit=10)
    if not rows:
        await message.reply("Пока пусто. Напишите #балл или #челлендж1 в этом чате, чтобы начать.")
        return
    lines = ["🏆 <b>Топ этого чата</b>"]
    for i, (user_id, pts, username, full_name) in enumerate(rows, start=1):
        name = f"@{username}" if username else f'<a href="tg://user?id={user_id}">{full_name}</a>'
        lines.append(f"{i}. {name} — <b>{pts}</b>")
    await message.reply("\n".join(lines), disable_web_page_preview=True)

# --- Messages / scoring ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_text(message: types.Message):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.text or (message.from_user and message.from_user.is_bot):
        return
    text_lc = message.text.strip().lower()

    if "#челлендж1" in text_lc:
        new_points = add_point(message.chat.id, message.from_user, amount=5)
        sent = await message.reply(f"✅ Вам засчитано <b>+5</b> баллов! Ваш баланс: <b>{new_points}</b>")
        async def _autodelete():
            await asyncio.sleep(5)
            try:
                await bot.delete_message(sent.chat.id, sent.message_id)
            except Exception:
                pass
        asyncio.create_task(_autodelete())
        return

    if HASHTAG_PATTERN.search(text_lc):
        new_points = add_point(message.chat.id, message.from_user, amount=1)
        await message.reply(f"✅ Балл засчитан! Теперь у вас <b>{new_points}</b>.")

# --- Startup hooks ---
async def startup_common():
    # Проверим токен и снимем/поставим вебхук по режиму
    me = await bot.get_me()
    logger.info(f"Authorized as @{me.username} (id={me.id})")
    if WEBHOOK_URL:
        # Webhook mode
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True, allowed_updates=["message"])
        logger.info(f"Webhook set to {WEBHOOK_URL}")
    else:
        # Polling mode
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted. Using polling.")

def main():
    init_db()
    if WEBHOOK_URL:
        # --- Webhook mode ---
        parsed = urlparse(WEBHOOK_URL)
        webhook_path = parsed.path or "/webhook"
        logger.info(f"Starting webhook server on {HOST}:{PORT}, path={webhook_path}")
        executor.start_webhook(
            dispatcher=dp,
            webhook_path=webhook_path,
            on_startup=lambda _: asyncio.get_event_loop().create_task(startup_common()),
            skip_updates=True,
            host=HOST,
            port=PORT,
        )
    else:
        # --- Polling mode ---
        logger.info("Starting bot polling...")
        try:
            executor.start_polling(dp, skip_updates=True, on_startup=lambda _: asyncio.get_event_loop().create_task(startup_common()))
        except ConflictError as e:
            logger.error(f"Polling conflict: {e}")
            # Если всё же конфликт — предложим явный переход на webhook
            raise

if __name__ == "__main__":
    main()
