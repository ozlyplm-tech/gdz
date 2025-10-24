# ---------- BOOT & SAFE PROXY CLEANUP ----------
import os

# ВАЖНО: убираем прокси из окружения, иначе openai падал с
# TypeError: Client.__init__() got an unexpected keyword argument 'proxies'
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

import time
import asyncio
import aiosqlite
from typing import Optional

from telegram import (
    Update, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters
)

# ---------- ENV ----------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задано")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL (или PUBLIC_URL) не задан")

PORT = int(os.getenv("PORT", "10000"))

# цены в Stars
PRICE_DAY   = int(os.getenv("PREMIUM_DAY",   "99"))
PRICE_WEEK  = int(os.getenv("PREMIUM_WEEK",  "299"))
PRICE_MONTH = int(os.getenv("PREMIUM_MONTH", "399"))
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "2"))

# бесплатные лимиты
FREE_TEXTS_PER_DAY = 20
FREE_PHOTOS_PER_DAY = 10

CURRENCY = "XTR"  # Stars
DB_PATH = "bot.sqlite3"

# ---------- OpenAI ----------
# Обнови пакет до свежего в requirements.txt:
# openai==1.51.2  (или актуальная стабильная)
from openai import OpenAI
import httpx

oai_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    # Явно создаём httpx-клиент без прокси
    _http = httpx.Client(timeout=30.0)
    oai_client = OpenAI(api_key=OPENAI_API_KEY, http_client=_http)

# ---------- DB utils ----------
def today_key() -> str:
    return time.strftime("%Y%m%d", time.gmtime())

