# -*- coding: utf-8 -*-
import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, executor, types

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
if not TOKEN:
    raise SystemExit("❌ Set TELEGRAM_BOT_TOKEN (or BOT_TOKEN).")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("debug-bot")

bot = Bot(token=TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# --- Utils ---
async def reply_autodel(message: types.Message, text: str, delay: int = 5):
    """Reply and auto-delete bot reply after delay seconds."""
    sent = await message.reply(text)
    async def _autodelete():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(sent.chat.id, sent.message_id)
            print("DEBUG: deleted bot reply")
        except Exception as e:
            print(f"DEBUG: failed to delete bot reply: {e}")
    asyncio.create_task(_autodelete())

async def delete_user_command_if_group(message: types.Message):
    if message.chat.type in ("group", "supergroup"):
        try:
            await bot.delete_message(message.chat.id, message.message_id)
            print("DEBUG: deleted user command")
        except Exception as e:
            print(f"DEBUG: failed to delete user command: {e}")

# --- Commands ---
@dp.message_handler(commands=["start", "help", "баланс"])
async def cmd_balance(message: types.Message):
    text = "Ваш баланс: (отладка, цифр нет)"
    await reply_autodel(message, text)
    await delete_user_command_if_group(message)

# --- Text handler ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def handle_text(message: types.Message):
    text_lc = message.text.lower()
    print(f"DEBUG TEXT: chat={message.chat.id}, from={message.from_user.id}, text='{message.text}', entities={message.entities}")

    if "челлендж1" in text_lc:
        await reply_autodel(message, "🎉 Поймал #челлендж1! (+5 баллов по задумке)")

# --- Catch-all debug ---
@dp.message_handler(content_types=types.ContentType.ANY)
async def debug_all(message: types.Message):
    print(f"DEBUG ANY: type={message.content_type}, chat={message.chat.id}, from={message.from_user.id}, text={getattr(message, 'text', None)}")

# --- Startup ---
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
    if WEBHOOK_URL:
        from aiogram.utils.executor import start_webhook
        from urllib.parse import urlparse
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
