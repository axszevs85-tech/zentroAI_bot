import os
import io
import json
import time
import base64
import asyncio
import logging
import sqlite3

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from anthropic import AsyncAnthropic

# =========================
# SOZLAMALAR
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

# Mavjud modellar (tugmalar shu yerdan olinadi)
MODELS = {
    "haiku": ("⚡ Haiku (tez)", "claude-haiku-5-5"),
    "sonnet": ("🧠 Sonnet (aqlli)", "claude-sonnet-5-5"),
    "opus": ("🚀 Opus (eng kuchli)", "claude-opus-5-5"),
}
DEFAULT_MODEL = os.getenv("CLAUDE_MODEL_KEY", "sonnet")

MAX_HISTORY = 20          # eslab qolinadigan xabarlar soni
MAX_TOKENS = 2000
EDIT_INTERVAL = 1.2       # streaming paytida xabarni yangilash oralig'i (soniya)
TG_LIMIT = 4000           # Telegram xabar chegarasi (4096 dan xavfsiz past)

SYSTEM_PROMPT = (
    "You are a helpful, accurate and friendly AI assistant inside a Telegram bot. "
    "Always reply in the same language as the user. "
    "Keep answers clear and well organized. Use plain text (no HTML)."
)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN sozlanmagan!")
if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY sozlanmagan!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

# Bir foydalanuvchi bir vaqtda faqat bitta so'rov yuborsin
user_locks: dict[int, asyncio.Lock] = {}


# =========================
# MA'LUMOTLAR BAZASI (SQLite — bot o'chsa ham saqlanadi)
# =========================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute(
    """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY,
        model TEXT NOT NULL,
        history TEXT NOT NULL DEFAULT '[]'
    )
    """
)
db.commit()


def get_user(uid: int) -> dict:
    row = db.execute("SELECT model, history FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users (id, model, history) VALUES (?, ?, '[]')",
            (uid, DEFAULT_MODEL),
        )
        db.commit()
        return {"model": DEFAULT_MODEL, "history": []}
    model = row[0] if row[0] in MODELS else DEFAULT_MODEL
    return {"model": model, "history": json.loads(row[1])}


def save_user(uid: int, model: str, history: list) -> None:
    db.execute(
        "UPDATE users SET model = ?, history = ? WHERE id = ?",
        (model, json.dumps(history[-MAX_HISTORY:], ensure_ascii=False), uid),
    )
    db.commit()


# =========================
# TUGMALAR
# =========================

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if key == current else "") + label,
                callback_data=f"model:{key}",
            )
        ]
        for key, (label, _) in MODELS.items()
    ]
    rows.append([InlineKeyboardButton(text="🧹 Yangi suhbat", callback_data="new_chat")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def answer_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧹 Yangi suhbat", callback_data="new_chat"),
                InlineKeyboardButton(text="⚙️ Model", callback_data="show_models"),
            ]
        ]
    )


# =========================
# YORDAMCHI FUNKSIYALAR
# =========================

def split_text(text: str, size: int = TG_LIMIT) -> list[str]:
    """Uzun javobni Telegram chegarasiga mos bo'laklarga bo'ladi."""
    chunks = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode=None, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            logger.warning("edit_text xatosi: %s", e)


async def stream_claude(status: Message, model_id: str, history: list, content) -> str:
    """Claude javobini oqim (streaming) bilan oladi va xabarni jonli yangilaydi."""
    buf = ""
    last_edit = 0.0

    async with claude.messages.stream(
        model=model_id,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=history + [{"role": "user", "content": content}],
    ) as stream:
        async for piece in stream.text_stream:
            buf += piece
            now = time.monotonic()
            if now - last_edit >= EDIT_INTERVAL and len(buf) < TG_LIMIT:
                await safe_edit(status, buf + " ▌")
                last_edit = now

    return buf.strip()


async def process(message: Message, content, history_text: str) -> None:
    """Matn ham, rasm ham shu yerdan o'tadi."""
    uid = message.from_user.id
    lock = user_locks.setdefault(uid, asyncio.Lock())

    if lock.locked():
        await message.answer("⏳ Oldingi javob hali tayyorlanmoqda, biroz kuting.")
        return

    async with lock:
        data = get_user(uid)
        model_id = MODELS[data["model"]][1]
        status = await message.answer("⏳ <b>O'ylayapman...</b>")
        await bot.send_chat_action(message.chat.id, "typing")

        try:
            answer = await stream_claude(status, model_id, data["history"], content)
            if not answer:
                answer = "AI bo'sh javob qaytardi."

            data["history"] += [
                {"role": "user", "content": history_text},
                {"role": "assistant", "content": answer},
            ]
            save_user(uid, data["model"], data["history"])

            chunks = split_text(answer)
            for i, chunk in enumerate(chunks):
                markup = answer_keyboard() if i == len(chunks) - 1 else None
                if i == 0:
                    await safe_edit(status, chunk, markup)
                else:
                    await message.answer(chunk, parse_mode=None, reply_markup=markup)

        except Exception:
            logger.exception("Claude javobida xato")
            await safe_edit(
                status,
                "⚠️ Xatolik yuz berdi. API kaliti, model nomi yoki limitni tekshiring.",
            )


