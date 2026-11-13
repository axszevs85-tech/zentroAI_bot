import os
import html
import sqlite3
import logging
from datetime import date
from urllib.parse import quote

import aiohttp
from aiohttp import web
from anthropic import AsyncAnthropic
from telegram import (
    Update,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as M,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("zentroai")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MODEL = os.getenv("MODEL", "claude-haiku-5-5")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "zentroai.db")
FREE_LIMIT = 2
AI_DAILY_LIMIT = int(os.getenv("AI_DAILY_LIMIT", "30"))

WEBAPP_URL = os.getenv("WEBAPP_URL", "")
if not WEBAPP_URL and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
    WEBAPP_URL = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]

claude = AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SERVICES = {
    "tags": ("🏷 Teglar", "Video mavzusini yozing (masalan: «Toshkentda arzon ovqat»)."),
    "desc": ("📝 Tavsif", "Video mavzusi va asosiy fikrlarini yozing."),
    "logo": ("🎨 Logo", "Kanal nomi va yo'nalishini yozing (masalan: «Texno Uz, telefon sharhlari»)."),
    "banner": ("🖼 Banner", "Kanal nomi va yo'nalishini yozing (masalan: «Texno Uz, telefon sharhlari»)."),
}

SYSTEMS = {
    "tags": "Sen YouTube SEO mutaxassisisan. Berilgan mavzu uchun 15 ta eng yaxshi tegni "
            "o'zbek tilida, vergul bilan ajratib, faqat teglarning o'zini yoz.",
    "desc": "Sen YouTube SEO mutaxassisisan. Berilgan mavzu uchun o'zbek tilida jozibali "
            "video tavsifi yoz: 2-3 gaplik kirish, qisqa ro'yxat, obuna chaqirig'i va 5 ta heshteg.",
}


# ---------- DB ----------
def db(sql, args=(), fetch=False):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute(sql, args)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()


db("CREATE TABLE IF NOT EXISTS usage(user_id INTEGER, service TEXT, n INTEGER DEFAULT 0, "
   "PRIMARY KEY(user_id, service))")
db("CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT, owner_id INTEGER)")
db("CREATE TABLE IF NOT EXISTS approved(chat_id INTEGER, user_id INTEGER)")
db("CREATE TABLE IF NOT EXISTS ai_daily(user_id INTEGER, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))")


def used(uid, svc):
    r = db("SELECT n FROM usage WHERE user_id=? AND service=?", (uid, svc), True)
    return r[0][0] if r else 0


def left(uid, svc):
    return max(0, FREE_LIMIT - used(uid, svc))


def add_use(uid, svc):
    db("INSERT INTO usage(user_id, service, n) VALUES(?,?,1) "
       "ON CONFLICT(user_id, service) DO UPDATE SET n = n + 1", (uid, svc))


# ---------- Tugmalar ----------
def main_menu():
    rows = [
        [B("🤖 AI suhbat", callback_data="ai", style="primary")],
        [B("🏷 Teglar", callback_data="svc:tags", style="primary"),
         B("📝 Tavsif", callback_data="svc:desc", style="primary")],
        [B("🎨 Logo", callback_data="svc:logo", style="primary"),
         B("🖼 Banner", callback_data="svc:banner", style="primary")],
        [B("✅ Avto zayavka", callback_data="zay", style="success")],
    ]
    if WEBAPP_URL.startswith("https://"):
        rows.append([B("📱 Mini App", web_app=WebAppInfo(url=WEBAPP_URL))])
    rows.append([B("🎁 Tekin xizmatlarim", callback_data="free")])
    return M(rows)


def back_menu(extra=None):
    rows = list(extra or [])
    rows.append([B("⬅️ Asosiy menyu", callback_data="menu", style="danger")])
    return M(rows)


MAIN_TEXT = "🏠 <b>Asosiy menyu</b>\nKerakli xizmatni tanlang 👇"


# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("svc", None)
    await update.message.reply_text(
        "👋 Salom! Men <b>ZentroAI</b> — aqlli AI yordamchiman, shuningdek kanalingiz uchun teg, tavsif, logo va banner yasayman.\n"
        "Har xizmat 2 marta tekin 🎁\n\n" + MAIN_TEXT,
        parse_mode="HTML", reply_markup=main_menu())


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "menu":
        context.user_data.pop("svc", None)
        await q.edit_message_text(MAIN_TEXT, parse_mode="HTML", reply_markup=main_menu())

    elif data == "ai":
        context.user_data["svc"] = "ai"
        await q.edit_message_text(
            "🤖 <b>AI suhbat</b> yoqildi.\nIstalgan savolingizni yozing ✍️",
            parse_mode="HTML",
            reply_markup=back_menu([[B("🧹 Yangi suhbat", callback_data="ainew")]]))

    elif data == "ainew":
        context.user_data["hist"] = []
        await q.edit_message_text(
            "🧹 Suhbat tozalandi. Yangi savolingizni yozing ✍️",
            reply_markup=back_menu())

    elif data.startswith("svc:"):
        svc = data.split(":")[1]
        name, prompt = SERVICES[svc]
        if left(uid, svc) <= 0:
            await q.edit_message_text(
                f"{name}: tekin limit tugadi 😔\nBoshqa xizmatlardan foydalaning.",
                reply_markup=back_menu())
            return
        context.user_data["svc"] = svc
        await q.edit_message_text(
            f"{name}\nQolgan tekin urinish: {left(uid, svc)}\n\n✍️ {prompt}",
            reply_markup=back_menu())

    elif data == "free":
        lines = [f"{name}: {left(uid, s)}/{FREE_LIMIT}" for s, (name, _) in SERVICES.items()]
        await q.edit_message_text("🎁 <b>Tekin xizmatlarim</b>\n\n" + "\n".join(lines),
                                  parse_mode="HTML", reply_markup=back_menu())

    elif data == "zay":
        text = (
            "✅ <b>Avto zayavka</b>\n\n"
            "Kanal/guruhingizga so'rov bilan kirmoqchi bo'lganlarni bot avtomatik qabul qiladi "
            "va ularga salom xabari yuboradi.\n\n"
            "<b>Yoqish:</b>\n"
            "1. Meni kanal/guruhga admin qiling («Foydalanuvchilarni taklif qilish» huquqi bilan).\n"
            "2. Taklif havolasini «So'rov bilan qo'shilish» rejimida yarating.\n"
            "3. Tayyor! Har kim so'rov yuborsa, avtomatik qabul qilinadi."
        )
        await q.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=back_menu([[B("📋 Mening kanallarim", callback_data="mych", style="success")]]))

    elif data == "mych":
        rows = db("SELECT chat_id, title FROM chats WHERE owner_id=?", (uid,), True)
        if not rows:
            text = "Hali kanal ulanmagan. Meni kanalga admin qiling 👆"
        else:
            lines = []
            for cid, title in rows:
                n = db("SELECT COUNT(*) FROM approved WHERE chat_id=?", (cid,), True)[0][0]
                lines.append(f"• {html.escape(title or str(cid))} — {n} ta qabul qilindi")
            text = "📋 <b>Mening kanallarim</b>\n\n" + "\n".join(lines)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=back_menu())


async def ask_claude(system, prompt, max_tokens=900):
    r = await claude.messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}])
    return r.content[0].text.strip()


async def make_image(svc, user_text):
    kind = "YouTube channel logo, round emblem" if svc == "logo" else "YouTube channel banner, wide cinematic"
    eng = await ask_claude(
        "Translate and expand the user's idea into ONE short English image-generation prompt "
        "(max 40 words). Output only the prompt. No text/letters in the image.",
        f"{kind}. Idea: {user_text}", max_tokens=150)
    w, h = (1024, 1024) if svc == "logo" else (1600, 900)
    url = f"https://image.pollinations.ai/prompt/{quote(eng)}?width={w}&height={h}&nologo=true"
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


AI_SYSTEM = (
    "Sen ZentroAI — do'stona va aqlli AI yordamchisan. Foydalanuvchi qaysi tilda yozsa, "
    "shu tilda (asosan o'zbek tilida) aniq va tushunarli javob ber."
)


async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    today = date.today().isoformat()
    r = db("SELECT n FROM ai_daily WHERE user_id=? AND day=?", (uid, today), True)
    if (r[0][0] if r else 0) >= AI_DAILY_LIMIT:
        await update.message.reply_text(
            f"Bugungi AI limiti ({AI_DAILY_LIMIT} ta xabar) tugadi. Ertaga qaytib keling 🙂",
            reply_markup=back_menu())
        return
    hist = context.user_data.setdefault("hist", [])
    hist.append({"role": "user", "content": update.message.text})
    del hist[:-12]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    await update.message.chat.send_action("typing")
    try:
        resp = await claude.messages.create(
            model=MODEL, max_tokens=1000, system=AI_SYSTEM, messages=hist)
        answer = resp.content[0].text.strip()
    except Exception:
        log.exception("AI xatosi")
        hist.pop()
        await update.message.reply_text("❌ Xatolik yuz berdi, birozdan keyin urinib ko'ring.")
        return
    hist.append({"role": "assistant", "content": answer})
    db("INSERT INTO ai_daily(user_id, day, n) VALUES(?,?,1) "
       "ON CONFLICT(user_id, day) DO UPDATE SET n = n + 1", (uid, today))
    await update.message.reply_text(
        answer[:4000],
        reply_markup=back_menu([[B("🧹 Yangi suhbat", callback_data="ainew")]]))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    svc = context.user_data.get("svc")
    if not svc:
        await update.message.reply_text("Menyudan xizmat tanlang 👇", reply_markup=main_menu())
        return
    if svc == "ai":
        await ai_chat(update, context)
        return
    uid = update.effective_user.id
    if left(uid, svc) <= 0:
        context.user_data.pop("svc", None)
        await update.message.reply_text("Tekin limit tugadi 😔", reply_markup=main_menu())
        return

    wait = await update.message.reply_text("⏳ Tayyorlanmoqda...")
    try:
        if svc in ("tags", "desc"):
            result = await ask_claude(SYSTEMS[svc], update.message.text)
            await wait.edit_text(result[:4000])
        else:
            img = await make_image(svc, update.message.text)
            await update.message.reply_photo(photo=img, caption=SERVICES[svc][0] + " tayyor ✅")
            await wait.delete()
        add_use(uid, svc)
    except Exception:
        log.exception("xizmat xatosi")
        await wait.edit_text("❌ Xatolik yuz berdi, birozdan keyin urinib ko'ring.")
        return
    context.user_data.pop("svc", None)
    await update.message.reply_text(MAIN_TEXT, parse_mode="HTML", reply_markup=main_menu())


