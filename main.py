import os
import json
import asyncio
import logging
from datetime import date

from aiohttp import web
from aiogram import Bot, Dispatcher, types
import gspread
from google.oauth2.service_account import Credentials


# ---------- Логирование ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")


# ---------- Настройки ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")

WEBHOOK_HOST = os.getenv("WEBHOOK_URL")  # например, https://xxx.onrender.com
WEBHOOK_PATH = ""
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

PORT = int(os.getenv("PORT", "10000"))

SHEET_NAME = "challenge-points"
CHALLENGE_POINTS = 5
AUTO_DELETE_SECONDS = 5
TAG_TEXT = "#яздесь"


# ---------- Авторизация Google Sheets ----------
svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
if not svc_json_env:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON не установлен")

try:
    service_account_info = json.loads(svc_json_env)
except Exception as e:
    raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON содержит некорректный JSON") from e

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)

try:
    sheet = gc.open(SHEET_NAME).sheet1
except Exception as e:
    raise RuntimeError(f"Не удалось открыть таблицу {SHEET_NAME}: {e}")


# ---------- Хранение данных в Google Sheets ----------
# Структура листа: user_id | username | balance | last_checkin

def get_user_row(user_id: int):
    records = sheet.get_all_records()
    for idx, row in enumerate(records, start=2):  # строка 1 — заголовки
        if str(row["user_id"]) == str(user_id):
            return idx, row
    return None, None

def get_balance(user_id: int) -> int:
    _, row = get_user_row(user_id)
    if row:
        return int(row.get("balance", 0))
    return 0

def set_balance(user_id: int, username: str, balance: int):
    row_idx, row = get_user_row(user_id)
    if row_idx:
        sheet.update_cell(row_idx, 3, balance)  # столбец "balance"
        sheet.update_cell(row_idx, 2, username)  # обновляем username
    else:
        sheet.append_row([str(user_id), username, balance, ""])

def get_last_checkin_date(user_id: int):
    _, row = get_user_row(user_id)
    if row:
        return row.get("last_checkin") or None
    return None

def set_last_checkin_date(user_id: int, iso_date: str):
    row_idx, row = get_user_row(user_id)
    if row_idx:
        sheet.update_cell(row_idx, 4, iso_date)
    else:
        sheet.append_row([str(user_id), "", 0, iso_date])


# ---------- Хелперы ----------
async def delete_message_later(bot, chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECONDS):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение {message_id}: {e}")

async def reply_autodel(message, text: str, parse_mode="HTML", delay: int = AUTO_DELETE_SECONDS):
    sent = await message.reply(text, parse_mode=parse_mode, disable_web_page_preview=True)
    asyncio.create_task(delete_message_later(message.bot, message.chat.id, sent.message_id, delay))

def today_iso() -> str:
    return date.today().isoformat()

def has_here_tag(message: types.Message) -> bool:
    if not message.text:
        return False
    txt = message.text.strip()

    if message.entities:
        for ent in message.entities:
            if ent.type == "hashtag":
                tag = txt[ent.offset: ent.offset + ent.length]
                if tag.casefold() == TAG_TEXT.casefold():
                    return True

    if TAG_TEXT.casefold() in txt.casefold().split():
        return True

    return False


# ---------- Bot ----------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)


@dp.message_handler(commands=["balance", "баланс"])
@dp.message_handler(lambda m: m.text and m.text.strip().lower() in ("баланс", "#баланс"))
async def cmd_balance(message: types.Message):
    bal = get_balance(message.from_user.id)
    await reply_autodel(message, f"💰 Ваш баланс: <b>{bal}</b> баллов.")


@dp.message_handler(commands=["top", "топ"])
async def cmd_top(message: types.Message):
    records = sheet.get_all_records()
    # сортировка по баллам
    sorted_records = sorted(records, key=lambda r: int(r.get("balance", 0)), reverse=True)
    top5 = sorted_records[:5]

    if not top5:
        await reply_autodel(message, "Пока что таблица пуста.")
        return

    lines = ["🏆 <b>Топ участников</b>:"]
    for idx, row in enumerate(top5, start=1):
        uname = row.get("username") or "Без имени"
        bal = row.get("balance", 0)
        lines.append(f"{idx}. {uname} — <b>{bal}</b>")

    await reply_autodel(message, "\n".join(lines))


@dp.message_handler(content_types=types.ContentTypes.TEXT)
async def on_text(message: types.Message):
    txt = message.text.strip()

    # Хештег #яздесь
    if has_here_tag(message):
        user_id = message.from_user.id
        username = message.from_user.username or f"{message.from_user.first_name}"

        last_iso = get_last_checkin_date(user_id)
        if last_iso == today_iso():
            await reply_autodel(message, "🙌 Вы уже отмечались сегодня. Увидимся завтра!")
            return

        current = get_balance(user_id)
        new_balance = current + CHALLENGE_POINTS
        set_balance(user_id, username, new_balance)
        set_last_checkin_date(user_id, today_iso())

        await reply_autodel(
            message,
            f"🎉 Поздравляю! +{CHALLENGE_POINTS} баллов. Теперь у вас: <b>{new_balance}</b> 🌟"
        )
        return


# ---------- Webhook ----------
async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    logger.info("👋 Shutdown complete")

async def handle_webhook(request):
    data = await request.json()
    update = types.Update.to_object(data)
    await dp.process_update(update)
    return web.Response()

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)


# ---------- Запуск ----------
if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=PORT)
