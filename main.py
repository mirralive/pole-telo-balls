# -*- coding: utf-8 -*-
import os
import re
import sqlite3
import logging
import asyncio
from contextlib import closing

from aiogram import Bot, Dispatcher, executor, types
from aiogram.utils.exceptions import ConflictError

# --- Configuration ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN environment variable with your bot token.")

DB_PATH = os.getenv("DB_PATH", "scores.db")

# Обычные хэштеги для +1
HASHTAG_PATTERN = re.compile(r"(?i)(#\s*\+\s*1|#балл(ы)?|#очки|#score|#point|#points)\b")

# Спец-ключ для челленджа (+5)
CHALLENGE_TAG = "#челлендж1"

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

def add_point(chat_id: int, user: types.User, amount: int = 1) -> int:
    """Прибавляет amount баллов пользователю и возвращает его новый баланс."""
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

# --- Help/commands ---
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "Привет! Я считаю баллы по хэштегам в комментариях (группе обсуждений).\n\n"
        "Как набрать очки:\n"
        "• Напиши: <b>#челлендж1</b> — получишь <b>+5</b> баллов.\n"
        "• Напиши: <b>#балл</b> или <b>#+1</b> — получишь <b>+1</b> балл.\n\n"
        "Команды:\n"
        "• /баланс — показать твой баланс\n"
        "• /моибаллы — то же самое\n"
        "• /top — топ-10 по чату\n\n"
        "<i>Важно: добавь меня в привязанную к каналу группу-комментарии и отключи Privacy в @BotFather.</i>"
    )
    await message.reply(text)

@dp.message_handler(commands=["моибаллы", "my", "me", "moi", "moibal"])
async def cmd_my(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await message.reply(f"Твои баллы: <b>{pts}</b>")

@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: types.Message):
    pts = get_points(message.chat.id, message.from_user.id)
    await message.reply(f"Ваш баланс: <b>{pts}</b>")

@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    rows = get_top(message.chat.id, limit=10)
    if not rows:
        await message.reply("Пока пусто. Напиши #балл или #челлендж1 в этом чате, чтобы начать.")
        return
    lines = ["🏆 <b>Топ этого чата</b>"]
    for i, (user_id, pts, username, full_name) in enumerate(rows, start=1):
        name = f"@{username}" if username else f'<a href="tg://user?id={user_id}">{full_name}</a>'
        lines.append(f"{i}. {name} — <b>{pts}</b>")
    await message.reply("\n".join(lines), disable_web_page_preview=True)

# --- Messages / scoring ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_text(message: types.Message):
    # Считаем только в группах/супергруппах (комментарии)
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.text:
        return
    if message.from_user and message.from_user.is_bot:
        return

    text_lc = message.text.strip().lower()

    # 1) Спец-правило: #челлендж1 = +5
    if CHALLENGE_TAG in text_lc:
        new_points = add_point(message.chat.id, message.from_user, amount=5)
        sent = await message.reply(
            f"✅ Вам засчитано <b>+5</b> баллов! Ваш баланс: <b>{new_points}</b>"
        )
        # авто-удаление ответа бота через 5 секунд
        async def _autodelete():
            await asyncio.sleep(5)
            try:
                await bot.delete_message(sent.chat.id, sent.message_id)
            except Exception:
                pass
        asyncio.create_task(_autodelete())
        return

    # 2) Обычные хэштеги (+1)
    if HASHTAG_PATTERN.search(text_lc):
        new_points = add_point(message.chat.id, message.from_user, amount=1)
        await message.reply(f"✅ Балл засчитан! Теперь у вас <b>{new_points}</b>.")

# --- Startup hook: drop webhook to avoid conflicts with polling ---
async def on_startup(dp: Dispatcher):
    try:
        # снимаем webhook и сбрасываем «висящие» апдейты
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (if any). Switching to polling.")
    except Exception as e:
        logger.warning(f"Couldn't delete webhook: {e}")

def main():
    init_db()
    logger.info("Starting bot polling...")
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
    except ConflictError as e:
        # На всякий случай ловим конфликт и пробуем ещё раз после сброса вебхука
        logger.error(f"Polling conflict: {e}")
        # второй запуск после задержки
        async def restart():
            await asyncio.sleep(2)
            await on_startup(dp)
            executor.start_polling(dp, skip_updates=True, on_startup=on_startup)
        asyncio.get_event_loop().run_until_complete(restart())

if __name__ == "__main__":
    main()
