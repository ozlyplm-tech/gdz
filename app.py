# ---------- BOOT & SAFE PROXY CLEANUP ----------
import os
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)

import time
import asyncio
import aiosqlite
from typing import Optional, Callable, Awaitable

from telegram import (
    Update, LabeledPrice, InlineKeyboardMarkup, InlineKeyboardButton,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters
)

# ---------- ENV ----------
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задано")

OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY", "")
PUBLIC_URL      = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
if not PUBLIC_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL (или PUBLIC_URL) не задан")

PORT = int(os.getenv("PORT") or 8080)

# цены в Stars
PRICE_DAY   = int(os.getenv("PREMIUM_DAY",   "199"))  # 1 день — 199⭐
PRICE_WEEK  = int(os.getenv("PREMIUM_WEEK",  "399"))  # 1 неделя — 399⭐
PRICE_MONTH = int(os.getenv("PREMIUM_MONTH", "599"))  # 1 месяц — 599⭐
REF_BONUS_DAYS = int(os.getenv("REF_BONUS_DAYS", "2"))

# бесплатные лимиты
FREE_TEXTS_PER_DAY  = 20
FREE_PHOTOS_PER_DAY = 10

# платежи
CURRENCY        = "XTR"      # Telegram Stars
PROVIDER_TOKEN  = ""         # для Stars провайдер не нужен — оставляем пустую строку

DB_PATH = "bot.sqlite3"

# ---------- OpenAI ----------
from openai import OpenAI
from openai import RateLimitError
import httpx

oai_client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    _http = httpx.Client(timeout=30.0)   # без прокси
    oai_client = OpenAI(api_key=OPENAI_API_KEY, http_client=_http)

# ---------- Pretty image rendering ----------
import io
import textwrap
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def _latexish_to_mathtext(s: str) -> str:
    """Конвертируем \\[...], \\(...), $$...$$ в mathtext $...$."""
    s = s.replace("\\[", "$").replace("\\]", "$")
    s = s.replace("\\(", "$").replace("\\)", "$")
    s = s.replace("$$", "$")
    return s

def render_answer_png(text: str) -> bytes:
    """Рисуем аккуратный листок с решением и формулами. Возвращает PNG-байты."""
    text = _latexish_to_mathtext(text)

    wrapped_lines = []
    for line in text.splitlines():
        if line.strip().startswith("$") and line.strip().endswith("$"):
            wrapped_lines.append(line)
        else:
            wrapped_lines.extend(textwrap.wrap(line, width=70) or [""])

    height = max(1.0, 0.6 + 0.35 * len(wrapped_lines))
    fig = plt.figure(figsize=(8.0, height), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")

    plt.rcParams.update({
        "font.size": 12,
        "font.family": "DejaVu Sans",
        "mathtext.fontset": "dejavusans",
    })

    y = 0.95
    for line in wrapped_lines:
        ax.text(0.05, y, line, va="top", ha="left", wrap=True)
        y -= 0.04

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight", pad_inches=0.35)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()

def _looks_math_heavy(t: str) -> bool:
    t = t or ""
    triggers = [
        "\\frac", "\\sqrt", "\\sum", "\\int", "\\ge", "\\le", "\\neq",
        "\\rightarrow", "\\left", "\\right", "\\cdot", "\\times",
        "\\mathbb", "\\overline", "\\underline", "$", "^{", "_{"
    ]
    return len(t) > 700 or any(x in t for x in triggers)

def _escape_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def _send_typing(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, sec: float = 0.8):
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    await asyncio.sleep(sec)

async def _answer_with_thinking(
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    make_text_coro: Callable[[], Awaitable[str]]
):
    """Показываем этапы и шлём либо текст, либо PNG с формулами."""
    msg = await ctx.bot.send_message(chat_id, "🤔 Думаю над задачей…")
    try:
        await _send_typing(ctx, chat_id, 0.8)
        await msg.edit_text("🧠 Анализирую условие…")
        await _send_typing(ctx, chat_id, 0.8)
        await msg.edit_text("📐 Составляю решение…")

        text = await make_text_coro()

        if _looks_math_heavy(text):
            png = render_answer_png(text)
            await msg.delete()
            await ctx.bot.send_photo(chat_id, png, caption="Готово ✅")
        else:
            await msg.edit_text(_escape_html(text), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as e:
        try:
            await msg.edit_text(f"Упс… {_escape_html(type(e).__name__)}")
        except:
            await ctx.bot.send_message(chat_id, f"Упс… {_escape_html(type(e).__name__)}")

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

# ---------- Helpers: remaining ----------
async def get_remaining(uid: int) -> tuple[int, int]:
    day = today_key()
    used_texts, used_photos = await get_usage(day, uid)
    rem_texts  = max(0, FREE_TEXTS_PER_DAY  - used_texts)
    rem_photos = max(0, FREE_PHOTOS_PER_DAY - used_photos)
    return rem_texts, rem_photos

# ---------- Keyboards ----------
def premium_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💎 День безлимита · {PRICE_DAY}⭐",   callback_data="buy:day")],
        [InlineKeyboardButton(f"💎 Неделя безлимита · {PRICE_WEEK}⭐", callback_data="buy:week")],
        [InlineKeyboardButton(f"💎 Месяц безлимита · {PRICE_MONTH}⭐", callback_data="buy:month")],
    ])

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Новое задание", callback_data="menu:new")],
        [InlineKeyboardButton("💎 Подписка",      callback_data="menu:buy")],
        [InlineKeyboardButton("🤝 Реф-ссылка",    callback_data="menu:ref")],
    ])

