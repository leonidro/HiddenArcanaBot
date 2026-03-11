import os
import time
import logging
import sqlite3
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, PreCheckoutQueryHandler, CallbackQueryHandler
)
from openai import OpenAI

# ─── Логирование ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ─── Конфиг ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN")
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY")

FREE_QUESTIONS    = 3
STARS_PRICE       = 50          # 50 Stars ≈ $1
SUBSCRIPTION_DAYS = 30
COOLDOWN_SECONDS  = 10          # минимум секунд между вопросами
MAX_QUESTION_LEN  = 500         # максимальная длина вопроса

openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ─── Cooldown (в памяти) ──────────────────────────────────────────────────────
# { user_id: timestamp последнего запроса }
last_request: dict[int, float] = {}


# ─── База данных ──────────────────────────────────────────────────────────────
# check_same_thread=False — нужно для asyncio/многопоточности
DB_CONN = sqlite3.connect("users.db", check_same_thread=False)

def init_db():
    c = DB_CONN.cursor()
    # Таблица пользователей
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            language    TEXT DEFAULT 'ru',
            free_used   INTEGER DEFAULT 0,
            sub_until   TEXT,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Таблица статистики
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER,
            event           TEXT,      -- 'question' | 'payment' | 'start'
            value           REAL DEFAULT 0,
            created_at      TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    DB_CONN.commit()

def get_user(user_id: int) -> dict | None:
    c = DB_CONN.cursor()
    c.execute(
        "SELECT user_id, username, language, free_used, sub_until FROM users WHERE user_id = ?",
        (user_id,)
    )
    row = c.fetchone()
    if row:
        return {"user_id": row[0], "username": row[1], "language": row[2],
                "free_used": row[3], "sub_until": row[4]}
    return None

def ensure_user(user_id: int, username: str, language: str):
    c = DB_CONN.cursor()
    c.execute(
        "INSERT OR IGNORE INTO users (user_id, username, language) VALUES (?, ?, ?)",
        (user_id, username, language)
    )
    # Если пользователь уже есть — обновляем язык (мог сменить)
    c.execute(
        "UPDATE users SET language = ? WHERE user_id = ?",
        (language, user_id)
    )
    DB_CONN.commit()

def increment_free(user_id: int):
    c = DB_CONN.cursor()
    c.execute("UPDATE users SET free_used = free_used + 1 WHERE user_id = ?", (user_id,))
    DB_CONN.commit()

def activate_subscription(user_id: int):
    until = (datetime.now() + timedelta(days=SUBSCRIPTION_DAYS)).isoformat()
    c = DB_CONN.cursor()
    c.execute("UPDATE users SET sub_until = ? WHERE user_id = ?", (until, user_id))
    DB_CONN.commit()

def log_event(user_id: int, event: str, value: float = 0):
    """Логируем события для статистики."""
    c = DB_CONN.cursor()
    c.execute(
        "INSERT INTO stats (user_id, event, value) VALUES (?, ?, ?)",
        (user_id, event, value)
    )
    DB_CONN.commit()

def has_active_sub(user: dict) -> bool:
    if not user or not user["sub_until"]:
        return False
    return datetime.fromisoformat(user["sub_until"]) > datetime.now()

def can_ask(user: dict) -> bool:
    if has_active_sub(user):
        return True
    return user["free_used"] < FREE_QUESTIONS


# ─── Мультиязычность ──────────────────────────────────────────────────────────
# Поддерживаем ru / uk (украинский) / en
# Telegram отдаёт language_code: 'ru', 'uk', 'en', и т.д.

def detect_lang(tg_lang: str | None) -> str:
    if tg_lang in ("uk",):
        return "uk"
    if tg_lang in ("en",):
        return "en"
    return "ru"