# ---------- Avto zayavka ----------
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    r = update.chat_join_request
    try:
        await r.approve()
    except Exception:
        log.exception("approve xatosi")
        return
    db("INSERT INTO approved(chat_id, user_id) VALUES(?,?)", (r.chat.id, r.from_user.id))
    try:
        await context.bot.send_message(
            r.user_chat_id,
            f"✅ <b>{html.escape(r.chat.title or '')}</b> ga so'rovingiz qabul qilindi. Xush kelibsiz!",
            parse_mode="HTML")
    except Exception:
        pass  # foydalanuvchi botga yozishni yopgan bo'lishi mumkin


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.my_chat_member
    if m.chat.type == "private":
        return
    status = m.new_chat_member.status
    if status == "administrator":
        db("INSERT OR REPLACE INTO chats(chat_id, title, owner_id) VALUES(?,?,?)",
           (m.chat.id, m.chat.title, m.from_user.id))
        try:
            await context.bot.send_message(
                m.from_user.id,
                f"✅ Avto zayavka yoqildi: <b>{html.escape(m.chat.title or '')}</b>\n"
                "Taklif havolasi «So'rov bilan qo'shilish» rejimida bo'lsin.",
                parse_mode="HTML")
        except Exception:
            pass
    elif status in ("left", "kicked"):
        db("DELETE FROM chats WHERE chat_id=?", (m.chat.id,))


# ---------- Mini App ----------
WEBAPP_HTML = """<!doctype html>
<html lang="uz"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>ZentroAI</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:var(--tg-theme-bg-color,#fff);--tx:var(--tg-theme-text-color,#111);
--hint:var(--tg-theme-hint-color,#777);--card:var(--tg-theme-secondary-bg-color,#f2f3f5);
--btn:var(--tg-theme-button-color,#2f80ed);--btnt:var(--tg-theme-button-text-color,#fff)}
*{box-sizing:border-box}
body{margin:0;padding:20px 16px 96px;background:var(--bg);color:var(--tx);
font-family:-apple-system,Roboto,system-ui,sans-serif}
h1{font-size:24px;margin:8px 0 4px}p.sub{color:var(--hint);margin:0 0 20px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.card{background:var(--card);border-radius:18px;padding:16px}
.card .i{font-size:30px}.card b{display:block;margin:8px 0 2px}
.card span{color:var(--hint);font-size:13px}
.wide{grid-column:1/3}
.bar{position:fixed;left:0;right:0;bottom:0;padding:12px 16px 20px;background:var(--bg)}
button{width:100%;border:0;border-radius:14px;padding:15px;font-size:16px;font-weight:600;
background:var(--btn);color:var(--btnt)}
</style></head><body>
<h1 id="hi">ZentroAI</h1>
<p class="sub">Kanalingiz uchun hammasi bir joyda</p>
<div class="grid">
<div class="card wide"><div class="i">🤖</div><b>AI suhbat</b><span>Istalgan savolga javob</span></div>
<div class="card"><div class="i">🏷</div><b>Teglar</b><span>SEO teglar</span></div>
<div class="card"><div class="i">📝</div><b>Tavsif</b><span>Video tavsifi</span></div>
<div class="card"><div class="i">🎨</div><b>Logo</b><span>AI logo</span></div>
<div class="card"><div class="i">🖼</div><b>Banner</b><span>Kanal banneri</span></div>
<div class="card wide"><div class="i">✅</div><b>Avto zayavka</b><span>So'rovlarni avtomatik qabul qilish</span></div>
</div>
<div class="bar"><button onclick="Telegram.WebApp.close()">Botga qaytish</button></div>
<script>
const tg=Telegram.WebApp;tg.ready();tg.expand();
const u=tg.initDataUnsafe&&tg.initDataUnsafe.user;
if(u){document.getElementById('hi').textContent='Salom, '+u.first_name+' 👋';}
</script></body></html>"""


async def index(request):
    return web.Response(text=WEBAPP_HTML, content_type="text/html")


async def post_init(app: Application):
    site_app = web.Application()
    site_app.router.add_get("/", index)
    runner = web.AppRunner(site_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Mini App server: port %s", PORT)


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