def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ В главное меню", callback_data="menu:back")]
    ])

def with_back(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows = [list(row) for row in markup.inline_keyboard]
    rows.append([InlineKeyboardButton("⬅️ В главное меню", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows)

# ---------- OpenAI helpers ----------
STYLE = (
    "Пиши для школьника. Структура: <b>Кратко условие</b>, <b>Шаги</b> (1–5), <b>Ответ</b>."
    " Формулы отдавай в LaTeX-нотации (\\frac, \\sqrt, степени через ^), чтобы их можно было рендерить."
)

async def solve_text_with_openai(prompt: str) -> str:
    if not oai_client:
        return "OpenAI ключ не задан. Добавь его в переменные окружения."
    try:
        resp = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": STYLE},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=900,
        )
        return resp.choices[0].message.content.strip()
    except RateLimitError:
        return "Пока не могу ответить — исчерпан лимит OpenAI (429). Попробуй позже 🙏"
    except Exception as e:
        return f"Упс, что-то пошло не так: {type(e).__name__}"

async def solve_image_with_openai(file_url: str, question: str) -> str:
    if not oai_client:
        return "OpenAI ключ не задан. Добавь его в переменные окружения."
    try:
        resp = oai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (question or "Реши задачу по фото. ") + STYLE},
                    {"type": "image_url", "image_url": {"url": file_url}},
                ],
            }],
            temperature=0.2,
            max_tokens=1000,
        )
        return resp.choices[0].message.content.strip()
    except RateLimitError:
        return "Пока не могу обработать фото — исчерпан лимит OpenAI (429). Попробуй позже 🙏"
    except Exception as e:
        return f"Упс, что-то пошло не так: {type(e).__name__}"

# ---------- UI Texts ----------
WELCOME_TEXT = (
    "<b>Привет! 👋 Я — умный бот Решебник!</b>\n\n"
    "<b>Что я умею:</b>\n"
    "📝 Решаю задачи любой сложности\n"
    "🧠 Понимаю фото из учебника и голосовые сообщения\n"
    "📚 Пишу сочинения, рефераты и эссе\n"
    "🧮 Работаю с математическими формулами\n\n"
    "<b>Лайфхаки:</b>\n"
    "• Делай чёткие фото при хорошем свете\n"
    "• Укажи номер задания на фото\n"
    "• Жми «Новое задание» для следующей задачи\n\n"
    "Погнали! Скидывай своё задание 🚀"
)

