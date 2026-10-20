import os
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from anthropic import AsyncAnthropic
from google import genai

# =========================
# CONFIGURATION
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL")
GEMINI_MODEL = os.getenv("GEMINI_MODEL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN sozlanmagan!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None
gemini = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# Demo xotira: bot qayta ishga tushganda ma'lumotlar yo'qoladi.
# Doimiy saqlash uchun SQLite yoki PostgreSQL ishlating.
user_data = {}
MAX_HISTORY = 20


def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    model = user_data.get(user_id, {}).get("model", "claude")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Claude" if model == "claude" else "🤖 Claude",
                    callback_data="model:claude",
                ),
                InlineKeyboardButton(
                    text="✅ Gemini" if model == "gemini" else "💎 Gemini",
                    callback_data="model:gemini",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Yangi suhbat",
                    callback_data="new_chat",
                )
            ],
        ]
    )


async def ask_claude(history: list, prompt: str) -> str:
    if not claude:
        raise RuntimeError("Claude API kaliti sozlanmagan.")
    if not CLAUDE_MODEL:
        raise RuntimeError("CLAUDE_MODEL muhit o‘zgaruvchisida sozlanmagan.")
    response = await claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=(
            "You are a helpful, accurate AI assistant. "
            "Reply in the same language as the user."
        ),
        messages=history + [{"role": "user", "content": prompt}],
    )
    return "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )


async def ask_gemini(history: list, prompt: str) -> str:
    if not gemini:
        raise RuntimeError("Gemini API kaliti sozlanmagan.")
    if not GEMINI_MODEL:
        raise RuntimeError("GEMINI_MODEL muhit o‘zgaruvchisida sozlanmagan.")

    conversation = []
    for item in history:
        role = "model" if item["role"] == "assistant" else "user"
        conversation.append({
            "role": role,
            "parts": [{"text": item["content"]}],
        })

    conversation.append({"role": "user", "parts": [{"text": prompt}]})

    response = await asyncio.to_thread(
        gemini.models.generate_content,
        model=GEMINI_MODEL,
        contents=conversation,
        config={
            "system_instruction": (
                "You are a helpful, accurate AI assistant. "
                "Reply in the same language as the user."
            ),
            "max_output_tokens": 2000,
        },
    )
    if not response.text:
        raise RuntimeError("Gemini javob qaytarmadi.")
    return response.text


@dp.message(CommandStart())
async def start_handler(message: Message):
    uid = message.from_user.id
    user_data.setdefault(uid, {"model": "claude", "history": []})

    await message.answer(
        "👋 <b>Zamonaviy AI botga xush kelibsiz!</b>\n\n"
        "Claude va Gemini yordamida savollarga javob beraman, "
        "matn yozaman, tarjima qilaman va dasturlashda yordam beraman.\n\n"
        "👇 AI modelini tanlang va xabaringizni yuboring.",
        reply_markup=main_keyboard(uid),
    )


@dp.callback_query(F.data.startswith("model:"))
async def select_model(callback: CallbackQuery):
    uid = callback.from_user.id
    model = callback.data.split(":", 1)[1]

    if model not in {"claude", "gemini"}:
        await callback.answer("Noto‘g‘ri model!")
        return

    if model == "claude" and not claude:
        await callback.answer("Claude API sozlanmagan!", show_alert=True)
        return
    if model == "gemini" and not gemini:
        await callback.answer("Gemini API sozlanmagan!", show_alert=True)
        return

    user_data.setdefault(uid, {"model": "claude", "history": []})
    user_data[uid]["model"] = model

    await callback.answer("AI modeli almashtirildi!")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=main_keyboard(uid))


@dp.callback_query(F.data == "new_chat")
async def new_chat(callback: CallbackQuery):
    uid = callback.from_user.id
    user_data.setdefault(uid, {"model": "claude", "history": []})
    user_data[uid]["history"] = []

    await callback.answer("Yangi suhbat boshlandi!")
    if callback.message:
        await callback.message.answer(
            "🧹 Suhbat xotirasi tozalandi. Yangi savolingizni yuboring."
        )


@dp.message(Command("admin"))
async def admin_handler(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Bu buyruq faqat administrator uchun.")
        return

    await message.answer(
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👥 Xotirada saqlangan foydalanuvchilar: {len(user_data)}\n"
        f"🤖 Claude: {'Faol' if claude else 'Sozlanmagan'}\n"
        f"💎 Gemini: {'Faol' if gemini else 'Sozlanmagan'}"
    )


@dp.message(F.text)
async def chat_handler(message: Message):
    uid = message.from_user.id
    prompt = message.text.strip()

    if not prompt or prompt.startswith("/"):
        return
    user_data.setdefault(uid, {"model": "claude", "history": []})
    data = user_data[uid]
    history = data["history"]
    model = data["model"]

    status = await message.answer("⏳ <b>AI javob tayyorlamoqda...</b>")

    try:
        if model == "claude":
            answer = await ask_claude(history, prompt)
        else:
            answer = await ask_gemini(history, prompt)

        if not answer:
            answer = "AI bo‘sh javob qaytardi."

        history.extend([
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": answer},
        ])
        data["history"] = history[-MAX_HISTORY:]

        await status.delete()

        # Telegram xabar chegarasidan oshmaslik uchun javobni bo'lamiz.
        chunks = [answer[i:i + 4000] for i in range(0, len(answer), 4000)]
        for chunk in chunks:
            await message.answer(chunk, reply_markup=main_keyboard(uid))

    except Exception:
        logger.exception("AI javobida xato")
        try:
            await status.edit_text(
                "⚠️ <b>Xatolik yuz berdi.</b>\n\n"
                "API kalitlari, model nomi va API limitlarini tekshiring."
            )
        except Exception:
            await message.answer(
                "⚠️ Xatolik yuz berdi. API kalitlari, model nomi va limitlarni tekshiring."
            )


async def main():
    await bot.delete_webhook(drop_pending_updates=False)
    logger.info("AI bot ishga tushmoqda...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