# =========================
# BUYRUQLAR
# =========================

@dp.message(CommandStart())
async def start_handler(message: Message):
    data = get_user(message.from_user.id)
    await message.answer(
        "👋 <b>Zamonaviy AI botga xush kelibsiz!</b>\n\n"
        "Men <b>Claude</b> asosida ishlayman:\n"
        "• savollarga javob beraman\n"
        "• matn yozaman va tarjima qilaman\n"
        "• dasturlashda yordam beraman\n"
        "• 📷 rasmlarni tahlil qilaman (rasm yuboring)\n\n"
        "Buyruqlar: /new — yangi suhbat, /model — modelni tanlash\n\n"
        "👇 Modelni tanlang yoki savolingizni yuboring.",
        reply_markup=model_keyboard(data["model"]),
    )


@dp.message(Command("new"))
async def new_command(message: Message):
    data = get_user(message.from_user.id)
    save_user(message.from_user.id, data["model"], [])
    await message.answer("🧹 Suhbat xotirasi tozalandi. Yangi savolingizni yuboring.")


@dp.message(Command("model"))
async def model_command(message: Message):
    data = get_user(message.from_user.id)
    await message.answer("⚙️ Modelni tanlang:", reply_markup=model_keyboard(data["model"]))


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Bu buyruq faqat administrator uchun.")
        return

    total = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    per_model = db.execute("SELECT model, COUNT(*) FROM users GROUP BY model").fetchall()
    lines = "\n".join(f"• {MODELS.get(m, (m,))[0]}: {c}" for m, c in per_model)
    await message.answer(
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Foydalanuvchilar: {total}\n"
        f"{lines}"
    )


# =========================
# TUGMA (CALLBACK) HANDLERLARI
# =========================

@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    uid = callback.from_user.id
    key = callback.data.split(":", 1)[1]

    if key not in MODELS:
        await callback.answer("Noto'g'ri model!", show_alert=True)
        return

    data = get_user(uid)
    save_user(uid, key, data["history"])
    await callback.answer(f"Model: {MODELS[key][0]}")
    if callback.message:
        try:
            await callback.message.edit_reply_markup(reply_markup=model_keyboard(key))
        except TelegramBadRequest:
            pass


@dp.callback_query(F.data == "show_models")
async def show_models(callback: CallbackQuery):
    data = get_user(callback.from_user.id)
    await callback.answer()
    if callback.message:
        await callback.message.answer("⚙️ Modelni tanlang:", reply_markup=model_keyboard(data["model"]))


@dp.callback_query(F.data == "new_chat")
async def new_chat(callback: CallbackQuery):
    uid = callback.from_user.id
    data = get_user(uid)
    save_user(uid, data["model"], [])
    await callback.answer("Yangi suhbat boshlandi!")
    if callback.message:
        await callback.message.answer("🧹 Suhbat xotirasi tozalandi. Yangi savolingizni yuboring.")


# =========================
# XABAR HANDLERLARI
# =========================

@dp.message(F.photo)
async def photo_handler(message: Message):
    """Rasm yuborilsa — Claude uni ko'rib tahlil qiladi."""
    buffer = io.BytesIO()
    await bot.download(message.photo[-1], destination=buffer)
    image_b64 = base64.b64encode(buffer.getvalue()).decode()

    caption = (message.caption or "Bu rasmda nima bor? Batafsil tushuntir.").strip()
    content = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
        },
        {"type": "text", "text": caption},
    ]
    # Tarixda rasmning o'zini saqlamaymiz (hajm katta bo'ladi), faqat matn.
    await process(message, content, f"[Rasm yuborildi] {caption}")


@dp.message(F.text)
async def chat_handler(message: Message):
    prompt = message.text.strip()
    if not prompt or prompt.startswith("/"):
        return
    await process(message, prompt, prompt)


# =========================
# ISHGA TUSHIRISH
# =========================

async def main():
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("AI bot ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
