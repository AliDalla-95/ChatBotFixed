# #region agent log
# import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H3","location":"client.py:top","message":"script started importing","data":{},"timestamp":__import__('time').time()})+'\n')
# #endregion
import logging

import config
import re
import hashlib
from urllib.parse import urlparse, parse_qs, unquote
import sqlite3
import random
import smtplib
from email.message import EmailMessage
from datetime import datetime
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackQueryHandler
)
import os
import sys
from pathlib import Path
import psutil
from telegram.error import Conflict
import warnings
import psycopg2
import psycopg2.pool
from psycopg2 import errors
import phonenumbers
from phonenumbers import geocoder
from telegram.warnings import PTBUserWarning

# Keep PTB warnings visible
warnings.filterwarnings("ignore", category=PTBUserWarning)


# ========== INSTAGRAM HELPERS ==========
# We only *parse* Instagram links locally (no external API calls).
# Goal:
# - Extract username from the provided Instagram URL
# - Generate a deterministic channel_id for that username (so the same link/username always yields the same channel_id)

INSTAGRAM_RESERVED_PATHS = {
    "p", "reel", "tv", "stories", "explore", "accounts", "about", "developer", "help",
    "graphql", "oauth", "api", "tags", "direct", "privacy", "terms", "press", "web",
    "support", "challenge", "emailsignup", "login", "signup",
    # App-deep-link variants sometimes appear in shared URLs (handled explicitly)
}

def extract_instagram_username(raw_input: str):
    """Extract Instagram username from a profile URL.

    Accepted examples:
      - https://www.instagram.com/username/
      - http://instagram.com/username
      - instagram.com/username?utm_source=...
      - https://m.instagram.com/username/
      - https://www.instagram.com/stories/username/123...
    Returns:
      username (lowercased) or None if invalid/unsupported.
    """
    if not raw_input:
        return None

    s = raw_input.strip()

    # If the user forgot the scheme, add it so urlparse works correctly
    if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', s):
        s = "https://" + s

    try:
        parsed = urlparse(s)
    except Exception:
        return None

    host = (parsed.netloc or "").lower()
    # strip port if any
    host = host.split(":")[0]

    if not host:
        return None

    # Handle Instagram redirect-wrapper links like:
    #   https://l.instagram.com/?u=<encoded_profile_url>
    if host == "l.instagram.com":
        try:
            qs = parse_qs(parsed.query or "")
            target = (qs.get("u") or [None])[0]
            if target:
                return extract_instagram_username(unquote(target))
        except Exception:
            return None
        return None

    # Allow Instagram/IG short domains (including subdomains)
    if not (host.endswith("instagram.com") or host.endswith("instagr.am") or host.endswith("ig.me")):
        return None

    # Take first non-empty path segment
    parts = [p for p in (parsed.path or "").split("/") if p]
    if not parts:
        return None

    first = parts[0].lower()

    # ig.me/m/<username> (Instagram deep-link/share)
    if host.endswith("ig.me"):
        if first in ("m", "u", "_u"):
            if len(parts) < 2:
                return None
            username = parts[1]
        else:
            username = parts[0]
    # /stories/<username>/...
    elif first == "stories":
        if len(parts) < 2:
            return None
        username = parts[1]
    # /_u/<username>/... or /u/<username>/...
    elif first in ("_u", "u"):
        if len(parts) < 2:
            return None
        username = parts[1]
    else:
        # Reject non-profile paths (posts, reels, etc.)
        if first in INSTAGRAM_RESERVED_PATHS:
            return None
        username = parts[0]

    username = username.strip().lstrip("@").strip()
    # Instagram usernames: letters, digits, underscore, dot (max 30)
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return None

    return username.lower()

def canonical_instagram_profile_url(username: str) -> str:
    """Return canonical Instagram profile URL."""
    u = (username or "").strip().lstrip("@").lower()
    return f"https://www.instagram.com/{u}/"

def generate_instagram_channel_id(username: str) -> str:
    """Generate deterministic channel_id for an Instagram username.

    We use SHA-256 and keep it short & stable:
    - Prefix: IG
    - 22 hex chars from sha256 -> total length 24 (similar to YouTube channel_id length)
    """
    u = (username or "").strip().lstrip("@").lower()
    digest = hashlib.sha256(u.encode("utf-8")).hexdigest()
    return "IG" + digest[:22]


# ========== CONFIGURATION ==========admin
TELEGRAM_TOKEN = config.CLIENT_BOT_TOKEN


# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Start logging: save who pressed /start for this bot =====
BOT_NAME = "Client"

# BOT_START_TABLE_SQL = """
# CREATE TABLE IF NOT EXISTS bot_starts (
#     id BIGSERIAL PRIMARY KEY,
#     telegram_id BIGINT NOT NULL,
#     username TEXT,
#     full_name TEXT,
#     bot_name TEXT NOT NULL,
#     started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#     last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
#     UNIQUE (telegram_id, bot_name)
# );
# """

def _tg_username(u):
    username = getattr(u, "username", None)
    return f"@{username}" if username else None

def _tg_full_name(u):
    # PTB provides .full_name, but keep fallback
    full = getattr(u, "full_name", None)
    if full:
        return full
    first = getattr(u, "first_name", None)
    last = getattr(u, "last_name", None)
    parts = [p for p in [first, last] if p]
    return " ".join(parts) if parts else None

# def ensure_bot_starts_table(conn):
#     with conn.cursor() as cur:
#         cur.execute(BOT_START_TABLE_SQL)

def log_bot_start(user):
    """Upsert user into bot_starts (one row per (telegram_id, bot_name))."""
    conn = connection_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bot_starts (telegram_id, username, full_name, bot_name)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id, bot_name)
                DO UPDATE SET username = EXCLUDED.username,
                              full_name = EXCLUDED.full_name,
                              last_seen_at = NOW();
                """,
                (int(getattr(user, "id")), _tg_username(user), _tg_full_name(user), BOT_NAME),
            )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"bot_starts log failed: {e}")
    finally:
        try:
            connection_pool.putconn(conn)
        except Exception:
            pass


# ========== UPDATED STATES ==========
(
    EMAIL,
    CODE_VERIFICATION,
    PHONE,
    FULLNAME,
    COUNTRY,
    CHANNEL_URL,
    SUBSCRIPTION_CHOICE,
    COMPANY_CHOICE,          # الآن: اختيار شركة الدفع أولاً
    AWAIT_payment_ID,        # ثم: إدخال رقم الدفع
    SUPPORT_MESSAGE,
    CONFIRM_payment_UPDATE   # جديد: تأكيد قبل التحديث
) = range(11)


# ========== MENU SYSTEM ==========
# ========== UPDATED MENU SYSTEM ==========



# ========== UPDATED MENU SYSTEM ==========
START_MENU = [
    ["📝 Register", "Start"],
    ["Get started"],
]

START_MENU_ar = [
    ["تسجيل الدخول 📝", "بدء"],
    ["إبدأ العمل"],
]

MAIN_MENU_OPTIONS = [
    ["Main Menu"],
    ["🔍 Input Your Instagram Page URL"],
    ["📋 My Profile", "My Pages Done"],
    ["📌 My Pages", "📌 My Pages Accept"],
    ["🗑 Delete Page", "Delete Page accept"]
    # ["🗑 Delete Page", "Delete Page accept", "Educational video 📹"]
]

MAIN_MENU_OPTIONS_ar = [
    ["القائمة الرئيسية"],
    ["أدخل رابط حساب انستغرام 🔍"],
    ["الملف الشخصي 📋", "قنواتي التي تم إنجازها"],
    ["قنواتي التي أدخلتها 📌", "قنواتي التي تم قبولها بعد الدفع 📌"],
    ["حذف قناة 🗑", "حذف قناة مقبولة"]
    # ["حذف قناة 🗑", "حذف قناة مقبولة", "فيديو تعليمي 📹"]
]

MAIN_MENU_WITH_SUPPORT = [
    ["📝 Register", "Start"],
    ["Get started"],
    ["Support", "Help"],
]

MAIN_MENU_WITH_SUPPORT_ar = [
    ["تسجيل الدخول 📝", "بدء"],
    ["إبدأ العمل"],
    ["مساعدة", "الدعم"],
]

ADMIN_MENU = [
    ["Start", "👑 Admin Panel"],
    ["🔍 Input Your Instagram Page URL"],
    ["📋 My Profile", "My Pages Done"],
    ["📌 My Pages", "📌 My Pages Accept"],
    ["🗑 Delete Page", "Delete Page accept"]
]


DB_DSN = getattr(config, "DATABASE_URL", None) or getattr(config, "DATABASE_CONFIG", None)
if not DB_DSN:
    raise RuntimeError("DATABASE_URL / DATABASE_CONFIG is not set in config.py")

connection_pool = psycopg2.pool.SimpleConnectionPool(1, 1000, DB_DSN)





async def is_admins(admins_id: int) -> bool:
    """Check if user is banned with DB connection handling"""
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT admins_name FROM admins WHERE admins_id = %s", (admins_id,))
        return bool(c.fetchone())
        # result = c.fetchone()
        # if result:
        #     return True
        # else:
        #     return False
    except Exception as e:
        logger.error(f"Ban check failed: {str(e)}")
        return False
    finally:
        conn.close()



class PooledConn:
    """Small wrapper so conn.close() returns connection back to the pool (instead of closing it)."""
    def __init__(self, pool: psycopg2.pool.AbstractConnectionPool):
        self._pool = pool
        self._conn = pool.getconn()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        # Return to pool safely (rollback to avoid leaking open transactions)
        try:
            if self._conn and getattr(self._conn, "closed", 1) == 0:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        finally:
            try:
                self._pool.putconn(self._conn)
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None


def get_conn():
    return PooledConn(connection_pool)

def put_conn(conn):
    if conn is None:
        return
    # If it's our wrapper -> close() returns to pool
    if isinstance(conn, PooledConn):
        conn.close()
        return
    # Otherwise it's a raw psycopg2 connection
    try:
        connection_pool.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


def generate_confirmation_code() -> str:
    return ''.join(random.choices('0123456789', k=6))

def send_confirmation_email(email: str, code: str) -> bool:
    try:
        msg = EmailMessage()
        msg.set_content(f"Your confirmation code is: {code}")
        msg['Subject'] = "Confirmation Code"
        msg['From'] = getattr(config, 'EMAIL_FROM', config.SMTP_USERNAME)
        msg['To'] = email

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)
            return True
    except Exception as e:
        logger.error(f"Email send failed: {str(e)}")
        return False

async def verify_code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lang = update.effective_user.language_code or 'en'
    user_code = update.message.text.strip()
    stored_code = context.user_data.get("confirmation_code")

    # Handle cancellation
    if user_code in ["Cancel ❌", "إلغاء ❌"]:
        msg = "تم إلغاء التسجيل ❌" if user_lang.startswith('ar') else "❌ Registration cancelled"
        await update.message.reply_text(msg, reply_markup=await get_menu(user_lang, update.effective_user.id))
        return ConversationHandler.END

    if not stored_code:
        error_msg = "Session expired" if user_lang != 'ar' else "انتهت الجلسة"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

    if user_code != stored_code:
        error_msg = "❌ Invalid code" if user_lang != 'ar' else "❌ رمز غير صحيح"
        await update.message.reply_text(error_msg)
        return CODE_VERIFICATION

    # Code verified - proceed to phone
    contact_msg = (
        "📱 Share your phone number ⬇️ or skip:" 
        if user_lang != 'ar' else 
        "شارك رقم هاتفك ⬇️ أو اضغط تخطي: 📱\n(في حال اخترت التخطي لن يتم تسجيل رقم هاتفك)"
    )
    contact_btn = (
        "Share your phone number⬇️" 
        if user_lang != 'ar' else 
        "⬇️ مشاركة رقم الهاتف من هنا"
    )
    skip_btn = "Skip" if user_lang != 'ar' else "تخطي"
    cancel_btn = "Cancel ❌" if user_lang != 'ar' else "إلغاء ❌"
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton(contact_btn, request_contact=True)],  # First row: contact button
            [skip_btn, cancel_btn]                                           # Second row: cancel button
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text(contact_msg, reply_markup=keyboard)
    return PHONE


async def handle_skip_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle phone number skipping during registration"""
    user = update.effective_user
    user_lang = user.language_code or 'en'
    
    # Set default values
    context.user_data["phone"] = "+0000000000"
    fullname = user.name
    email = context.user_data["email"]
    registration_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO clients 
            (telegram_id, email, phone, fullname, country, registration_date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            user.id,
            email,
            "+0000000000",
            fullname,
            "N/A",  # Default country
            registration_date
        ))
        conn.commit()

        # Confirmation message
        if user_lang.startswith('ar'):
            msg = (
                f"✅ اكتملت عملية التسجيل بنجاح :\n"
                f"👤 أسمك: {escape_markdown(fullname)}\n"
                f"📧 بريدك الإلكتروني: {escape_markdown_2(email)}\n"
                f"📱 رقم هاتفك: +0000000000\n"
                f"🌍 بلدك: N/A \n"
                f"⭐ تاريخ التسجيل: {escape_markdown_2(registration_date)}"
            )
        else:
            msg = (
                f"✅ Registration Complete:\n"
                f"👤 Name: {escape_markdown(fullname)}\n"
                f"📧 Email: {escape_markdown_2(email)}\n"
                f"📱 Phone: +0000000000\n"
                f"🌍 Country: N/A\n"
                f"⭐ Registration Date: {escape_markdown_2(registration_date)}"
            )
            
        await update.message.reply_text(msg, reply_markup=await get_menu(user_lang, user.id))
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Skip phone registration error: {str(e)}")
        error_msg = "❌ فشل التسجيل" if user_lang.startswith('ar') else "❌ Registration failed"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END
    finally:
        conn.close()


