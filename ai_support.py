"""
╔══════════════════════════════════════════════════════════╗
║       🤖 مساعد الدعم بالذكاء الاصطناعي — ai_support.py  ║
║                                                          ║
║  يعمل بـ Pyrogram كحساب مستخدم منفصل                    ║
║  يستخدم Google Gemini API                                ║
║  متخصص في البوت فقط: خدمات، أسعار، طرق شحن              ║
╚══════════════════════════════════════════════════════════╝

التشغيل:
    python ai_support.py

المتطلبات:
    pip install pyrogram tgcrypto google-generativeai aiohttp

الإعداد في config.py:
    AI_API_ID       = "..."   # من my.telegram.org
    AI_API_HASH     = "..."
    AI_BOT_TOKEN    = "..."   # توكن حساب بوت الدعم (مختلف عن البوت الرئيسي)
    GEMINI_API_KEY  = "..."   # من aistudio.google.com
"""

import asyncio
import logging
import sqlite3
import os
import sys
import json
from datetime import datetime, timedelta
from typing import Optional

import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import FloodWait, UserIsBlocked

# ── إعدادات ─────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as cfg

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("data/ai_support.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("AI-Support")

# ── قراءة إعدادات AI من config.py ───────────────────────────
AI_API_ID      = getattr(cfg, "AI_API_ID",      "")
AI_API_HASH    = getattr(cfg, "AI_API_HASH",    "")
AI_BOT_TOKEN   = getattr(cfg, "AI_BOT_TOKEN",   "")
GEMINI_API_KEY = getattr(cfg, "GEMINI_API_KEY", "")
MAIN_BOT_USERNAME = getattr(cfg, "BOT_USERNAME", "")
DB_PATH        = getattr(cfg, "DATABASE_PATH",  "data/bot.db")

# معدل الرد: رسالة واحدة كل X ثانية لكل مستخدم
RATE_LIMIT_SEC   = 3
# الحد الأقصى لسجل المحادثة المُرسل لـ Gemini
MAX_HISTORY      = 10
# الحد الأقصى للرد
MAX_OUTPUT_TOKENS = 700


# ══════════════════════════════════════════════════════════════
#  قراءة بيانات البوت من قاعدة البيانات
# ══════════════════════════════════════════════════════════════

def _db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def fetch_bot_context() -> str:
    """
    يجمع معلومات البوت الكاملة من قاعدة البيانات:
    المنصات، الفئات، الخدمات، طرق الشحن، الإعدادات.
    """
    try:
        conn = _db_conn()
        cur  = conn.cursor()

        # ── المنصات والخدمات ────────────────────────────────
        platforms = cur.execute(
            "SELECT id, name, emoji FROM platforms WHERE is_active=1 ORDER BY sort_order"
        ).fetchall()

        services_text = ""
        for p in platforms:
            cats = cur.execute(
                "SELECT id, name, emoji FROM categories WHERE platform_id=? AND is_active=1 ORDER BY sort_order",
                (p["id"],)
            ).fetchall()
            if not cats:
                continue
            services_text += f"\n### {p['emoji']} {p['name']}\n"
            for c in cats:
                svcs = cur.execute(
                    """SELECT name, price_per_1000, min_qty, max_qty,
                              speed, quality, warranty, description
                       FROM services
                       WHERE category_id=? AND is_active=1
                       ORDER BY sort_order LIMIT 20""",
                    (c["id"],)
                ).fetchall()
                if not svcs:
                    continue
                services_text += f"\n  {c['emoji']} {c['name']}:\n"
                for s in svcs:
                    line = (
                        f"    - {s['name']}: "
                        f"${s['price_per_1000']:.3f}/1000 | "
                        f"الحد الأدنى {s['min_qty']:,} | الحد الأقصى {s['max_qty']:,}"
                    )
                    if s["speed"]:    line += f" | السرعة: {s['speed']}"
                    if s["quality"]:  line += f" | الجودة: {s['quality']}"
                    if s["warranty"]: line += f" | الضمان: {s['warranty']}"
                    services_text += line + "\n"

        # ── طرق الشحن ────────────────────────────────────────
        def _s(key, default=""):
            r = cur.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return r["value"] if r else default

        pay_methods = []

        if _s("pay_bep20") == "1":
            addr = _s("bep20_address", "")
            rate = _s("bep20_usdt_rate", "1")
            minu = _s("bep20_min_usdt",  "5")
            pay_methods.append(
                f"💎 USDT BEP20 (Binance Smart Chain)\n"
                f"   العنوان: {addr}\n"
                f"   الحد الأدنى: {minu} USDT | سعر الصرف: 1 USDT = ${rate}"
            )

        if _s("pay_trc20") == "1":
            addr = _s("trc20_address", "")
            rate = _s("trc20_usdt_rate", "1")
            minu = _s("trc20_min_usdt",  "5")
            pay_methods.append(
                f"🟣 USDT TRC20 (Tron)\n"
                f"   العنوان: {addr}\n"
                f"   الحد الأدنى: {minu} USDT | سعر الصرف: 1 USDT = ${rate}"
            )

        if _s("pay_stars") == "1":
            rate    = _s("stars_per_dollar", "85")
            min_usd = _s("stars_min_usd",    "1")
            pay_methods.append(
                f"⭐ نجوم تيليغرام\n"
                f"   {rate} نجمة = $1 | الحد الأدنى: ${min_usd}"
            )

        if _s("pay_binance") == "1":
            pay_id = _s("binance_pay_id", "")
            pay_methods.append(
                f"💛 Binance Pay\n"
                f"   Pay ID: {pay_id}\n"
                f"   بعد التحويل أرسل Order ID للبوت ليتم التحقق تلقائياً"
            )

        if _s("vodafone_number"):
            vod = _s("vodafone_number")
            pay_methods.append(
                f"📱 فودافون كاش\n"
                f"   رقم الاستلام: {vod}\n"
                f"   أرسل صورة الإيصال للبوت بعد التحويل"
            )

        pay_text = "\n\n".join(pay_methods) if pay_methods else "لا توجد طرق شحن مفعّلة حالياً."

        # ── معلومات إضافية ───────────────────────────────────
        support_link  = _s("support_link",  "")
        instructions  = _s("instructions",  "")
        orders_ch     = _s("orders_channel","")
        ref_pct       = _s("referral_pct",  "5")

        conn.close()

        extra = ""
        if instructions:
            extra += f"\n\n### تعليمات البوت\n{instructions}"
        if support_link:
            extra += f"\n\n### رابط الدعم المباشر: {support_link}"
        if orders_ch:
            extra += f"\n\n### قناة الطلبات: {orders_ch}"

        return f"""
## الخدمات المتاحة في البوت
{services_text}

## طرق الشحن والدفع
{pay_text}

## نظام الإحالة
نسبة الربح من الإحالة: {ref_pct}%
اجلب أصدقاءك واربح {ref_pct}% من كل شحن يقومون به.

## كيفية الاستخدام
1. اشحن رصيدك بإحدى طرق الدفع المتاحة
2. اختر المنصة → الفئة → الخدمة
3. أدخل الرابط والكمية
4. أكد الطلب وسيُنفَّذ تلقائياً

## معلومات عامة
- البوت يعمل على مدار الساعة
- الطلبات تُنفَّذ تلقائياً عبر نظام SMM
- يمكنك تتبع طلباتك من قسم "طلباتي"
{extra}
""".strip()

    except Exception as e:
        logger.error(f"[DB] خطأ في قراءة بيانات البوت: {e}")
        return "لا تتوفر بيانات كافية حالياً."


# ══════════════════════════════════════════════════════════════
#  نظام المحادثة مع Gemini
# ══════════════════════════════════════════════════════════════

class ConversationManager:
    """يحفظ تاريخ المحادثة لكل مستخدم."""

    def __init__(self):
        self._history: dict[int, list] = {}
        self._last_msg: dict[int, datetime] = {}

    def is_rate_limited(self, uid: int) -> bool:
        last = self._last_msg.get(uid)
        if last and (datetime.now() - last).total_seconds() < RATE_LIMIT_SEC:
            return True
        self._last_msg[uid] = datetime.now()
        return False

    def add_user_msg(self, uid: int, text: str):
        if uid not in self._history:
            self._history[uid] = []
        self._history[uid].append({"role": "user", "parts": [text]})
        # حافظ على آخر MAX_HISTORY رسائل فقط
        if len(self._history[uid]) > MAX_HISTORY * 2:
            self._history[uid] = self._history[uid][-MAX_HISTORY * 2:]

    def add_model_msg(self, uid: int, text: str):
        if uid not in self._history:
            self._history[uid] = []
        self._history[uid].append({"role": "model", "parts": [text]})

    def get_history(self, uid: int) -> list:
        return self._history.get(uid, [])

    def clear_history(self, uid: int):
        self._history.pop(uid, None)


conv_manager = ConversationManager()


def build_system_prompt(bot_context: str) -> str:
    return f"""أنت مساعد ذكاء اصطناعي متخصص لبوت SMM (التسويق عبر وسائل التواصل الاجتماعي) على تيليغرام.
اسمك "مساعد {MAIN_BOT_USERNAME}".

## مهمتك
- الإجابة على استفسارات المستخدمين حول البوت فقط
- اقتراح الخدمات المناسبة بناءً على احتياجاتهم
- شرح طرق الشحن خطوة بخطوة
- مساعدة المستخدمين في حل مشاكلهم

## قواعد مهمة
- تحدث باللغة العربية دائماً (إلا لو المستخدم كلمك بلغة أخرى)
- اذكر الأسعار بدقة من البيانات المتاحة
- لا تتحدث عن أي موضوع خارج نطاق البوت
- لو المستخدم سألك عن شيء مش موجود في البوت قوله بوضوح
- ردودك مختصرة وعملية، لا تطول بلا داعٍ
- لو المشكلة تتطلب تدخل الأدمن، وجّه المستخدم للتواصل المباشر
- عند اقتراح خدمة، اذكر السعر والحد الأدنى والحد الأقصى

## بيانات البوت الحالية
{bot_context}

## عند سؤال عن خدمة معينة
اقترح الخيار الأنسب واذكر:
1. اسم الخدمة
2. السعر لكل 1000
3. الحد الأدنى والأقصى
4. إجمالي التكلفة إذا ذكر المستخدم كمية محددة

## لا تذكر أبداً
- أنك مدعوم بـ Gemini أو Google AI
- أي معلومات تقنية داخلية عن البوت
- أسعار أو خدمات غير موجودة في البيانات
"""


async def ask_gemini(uid: int, user_message: str, bot_context: str) -> str:
    """يرسل الرسالة لـ Gemini مع السياق الكامل ويُعيد الرد."""
    try:
        model = genai.GenerativeModel(
            model_name="gemini-1.5-flash",
            system_instruction=build_system_prompt(bot_context),
            generation_config=genai.types.GenerationConfig(
                max_output_tokens=MAX_OUTPUT_TOKENS,
                temperature=0.4,
            ),
        )

        # بناء تاريخ المحادثة
        history = conv_manager.get_history(uid)
        chat = model.start_chat(history=history)

        response = await asyncio.to_thread(chat.send_message, user_message)
        return response.text.strip()

    except Exception as e:
        logger.error(f"[GEMINI] خطأ: {e}")
        return "عذراً، حدث خطأ مؤقت. حاول مرة أخرى بعد لحظة."


# ══════════════════════════════════════════════════════════════
#  Pyrogram Bot
# ══════════════════════════════════════════════════════════════

app = Client(
    name="ai_support_session",
    api_id=AI_API_ID,
    api_hash=AI_API_HASH,
    bot_token=AI_BOT_TOKEN,
    workdir="data/",
)

# تحديث السياق كل 10 دقائق
_bot_context_cache: str = ""
_cache_updated_at: Optional[datetime] = None
_CACHE_TTL = timedelta(minutes=10)


def get_bot_context() -> str:
    global _bot_context_cache, _cache_updated_at
    now = datetime.now()
    if _cache_updated_at is None or (now - _cache_updated_at) > _CACHE_TTL:
        _bot_context_cache = fetch_bot_context()
        _cache_updated_at  = now
        logger.info("[CTX] تم تحديث سياق البوت")
    return _bot_context_cache


# ── /start ───────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
async def on_start(client: Client, message: Message):
    conv_manager.clear_history(message.from_user.id)
    bot_name = MAIN_BOT_USERNAME or "البوت"
    await message.reply_text(
        f"👋 أهلاً! أنا مساعد @{bot_name} الذكي.\n\n"
        f"يمكنني مساعدتك في:\n"
        f"• 📦 الاستفسار عن الخدمات والأسعار\n"
        f"• 💳 شرح طرق الشحن والدفع\n"
        f"• 🛒 اقتراح أنسب خدمة لاحتياجك\n"
        f"• ❓ الإجابة على أسئلتك\n\n"
        f"ابدأ بكتابة سؤالك مباشرة! 🚀"
    )


# ── /clear — مسح سجل المحادثة ──────────────────────────────

@app.on_message(filters.command("clear") & filters.private)
async def on_clear(client: Client, message: Message):
    conv_manager.clear_history(message.from_user.id)
    await message.reply_text("✅ تم مسح سجل المحادثة. ابدأ سؤالاً جديداً!")


# ── /refresh — تحديث بيانات البوت (أدمن فقط) ──────────────

@app.on_message(filters.command("refresh") & filters.private)
async def on_refresh(client: Client, message: Message):
    if message.from_user.id != cfg.ADMIN_ID:
        return
    global _cache_updated_at
    _cache_updated_at = None
    get_bot_context()
    await message.reply_text("✅ تم تحديث بيانات البوت بنجاح!")


# ── /services — عرض الخدمات المتاحة ────────────────────────

@app.on_message(filters.command("services") & filters.private)
async def on_services(client: Client, message: Message):
    await message.reply_text("⏳ جاري جلب الخدمات...")
    try:
        conn = _db_conn()
        platforms = conn.execute(
            "SELECT id, name, emoji FROM platforms WHERE is_active=1 ORDER BY sort_order"
        ).fetchall()

        if not platforms:
            await message.reply_text("❌ لا توجد خدمات متاحة حالياً.")
            return

        text = "📦 <b>المنصات المتاحة:</b>\n\n"
        for p in platforms:
            cats = conn.execute(
                "SELECT name, emoji FROM categories WHERE platform_id=? AND is_active=1",
                (p["id"],)
            ).fetchall()
            cats_str = " | ".join(f"{c['emoji']} {c['name']}" for c in cats)
            text += f"{p['emoji']} <b>{p['name']}</b>\n{cats_str}\n\n"

        conn.close()
        text += f"اسألني عن أي خدمة وسأخبرك بالأسعار والتفاصيل! 💬"
        await message.reply_text(text, parse_mode="html")

    except Exception as e:
        logger.error(f"[SERVICES] خطأ: {e}")
        await message.reply_text("❌ حدث خطأ. حاول مرة أخرى.")


# ── /prices — عرض طرق الشحن ─────────────────────────────────

@app.on_message(filters.command("prices") & filters.private)
async def on_prices(client: Client, message: Message):
    ctx = get_bot_context()
    uid = message.from_user.id
    reply = await ask_gemini(
        uid,
        "اعرض لي طرق الشحن والدفع المتاحة بالتفصيل مع التعليمات",
        ctx,
    )
    conv_manager.add_user_msg(uid, "طرق الشحن المتاحة؟")
    conv_manager.add_model_msg(uid, reply)
    await message.reply_text(reply)


# ── الرسائل العادية ──────────────────────────────────────────

@app.on_message(filters.text & filters.private & ~filters.command(["start", "clear", "refresh", "services", "prices"]))
async def on_message(client: Client, message: Message):
    uid  = message.from_user.id
    text = (message.text or "").strip()

    if not text:
        return

    # Rate limiting
    if conv_manager.is_rate_limited(uid):
        return

    # نقاط توقف مباشرة (لا تحتاج Gemini)
    lower = text.lower()
    if any(w in lower for w in ["شكرا", "شكراً", "ثانكس", "thanks", "thank you"]):
        await message.reply_text("العفو! 😊 أنا هنا إذا احتجت أي مساعدة أخرى.")
        return

    # مؤشر الكتابة
    async with client.action(message.chat.id, "typing"):
        ctx   = get_bot_context()
        reply = await ask_gemini(uid, text, ctx)

    # حفظ في السجل
    conv_manager.add_user_msg(uid, text)
    conv_manager.add_model_msg(uid, reply)

    try:
        await message.reply_text(reply)
    except FloodWait as e:
        logger.warning(f"[FLOOD] انتظار {e.value} ثانية")
        await asyncio.sleep(e.value)
        await message.reply_text(reply)
    except UserIsBlocked:
        logger.info(f"[BLOCKED] المستخدم {uid} حجب البوت")
    except Exception as e:
        logger.error(f"[SEND] خطأ: {e}")


# ══════════════════════════════════════════════════════════════
#  نقطة التشغيل
# ══════════════════════════════════════════════════════════════

async def main():
    if not AI_API_ID or not AI_API_HASH or not AI_BOT_TOKEN:
        logger.error(
            "❌ تأكد من ضبط AI_API_ID, AI_API_HASH, AI_BOT_TOKEN في config.py"
        )
        return

    if not GEMINI_API_KEY:
        logger.error("❌ تأكد من ضبط GEMINI_API_KEY في config.py")
        return

    # تهيئة Gemini
    genai.configure(api_key=GEMINI_API_KEY)
    logger.info("✅ تم تهيئة Gemini API")

    # تحميل بيانات البوت
    get_bot_context()
    logger.info("✅ تم تحميل بيانات البوت")

    logger.info("🤖 بدء تشغيل بوت الدعم الذكي...")
    await app.start()
    me = await app.get_me()
    logger.info(f"✅ البوت يعمل: @{me.username}")

    await asyncio.Event().wait()   # تشغيل مستمر


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("⏹ تم إيقاف بوت الدعم")