TEXTS = {
    "ru": {
        "welcome": (
            "🔮 *Добро пожаловать, {name}!*\n\n"
            "Я помогу найти ответы через карты Таро.\n"
            "Просто напишите свой вопрос — и карты заговорят."
        ),
        "free_left":      "\n\n🎁 У вас *{n} бесплатных расклада*",
        "sub_active":     "\n\n✅ *Подписка активна до {date}*",
        "footer":         "\n\n_Карты не предсказывают судьбу — они показывают возможные пути._",
        "thinking":       "🔮 Карты открываются... Подождите немного...",
        "reading_header": "🌙 *Расклад для вопроса:*\n_{question}_\n\n{answer}",
        "error":          "😔 Карты сейчас не отвечают... Попробуйте снова через минуту.",
        "cooldown":       "⏳ Подождите {sec} сек. перед следующим вопросом.",
        "too_long":       "✏️ Вопрос слишком длинный. Пожалуйста, уложитесь в {max} символов.",
        "warn_1left":     "💫 *Остался {n} бесплатный расклад*\nПосле него — подписка за {price} Stars (~$1)",
        "warn_0left":     "🌑 *Это был ваш последний бесплатный расклад.*\nПродолжайте путешествие:",
        "paywall_title":  f"🌑 *Ваши {FREE_QUESTIONS} бесплатных расклада исчерпаны*",
        "paywall_body": (
            "\n\nКарты готовы открывать тайны и дальше — "
            "но для этого нужна совсем небольшая жертва 🕯\n\n"
            "За *{price} Telegram Stars* (~$1) вы получаете:\n"
            "• ♾ *Безлимитные расклады* на 30 дней\n"
            "• 🃏 Ответы на любые вопросы — любовь, работа, выбор пути\n"
            "• ⚡️ Мгновенные расшифровки 24/7\n\n"
            "_Telegram Stars покупаются прямо в приложении за 30 секунд_"
        ),
        "btn_buy":  "✨ Подписка за {price} Stars (~$1) — 30 дней",
        "btn_info": "🎁 Что входит в подписку?",
        "info_text": (
            "🃏 *Подписка Таро — что входит*\n\n"
            "💰 Цена: *{price} Telegram Stars* (~$1 / месяц)\n\n"
            "📌 Включено:\n"
            "• Безлимитные расклады на любые вопросы\n"
            "• Любовь и отношения 💕\n"
            "• Карьера и деньги 💼\n"
            "• Важные решения и выбор пути 🔀\n"
            "• Самопознание и духовный рост 🌿\n\n"
            "⏳ Срок: 30 дней с момента оплаты\n\n"
            "_Telegram Stars — официальная валюта Telegram. Купить: Настройки → Telegram Stars_"
        ),
        "invoice_title": "🔮 Таро — Безлимитные расклады",
        "invoice_desc":  "30 дней безлимитных раскладов на любые вопросы",
        "invoice_label": "Подписка на 30 дней",
        "paid_ok": (
            "✨ *Оплата прошла успешно!*\n\n"
            "Ваша подписка активна до *{date}*\n\n"
            "🌙 Задавайте любое количество вопросов — карты всегда готовы ответить.\n\n"
            "Начните прямо сейчас — напишите свой вопрос 🃏"
        ),
        "gpt_lang": "ru",
    },
    "uk": {
        "welcome": (
            "🔮 *Ласкаво просимо, {name}!*\n\n"
            "Я допоможу знайти відповіді через карти Таро.\n"
            "Просто напишіть своє запитання — і карти заговорять."
        ),
        "free_left":      "\n\n🎁 У вас *{n} безкоштовних розкладів*",
        "sub_active":     "\n\n✅ *Підписка активна до {date}*",
        "footer":         "\n\n_Карти не передбачають долю — вони показують можливі шляхи._",
        "thinking":       "🔮 Карти відкриваються... Зачекайте хвилинку...",
        "reading_header": "🌙 *Розклад для питання:*\n_{question}_\n\n{answer}",
        "error":          "😔 Карти зараз не відповідають... Спробуйте знову за хвилину.",
        "cooldown":       "⏳ Зачекайте {sec} сек. перед наступним питанням.",
        "too_long":       "✏️ Питання надто довге. Будь ласка, вкладіться у {max} символів.",
        "warn_1left":     "💫 *Залишився {n} безкоштовний розклад*\nПісля нього — підписка за {price} Stars (~$1)",
        "warn_0left":     "🌑 *Це був ваш останній безкоштовний розклад.*\nПродовжуйте подорож:",
        "paywall_title":  f"🌑 *Ваші {FREE_QUESTIONS} безкоштовних розкладів вичерпані*",
        "paywall_body": (
            "\n\nКарти готові відкривати таємниці й надалі — "
            "але для цього потрібна невелика жертва 🕯\n\n"
            "За *{price} Telegram Stars* (~$1) ви отримуєте:\n"
            "• ♾ *Безліміт розкладів* на 30 днів\n"
            "• 🃏 Відповіді на будь-які питання — кохання, робота, вибір шляху\n"
            "• ⚡️ Миттєві розшифровки 24/7\n\n"
            "_Telegram Stars купуються прямо у застосунку за 30 секунд_"
        ),
        "btn_buy":  "✨ Підписка за {price} Stars (~$1) — 30 днів",
        "btn_info": "🎁 Що входить у підписку?",
        "info_text": (
            "🃏 *Підписка Таро — що входить*\n\n"
            "💰 Ціна: *{price} Telegram Stars* (~$1 / місяць)\n\n"
            "📌 Включено:\n"
            "• Безліміт розкладів на будь-які питання\n"
            "• Кохання і стосунки 💕\n"
            "• Кар'єра і гроші 💼\n"
            "• Важливі рішення і вибір шляху 🔀\n"
            "• Самопізнання і духовне зростання 🌿\n\n"
            "⏳ Термін: 30 днів з моменту оплати\n\n"
            "_Telegram Stars — офіційна валюта Telegram. Купити: Налаштування → Telegram Stars_"
        ),
        "invoice_title": "🔮 Таро — Безліміт розкладів",
        "invoice_desc":  "30 днів безліміту розкладів на будь-які питання",
        "invoice_label": "Підписка на 30 днів",
        "paid_ok": (
            "✨ *Оплата пройшла успішно!*\n\n"
            "Ваша підписка активна до *{date}*\n\n"
            "🌙 Задавайте будь-яку кількість питань — карти завжди готові відповісти.\n\n"
            "Починайте просто зараз — напишіть своє питання 🃏"
        ),
        "gpt_lang": "Ukrainian",
    },
    "en": {
        "welcome": (
            "🔮 *Welcome, {name}!*\n\n"
            "I will help you find answers through Tarot cards.\n"
            "Just write your question — and the cards will speak."
        ),
        "free_left":      "\n\n🎁 You have *{n} free readings*",
        "sub_active":     "\n\n✅ *Subscription active until {date}*",
        "footer":         "\n\n_Cards don't predict fate — they show possible paths._",
        "thinking":       "🔮 The cards are revealing... Please wait a moment...",
        "reading_header": "🌙 *Reading for your question:*\n_{question}_\n\n{answer}",
        "error":          "😔 The cards aren't responding right now... Please try again in a minute.",
        "cooldown":       "⏳ Please wait {sec} sec. before your next question.",
        "too_long":       "✏️ Your question is too long. Please keep it under {max} characters.",
        "warn_1left":     "💫 *{n} free reading left*\nAfter that — subscription for {price} Stars (~$1)",
        "warn_0left":     "🌑 *That was your last free reading.*\nContinue your journey:",
        "paywall_title":  f"🌑 *Your {FREE_QUESTIONS} free readings are used up*",
        "paywall_body": (
            "\n\nThe cards are ready to reveal more secrets — "
            "but it requires a small offering 🕯\n\n"
            "For *{price} Telegram Stars* (~$1) you get:\n"
            "• ♾ *Unlimited readings* for 30 days\n"
            "• 🃏 Answers to any questions — love, career, life choices\n"
            "• ⚡️ Instant readings 24/7\n\n"
            "_Telegram Stars can be purchased right in the app in 30 seconds_"
        ),
        "btn_buy":  "✨ Subscribe for {price} Stars (~$1) — 30 days",
        "btn_info": "🎁 What's included?",
        "info_text": (
            "🃏 *Tarot Subscription — what's included*\n\n"
            "💰 Price: *{price} Telegram Stars* (~$1 / month)\n\n"
            "📌 Included:\n"
            "• Unlimited readings on any topic\n"
            "• Love & relationships 💕\n"
            "• Career & money 💼\n"
            "• Important decisions & life choices 🔀\n"
            "• Self-discovery & spiritual growth 🌿\n\n"
            "⏳ Duration: 30 days from purchase\n\n"
            "_Telegram Stars is Telegram's official currency. Buy: Settings → Telegram Stars_"
        ),
        "invoice_title": "🔮 Tarot — Unlimited Readings",
        "invoice_desc":  "30 days of unlimited readings on any topic",
        "invoice_label": "30-day Subscription",
        "paid_ok": (
            "✨ *Payment successful!*\n\n"
            "Your subscription is active until *{date}*\n\n"
            "🌙 Ask as many questions as you want — the cards are always ready.\n\n"
            "Start right now — write your question 🃏"
        ),
        "gpt_lang": "English",
    },
}

