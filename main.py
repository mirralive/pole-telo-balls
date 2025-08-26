import os
import json
import logging
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.utils.executor import start_webhook

import gspread
from google.oauth2.service_account import Credentials


# === НАСТРОЙКИ ===
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

WEBHOOK_HOST = os.getenv("WEBHOOK_URL")  # пример: https://<your-subdomain>.onrender.com
if not WEBHOOK_HOST:
    raise RuntimeError("WEBHOOK_URL is not set")

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = int(os.getenv("PORT", "10000"))

# Название Google Sheets (файл в твоём Google Drive)
SHEET_NAME = "ЯЗДЕСЬ"

# Сколько баллов за отметку
POINTS_PER_TAG = 5

# Сам тег (регистр не важен)
TAG_TEXT = "яздесь"  # без решётки, мы её уберём при сравнении


# === ЛОГИ ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("points-bot")


# === ИНИЦИАЛИЗАЦИЯ БОТА ===
bot = Bot(token=API_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# === GOOGLE SHEETS через ENV JSON/BASE64 ===
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

svc_json_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
svc_b64_env = os.getenv("GOOGLE_SERVICE_ACCOUNT_BASE64")

service_account_info = None
if svc_json_env:
    try:
        service_account_info = json.loads(svc_json_env)
    except json.JSONDecodeError:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON содержит некорректный JSON")
elif svc_b64_env:
    import base64
    try:
        decoded = base64.b64decode(svc_b64_env).decode("utf-8")
        service_account_info = json.loads(decoded)
    except Exception as e:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_BASE64 некорректен") from e
else:
    raise RuntimeError("Нужно задать GOOGLE_SERVICE_ACCOUNT_JSON (или GOOGLE_SERVICE_ACCOUNT_BASE64)")

from google.oauth2.service_account import Credentials
creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open(SHEET_NAME).sheet1
if not sheet.row_values(1):
    sheet.append_row(["UserID", "Username", "Points", "LastDate"])

# === УТИЛИТЫ РАБОТЫ С ТАБЛИЦЕЙ ===
def _today_utc_str() -> str:
    # фиксируем день в UTC, чтобы логика была стабильна на сервере
    return datetime.now(timezone.utc).date().isoformat()


def _fetch_records() -> list[dict]:
    # Приводим типы: Points → int, LastDate → str (ISO)
    records = sheet.get_all_records()
    for r in records:
        # gspread вернёт int/float/str — нормализуем
        try:
            r["Points"] = int(r.get("Points", 0))
        except Exception:
            r["Points"] = 0
        # LastDate оставляем строкой
        r["LastDate"] = str(r.get("LastDate", "")).strip()
        r["UserID"] = str(r.get("UserID", "")).strip()
        r["Username"] = str(r.get("Username", "")).strip()
    return records


def _find_user_row(user_id: int) -> tuple[int | None, dict | None]:
    """Найдёт строку пользователя (номер строки и запись). Нумерация строк с 1."""
    uid = str(user_id)
    records = _fetch_records()
    for idx, rec in enumerate(records, start=2):  # с 2, т.к. заголовки в 1
        if rec["UserID"] == uid:
            return idx, rec
    return None, None


def get_balance(user_id: int) -> int:
    _, rec = _find_user_row(user_id)
    return rec["Points"] if rec else 0


def add_points_if_first_today(user: types.User, add_points: int) -> tuple[bool, int]:
    """
    Добавит баллы, если сегодня отметки ещё не было.
    Вернёт (добавлено_ли, новый_баланс).
    """
    today = _today_utc_str()
    row, rec = _find_user_row(user.id)
    if rec:
        if rec["LastDate"] == today:
            # Уже отмечался сегодня
            return False, rec["Points"]
        new_points = rec["Points"] + add_points
        sheet.update_cell(row, 3, new_points)   # Points (колонка 3)
        sheet.update_cell(row, 4, today)        # LastDate (колонка 4)
        return True, new_points
    else:
        # Новая запись
        username = user.username or f"{user.first_name or ''} {user.last_name or ''}".strip() or f"id{user.id}"
        sheet.append_row([str(user.id), username, add_points, today])
        return True, add_points


def get_top(n: int = 10) -> list[dict]:
    recs = _fetch_records()
    recs.sort(key=lambda x: x["Points"], reverse=True)
    return recs[:n]


async def reply_autodel(message: Message, text: str, delay: int = 5):
    """Ответить и удалить ответ бота через delay секунд."""
    sent = await message.reply(text, disable_web_page_preview=True)
    await asyncio.sleep(delay)
    try:
        await sent.delete()
    except Exception:
        pass


def message_has_tag(msg: Message, wanted: str) -> bool:
    """
    Проверяем наличие нужного хештега по entity (надёжнее, чем искать подстроку).
    wanted ожидается без '#', сравниваем в нижнем регистре.
    """
    if not msg.text:
        return False
    low = msg.text.casefold()
    # Быстрый путь: вдруг просто есть как подстрока
    if f"#{wanted}" in low:
        return True

    if msg.entities:
        for ent in msg.entities:
            if ent.type == "hashtag":
                tag = msg.text[ent.offset: ent.offset + ent.length]  # включая '#'
                if tag.lstrip("#").casefold() == wanted:
                    return True
    return False


# === ХЭНДЛЕРЫ ===
@dp.message_handler(commands=["start"])
async def cmd_start(message: Message):
    await message.reply(
        "👋 Привет!\n"
        "Отмечайся хештегом <b>#яздесь</b> один раз в день и получай +5 баллов.\n\n"
        "📊 Команды:\n"
        "• /баланс — ваш текущий баланс (ответ исчезнет через 5 сек)\n"
        "• /топ — общий рейтинг участников\n"
    )


@dp.message_handler(commands=["баланс"])
async def cmd_balance(message: Message):
    balance = get_balance(message.from_user.id)
    await reply_autodel(message, f"📊 Ваш баланс: <b>{balance}</b> баллов")


@dp.message_handler(commands=["топ"])
async def cmd_top(message: Message):
    top = get_top(10)
    if not top:
        await message.reply("Пока нет участников.")
        return

    lines = ["🏆 <b>ТОП участников</b>\n"]
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, u in enumerate(top, start=1):
        name = u["Username"] or f"id{u['UserID']}"
        prefix = medals.get(i, f"{i}.")
        lines.append(f"{prefix} {name} — {u['Points']} баллов")
    await message.reply("\n".join(lines))


@dp.message_handler(lambda m: message_has_tag(m, TAG_TEXT))
async def on_hashtag(message: Message):
    added, new_balance = add_points_if_first_today(message.from_user, POINTS_PER_TAG)
    if added:
        # поздравление (исчезает)
        await reply_autodel(
            message,
            f"🎉 Ура, {message.from_user.first_name}!\n"
            f"Вам начислено <b>+{POINTS_PER_TAG}</b> баллов.\n"
            f"Теперь у вас <b>{new_balance}</b> 💎",
            delay=5,
        )
    else:
        # уже отмечался (исчезает)
        await reply_autodel(
            message,
            "⏳ Сегодня вы уже отмечались! Жду вас завтра 😉",
            delay=5,
        )


# === ЖИЗНЕННЫЙ ЦИКЛ (WEBHOOK) ===
async def on_startup(dp: Dispatcher):
    await bot.delete_webhook()  # на всякий случай очистим старый
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")


async def on_shutdown(dp: Dispatcher):
    logger.info("👋 Shutdown...")
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    await bot.session.close()
    logger.info("👋 Shutdown complete")


if __name__ == "__main__":
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host=WEBAPP_HOST,
        port=WEBAPP_PORT,
    )