def now() -> int:
    return int(time.time())

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            premium_until INTEGER DEFAULT 0,
            referrer_id INTEGER
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS usage(
            day TEXT,
            user_id INTEGER,
            texts INTEGER DEFAULT 0,
            photos INTEGER DEFAULT 0,
            PRIMARY KEY(day, user_id)
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS payments(
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            stars INTEGER,
            created_at INTEGER
        )""")
        await db.execute("""
        CREATE TABLE IF NOT EXISTS referrals(
            referrer_id INTEGER,
            invited_id INTEGER,
            UNIQUE(referrer_id, invited_id)
        )""")
        await db.commit()

async def ensure_user(user_id: int, referrer_id: int | None = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, premium_until, referrer_id) VALUES(?, 0, ?)",
            (user_id, referrer_id),
        )
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT premium_until, referrer_id FROM users WHERE user_id=?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row if row else (0, None)

async def set_referrer(invited_id: int, ref_id: int):
    if invited_id == ref_id:
        return
    pu, ref_exists = await get_user(invited_id)
    if ref_exists is not None:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL",
            (ref_id, invited_id)
        )
        await db.execute(
            "INSERT OR IGNORE INTO referrals(referrer_id, invited_id) VALUES(?,?)",
            (ref_id, invited_id)
        )
        await db.commit()

async def add_premium_days(user_id: int, days: int):
    pu, _ = await get_user(user_id)
    base = max(pu, now())
    new_until = base + days * 86400
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET premium_until=? WHERE user_id=?",
            (new_until, user_id)
        )
        await db.commit()
    return new_until

async def is_premium(user_id: int) -> bool:
    pu, _ = await get_user(user_id)
    return pu > now()

def human_until(ts: int) -> str:
    lt = time.localtime(ts)
    return time.strftime("%d.%m.%Y %H:%M", lt)

async def get_usage(day: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT texts, photos FROM usage WHERE day=? AND user_id=?",
            (day, user_id)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return (0, 0)
            return row

async def inc_usage(day: str, user_id: int, kind: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO usage(day,user_id,texts,photos) VALUES(?,?,0,0)",
            (day, user_id)
        )
        if kind == "text":
            await db.execute(
                "UPDATE usage SET texts=texts+1 WHERE day=? AND user_id=?",
                (day, user_id)
            )
        else:
            await db.execute(
                "UPDATE usage SET photos=photos+1 WHERE day=? AND user_id=?",
                (day, user_id)
            )
        await db.commit()

# ---------- Keyboards ----------
def premium_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"День безлимита · {PRICE_DAY}⭐", callback_data="buy:day")],
        [InlineKeyboardButton(f"Неделя безлимита · {PRICE_WEEK}⭐", callback_data="buy:week")],
        [InlineKeyboardButton(f"Месяц безлимита · {PRICE_MONTH}⭐", callback_data="buy:month")],
    ])

# ---------- Handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = (update.message.text or "").strip()
    ref_id = None
    if " " in text:
        _, arg = text.split(" ", 1)
        if arg.startswith("ref_"):
            val = arg[4:]
            if val.isdigit():
                ref_id = int(val)

    await ensure_user(chat.id)
    if ref_id:
        await set_referrer(chat.id, ref_id)

    prem = await is_premium(chat.id)
    me = await ctx.bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{chat.id}"

    msg = (
        "Привет! Я решаю задачи по тексту и фото.\n\n"
        f"Статус: {'🟢 Премиум' if prem else '⚪️ Обычный'}\n\n"
        f"Лимиты без премиума: {FREE_TEXTS_PER_DAY} текстов и {FREE_PHOTOS_PER_DAY} фото/день.\n\n"
        f"Твоя реф-ссылка:\n{ref_link}\n"
        "За оплату приглашённого друга ты получишь бонусные дни ✨\n\n"
        "Открой /premium чтобы подключить безлимит (за Stars)."
    )
    await update.message.reply_text(msg)

async def premium(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pu, _ = await get_user(chat_id)
    status = "🟢 Премиум до " + human_until(pu) if pu > now() else "⚪️ Обычный"
    await update.message.reply_text(
        f"Premium-подписка (безлимит ответов)\n\nСтатус: {status}\n\n"
        "Если у вас нет Stars — купите их:\n— https://fragment.com/stars\n— @PremiumBot",
        reply_markup=premium_keyboard(),
        disable_web_page_preview=True
    )

async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    plan = q.data.split(":", 1)[1]
    chat_id = q.message.chat.id

    if plan == "day":
        title, amount, days = "День безлимита", PRICE_DAY, 1
    elif plan == "week":
        title, amount, days = "Неделя безлимита", PRICE_WEEK, 7
    else:
        title, amount, days = "Месяц безлимита", PRICE_MONTH, 30

    payload = f"prem:{chat_id}:{days}:{now()}"
    prices = [LabeledPrice(label=title, amount=amount)]

    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=f"Премиум на {days} дн. Безлимит ответов.",
        payload=payload,
        currency=CURRENCY,
        prices=prices,
    )

async def precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    payload = sp.invoice_payload
    try:
        _, uid_s, days_s, _ts = payload.split(":")
        uid, days = int(uid_s), int(days_s)
    except Exception:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO payments(invoice_id,user_id,stars,created_at) VALUES(?,?,?,?)",
            (sp.telegram_payment_charge_id, uid, sp.total_amount, now())
        )
        await db.commit()

    new_until = await add_premium_days(uid, days)
    await update.message.reply_text(
        f"Оплата успешна! Премиум активен до {human_until(new_until)} ✅"
    )

    _, referrer = await get_user(uid)
    if referrer:
        ref_until = await add_premium_days(referrer, REF_BONUS_DAYS)
        try:
            await ctx.bot.send_message(
                referrer,
                f"Твой реферал оформил премиум! +{REF_BONUS_DAYS} дн. 🎁\n"
                f"Премиум до {human_until(ref_until)}"
            )
        except:
            pass

# ---- OpenAI helpers ----
async def solve_text_with_openai(prompt: str) -> str:
    if not oai_client:
        return "OpenAI ключ не задан. Добавь его в переменные окружения."
    resp = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Ты кратко решаешь задачи и объясняешь ход решения."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
        max_tokens=600,
    )
    return resp.choices[0].message.content.strip()

async def solve_image_with_openai(file_url: str, question: str) -> str:
    if not oai_client:
        return "OpenAI ключ не задан. Добавь его в переменные окружения."
    resp = oai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": question or "Разбери и реши то, что на фото."},
                {"type": "image_url", "image_url": {"url": file_url}},
            ],
        }],
        temperature=0.2,
        max_tokens=700,
    )
    return resp.choices[0].message.content.strip()

# ---------- App ----------
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium))

    app.add_handler(CallbackQueryHandler(cb_buy, pattern=r"^buy:(day|week|month)$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

def main():
    asyncio.run(init_db())
    app = build_app()

    webhook_path = f"/bot{os.urandom(8).hex()}"
    webhook_url  = f"{PUBLIC_URL.rstrip('/')}{webhook_path}"

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=webhook_path,
        webhook_url=webhook_url,
        drop_pending_updates=True,
        stop_signals=None,
    )

if __name__ == "__main__":
    main()