def t(lang: str, key: str, **kwargs) -> str:
    text = TEXTS.get(lang, TEXTS["ru"]).get(key, "")
    return text.format(**kwargs) if kwargs else text


# ─── GPT системные промпты по языку ──────────────────────────────────────────
SYSTEM_PROMPTS = {
    "ru": (
        "Ты — мудрый и загадочный таролог с многолетним опытом. "
        "Когда пользователь задаёт вопрос, делай расклад на 3 картах Таро.\n\n"
        "Формат:\n"
        "1. Три карты (можно перевёрнутые): название, позиция (прошлое/настоящее/будущее), "
        "толкование применительно к вопросу (2-3 предложения).\n"
        "2. Общий вывод (3-4 предложения).\n\n"
        "Пиши мистично, образно, конкретно по вопросу. Используй эмодзи."
    ),
    "uk": (
        "Ти — мудрий і загадковий таролог із багаторічним досвідом. "
        "Коли користувач ставить питання, роби розклад на 3 картах Таро.\n\n"
        "Формат:\n"
        "1. Три карти (можна перевернуті): назва, позиція (минуле/теперішнє/майбутнє), "
        "тлумачення стосовно питання (2-3 речення).\n"
        "2. Загальний висновок (3-4 речення).\n\n"
        "Пиши містично, образно, конкретно по питанню. Використовуй емодзі. Відповідай українською."
    ),
    "en": (
        "You are a wise and mysterious tarot reader with many years of experience. "
        "When the user asks a question, perform a 3-card Tarot reading.\n\n"
        "Format:\n"
        "1. Three cards (can be reversed): name, position (past/present/future), "
        "interpretation related to the question (2-3 sentences).\n"
        "2. Overall conclusion (3-4 sentences).\n\n"
        "Write mystically, vividly, and specifically to the question. Use emojis."
    ),
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def paywall_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(lang, "btn_buy", price=STARS_PRICE), callback_data=f"buy:{lang}")],
        [InlineKeyboardButton(t(lang, "btn_info"), callback_data=f"info:{lang}")],
    ])