# ---------- Handlers: menu & screens ----------
async def show_main_menu(chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    await ctx.bot.send_message(
        chat_id=chat_id,
        text=WELCOME_TEXT,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb()
    )

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    text = (update.message.text or "").strip() if update.message else ""
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

    await show_main_menu(chat.id, ctx)

async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data.split(":", 1)[1]
    chat_id = q.message.chat.id

    if data == "new":
        rem_t, rem_p = await get_remaining(chat_id)
        if rem_t > 0 or rem_p > 0:
            text = (
                "🆕 <b>Новое задание</b>\n\n"
                "Сегодня осталось:\n"
                f"• Текстов: <b>{rem_t}</b> из {FREE_TEXTS_PER_DAY}\n"
                f"• Фото: <b>{rem_p}</b> из {FREE_PHOTOS_PER_DAY}\n\n"
                "Пришли задачу текстом или фото — я решу и объясню 😉"
            )
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb())
        else:
            text = (
                "🆕 <b>Новое задание</b>\n\n"
                "На сегодня бесплатные запросы закончились. "
                "Хочешь безлимит? Подключи подписку 💎"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("💎 Оформить безлимит", callback_data="menu:buy")],
                [InlineKeyboardButton("⬅️ В главное меню",   callback_data="menu:back")]
            ])
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    elif data == "buy":
        pu, _ = await get_user(chat_id)
        status = "🟢 Премиум до " + human_until(pu) if pu > now() else "⚪️ Обычный"
        text = (
            "💎 <b>Подписка</b>\n\n"
            f"Статус: {status}\n\n"
            "Безлимит ответов и приоритет.\n\n"
            "<b>Тарифы:</b>\n"
            f"• 1 день — <b>{PRICE_DAY}⭐</b>\n"
            f"• 1 неделя — <b>{PRICE_WEEK}⭐</b>\n"
            f"• 1 месяц — <b>{PRICE_MONTH}⭐</b>\n"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=with_back(premium_keyboard())
        )

    elif data == "ref":
        me = await ctx.bot.get_me()
        ref_link = f"https://t.me/{me.username}?start=ref_{chat_id}"
        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            f"Твоя ссылка:\n<code>{ref_link}</code>\n\n"
            f"За оплату приглашённого друга — +{REF_BONUS_DAYS} дн. премиума 🎁"
        )
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=back_kb(), disable_web_page_preview=True)

    elif data == "back":
        await q.delete_message()
        await show_main_menu(chat_id, ctx)

# ---------- Payments (buy buttons) ----------
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
    prices = [LabeledPrice(label=title, amount=amount)]  # amount в Stars

    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=f"Премиум на {days} дн. Безлимит ответов.",
        payload=payload,
        currency=CURRENCY,   # XTR
        prices=prices,
        provider_token="",   # Stars не требуют провайдера
        start_parameter=f"prem_{plan}",
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
        f"Оплата успешна! Премиум активен до {human_until(new_until)} ✅",
        reply_markup=back_kb()
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

# ---- Business logic: text/photo limits ----
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text or ""

    if await is_premium(chat_id):
        await _answer_with_thinking(ctx, chat_id, lambda: solve_text_with_openai(text))
        return

    day = today_key()
    used_texts, _ = await get_usage(day, chat_id)
    if used_texts >= FREE_TEXTS_PER_DAY:
        await update.message.reply_text(
            "Лимит на сегодня исчерпан. Открой меню → 💎 Подписка, чтобы получить безлимит 🙂",
            reply_markup=main_menu_kb()
        )
        return

    await inc_usage(day, chat_id, "text")
    await _answer_with_thinking(ctx, chat_id, lambda: solve_text_with_openai(text))

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or ""

    photo = update.message.photo[-1]
    file = await ctx.bot.get_file(photo.file_id)
    file_url = file.file_path

    if await is_premium(chat_id):
        await _answer_with_thinking(ctx, chat_id, lambda: solve_image_with_openai(file_url, caption))
        return

    day = today_key()
    _, used_photos = await get_usage(day, chat_id)
    if used_photos >= FREE_PHOTOS_PER_DAY:
        await update.message.reply_text(
            "Лимит фото на сегодня исчерпан. Открой меню → 💎 Подписка → безлимит 🙂",
            reply_markup=main_menu_kb()
        )
        return

    await inc_usage(day, chat_id, "photo")
    await _answer_with_thinking(ctx, chat_id, lambda: solve_image_with_openai(file_url, caption))

# ---------- App ----------
def build_app() -> Application:
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(menu_router, pattern=r"^menu:(new|buy|ref|back)$"))

    # покупка
    app.add_handler(CallbackQueryHandler(cb_buy, pattern=r"^buy:(day|week|month)$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful))

    # сообщения
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app

def main():
    asyncio.run(init_db())
    app = build_app()

    webhook_path = os.getenv("WEBHOOK_PATH") or f"/webhook/{TOKEN.split(':')[0]}"
    webhook_url  = f"{PUBLIC_URL.rstrip('/')}{webhook_path}"
    print(f"[BOOT] Setting webhook to: {webhook_url}")

    # фикс для Python 3.13
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

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