async def get_menu2(user_lang: str, user_id: int) -> ReplyKeyboardMarkup:
    if await is_admins(user_id):
        return ReplyKeyboardMarkup(ADMIN_MENU, resize_keyboard=True)
    if (user_lang or "").startswith("ar"):
        return ReplyKeyboardMarkup(MAIN_MENU_OPTIONS_ar, resize_keyboard=True)
    return ReplyKeyboardMarkup(MAIN_MENU_OPTIONS, resize_keyboard=True)


async def get_menu(user_lang: str, user_id: int) -> ReplyKeyboardMarkup:
    if await is_admins(user_id):
        return ReplyKeyboardMarkup(ADMIN_MENU, resize_keyboard=True)
    if (user_lang or "").startswith("ar"):
        return ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT_ar, resize_keyboard=True)
    return ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT, resize_keyboard=True)




async def is_registered(telegram_id: int) -> bool:
    """Check if user is registered"""
    conn = get_conn()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM clients WHERE telegram_id = %s", (telegram_id,))
            return bool(c.fetchone())
    finally:
        put_conn(conn)

# ========== CORE BOT FUNCTIONALITY ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command with dynamic menu"""
    user = update.effective_user
    log_bot_start(user)
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
    menu = await get_menu(user_lang, user.id)
    # Auto-register admin if not in database
    msg = " أهلا وسهلا  👋" if user_lang.startswith('ar') else "👋 Welcome"
    if await is_admins(user.id) and not await is_registered(user.id):
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO clients 
            (telegram_id, email, phone, fullname, country, registration_date, is_admin)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            user.id,
            "admin@example.com",
            "0000000000",
            update.effective_user.name,
            "Adminland",
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            True
        ))
        conn.commit()
        conn.close()
        await update.message.reply_text(f"{msg} {user.first_name}!",
        reply_markup=menu)
        return
    
    await update.message.reply_text(
        f"{msg} {user.first_name}!",
        reply_markup=menu
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process menu button selections with new navigation structure"""
    text = update.message.text
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'

    # Handle Arabic menu first
    if user_lang.startswith('ar'):
        if text == "تسجيل الدخول 📝":
            await handle_registration(update, context)
        elif text == "إبدأ العمل":
            await update.message.reply_text(
                "الرجاء اختيار خيار:",
                reply_markup=ReplyKeyboardMarkup(MAIN_MENU_OPTIONS_ar, resize_keyboard=True)
            )
        elif text == "القائمة الرئيسية":
            await update.message.reply_text(
                "القائمة الرئيسية:",
                reply_markup=ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT_ar, resize_keyboard=True)
            )
        elif text == "مساعدة":
            await help_us(update, context)
        elif text == "أدخل رابط حساب انستغرام 🔍":
            await handle_channel_verification(update, context)
        elif text == "الملف الشخصي 📋":
            await profile_command(update, context)
        elif text == "قنواتي التي أدخلتها 📌":
            await list_Pages(update, context)
        elif text == "قنواتي التي تم قبولها بعد الدفع 📌":
            await list_Pages_paid(update, context)
        elif text == "قنواتي التي تم إنجازها":
            await list_Pages_Done(update, context)
        elif text == "حذف قناة 🗑":
            await delete_channel(update, context)
        # elif text == "فيديو تعليمي 📹":
        #     await send_educational_video(update, context)
        elif text == "🔙 Main Menu":
            await show_main_menu(update, user)
        elif text == "بدء":
            await start(update, context)
        else:
            await handle_unknown_command(update, user_lang)

    # Handle English menu
    else:
        if text == "📝 Register":
            await handle_registration(update, context)
        elif text == "Get started":
            await update.message.reply_text(
                "Please choose an option:",
                reply_markup=ReplyKeyboardMarkup(MAIN_MENU_OPTIONS, resize_keyboard=True)
            )
        elif text == "Main Menu":
            await update.message.reply_text(
                "Main Menu:",
                reply_markup=ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT, resize_keyboard=True)
            )
        elif text == "Help":
            await help_us(update, context)
        elif text == "🔍 Input Your Instagram Page URL":
            await handle_channel_verification(update, context)
        elif text == "📋 My Profile":
            await profile_command(update, context)
        elif text == "📌 My Pages":
            await list_Pages(update, context)
        elif text == "📌 My Pages Accept":
            await list_Pages_paid(update, context)
        elif text == "My Pages Done":
            await list_Pages_Done(update, context)
        elif text == "🗑 Delete Page":
            await delete_channel(update, context)
        # elif text == "Educational video 📹":
        #     await send_educational_video(update, context)
        elif text == "🔙 Main Menu":
            await show_main_menu(update, user)
        elif text == "Start":
            await start(update, context)
        else:
            await handle_unknown_command(update, user_lang)

async def handle_unknown_command(update: Update, user_lang: str):
    """Send user back to the main menu directly (no error message)."""
    user = update.effective_user

    menu_text = "🏠 القائمة الرئيسية:" if (user_lang or "").startswith("ar") else "🏠 Main Menu:"
    reply_markup = await get_menu(user_lang, user.id)  # نفس قائمتك الديناميكية (Admin/عادي + عربي/إنجليزي)

    await update.message.reply_text(menu_text, reply_markup=reply_markup)


# async def show_appropriate_menu(update: Update, user_lang: str):
#     """Show correct menu based on current state"""
#     current_text = update.message.text
#     if user_lang.startswith('ar'):
#         if current_text in [btn for row in MAIN_MENU_OPTIONS_ar for btn in row]:
#             await update.message.reply_text("الرجاء اختيار خيار:", reply_markup=ReplyKeyboardMarkup(MAIN_MENU_OPTIONS_ar, resize_keyboard=True))
#         elif current_text in [btn for row in MAIN_MENU_WITH_SUPPORT_ar for btn in row]:
#             await update.message.reply_text("القائمة الرئيسية:", reply_markup=ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT_ar, resize_keyboard=True))
#         else:
#             await update.message.reply_text("مرحبا بك!", reply_markup=await get_menu(user_lang, update.effective_user.id))
#     else:
#         if current_text in [btn for row in MAIN_MENU_OPTIONS for btn in row]:
#             await update.message.reply_text("Please choose an option:", reply_markup=ReplyKeyboardMarkup(MAIN_MENU_OPTIONS, resize_keyboard=True))
#         elif current_text in [btn for row in MAIN_MENU_WITH_SUPPORT for btn in row]:
#             await update.message.reply_text("Main Menu:", reply_markup=ReplyKeyboardMarkup(MAIN_MENU_WITH_SUPPORT, resize_keyboard=True))
#         else:
#             await update.message.reply_text("Welcome!", reply_markup=await get_menu(user_lang, update.effective_user.id))





async def help_us(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display available links"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        if user_lang.startswith('ar'):
            user_lang_detail = "ar"
        else:
            user_lang_detail = "en"
        user_id = update.effective_user.id
        
        if await is_banned(user_id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return

        if not await is_registered(user_id):
            msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
            await update.message.reply_text(msg)
            return 
        
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT message_help FROM help_us WHERE lang = %s AND bot = %s",
            (user_lang_detail,"client",)
        )
        result = c.fetchone()
        if result:
            res = result[0]
            reply_markup = await get_menu(user_lang, user_id)
            await update.message.reply_text(res, reply_markup=reply_markup)
        else:
            reply_markup = await get_menu(user_lang, user_id)
            await update.message.reply_text("Help Message", reply_markup=reply_markup)
        conn.close()
    except Exception as e:
        logger.error(f"Help error: {e}")
        msg = "لا يمكن تحميل رسالة المساعدة حاليا ⚠️" if user_lang.startswith('ar') else "⚠️ Error in Help us"
        await update.message.reply_text(msg)



async def show_main_menu(update: Update, user):
    """Display the appropriate main menu (works for messages + callback queries)"""
    user_lang = update.effective_user.language_code or 'en'
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "Main Menu:",
            reply_markup=await get_menu(user_lang, user.id)
        )


async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start registration process"""
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
    if await is_registered(user.id):
        msg = " أنت بالفعل مسجل ℹ️" if user_lang.startswith('ar') else "ℹ️ You're already registered!"
        await update.message.reply_text(msg)
        return
    if user_lang.startswith('ar'):
        keyboard = [["إلغاء ❌"]]
        msg = "من فضلك قم بإدخال بريدك الإلكتروني للمتابعة"
    else:
        keyboard = [["Cancel ❌"]]
        msg = "Please enter your email address:"
        
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return EMAIL



async def list_Pages_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all submitted Pages for the current user with likes count"""
    user = update.effective_user
    
    # Check if user is banned
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
        
    try:
        # Check if user is registered
        if not await is_registered(user.id):
            msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
            await update.message.reply_text(msg)
            return
            
        conn = get_conn()
        c = conn.cursor()
        
        # Get Pages with likes count FOR CURRENT USER ONLY
        c.execute("""
            SELECT l.description, l.youtube_link, l.channel_id, l.submission_date,l.subscription_count,
                   COALESCE(k.channel_likes, 0) AS likes_count
            FROM links l
            LEFT JOIN likes k ON l.id = k.id
            WHERE l.added_by = %s
            ORDER BY l.submission_date DESC
        """, (user.id,))  # Make sure user.id is correctly passed
        
        Pages = c.fetchall()
        conn.close()
        
        if not Pages:
            msg = "ليس لدي قنوات تم قبولها يرجى إضافة قنوات أو الدفع للقنوات التي تم إضافتها سابقا📭" if user_lang.startswith('ar') else "📭 You haven't submitted any Pages yet or did not paid for them."
            await update.message.reply_text(msg)
            return
            
        response = ["📋 Your Submitted Pages:"]
        for idx, (description, youtube_link, channel_id, submission_date, subscription_count, likes) in enumerate(Pages, 1):
            if user_lang.startswith('ar'):
                response.append(
                    f"{idx}. {description}\n"
                    f"🔗 {youtube_link}\n"
                    f"🆔 معرف القناة: {channel_id}\n"
                    f"📅 تاريخ إضافتها: {submission_date}\n"
                    f"❤️ المطلوب: {subscription_count} متابع\n"
                    f"❤️ عدد الاشتراكات: {likes}\n"
                    f"{'-'*40}"
                )
            else:
                response.append(
                    f"{idx}. {description}\n"
                    f"🔗 {youtube_link}\n"
                    f"🆔 Page ID: {channel_id}\n"
                    f"📅 Submitted: {submission_date}\n"
                    f"❤️ Required followers: {subscription_count}\n"
                    f"❤️ Likes: {likes}\n"
                    f"{'-'*40}"
                )
            
        # Split long messages to avoid Telegram message limits
        message = "\n\n".join(response)
        if len(message) > 4096:
            for x in range(0, len(message), 4096):
                await update.message.reply_text(message[x:x+4096])
        else:
            await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"List Pages error: {str(e)}")
        msg = " حدث خطأ أثناء إضافة القناة ❌" if user_lang.startswith('ar') else "❌ Error retrieving your Pages"
        await update.message.reply_text(msg)



async def list_Pages_Done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all submitted Pages for the current user with likes count"""
    user = update.effective_user
    
    # Check if user is banned
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
        
    try:
        # Check if user is registered
        if not await is_registered(user.id):
            msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
            await update.message.reply_text(msg)
            return
            
        conn = get_conn()
        c = conn.cursor()
        
        # Get Pages with likes count FOR CURRENT USER ONLY
        c.execute("""
            SELECT channel_name, url , channel_id, subscription_count, channel_likes FROM likes
            WHERE user_id = %s AND status = %s
            ORDER BY id DESC
        """, (user.id,True,))  # Make sure user.id is correctly passed
        
        Pages = c.fetchall()
        conn.close()
        
        if not Pages:
            msg = "ليس لدي قنوات تم قبولها يرجى إضافة قنوات أو الدفع للقنوات التي تم إضافتها سابقا📭" if user_lang.startswith('ar') else "📭 You haven't submitted any Pages yet or did not paid for them."
            await update.message.reply_text(msg)
            return
        
        response = ["📋 Your Submitted Pages:"]
        for idx, (channel_name, url, channel_id, subscription_count, channel_likes) in enumerate(Pages, 1):
            if user_lang.startswith('ar'):
                response.append(
                    f"{idx}. {channel_name}\n"
                    f"🔗 {url}\n"
                    f"🆔 معرف القناة: {channel_id}\n"
                    f"❤️ عدد الاشتراكات: {subscription_count}\n"
                    f"❤️ عدد اللايكات: {channel_likes}\n"
                    f"{'-'*40}"
                )
            else:
                response.append(
                    f"{idx}. {channel_name}\n"
                    f"🔗 {url}\n"
                    f"🆔 Page ID: {channel_id}\n"
                    f"❤️ Description Count {subscription_count}\n"
                    f"❤️ Likes: {channel_likes}\n"
                    f"{'-'*40}"
                )
            
        # Split long messages to avoid Telegram message limits
        message = "\n\n".join(response)
        if len(message) > 4096:
            for x in range(0, len(message), 4096):
                await update.message.reply_text(message[x:x+4096])
        else:
            await update.message.reply_text(message)

    except Exception as e:
        logger.error(f"List Pages error: {str(e)}")
        msg = " حدث خطأ أثناء إضافة القناة ❌" if user_lang.startswith('ar') else "❌ Error retrieving your Pages"
        await update.message.reply_text(msg)



# ========== INSTAGRAM PROFILE VERIFICATION ==========

async def process_channel_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process Instagram profile URL (no external API calls).

    - Extract Instagram username from the provided link
    - Generate deterministic channel_id from the username (same username => same channel_id)
    - Ask the user for the required follower count
    """
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'

    try:
        # Safety checks (so it works سواء ضغط زر القائمة أو لصق الرابط مباشرة)
        if await is_banned(user.id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        if not await is_registered(user.id):
            msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
            await update.message.reply_text(msg)
            return ConversationHandler.END

        raw_input = (update.message.text or "").strip()

        username = extract_instagram_username(raw_input)
        if not username:
            if user_lang.startswith('ar'):
                msg = (
                    "❌ صيغة رابط انستغرام غير صحيحة.\n"
                    "✅ مثال صحيح:\n"
                    "https://www.instagram.com/username/\n"
                    "🔸 الرجاء إدخال رابط حساب (Page) وليس رابط منشور/ريل."
                )
            else:
                msg = (
                    "❌ Invalid Instagram URL format.\n"
                    "✅ Example:\n"
                    "https://www.instagram.com/username/\n"
                    "🔸 Please send a Page URL (not a post/reel link)."
                )
            await update.message.reply_text(msg)
            return CHANNEL_URL  # allow retry

        # Extracted from link
        channel_name = username  # Instagram username (best available without external APIs)
        url = canonical_instagram_profile_url(username)  # canonical URL to keep it consistent in DB

        conn = get_conn()
        try:
            c = conn.cursor()

            # Limit: max 10 active Pages per user (same logic as handle_channel_verification)
            c.execute("SELECT COUNT(*) FROM links_success WHERE added_by = %s", (user.id,))
            row = c.fetchone()
            current_count = int(row[0]) if row and row[0] is not None else 0
            if current_count >= 10:
                msg = "🚫 لديك عدد كبير من القنوات يرجى الانتظار لحين اكتمال مهمة قناة" if user_lang.startswith('ar') else "🚫 You have alot of Pages please wait for end one channel"
                await update.message.reply_text(msg)
                return ConversationHandler.END

            # Resolve channel_id (stable):
            # 1) If this Instagram URL/username exists in DB already, reuse the stored channel_id
            # 2) Otherwise generate deterministic ID from username
            # This guarantees that adding the same account many times yields the same channel_id.
            existing_id = None

            # Prefer matching by canonical URL first (case-insensitive)
            c.execute("SELECT channel_id FROM links WHERE LOWER(youtube_link) = LOWER(%s) LIMIT 1", (url,))
            row = c.fetchone()
            if not row:
                c.execute("SELECT channel_id FROM links_success WHERE LOWER(youtube_link) = LOWER(%s) LIMIT 1", (url,))
                row = c.fetchone()

            # Fallback to matching by stored description if URL differs in old rows
            if not row:
                c.execute("SELECT channel_id FROM links WHERE LOWER(description) = LOWER(%s) LIMIT 1", (channel_name,))
                row = c.fetchone()
            if not row:
                c.execute("SELECT channel_id FROM links_success WHERE LOWER(description) = LOWER(%s) LIMIT 1", (channel_name,))
                row = c.fetchone()

            if row and row[0]:
                existing_id = str(row[0]).strip()

            channel_id = existing_id or generate_instagram_channel_id(username)

            # Optional hint if the user is repeating the same URL (but we still allow it)
            c.execute(
                """
                SELECT COUNT(*)
                FROM links_success
                WHERE added_by = %s AND LOWER(youtube_link) = LOWER(%s)
                """,
                (user.id, url)
            )
            cnt = c.fetchone()
            already_submitted_same_url = int(cnt[0]) if cnt and cnt[0] is not None else 0
            if already_submitted_same_url > 0:
                hint = (
                    "ℹ️ هذا الحساب تم إدخاله سابقاً، وسيتم استخدام نفس معرف القناة مرة أخرى." 
                    if user_lang.startswith('ar')
                    else "ℹ️ This account was submitted before. The same Page ID will be reused."
                )
                await update.message.reply_text(hint)

            context.user_data['channel_data'] = {
                'url': url,
                'channel_id': channel_id,
                'channel_name': channel_name
            }

            # Create follower keyboard
            if user_lang.startswith('ar'):
                keyboard = [["100 متابع", "1000 متابع"], ["إلغاء ❌"]]
                msg = "اختر عدد المتابعين المطلوب:"
            else:
                keyboard = [["100 Followers", "1000 Followers"], ["Cancel ❌"]]
                msg = "Choose the desired follower count:"

            await update.message.reply_text(
                msg,
                reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            )
            return SUBSCRIPTION_CHOICE

        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Instagram Page processing error: {str(e)}")
        msg = "حدث خطأ غير متوقع يرجى إعادة المحاولة لاحقا ❌" if user_lang.startswith('ar') else "❌ An error occurred. Please try again."
        await update.message.reply_text(msg, reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END


# def filter_non_arabic_words(text, url):    
#     # Regex to match only English words and spaces
#     english_re = re.compile(r'\b[a-zA-Z0-9]+\b(?:\s+[a-zA-Z0-9]+\b)*')
    
#     # Find all matching English parts
#     matches = english_re.findall(text)
    
#     # Join them into a single string and remove extra spaces
#     filtered_text = ' '.join(''.join(matches).split())

#     # If no English words are found, check for @ in URL
#     if not filtered_text.strip():
#         at_match = re.search(r'@([a-zA-Z0-9_]+)', url)
#         if at_match:
#             return at_match.group(1)  # Return the part after @
#         else:
#             return text  # Return the original text if no @ is found

#     return filtered_text


#the best
def filter_non_arabic_words(text: str, url: str) -> str:
    """
    Filters text to keep only English words, numbers, and spaces
    - Returns extracted content if found
    - Falls back to @username from URL if no English content
    - Returns original text as last resort
    
    :param text: Input text to filter
    :param url: URL for fallback extraction
    :return: Filtered text according to rules
    """
    
    # Regex explanation:
    # - r'^[a-zA-Z0-9 ]+$': Match entire string containing only:
    #   - a-z (lowercase English letters)
    #   - A-Z (uppercase English letters)
    #   - 0-9 (numbers)
    #   - Spaces
    # - The '^' and '$' ensure full string match
    english_pattern = re.compile(r'^[a-zA-Z0-9 ]+$')
    
    # 1. Split text into potential segments
    segments = text.split()
    
    # 2. Filter valid English segments
    valid_segments = [
        segment for segment in segments 
        if english_pattern.match(segment)
    ]
    
    # 3. Reconstruct filtered text with single spaces
    filtered_text = ' '.join(valid_segments)
    
    # 4. Fallback to URL @username if no valid content
    if not filtered_text:
        username_match = re.search(r'@([a-zA-Z0-9_]+)', url)
        return username_match.group(1) if username_match else text
    
    return filtered_text

# def filter_non_arabic_words(text):
#     # This regex will help us detect if a word contains any Arabic character.
#     arabic_re = re.compile(r'[\u0600-\u06FF]')
#     # arabic_re = re.compile(r'[a-zA-Z\s]+')
    
#     # Split the text into words. (This simple split may not handle punctuation perfectly.)
#     words = text.split()
#     filtered_words = []
    
#     for word in words:
#         # If the word does NOT contain any Arabic letter, keep it.
#         if not arabic_re.search(word):
#             filtered_words.append(word)
    
#     # Join the words back into a single string.
#     return ' '.join(filtered_words)


# ========== ADDITIONAL FUNCTIONS ==========
async def list_Pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all submitted Pages for the current user with likes count"""
    user = update.effective_user
    
    # Check if user is banned
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
        
    try:
        # Check if user is registered
        if not await is_registered(user.id):
            msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
            await update.message.reply_text(msg)
            return
            
        conn = get_conn()
        c = conn.cursor()
        
        # Get Pages with likes count FOR CURRENT USER ONLY
        c.execute("""
            SELECT id, description, youtube_link, channel_id, submission_date, id_pay
            FROM links_success
            WHERE added_by = %s
            ORDER BY submission_date DESC
        """, (user.id,))  # Make sure user.id is correctly passed
        
        Pages = c.fetchall()
        keyboard = []
        conn.close()
        
        if not Pages:
            msg = " ليس لدي قنوات تمت إضافتها بعد 📭" if user_lang.startswith('ar') else "📭 You haven't submitted any Pages yet"
            await update.message.reply_text(msg)
            return
            
        # response = ["📋 Your Submitted Pages:"]
        # for idx, (name, url, channel_id, date, likes) in enumerate(Pages, 1):
        #     if user_lang.startswith('ar'):
        #         response.append(
        #             f"{idx}. {name}\n"
        #             f"🔗 {url}\n"
        #             f"🆔 معرف القناة: {channel_id}\n"
        #             f"📅 تاريخ إضافتها: {date}\n"
        #             f"❤️ عدد الاشتراكات: {likes}\n"
        #             f"{'-'*40}"
        #         )
        #     else:
        #         response.append(
        #             f"{idx}. {name}\n"
        #             f"🔗 {url}\n"
        #             f"🆔 Channel ID: {channel_id}\n"
        #             f"📅 Submitted: {date}\n"
        #             f"❤️ Likes: {likes}\n"
        #             f"{'-'*40}"
        #         )
            
        # # Split long messages to avoid Telegram message limits
        # message = "\n\n".join(response)
        # if len(message) > 4096:
        #     for x in range(0, len(message), 4096):
        #         await update.message.reply_text(message[x:x+4096])
        # else:
        #     await update.message.reply_text(message)

        for channel in Pages:
            id, description, youtube_link, channel_id, submission_date, id_pay = channel
            
            # English format: ID | Short Description | payment
            # if user_lang != 'ar':
            #     button_text = f"🆔{id} |{description}| 💳{id_pay or '?'}"
            # # Arabic format: رقم | وصف مختصر | الدفع
            # else:
            #     button_text = f"🆔{id} |{description}| 💳{id_pay or '؟'}"
            button_text = f"🆔{id} |{description}| 💳{id_pay or '?'}"
            
            
            # Truncate to 30 characters for mobile display
            button_text = button_text[:40] + ".." if len(button_text) > 40 else button_text
            
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"channel_{id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = "📋 Your Pages:" if user_lang != 'ar' else "📋 قنواتك:"
        await update.message.reply_text(message_text, reply_markup=reply_markup)
        
    except Exception as e:
        logger.error(f"List Pages error: {str(e)}")
        error_msg = "❌ Error retrieving Pages" if user_lang != 'ar' else "❌ خطأ في استرجاع القنوات"
        await update.message.reply_text(error_msg)
    finally:
        conn.close()
        
async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allow users to cancel registration at any point"""
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    context.user_data.clear()
    msg = "تم إلغاء التسجيل ❌" if user_lang.startswith('ar') else "❌ Registration cancelled"
    await update.message.reply_text(msg,reply_markup=await get_menu(user_lang,user.id))
    return ConversationHandler.END


# ========== REGISTRATION FLOW HANDLERS ==========
async def email_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and store email"""
    user_lang = update.effective_user.language_code or 'en'
    email = update.message.text
    email_check = email.lower()
    if email in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_registration(update, context)
        return ConversationHandler.END

    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        msg = "❌ Invalid email format" if user_lang != 'ar' else "❌ صيغة البريد غير صحيحة"
        await update.message.reply_text(msg)
        return EMAIL
        
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM clients WHERE email = %s",(email_check,))
        user_data = c.fetchone()
        if user_data:
            error_msg = "❌ Your Email has Already Exists Change To A Deferent Email" if user_lang != 'ar' else "❌ هذا البريد الإلكتروني مستخدم بالفعل أدخل بريد آخر"
            await update.message.reply_text(error_msg)
            return EMAIL 

    except Exception as e:
        logger.error(f"Database Email error: {str(e)}")
        msg = "❌ Invalid email format" if user_lang != 'ar' else "❌ صيغة البريد الإلكتروني غير صحيحة"
        await update.message.reply_text(msg)
        return EMAIL
       
    finally:
        conn.close()
    # Generate and send code
    code = generate_confirmation_code()
    if not send_confirmation_email(email, code):
        error_msg = "Failed to send code" if user_lang != 'ar' else "فشل إرسال الرمز"
        await update.message.reply_text(error_msg)
        return EMAIL

    # Store code in context
    context.user_data["confirmation_code"] = code
    context.user_data["email"] = email

    # Ask for code verification
    cancel_btn = "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"
    await update.message.reply_text(
        "📧 A confirmation code has been sent to your email or in spam. Please enter it here:" if user_lang != 'ar' 
        else "📧 تم إرسال رمز التأكيد إلى بريدك الإلكتروني أو في رسائل البريد العشوائي (سبام) . الرجاء إدخاله هنا:",
        reply_markup=ReplyKeyboardMarkup([[cancel_btn]], resize_keyboard=True)
    )
    return CODE_VERIFICATION

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle received contact information and determine country automatically"""
    
    user_lang = update.effective_user.language_code or 'en'
    contact = update.message.contact
    # datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Verify the contact belongs to the user
    if update.message.text and update.message.text.strip() in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_registration(update, context)
        return ConversationHandler.END
        
    if contact.user_id != update.effective_user.id:
        msg = " من فضلك قم بتأكيد رقم هاتفك أولا ❌" if user_lang.startswith('ar') else "❌ Please share your own phone number!"
        await update.message.reply_text(msg)
        return PHONE
    
    phone_number = "+" + contact.phone_number
    # print(f"{phone_number}")
    context.user_data["phone"] = phone_number
    
    try:
        # Validate international format
        if not phone_number.startswith("+"):
            msg = "يجب أن يحتوي رقم الهاتف على أرقام فقط مسبوقة بإشارة +" if user_lang.startswith('ar') else "Phone number must include country code (e.g., +123456789)"
            raise ValueError(msg)
            
        # Parse phone number to determine country
        parsed_number = phonenumbers.parse(phone_number, None)
        country_name = geocoder.description_for_number(parsed_number, "en")
        country_name = country_name if country_name else "Unknown"
        
    except (phonenumbers.NumberParseException, ValueError) as e:
        logger.error(f"Phone number error: {e}")
        msg = "صيغة رقم الهاتف غير صحيحة يجب أن يحتوي رقم الهاتف على أرقام فقط مسبوقة بإشارة +" if user_lang.startswith('ar') else "❌ Invalid phone number format. Please share your contact using the button below and ensure it includes your country code (e.g., +123456789)."
        msg1 = " من فضلك قم بتأكيد رقم هاتف بالضغط على خيار تأكيد رقم الهاتف من القائمة 📱" if user_lang.startswith('ar') else "📱 Share Phone Number"
        await update.message.reply_text(
            msg,
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton(msg1, request_contact=True)]],
                resize_keyboard=True
            )
        )
        return PHONE
    
    # Proceed with registration
    fullname = update.effective_user.name
    user_data = context.user_data
    email = user_data["email"]
    registration_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            INSERT INTO clients 
            (telegram_id, email, phone, fullname, country, registration_date)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            update.effective_user.id,
            email,
            phone_number,
            fullname,
            country_name,
            registration_date
        ))
        conn.commit()
        if user_lang.startswith('ar'):
            await update.message.reply_text(
                # "✅ Registration complete!\n\n"
                f"✅ اكتملت عملية التسجيل بنجاح :\n"
                f"👤 أسمك: {escape_markdown(fullname)}\n"
                f"📧 بريدك الإلكتروني: {escape_markdown_2(str(email))}\n"
                f"📱 رقم هاتفك: {escape_markdown_2(phone_number)}\n"
                f"🌍 بلدك: {escape_markdown(country_name)}\n"
                f"⭐ تاريخ التسجيل: {escape_markdown_2(str(registration_date))}",
                reply_markup=await get_menu(user_lang,update.effective_user.id)
            )
        else:
            await update.message.reply_text(
                # "✅ Registration complete!\n\n"
                f"✅ Registration Complete:\n"
                f"👤 Name: {escape_markdown(fullname)}\n"
                f"📧 Email: {escape_markdown_2(str(email))}\n"
                f"📱 Phone: {escape_markdown_2(phone_number)}\n"
                f"🌍 Country: {escape_markdown(country_name)}\n"
                f"⭐ Registration Date: {escape_markdown_2(str(registration_date))}",
                reply_markup=await get_menu(user_lang,update.effective_user.id)
            )
        # Show main menu after registration       
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        msg = " فشلت عملية التسجيل يرجى إعادة المحاولة. ❌" if user_lang.startswith('ar') else "❌ Registration failed. Please try again."
        await update.message.reply_text(msg)
        return ConversationHandler.END
    finally:
        conn.close()
    
    return ConversationHandler.END

async def handle_invalid_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lang = update.effective_user.language_code or 'en'
    """Handle non-contact input in phone number stage"""
    
    if update.message.text and update.message.text.strip() in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_registration(update, context)
        return ConversationHandler.END
    
    msg = " قم بتأكيد رقم هاتفك من خلال الضغط على خيار تأكيد الرقم من القائمة 📱" if user_lang.startswith('ar') else "📱 Share Phone Number"
    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(msg, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    msg = " من فضلك استخدم الزر في الأسفل من القائمة لتأكيد رقم هاتفك ❌" if user_lang.startswith('ar') else "❌ Please use the button below to share your phone number."
    await update.message.reply_text(
        msg,
        reply_markup=contact_keyboard
    )
    return PHONE

# async def name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     """Store full name"""
#     # name = update.message.text.strip()
#     # if len(name) < 2 or len(name) > 100:
#     #     await update.message.reply_text("❌ Name must be 2-100 characters")
#     #     return FULLNAME
#     name = update.effective_user.first_name
#     context.user_data["fullname"] = name
#     await update.message.reply_text("🌍 Enter your country:")
#     return COUNTRY

# async def country_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
#     """Complete registration"""
#     country = update.message.text.strip()
#     if len(country) < 2 or len(country) > 60:
#         await update.message.reply_text("❌ Country name must be 2-60 characters")
#         return COUNTRY
#     name = update.effective_user.name
#     user_data = context.user_data
#     phone1 = "+" + user_data["phone"]
#     try:
        
#         conn = get_conn()
#         c = conn.cursor()
#         c.execute("""
#             INSERT INTO clients 
#             (telegram_id, email, phone, fullname, country, registration_date)
#             VALUES (%s, %s, %s, %s, %s, %s)
#         """, (
#             update.effective_user.id,
#             user_data["email"],
#             phone1,
#             name,
#             country,
#             datetime.now().strftime("%Y-%m-%d %H:%M:%S")
#         ))
#         conn.commit()
#         await update.message.reply_text(
#             "✅ Registration complete!",
#             reply_markup=await get_menu(update.effective_user.id)
#         )
#     except Exception as e:
#         logger.error(f"Database error: {str(e)}")
#         await update.message.reply_text("❌ Registration failed. Please try again.")
#     finally:
#         conn.close()
#     return ConversationHandler.END

# ========== ADMIN FUNCTIONALITY ==========
async def handle_channel_verification(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start channel verification process"""
    user = update.effective_user
    user_idd = user.id
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
    if not await is_registered(user.id):
        msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
        await update.message.reply_text(msg)
        return ConversationHandler.END
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM links_success where added_by = %s", (user_idd,))
        result = c.fetchone()
        re = result[0]
        if result[0] < 10:
            msg = " من فضلك أدخل رابط حساب انستغرام للتحقق والمتابعة 🔗" if user_lang.startswith('ar') else "🔗 Please input your Instagram Page URL:"
            main_btn   = "القائمة الرئيسية" if user_lang.startswith('ar') else "Main Menu"

            await update.message.reply_text(
                msg,
                reply_markup=ReplyKeyboardMarkup([[main_btn]], resize_keyboard=True)
            )
            return CHANNEL_URL

        else:
            msg = "🚫 لديك عدد كبير من القنوات يرجى الانتظار لحين اكتمال مهمة قناة" if user_lang.startswith('ar') else "🚫 You have alot of Pages please wait for end one Page"
            await update.message.reply_text(msg)
            return ConversationHandler.END
    except psycopg2.Error as e:
        logger.error(f"Ban check failed: {str(e)}")
        return False
    finally:
        conn.close()


async def handle_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel access control"""
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    if not await is_admins(user.id):
        await update.message.reply_text("🚫 Access denied!")
        return
    
    if user_lang.startswith('ar'):
        keyboard = [
            # ["📊 User Statistics", "📢 Broadcast Message"],
            
            ["🚫 Ban Client", "✅ UnBan Client"],
            ["🚫 Ban User", "✅ UnBan User"],
            ["حذف قناة 🗑", "🗑 Delete  All Pages"], # Updated buttons
            ["🔙 Main Menu"]
        ]
        await update.message.reply_text(
            "👑 Admin Panel:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    else:
        keyboard = [
            # ["📊 User Statistics", "📢 Broadcast Message"],
            
            ["🚫 Ban Client", "✅ UnBan Client"],
            ["🚫 Ban User", "✅ UnBan User"],
            ["🗑 Delete Page", "🗑 Delete  All Pages"], # Updated buttons
            ["🔙 Main Menu"]
        ]
        await update.message.reply_text(
            "👑 Admin Panel:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )

# ========== IMPROVED ERROR HANDLING ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception:", exc_info=context.error)

    user_lang = "en"
    if update and update.effective_user:
        user_lang = update.effective_user.language_code or "en"

    if isinstance(context.error, errors.UniqueViolation):
        msg_text = " خطأ في الإدخال ❌" if (user_lang or "").startswith('ar') else "❌ This entry already exists!"
    elif isinstance(context.error, errors.ForeignKeyViolation):
        msg_text = " مصدر غير معروف ❌" if (user_lang or "").startswith('ar') else "❌ Invalid reference!"
    else:
        msg_text = " أمر غير معروف يرجى إعادة المحاولة ⚠️" if (user_lang or "").startswith('ar') else "⚠️ An error occurred. Please try again."

    msg = update.effective_message if update else None
    if msg:
        try:
            uid = update.effective_user.id if (update and update.effective_user) else 0
            await msg.reply_text(msg_text, reply_markup=await get_menu(user_lang, uid) if uid else None)
        except Exception:
            # last-resort
            try:
                await msg.reply_text(msg_text)
            except Exception:
                pass

        
# ========== ADMIN DELETE Pages ==========
async def delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only channel deletion flow"""
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
    if not await is_registered(user.id):
        msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
        await update.message.reply_text(msg)
        return ConversationHandler.END
    # user = update.effective_user
    # if str(user.id) != ADMIN_TELEGRAM_ID:
    #     await update.message.reply_text("🚫 Access denied!")
    #     return ConversationHandler.END
    msg = "من فضلك أدخل رابط الحساب لحذفها" if user_lang.startswith('ar') else "Enter Page URL to delete:"
    await update.message.reply_text(msg)
    return "AWAIT_CHANNEL_URL"

async def delete_channel_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only channel deletion flow"""
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END
    if not await is_registered(user.id):
        msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
        await update.message.reply_text(msg)
        return ConversationHandler.END
    # user = update.effective_user
    # if str(user.id) != ADMIN_TELEGRAM_ID:
    #     await update.message.reply_text("🚫 Access denied!")
    #     return ConversationHandler.END
    msg = "من فضلك أدخل رابط الحساب لحذفها" if user_lang.startswith('ar') else "Enter Page URL to delete:"
    await update.message.reply_text(msg)
    return "AWAIT_CHANNEL_URL_ACCEPT"

async def confirm_delete_accept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and delete channel"""
    user_lang = update.effective_user.language_code or 'en'
    url = update.message.text.strip()
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT description FROM links WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        result = c.fetchone()
        
        if not result:
            msg = " عذرا القناة غير موجودة لحذفها ❌" if user_lang.startswith('ar') else "❌ Page not found"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        channel_name = result[0]
        c.execute("SELECT id FROM links WHERE youtube_link = %s and added_by = %s", (url, update.effective_user.id,))
        result_id = c.fetchone()
        msg = " عذرا القناة غير موجودة لحذفها ❌" if user_lang.startswith('ar') else "❌ Page not found"
        if not result_id:
            await update.message.reply_text(msg)
            return ConversationHandler.END
        # result_id_for_link = result_id[0]
        c.execute("DELETE FROM links WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        # c.execute("DELETE FROM user_link_status WHERE link_id = %s", (result_id_for_link,))
        conn.commit()
        if user_lang.startswith('ar'):
            await update.message.reply_text(
                f"✅ تم حذف القناة بنجاح :\n"
                f"📛 أسم القناة : {channel_name}\n"
                f"🔗 رابط الحساب: {url}"
            )
        else:
            await update.message.reply_text(
                f"✅ Page deleted:\n"
                f"📛 Name: {channel_name}\n"
                f"🔗 URL: {url}"
            )
    finally:
        conn.close()
    return ConversationHandler.END

async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and delete channel"""
    user_lang = update.effective_user.language_code or 'en'
    url = update.message.text.strip()
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT description FROM links_success WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        result = c.fetchone()
        if not result:
            msg = " عذرا القناة غير موجودة لحذفها ❌" if user_lang.startswith('ar') else "❌ Page not found"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        channel_name = result[0]
        c.execute("SELECT id FROM links_success WHERE youtube_link = %s and added_by = %s", (url, update.effective_user.id,))
        result_id = c.fetchone()
        msg = " عذرا القناة غير موجودة لحذفها ❌" if user_lang.startswith('ar') else "❌ Page not found"
        if not result_id:
            await update.message.reply_text(msg)
            return ConversationHandler.END
        # result_id_for_link = result_id[0]
        c.execute("DELETE FROM links_success WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        # c.execute("DELETE FROM user_link_status WHERE link_id = %s", (result_id_for_link,))
        conn.commit()
        if user_lang.startswith('ar'):
            await update.message.reply_text(
                f"✅ تم حذف القناة بنجاح :\n"
                f"📛 أسم القناة : {channel_name}\n"
                f"🔗 رابط الحساب: {url}"
            )
        else:
            await update.message.reply_text(
                f"✅ Page deleted:\n"
                f"📛 Name: {channel_name}\n"
                f"🔗 URL: {url}"
            )
    finally:
        conn.close()
    return ConversationHandler.END


AWAIT_CHANNEL_URL_ADMIN, AWAIT_CHANNEL_ADDER_ADMIN = range(2)

async def delete_channel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only channel deletion flow: prompt for channel URL."""
    user = update.effective_user
    if not await is_admins(user.id):
        await update.message.reply_text("🚫 Access denied!")
        return ConversationHandler.END

    await update.message.reply_text("Enter Page URL to delete:")
    return "AWAIT_CHANNEL_URL_ADMIN"

# Step 2: Receive the Channel URL and prompt for the adder
async def receive_channel_url_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the channel URL and prompt for the adder."""
    # Save the channel URL in user_data for later retrieval
    context.user_data["channel_url"] = update.message.text.strip()
    await update.message.reply_text("And enter the 'adder' (the user who added the Page):")
    return "AWAIT_ADDER"

# Step 3: Receive the adder and confirm deletion
async def confirm_delete_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm deletion using the stored channel URL and the provided adder."""
    adder = update.message.text.strip()  # Now this is the admin's input text, not a Message object
    url = context.user_data.get("channel_url")
    
    if not url:
        await update.message.reply_text("Page URL not found. Aborting deletion.")
        return ConversationHandler.END

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT description FROM links WHERE youtube_link = %s and adder = %s", (url, adder))
        result = c.fetchone()
        if not result:
            await update.message.reply_text("❌ Page not found")
            return ConversationHandler.END
            
        channel_name = result[0]
        c.execute("SELECT id FROM links WHERE youtube_link = %s and adder = %s", (url, adder,))
        result_id = c.fetchone()
        if not result_id:
            await update.message.reply_text("❌ Page not found")
            return ConversationHandler.END
        
        # c.execute("DELETE FROM user_link_status WHERE link_id = %s", (result_id_for_link,))
        c.execute("DELETE FROM links WHERE youtube_link = %s and adder = %s", (url, adder))
        conn.commit()
        await update.message.reply_text(
            f"✅ Page deleted:\n"
            f"📛 Name: {channel_name}\n"
            f"🔗 URL: {url}"
            f"👤 ADDER: {adder}"
        )
    finally:
        conn.close()

    return ConversationHandler.END
async def unban_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lang = update.effective_user.language_code or 'en'
    admin = update.effective_user
    if not await is_admins(admin.id):
        await update.message.reply_text("🚫 Access denied!")
        return

    if context.args:
        target_fullname = " ".join(context.args).strip()
    else:
        await update.message.reply_text("Usage: /uc <full name>")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id FROM clients
            WHERE fullname = %s 
        """, (target_fullname,))
        check_client = c.fetchone()
        if check_client:
            c.execute("""
                SELECT telegram_id FROM clients
                WHERE is_banned = True And fullname = %s 
            """, (target_fullname,))
            check = c.fetchone()
            if check:
                c = conn.cursor()
                c.execute("""
                    UPDATE clients 
                    SET is_banned = False 
                    WHERE fullname = %s
                """, (target_fullname,))
                
                if c.rowcount == 0:
                    await update.message.reply_text("❌ Client not found in database")
                    return
                    
                conn.commit()
                await update.message.reply_text(f"✅ Client '{target_fullname}' has been unbanned")
                
                c.execute("""
                    SELECT telegram_id FROM clients
                    WHERE fullname = %s
                """, (target_fullname,))
                user_data = c.fetchone()
                if user_data and user_data[0]:
                    try:
                        if user_lang.startswith('ar'):
                            await context.bot.send_message(
                                chat_id=user_data[0],
                                text=" تم السماح لك باستخدام هذا البوت من جديد ✅"
                            )
                        else:
                            await context.bot.send_message(
                                chat_id=user_data[0],
                                text="✅ Your access to this bot has been restored"
                            )
                    except Exception as e:
                        logger.error(f"Unban notification failed: {str(e)}")
            else:
                await update.message.reply_text("❌ Client Already restored")
                return
        else:
            await update.message.reply_text("❌ NO CLIENT")
            return
    finally:
        conn.close()

async def is_banned(telegram_id: int) -> bool:
    """Check if user is banned with DB connection handling"""
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT is_banned FROM clients WHERE telegram_id = %s", (telegram_id,))
        result = c.fetchone()
        return bool(result and result[0] == 1)
    except psycopg2.Error as e:
        logger.error(f"Ban check failed: {str(e)}")
        return False
    finally:
        conn.close()
                
# ========== Ban Client FUNCTIONALITY ==========
async def ban_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user from using the bot"""
    user_lang = update.effective_user.language_code or 'en'
    admin = update.effective_user
    if not await is_admins(admin.id):
        await update.message.reply_text("🚫 Access denied!")
        return

    # Extract user ID from message (could be reply or direct input)
    # target = None
    # if update.message.reply_to_message:
    #     target = update.message.reply_to_message.from_user.strip()
    if context.args:
        try:
            target = " ".join(context.args).strip()
        except ValueError:
            await update.message.reply_text("Usage: /bc <fullname> or reply to Clients's message")
            return
    else:
        await update.message.reply_text("Usage: /bc <fullname> or reply to Clients's message")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id FROM clients
            WHERE fullname = %s 
        """, (target,))
        check_client = c.fetchone()
        if check_client:
            # Ban Client
            c.execute("""
                SELECT telegram_id FROM clients
                WHERE is_banned = False And fullname = %s
            """, (target,))
            check = c.fetchone()
            if check:
                c.execute("""
                    UPDATE clients 
                    SET is_banned = True 
                    WHERE fullname = %s
                """, (target,))
                if c.rowcount == 0:
                    await update.message.reply_text("❌ Client not found in database")
                    return
                    
                conn.commit()
                await update.message.reply_text(f"✅ Client {target} has been banned")
                
                # Notify banned user if possible
                c.execute("""
                    SELECT telegram_id FROM clients
                    WHERE fullname = %s
                """, (target,))
                user_data = c.fetchone()
                if user_data and user_data[0]:
                    if user_lang.startswith('ar'):
                        await context.bot.send_message(
                            chat_id=user_data[0],
                            text=" لقد تم حظرك لاستخدام هذه البوت لعدم التقيد بسياسة الاستخدام 🚫"
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_data[0],
                            text="🚫 Your access to this bot has been revoked"
                        )
            else:
                await update.message.reply_text("❌ Client Already revoked")
                return
        else:
            await update.message.reply_text("❌ NO CLIENT")
            return
    except Exception as e:
        logger.error(f"Ban notification failed: {str(e)}")
            
    finally:
        conn.close()
def escape_markdown(text: str) -> str:
    """Escape all MarkdownV2 special characters"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + char if char in escape_chars else char for char in text])

def escape_markdown_2(text: str) -> str:
    """Escape all MarkdownV2 special characters"""
    escape_chars = r'_*[]()~`>#=|{}!'
    return ''.join(['\\' + char if char in escape_chars else char for char in text])

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user profile"""

    try:
        user_id = update.effective_user.id
        user_lang = update.effective_user.language_code or 'en'
        if await is_banned(user_id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return ConversationHandler.END
        profile = get_profile(user_id)
        if profile:
            fullname, email, phone, country, registration_date = profile
            if user_lang.startswith('ar'):
                response = (
                    f"📋 *معلومات الملف الشخصي*\n"
                    f"👤 أسمك: {escape_markdown(fullname)}\n"
                    f"📧 بريدك الإلكتروني: {escape_markdown(email)}\n"
                    f"📱 رقم هاتفك: {escape_markdown(str(phone))}\n"
                    f"🌍 بلدك: {escape_markdown(country)}\n"
                    f"⭐ تاريخ التسجيل: {escape_markdown(str(registration_date))}"
                )
                await update.message.reply_text(response, parse_mode="MarkdownV2")
            else:
                response = (
                    f"📋 *Profile Information*\n"
                    f"👤 Name: {escape_markdown(fullname)}\n"
                    f"📧 Email: {escape_markdown(email)}\n"
                    f"📱 Phone: {escape_markdown(str(phone))}\n"
                    f"🌍 Country: {escape_markdown(country)}\n"
                    f"⭐ Registration Date: {escape_markdown(str(registration_date))}"
                )
                await update.message.reply_text(response, parse_mode="MarkdownV2")
        else:
            msg = " أنت غير مسجل حاليا يرجى التسجيل ثم إعادة المحاولة لاحقا ❌" if user_lang.startswith('ar') else "❌ You're not registered! Register First"
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Profile error: {e}")
        await update.message.reply_text("⚠️ Couldn't load profile. Please try again.")

def get_profile(telegram_id: int) -> tuple:
    """Retrieve user profile data"""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT fullname, email, phone, country, registration_date FROM clients WHERE telegram_id = %s",
            (telegram_id,)
            )
        return c.fetchone()
    except Exception as e:
        logger.error(f"Error in get_profile: {e}")
        return None
    finally:
        conn.close()

async def ban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user from using the bot"""
    user_lang = update.effective_user.language_code or 'en'
    admin = update.effective_user
    if not await is_admins(admin.id):
        await update.message.reply_text("🚫 Access denied!")
        return

    # Extract user ID from message (could be reply or direct input)
    # target = None
    # if update.message.reply_to_message:
    #     target = update.message.reply_to_message.from_user.strip()
    if context.args:
        try:
            target = " ".join(context.args).strip()
        except ValueError:
            await update.message.reply_text("Usage: /bu <fullname> or reply to Users's message")
            return
    else:
        await update.message.reply_text("Usage: /bu <fullname> or reply to Users's message")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id FROM users
            WHERE full_name = %s 
        """, (target,))
        check_user = c.fetchone()
        if check_user:
            # Ban USER
            c.execute("""
                SELECT telegram_id FROM users
                WHERE is_banned = False And full_name = %s
            """, (target,))
            check = c.fetchone()
            if check:
                c.execute("""
                    UPDATE users 
                    SET is_banned = True
                    WHERE full_name = %s
                """, (target,))
                if c.rowcount == 0:
                    await update.message.reply_text("❌ User not found in database")
                    return
                    
                conn.commit()
                await update.message.reply_text(f"✅ User {target} has been banned")
                
                # # Notify banned user if possible
                # c.execute("""
                #     SELECT telegram_id FROM users
                #     WHERE full_name = %s
                # """, (target,))
                # user_data = c.fetchone()
                # if user_data and user_data[0]:
                #     if user_lang.startswith('ar'):
                #         await context.bot.send_message(
                #             chat_id=user_data[0],
                #             text=" لقد تم حظرك لاستخدام هذه البوت لعدم التقيد بسياسة الاستخدام 🚫"
                #         )
                #     else:
                #         await context.bot.send_message(
                #             chat_id=user_data[0],
                #             text="🚫 Your access to this bot has been revoked"
                #         )
            else:
                await update.message.reply_text("❌ User Already revoked")
                return
        else:
            await update.message.reply_text("❌ NO USER")
            return
    except Exception as e:
        logger.error(f"Ban notification failed: {str(e)}")
            
    finally:
        conn.close()

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_lang = update.effective_user.language_code or 'en'
    admin = update.effective_user
    if not await is_admins(admin.id):
        await update.message.reply_text("🚫 Access denied!")
        return

    if context.args:
        target_fullname = " ".join(context.args).strip()
    else:
        await update.message.reply_text("Usage: /uu <full name>")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id FROM users
            WHERE full_name = %s 
        """, (target_fullname,))
        check_user = c.fetchone()
        check_user_id = check_user[0]
        if check_user:
            c.execute("""
                SELECT telegram_id FROM users
                WHERE is_banned = True And full_name = %s 
            """, (target_fullname,))
            check = c.fetchone()
            if check:
                c = conn.cursor()
                c.execute("""
                    UPDATE users 
                    SET is_banned = False, date_block = NULL, block_num = 0  
                    WHERE full_name = %s
                """, (target_fullname,))
                c.execute("DELETE FROM users_block WHERE telegram_id = %s",(check_user_id,))            

                if c.rowcount == 0:
                    await update.message.reply_text("❌ User not found in database")
                    return
                    
                conn.commit()
                await update.message.reply_text(f"✅ User '{target_fullname}' has been unbanned")
                
                # c.execute("""
                #     SELECT telegram_id FROM users
                #     WHERE full_name = %s
                # """, (target_fullname,))
                # user_data = c.fetchone()
                # if user_data and user_data[0]:
                #     try:
                #         if user_lang.startswith('ar'):
                #             await context.bot.send_message(
                #                 chat_id=user_data[0],
                #                 text=" تم السماح لك باستخدام هذا البوت من جديد ✅"
                #             )
                #         else:
                #             await context.bot.send_message(
                #                 chat_id=user_data[0],
                #                 text="🚫 Your access to this bot has been revoked"
                #             )
            else:
                await update.message.reply_text("❌ User Already restored")
                return
        else:
            await update.message.reply_text("❌ NO USER")
            return
    except Exception as e:
        logger.error(f"Unban notification failed: {str(e)}")
    finally:
        conn.close()
                
                
async def handle_subscription_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_lang = update.effective_user.language_code or 'en'
    text = update.message.text.strip()

    # Handle cancellation
    if text in ["Cancel ❌", "إلغاء ❌"]:
        cancel_msg = "🚫 Operation cancelled" if user_lang != 'ar' else "🚫 تم الإلغاء"
        await update.message.reply_text(cancel_msg, reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END
    conn = get_conn()
    # Validate subscription choice
    if text in ["100 Followers", "100 متابع"]:
        subscription_count = 100
        # price = 6
    elif text in ["1000 Followers", "1000 متابع"]:
        subscription_count = 1000
        # price = 60
    else:
        error_msg = "❌ Invalid choice. Please select 100 or 1000 followers." if user_lang == 'en' else "❌ اختيار غير صحيح. يرجى اختيار 100 أو 1000 متابع"
        await update.message.reply_text(error_msg, reply_markup=await get_menu2(user_lang, user.id))
        return SUBSCRIPTION_CHOICE
    try:
            with conn.cursor() as c:
                c.execute("SELECT price FROM price WHERE required = %s", (subscription_count,))
                result_price = c.fetchone()
                if result_price:
                    price = result_price[0]
                else:
                    error_msg = "❌ Invalid choice. Please select 100 or 1000 followers." if user_lang == 'en' else "❌ اختيار غير صحيح. يرجى اختيار 100 أو 1000 متابع"
                    await update.message.reply_text(error_msg, reply_markup=await get_menu2(user_lang, user.id))
                    return SUBSCRIPTION_CHOICE 
    finally:
        put_conn(conn)
    # Store subscription count in context
    # context.user_data['subscription_count'] = subscription_count
    # context.user_data['price'] = price

    # # Define payment companies
    # companies = ["Vodafone Egypt", "Syriatel", "Mtn", "Alfa", "Touch", 
    #              "Etisalat Misr", "Orange Egypt", "Telecom Egypt", 
    #              "Zain Jordan", "Orange Jordan", "Umniah"]

    # # Prepare company selection keyboard
    # company_buttons = [[company] for company in companies]
    # cancel_btn = ["Cancel ❌"] if user_lang != 'ar' else ["إلغاء ❌"]
    # company_buttons.append(cancel_btn)

    # reply_markup = ReplyKeyboardMarkup(company_buttons, resize_keyboard=True)

    # # Prompt user to select company
    # prompt_msg = "Please select your payment company:" if user_lang != 'ar' else "الرجاء اختيار شركة الدفع:"
    # await update.message.reply_text(prompt_msg, reply_markup=reply_markup)




    # Retrieve data from context
    # price = context.user_data.get('price')
    # subscription_count = context.user_data.get('subscription_count')
    channel_data = context.user_data.get('channel_data', {})

    try:
        conn = get_conn()
        c = conn.cursor()

        # Get user's fullname
        c.execute("SELECT fullname FROM clients WHERE telegram_id = %s", (user.id,))
        ex = c.fetchone()[0]

        # Insert into database with payment company
        c.execute("""
            INSERT INTO links_success 
            (added_by, youtube_link, description, channel_id, submission_date, adder, subscription_count, price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user.id,
            channel_data.get('url'),
            channel_data.get('channel_name'),
            channel_data.get('channel_id'),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ex,
            subscription_count,
            price
        ))
        conn.commit()

        # Success message
        success_msg = (
            f"✅ Instagram Page registered successfully!\n\n"
            f"📛 Name: {channel_data.get('channel_name')}\n"
            f"🆔 ID: {channel_data.get('channel_id')}\n"
            f"🔗 URL: {channel_data.get('url')}\n"
            f"❤️ Requested followers: {subscription_count}\n"
            f"Important note: If the link is incorrect or fake, it will be automatically deleted even if payment has been made, as this violates company policy. You must carefully check the link, delete it if it is incorrect, and re-enter the correct link before making a payment. Thank you.\n"
            # f"🏢 payment Company: N/A"
        ) if user_lang != 'ar' else (
            f"✅ تمت عملية إضافة صفحة الانستغرام بنجاح تام\n\n"
            f"📛 أسم الحساب: {channel_data.get('channel_name')}\n"
            f"🆔 معرف القناة: {channel_data.get('channel_id')}\n"
            f"🔗 رابط الحساب: {channel_data.get('url')}\n"
            f"❤️ المتابعين المطلوبين: {subscription_count}\n"
            f"ملاحظة هامة: في حال كان الرابط غير صحيح أو مزيفاً، سيتم حذفه تلقائياً حتى بعد إتمام عملية الدفع، وذلك لمخالفته سياسة الشركة. لذا، يُرجى التحقق من الرابط جيداً، وحذفه إن كان غير صحيح، ثم إدخال الرابط الصحيح قبل إتمام عملية الدفع. شكراً لكم.\n"
            # f"🏢 شركة الدفع: لم يتم تحديد شركة للدفع"
        )

        await update.message.reply_text(
            success_msg,
            reply_markup=await get_menu2(user_lang, user.id)
        )

    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        error_msg = "❌ Error saving data." if user_lang != 'ar' else "❌ حدث خطأ أثناء حفظ البيانات."
        await update.message.reply_text(error_msg)
    finally:
        conn.close()

    return ConversationHandler.END



    # return COMPANY_CHOICE


async def company_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_lang = user.language_code or 'en'
    text = update.message.text.strip()

    # Cancel
    if text in ["Cancel ❌", "إلغاء ❌"]:
        cancel_msg = "🚫 تم الإلغاء" if user_lang.startswith('ar') else "🚫 Operation cancelled"
        await update.message.reply_text(cancel_msg, reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END

    allowed_companies = fetch_companies()
    if not allowed_companies:
        await update.message.reply_text("❌ لا توجد شركات مضافة في قاعدة البيانات.")
        return ConversationHandler.END

    if text not in allowed_companies:
        await update.message.reply_text("❌ شركة غير صالحة. اختر من القائمة.")
        return COMPANY_CHOICE


    # خزّن الشركة المختارة
    context.user_data["payment_company"] = text

    cancel_btn = "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"
    msg = "الآن أدخل رقم عملية الدفع:" if user_lang.startswith('ar') else "Now enter the payment ID:"

    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup([[cancel_btn]], resize_keyboard=True)
    )
    return AWAIT_payment_ID



async def handle_payment_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    user_lang = user.language_code or 'en'
    payment_id = update.message.text.strip()

    if payment_id in ["Cancel ❌", "إلغاء ❌"]:
        msg = "🚫 تم الإلغاء" if user_lang.startswith('ar') else "🚫 Cancelled"
        await update.message.reply_text(msg, reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END

    selected_channel = context.user_data.get("selected_channel")
    payment_company = context.user_data.get("payment_company")

    if not selected_channel or not payment_company:
        await update.message.reply_text("❌ حدث خطأ: لم يتم اختيار قناة/شركة.", reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END

    if not payment_id.isdigit():
        error_msg = "❌ رقم الدفع يجب أن يكون أرقام فقط!" if user_lang.startswith('ar') else "❌ payment ID must be digits only!"
        await update.message.reply_text(error_msg)
        return AWAIT_payment_ID

    # خزّن رقم الدفع
    context.user_data["payment_id"] = payment_id

    # رسالة تأكيد + أزرار
    confirm_text = (
        f"✅ تأكيد التحديث:\n"
        f"🏢 شركة الدفع: {payment_company}\n"
        f"🆔 رقم الدفع: {payment_id}\n\n"
        f"هل تريد التأكيد؟"
        if user_lang.startswith('ar')
        else
        f"✅ Confirm update:\n"
        f"🏢 payment: {payment_company}\n"
        f"🆔 payment ID: {payment_id}\n\n"
        f"Do you want to confirm?"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm", callback_data="pay_confirm"),
        InlineKeyboardButton("❌ Cancel", callback_data="pay_cancel"),
    ]])

    await update.message.reply_text(confirm_text, reply_markup=kb)
    return CONFIRM_payment_UPDATE




async def confirm_payment_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_lang = user.language_code or 'en'

    # امنع ضغطتين: احذف أزرار التأكيد فورًا
    try:
        if query.message:
            await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if query.data == "pay_cancel":
        msg = "تم الإلغاء ❌" if (user_lang or "").startswith('ar') else "Cancelled ❌"
        await query.message.reply_text(msg, reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END

    selected_channel = context.user_data.get("selected_channel")
    payment_company = context.user_data.get("payment_company")
    payment_id = context.user_data.get("payment_id")

    if not selected_channel or not payment_company or not payment_id:
        await query.message.reply_text("❌ Missing data. Please retry.", reply_markup=await get_menu2(user_lang, user.id))
        return ConversationHandler.END

    conn = None
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            UPDATE links_success
            SET id_pay = %s, payment_company = %s
            WHERE id = %s AND added_by = %s
        """, (payment_id, payment_company, selected_channel, user.id))
        conn.commit()

        ok = "✅ تم التحديث بنجاح" if (user_lang or "").startswith('ar') else "✅ Updated successfully"
        await query.message.reply_text(ok, reply_markup=await get_menu2(user_lang, user.id))

    except Exception as e:
        try:
            if conn:
                conn.rollback()
        except Exception:
            pass
        logger.error(f"Confirm update error: {e}")
        await query.message.reply_text("⚠️ DB error أثناء التحديث", reply_markup=await get_menu2(user_lang, user.id))

    finally:
        try:
            if conn:
                put_conn(conn)
        except Exception:
            pass

    return ConversationHandler.END



def fetch_companies(conn=None):
    close_after = False
    if conn is None:
        conn = get_conn()
        close_after = True
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT name
                FROM companies
                WHERE is_active = TRUE
                ORDER BY name
            """)
            return [r[0] for r in cur.fetchall()]
    finally:
        if close_after:
            put_conn(conn)




# ========== NEW HANDLERS ==========
async def channel_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    conn = None
    try:
        channel_id = query.data.split("_")[1]

        conn = get_conn()
        c = conn.cursor()
        c.execute("""
            SELECT description
            FROM links_success
            WHERE id = %s
        """, (channel_id,))
        row = c.fetchone()

        if not row:
            await query.message.reply_text("❌ Page not found")
            return ConversationHandler.END

        description = row[0]  # ✅ fix

        # خزّن القناة المختارة
        context.user_data["selected_channel"] = channel_id

        user_lang = query.from_user.language_code or 'en'
        cancel_btn = "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"

        # شركات الدفع
        companies = fetch_companies()
        company_buttons = [[c] for c in companies]
        company_buttons.append([cancel_btn])

        msg = (
            f"📛 أسم الحساب: {escape_markdown(str(description))}\n\n"
            f"اختر شركة الدفع أولاً:"
            if user_lang.startswith('ar')
            else
            f"📛 Page Name: {escape_markdown(str(description))}\n\n"
            f"Please choose the payment company first:"
        )

        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=msg,
            parse_mode="MarkdownV2",
            reply_markup=ReplyKeyboardMarkup(company_buttons, resize_keyboard=True)
        )

        await query.message.delete()

        return COMPANY_CHOICE

    except Exception as e:
        logger.error(f"Page button error: {str(e)}")
        await query.message.reply_text("❌ Error processing request")
        return ConversationHandler.END

    finally:
        if conn:
            try:
                put_conn(conn)  # الأفضل مع pool
            except Exception:
                pass




# async def send_educational_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
#     """Send educational video to user"""
#     try:
#         user_id = update.effective_user.id
#         user_lang = update.effective_user.language_code or 'en'
        
#         if not await is_registered(user_id):
#             msg = " من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please Register First."
#             await update.message.reply_text(msg)
#             return
        
#         if await is_banned(user_id):
#             msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
#             await update.message.reply_text(msg)
#             return

#         # Get random video from database
#         # conn = get_conn()
#         try:
#             video_dir = "client_educational_videos"
#             videos = [f for f in os.listdir(video_dir) if f.endswith(('.mp4', '.mov', '.avi'))]
#             if not videos:
#                 raise FileNotFoundError
            
#             file_path = os.path.join(video_dir, random.choice(videos))
#             caption = "🎓 Educational Video" if user_lang != 'ar' else "🎓 فيديو تعليمي"

#             await context.bot.send_video(
#                 chat_id=update.effective_chat.id,
#                 video=open(file_path, 'rb'),
#                 caption=caption,
#                 supports_streaming=True
#             )
            
#         except FileNotFoundError:
#             error_msg = "الفيديو غير متوفر حالياً ⚠️" if user_lang.startswith('ar') else "⚠️ Video not available"
#             await update.message.reply_text(error_msg)
            
#     except Exception as e:
#         logger.error(f"Video error: {str(e)}")
#         error_msg = "تعذر إرسال الفيديو ⚠️" if user_lang.startswith('ar') else "⚠️ Couldn't send video"
#         await update.message.reply_text(error_msg)




    # finally:
    #     conn.close()
        
        
        

# ========== SUPPORT CONVERSATION HANDLERS ==========
async def start_support_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start support conversation"""
    user = update.effective_user
    user_lang = user.language_code or 'en'
    
    if await is_banned(user.id):
        msg = "🚫 تم إلغاء وصولك" if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END

    if not await is_registered(user.id):
        msg = "❌ يرجى التسجيل أولاً" if user_lang.startswith('ar') else "❌ Please register first"
        await update.message.reply_text(msg)
        return ConversationHandler.END

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT 1 FROM support WHERE telegram_id = %s AND who_is = %s", (user.id,"client",))
        if c.fetchone():
            msg = (
                "⏳ أنت بالفعل أرسلت رسالة للدعم مسبقا يرجى الانتظار حتى يجيب فريق الدعم على رسالتك السابقة ثم بعد ذلك أرسل رسالة جديدة مرة أخرى شكرا لتفهمك." 
                if user_lang.startswith('ar') 
                else "⏳ You have already sent a message to support before. Please wait until the support team responds to your previous message and then send a new message again. Thank you for your understanding."
            )
            await update.message.reply_text(msg)
            return ConversationHandler.END
            
        # Prompt for support message
        cancel_btn = "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"
        msg = (
            "📩 الرجاء كتابة رسالتك للدعم:"
            if user_lang.startswith('ar') 
            else "📩 Please write your support message:"
        )
        await update.message.reply_text(
            msg,
            reply_markup=ReplyKeyboardMarkup([[cancel_btn]], resize_keyboard=True)
        )
        return SUPPORT_MESSAGE
        
    except Exception as e:
        logger.error(f"Support error: {str(e)}")
        error_msg = "⚠️ فشل بدء الدعم" if user_lang.startswith('ar') else "⚠️ Failed to start support"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END
    finally:
        conn.close()

async def save_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save support message to database"""
    user = update.effective_user
    user_lang = user.language_code or 'en'
    message = update.message.text.strip()

    if message in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_support(update, context)
        return ConversationHandler.END

    try:
        conn = get_conn()
        c = conn.cursor()
        
        # Get user's email from clients table
        c.execute("SELECT email FROM clients WHERE telegram_id = %s", (user.id,))
        email = c.fetchone()[0]
        
        # Insert support request
        c.execute("""
            INSERT INTO support 
            (telegram_id, message, user_name, email, message_date, who_is)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            user.id,
            message,
            user.full_name,
            email,
            datetime.now(),
            "client"
        ))
        conn.commit()

        success_msg = (
            f"✅ تم إرسال رسالتك إلى الدعم يرجى تفقد إيميلك\n📧 Email: {email} \n سوف يقوم فريق الدعم الخاص بنا بالتواصل معك في أقرب وقت ممكن." 
            if user_lang.startswith('ar') 
            else f"✅ Your message has been sent to support. Please check your email.\n {email} \nOur support team will contact you as soon as possible."
        )
        await update.message.reply_text(
            success_msg,
            reply_markup=await get_menu(user_lang, user.id)
        )
        
    except Exception as e:
        logger.error(f"Support save error: {str(e)}")
        error_msg = "⚠️ فشل إرسال الرسالة" if user_lang.startswith('ar') else "⚠️ Failed to send message"
        await update.message.reply_text(error_msg)
    finally:
        conn.close()
        
    return ConversationHandler.END

async def cancel_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel support request"""
    user = update.effective_user
    user_lang = user.language_code or 'en'
    
    msg = (
        "❌ تم إلغاء طلب الدعم" 
        if user_lang.startswith('ar') 
        else "❌ Support request cancelled"
    )
    await update.message.reply_text(msg, reply_markup=await get_menu(user_lang, user.id))
    return ConversationHandler.END

async def route_back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    # نفّذ نفس منطق الأزرار الموجود عندك
    await menu_handler(update, context)
    return ConversationHandler.END


        

def main() -> None:
    """Configure and start the bot with comprehensive error handling"""
    pid_file = Path("bot.pid")
    logger = logging.getLogger(__name__)
    # #region agent log
    # import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H1","location":"client.py:main","message":"main() started","data":{"pid_exists":pid_file.exists()},"timestamp":__import__('time').time()})+'\n')
    # #endregion

    try:
        # ========== PID FILE HANDLING ==========
        # Check for existing instances with validation
        if pid_file.exists():
            try:
                content = pid_file.read_text().strip()
                if not content:
                    raise ValueError("Empty PID file")
                
                old_pid = int(content)
                # #region agent log
                # import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H1","location":"client.py:pid_check","message":"checking old PID","data":{"old_pid":old_pid,"exists":psutil.pid_exists(old_pid)},"timestamp":__import__('time').time()})+'\n')
                # #endregion
                if psutil.pid_exists(old_pid):
                    print("⛔ Another bot instance is already running!")
                    print("❗ Use 'kill %d' or restart your computer" % old_pid)
                    sys.exit(1)
                    
            except (ValueError, psutil.NoSuchProcess) as e:
                logger.warning(f"Cleaning invalid PID file: {str(e)}")
                pid_file.unlink(missing_ok=True)
            except psutil.Error as e:
                logger.error(f"PID check failed: {str(e)}")
                sys.exit(1)

        # Write new PID file with atomic write
        try:
            with pid_file.open("w") as f:
                f.write(str(os.getpid()))
                os.fsync(f.fileno())
        except IOError as e:
            logger.critical(f"Failed to write PID file: {str(e)}")
            sys.exit(1)

        # ========== BOT INITIALIZATION ==========
        # #region agent log
        # import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H2","location":"client.py:bot_init","message":"building application","data":{},"timestamp":__import__('time').time()})+'\n')
        # #endregion
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        # Ensure bot_starts table exists
        # try:
        #     _c = connection_pool.getconn()
        #     try:
        #         ensure_bot_starts_table(_c)
        #         _c.commit()
        #     finally:
        #         connection_pool.putconn(_c)
        # except Exception as e:
        #     logger.error(f"Failed to ensure bot_starts table: {e}")
        # ========== HANDLER CONFIGURATION ==========
        # Admin conversation handler
        admin_conv = ConversationHandler(
            entry_points=[
                # Allow pasting Instagram profile URL directly (no menu button required)
                MessageHandler(filters.Regex(r"^🗑 Delete Page"), delete_channel),
                MessageHandler(filters.Regex(r"^Start$"), start),
                MessageHandler(filters.Regex(r"^📋 My Profile$"), profile_command),
                MessageHandler(filters.Regex(r"^📌 My Pages$"), list_Pages),
                MessageHandler(filters.Regex(r"^📌 My Pages Accept$"), list_Pages_paid),
                MessageHandler(filters.Regex(r"^My Pages Done$"), list_Pages_Done),
                MessageHandler(filters.Regex(r"^Delete Page accept$"), delete_channel_accept),
                MessageHandler(filters.Regex(r"^🗑 Delete  All Pages$"), delete_channel_admin),
                MessageHandler(filters.Regex(r"^🚫 Ban Client$"), ban_client),
                MessageHandler(filters.Regex(r"^✅ UnBan Client$"), unban_client),
                MessageHandler(filters.Regex(r"^🚫 Ban User$"), ban_user),
                MessageHandler(filters.Regex(r"^✅ UnBan User$"), unban_user),
                MessageHandler(filters.Regex(r"(?i)(?:https?://)?(?:[a-z0-9-]+\.)*(?:instagram\.com|instagr\.am|ig\.me)/"),process_channel_url),
            ],
            states={
                "AWAIT_CHANNEL_URL": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete)],
                "AWAIT_CHANNEL_URL_ACCEPT": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_accept)],
                "AWAIT_CHANNEL_URL_ADMIN": [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_url_admin)],
                "AWAIT_ADDER": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_admin)],
                CHANNEL_URL: [
                    # 1) لو ضغط زر إلغاء / قائمة / أي زر من القائمة، اخرج من الحالة
                    MessageHandler(filters.Regex(r"^(Main Menu|القائمة الرئيسية)$"), route_back_to_menu),
                    # 2) غير ذلك: اعتبره رابط وحاول معالجته
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_channel_url),
                ],
                AWAIT_payment_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_id)],
                SUBSCRIPTION_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_subscription_choice)],
                COMPANY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, company_handler)],
                CONFIRM_payment_UPDATE: [CallbackQueryHandler(confirm_payment_update_handler, pattern=r"^(pay_confirm|pay_cancel)$")],
            },
            fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
            per_message=False,
            map_to_parent={ConversationHandler.END: ConversationHandler.END}
        )

        # Main conversation handler
        conv_handler = ConversationHandler(
            entry_points=[
                # Allow pasting Instagram profile URL directly (no menu button required)
                MessageHandler(filters.Regex(r"^📝 Register$"), handle_registration),
                MessageHandler(filters.Regex(r"^📋 My Profile$"), profile_command),
                MessageHandler(filters.Regex(r"^🔍 Input Your Instagram Page URL$"), handle_channel_verification),
                MessageHandler(filters.Regex(r"^🗑 Delete Page$"), delete_channel),
                MessageHandler(filters.Regex(r"^Delete Page accept$"), delete_channel_accept),
                MessageHandler(filters.Regex(r"^حذف قناة مقبولة$"), delete_channel_accept),
                MessageHandler(filters.Regex(r"^تسجيل الدخول 📝$"), handle_registration),
                MessageHandler(filters.Regex(r"^الملف الشخصي 📋$"), profile_command),
                MessageHandler(filters.Regex(r"^أدخل رابط حساب انستغرام 🔍$"), handle_channel_verification),
                MessageHandler(filters.Regex(r"^حذف قناة 🗑$"), delete_channel),
                CallbackQueryHandler(channel_button_handler, pattern=r"^channel_"),
                MessageHandler(filters.Regex(r"(?i)(?:https?://)?(?:[a-z0-9-]+\.)*(?:instagram\.com|instagr\.am|ig\.me)/"),process_channel_url),
            ],
            states={
                EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_handler)],
                CODE_VERIFICATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, verify_code_handler)],
                PHONE: [
                    MessageHandler(filters.Regex(r'^(Skip|تخطي)$'), handle_skip_phone),
                    MessageHandler(filters.CONTACT, phone_handler),
                    MessageHandler(filters.ALL & ~filters.COMMAND, handle_invalid_contact),
                    CommandHandler('cancel', cancel_registration),
                    MessageHandler(filters.ALL, lambda u,c: u.message.reply_text("❌ Please use contact button!"))
                ],
                # FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler)],
                # COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, country_handler)],
                CHANNEL_URL: [
                    # 1) لو ضغط زر إلغاء / قائمة / أي زر من القائمة، اخرج من الحالة
                    MessageHandler(
                        filters.Regex(r"^(Main Menu)$"),
                        route_back_to_menu
                    ),

                    # 2) غير ذلك: اعتبره رابط وحاول معالجته
                    MessageHandler(filters.TEXT & ~filters.COMMAND, process_channel_url),
                ],
                "AWAIT_CHANNEL_URL": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete)],
                "AWAIT_CHANNEL_URL_ACCEPT": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_accept)],
                AWAIT_payment_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_id)],
                SUBSCRIPTION_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_subscription_choice)],
                COMPANY_CHOICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, company_handler)],
                CONFIRM_payment_UPDATE: [CallbackQueryHandler(confirm_payment_update_handler, pattern=r"^(pay_confirm|pay_cancel)$")],

            },
            fallbacks=[
                CommandHandler('cancel', cancel_registration),
                CommandHandler('cancel', lambda u,c: (
                    u.message.reply_text("Operation cancelled", reply_markup=get_menu(u.effective_user.language_code, u.effective_user.id)),
                    ConversationHandler.END
                ))
            ],
            per_message=False,
        )
        
        support_conv = ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex(r'^(Support|الدعم)$'), start_support_conversation)
            ],
            states={
                SUPPORT_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_support_message)]
            },
            fallbacks=[
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_support),
                CommandHandler("cancel", cancel_support)
            ],
            per_message=False,
        )

        # ========== HANDLER REGISTRATION ==========
        handlers = [
            CommandHandler("start", start),
            CommandHandler('profile', profile_command),
            conv_handler,
            # MessageHandler(filters.Regex(r'^(Educational video 📹|فيديو تعليمي 📹)$'),send_educational_video),
            admin_conv,
            support_conv,
            MessageHandler(filters.Regex(r"^👑 Admin Panel$"), handle_admin_panel),
            MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler),
            CommandHandler("bc", ban_client),
            CommandHandler("uc", unban_client),
            CommandHandler("bu", ban_user),
            CommandHandler("uu", unban_user),
            CommandHandler("myPages", list_Pages),
            CommandHandler("myPages_paid", list_Pages_paid)
        ]

        # ========== BAN CHECK WRAPPER ==========
        async def is_banned(telegram_id: int) -> bool:
            """Check if user is banned with DB connection handling"""
            try:
                conn = get_conn()
                c = conn.cursor()
                c.execute("SELECT is_banned FROM clients WHERE telegram_id = %s", (telegram_id,))
                result = c.fetchone()
                return bool(result and result[0] == 1)
            except psycopg2.Error as e:
                logger.error(f"Ban check failed2: {str(e)}")
                return False
            finally:
                conn.close()

        def wrap_handler(handler):
            """Safe handler wrapper with ban checking (works for messages + callback queries)."""
            if not hasattr(handler, 'callback'):
                return handler

            original_callback = handler.callback

            async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
                try:
                    # Allow /start and Start even if banned
                    if update.message and update.message.text:
                        text = update.message.text.strip()
                        if text in ("/start", "Start"):
                            return await original_callback(update, context)

                    user = update.effective_user
                    if user and await is_banned(user.id):
                        # stop spinner if callback
                        if update.callback_query:
                            try:
                                await update.callback_query.answer()
                            except Exception:
                                pass

                        msg = update.effective_message
                        if msg:
                            await msg.reply_text("🚫 Your access has been revoked")
                        return ConversationHandler.END

                    return await original_callback(update, context)

                except Exception as e:
                    logger.error(f"Handler error: {str(e)}")

                    msg = update.effective_message if update else None
                    if msg and update and update.effective_user:
                        ul = update.effective_user.language_code or "en"
                        await msg.reply_text("⚠️ حدث خطأ. الرجاء إعادة المحاولة.", reply_markup=await get_menu(ul, update.effective_user.id))
                    return ConversationHandler.END

            handler.callback = wrapped
            return handler


        # Apply ban checks to all handlers
        wrapped_handlers = [wrap_handler(h) for h in handlers]
        application.add_handlers(wrapped_handlers)
        # application.add_handler(CallbackQueryHandler(channel_button_handler, pattern=r"^channel_"))
        # ========== ERROR HANDLING ==========
        application.add_error_handler(error_handler)

        # ========== BOT STARTUP ==========
        # #region agent log
        #import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H4","location":"client.py:run_polling","message":"about to run_polling","data":{},"timestamp":__import__('time').time()})+'\n')
        # #endregion
        logger.info("Starting bot...")
        try:
            application.run_polling(
                poll_interval=2,
                timeout=30,
                drop_pending_updates=True
            )
        except Exception as poll_err:
            # #region agent log
            #import json as _json,traceback as _tb; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H5","location":"client.py:run_polling_error","message":"run_polling exception","data":{"error":str(poll_err),"traceback":_tb.format_exc()},"timestamp":__import__('time').time()})+'\n')
            # #endregion
            raise
        # #region agent log
        #import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H5","location":"client.py:after_polling","message":"run_polling finished normally","data":{},"timestamp":__import__('time').time()})+'\n')
        # #endregion

    except Conflict as e:
        # #region agent log
        #import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H5","location":"client.py:conflict","message":"Conflict exception","data":{"error":str(e)},"timestamp":__import__('time').time()})+'\n')
        # #endregion
        logger.critical(f"Bot conflict: {str(e)}")
        print("""
        🔌 Connection conflict detected!
        Possible solutions:
        1. Wait 10 seconds before restarting
        2. Check for other running instances
        3. Verify your bot token is unique
        """)
    except Exception as e:
        # #region agent log
        #import json as _json,traceback as _tb; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H4","location":"client.py:exception","message":"fatal exception","data":{"error":str(e),"traceback":_tb.format_exc()},"timestamp":__import__('time').time()})+'\n')
        # #endregion
        logger.critical(f"Fatal error: {str(e)}", exc_info=True)
    finally:
        # ========== CLEANUP ==========
        try:
            pid_file.unlink(missing_ok=True)
            logger.info("Cleanup completed")
        except Exception as e:
            logger.error(f"Cleanup failed: {str(e)}")

        # Ensure database connections are closed
        # sqlite3.connect(DATABASE_NAME).close()

if __name__ == "__main__":
    # #region agent log
    #import json as _json; open('/Users/admin/Desktop/ChatBotFixed/.cursor/debug.log','a').write(_json.dumps({"hypothesisId":"H3","location":"client.py:__main__","message":"about to call main()","data":{},"timestamp":__import__('time').time()})+'\n')
    # #endregion
    main()