async def do_reading(update: Update, question: str, lang: str):
    thinking = await update.message.reply_text(t(lang, "thinking"))
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPTS.get(lang, SYSTEM_PROMPTS["ru"])},
                {"role": "user", "content": question}
            ],
            max_tokens=1000,
            temperature=0.85
        )
        answer = resp.choices[0].message.content
        await thinking.delete()
        await update.message.reply_text(
            t(lang, "reading_header", question=question, answer=answer),
            parse_mode="Markdown"
        )
        logger.info("Reading done | user=%s | lang=%s | q_len=%d", update.effective_user.id, lang, len(question))
    except Exception:
        await thinking.delete()
        await update.message.reply_text(t(lang, "error"))
        logger.exception("OpenAI error | user=%s", update.effective_user.id)  # полный traceback


# ─── Хэндлеры ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = detect_lang(user.language_code)
    ensure_user(user.id, user.username or user.first_name, lang)
    log_event(user.id, "start")
    u = get_user(user.id)

    if has_active_sub(u):
        extra = t(lang, "sub_active", date=datetime.fromisoformat(u["sub_until"]).strftime("%d.%m.%Y"))
    else:
        remaining = max(0, FREE_QUESTIONS - u["free_used"])
        extra = t(lang, "free_left", n=remaining)

    await update.message.reply_text(
        t(lang, "welcome", name=user.first_name) + extra + t(lang, "footer"),
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    lang = detect_lang(user.language_code)
    ensure_user(user.id, user.username or user.first_name, lang)
    u = get_user(user.id)
    lang = u.get("language") or lang   # берём сохранённый язык

    question = update.message.text or ""

    # 1. Проверка длины
    if len(question) > MAX_QUESTION_LEN:
        await update.message.reply_text(t(lang, "too_long", max=MAX_QUESTION_LEN))
        return

    # 2. Cooldown — защита от спама
    now = time.time()
    last = last_request.get(user.id, 0)
    wait = COOLDOWN_SECONDS - (now - last)
    if wait > 0:
        await update.message.reply_text(t(lang, "cooldown", sec=int(wait) + 1))
        return
    last_request[user.id] = now

    # 3. Проверяем доступ
    if can_ask(u):
        is_sub = has_active_sub(u)
        if not is_sub:
            increment_free(user.id)
            u = get_user(user.id)
            remaining = FREE_QUESTIONS - u["free_used"]

        log_event(user.id, "question")
        await do_reading(update, question, lang)

        if not is_sub:
            if remaining == 1:
                await update.message.reply_text(
                    t(lang, "warn_1left", n=remaining, price=STARS_PRICE),
                    parse_mode="Markdown"
                )
            elif remaining == 0:
                await update.message.reply_text(
                    t(lang, "warn_0left"),
                    parse_mode="Markdown",
                    reply_markup=paywall_keyboard(lang)
                )
    else:
        await update.message.reply_text(
            t(lang, "paywall_title") + t(lang, "paywall_body", price=STARS_PRICE),
            parse_mode="Markdown",
            reply_markup=paywall_keyboard(lang)
        )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, _, lang = query.data.partition(":")
    lang = lang or "ru"

    if action == "info":
        await query.message.reply_text(
            t(lang, "info_text", price=STARS_PRICE),
            parse_mode="Markdown",
            reply_markup=paywall_keyboard(lang)
        )
    elif action == "buy":
        await context.bot.send_invoice(
            chat_id=query.message.chat_id,
            title=t(lang, "invoice_title"),
            description=t(lang, "invoice_desc"),
            payload="tarot_sub_30",
            currency="XTR",
            prices=[LabeledPrice(t(lang, "invoice_label"), STARS_PRICE)],
        )

async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)

