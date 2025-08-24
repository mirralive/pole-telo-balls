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

# Для правильного «сегодня» по твоему часовому поясу (Амстердам: лето 2, зима 1)
TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "0"))

# Паттерны хэштегов
PLUS_ONE_PATTERN = re.compile(r"(?i)(#\s*\+\s*1|#балл(ы)?|#очки|#score|#point|#points)\b")
CHALLENGE_TEXT = "#челлендж1"  # будем искать и как entity, и как голый текст

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

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
            # вставим пользователя, если его ещё нет
            cur.execute("""
                INSERT INTO scores(chat_id, user_id, points, username, full_name)
                VALUES (?, ?, 0, ?, ?)
                ON CONFLICT(chat_id, user_id) DO NOTHING
            """, (chat_id, user.id, user.username or "", f"{user.full_name}"))

            # начислим баллы
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
    """
    Лимит: 1 хэштег в день на человека.
    True — можно начислять (и мы отметим день),
    False — уже был хэштег сегодня.
    """
    today = current_local_date().isoformat()
    with sqlite3.connect(DB_PATH) as conn, closing(conn.cursor()) as cur:
        cur.execute("SELECT day FROM last_tag WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        row = cur.fetchone()
        if row and row[0] == today:
            return False
        # отмечаем сегодняшний день
        cur.execute("""
            INSERT INTO last_tag(chat_id, user_id, day) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET day=excluded.day
        """, (chat_id, user_id, today))
        conn.commit()
        return True

# =========================
#         UTILS
# =========================
def extract_hashtags_from(text: str, entities) -> list:
    """
    Возвращает список хэштегов в нижнем регистре из текста по entities.
    Если entities нет — вернётся пустой список (поэтому ниже мы всегда ещё смотрим «голый текст»).
    """
    tags = []
    if not text or not entities:
        return tags
    for ent in entities:
        if ent.type == "hashtag":
            tag = text[ent.offset: ent.offset + ent.length].lower().strip()
            tags.append(tag)
    return tags

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
    """Удаляем САМО сообщение пользователя с командой (требуются права 'Удалять сообщения')."""
    if message.chat.type in ("group", "supergroup"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
        except Exception:
            pass

# =========================
#        COMMANDS
# =========================
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    text = (
        "👋 Привет! Я считаю баллы по хэштегам в комментариях.\n\n"
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
#     HASHTAGS HANDLER
# =========================
@dp.message_handler(content_types=types.ContentType.ANY)
async def handle_any(message: types.Message):
    # работаем только в группах/супергруппах
    if message.chat.type not in ("group", "supergroup"):
        return
    if (message.from_user and message.from_user.is_bot):
        return

    # Берём и текст, и подпись к медиа
    text = (message.text or message.caption or "").strip()
    if not text:
        return

    text_lc = text.lower()

    # 1) Хэштеги из entities (текст)
    tags_text = extract_hashtags_from(message.text, message.entities)
    # 2) Хэштеги из caption_entities (подпись к фото/видео/док)
    tags_caption = extract_hashtags_from(getattr(message, "caption", None),
                                         getattr(message, "caption_entities", None))
    tags = set(tags_text + tags_caption)  # множество, всё в нижнем регистре

    # ---------- CHALLENGE (+5) ----------
    # триггер, если:
    #  a) в entities/caption_entities есть ровно '#челлендж1'
    #  b) или по «голому тексту» встречается подстрока '#челлендж1'
    is_challenge = (CHALLENGE_TEXT in tags) or (CHALLENGE_TEXT in text_lc)

    if is_challenge:
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=5)
        await reply_autodel(
            message,
            f"🎉 Поздравляю! Вам засчитано <b>+5</b> баллов.\n💰 Ваш баланс: <b>{new_points}</b>"
        )
        return

    # ---------- PLUS ONE (+1) ----------
    is_plus_one = False
    # через entities/caption_entities
    if any(t in ("#балл", "#баллы", "#очки", "#score", "#point", "#points", "#+1") for t in tags):
        is_plus_one = True
    # через «голый текст» (регэксп поддерживает варианты с пробелами: # + 1)
    elif PLUS_ONE_PATTERN.search(text_lc):
        is_plus_one = True

    if is_plus_one:
        if not can_post_tag(message.chat.id, message.from_user.id):
            await reply_autodel(message, "⏳ Сегодня вы уже использовали хэштег. Попробуйте завтра!")
            return
        new_points = add_point(message.chat.id, message.from_user, amount=1)
        # По твоему запросу подтверждение +1 оставляем висеть (не удаляем)
        await message.reply(f"✅ Балл засчитан! Теперь у вас <b>{new_points}</b>.")

# =========================
#        STARTUP
# =========================
async def startup_common():
    me = await bot.get_me()
    logger.info(f"Authorized as @{me.username} (id={me.id})")
    if WEBHOOK_URL:
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