async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    activate_subscription(user.id)
    log_event(user.id, "payment", value=STARS_PRICE)
    u = get_user(user.id)
    lang = u.get("language") or "ru"
    until = datetime.fromisoformat(u["sub_until"]).strftime("%d.%m.%Y")
    logger.info("Payment received | user=%s | stars=%s", user.id, STARS_PRICE)
    await update.message.reply_text(
        t(lang, "paid_ok", date=until),
        parse_mode="Markdown"
    )


# ─── Команда /stats (только для владельца) ────────────────────────────────────
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    c = DB_CONN.cursor()
    c.execute("SELECT COUNT(DISTINCT user_id) FROM users")
    total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM stats WHERE event = 'question'")
    total_questions = c.fetchone()[0]
    c.execute("SELECT COUNT(*), SUM(value) FROM stats WHERE event = 'payment'")
    row = c.fetchone()
    total_payments, total_stars = row[0], row[1] or 0
    c.execute("SELECT COUNT(*) FROM users WHERE sub_until > ?", (datetime.now().isoformat(),))
    active_subs = c.fetchone()[0]

    await update.message.reply_text(
        f"📊 *Статистика бота*\n\n"
        f"👤 Пользователей всего: *{total_users}*\n"
        f"🃏 Раскладов сделано: *{total_questions}*\n"
        f"💳 Оплат: *{total_payments}*\n"
        f"⭐️ Stars получено: *{int(total_stars)}* (~${int(total_stars) * 0.013:.0f})\n"
        f"✅ Активных подписок: *{active_subs}*",
        parse_mode="Markdown"
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("✅ Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
