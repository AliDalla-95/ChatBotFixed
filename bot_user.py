import smtplib
import random
from email.message import EmailMessage
import os
import re
from signal import SIGINT, SIGTERM
import logging
import warnings
from telegram.warnings import PTBUserWarning
from datetime import datetime, timedelta
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters
)
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
import config
import sys
import phonenumbers
from phonenumbers import geocoder
import uuid


# Keep PTB warnings visible
warnings.filterwarnings("ignore", category=PTBUserWarning)

# Configure logging to both file and console
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot_errors.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Configure HTTPX logging
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)

# Global dictionaries for state management (thread-safe due to user_id keys)
pending_submissions = {}  # Format: {user_id: {link_id, chat_id, message_id, description}}
user_pages = {}

# Conversation states
EMAIL, CODE_VERIFICATION, PHONE, CASH_NUMBER = range(4)
WITHDRAW_AMOUNT, CARRIER_SELECTION, UPDATE_CASH, SUPPORT_MESSAGE = range(4, 8)

# Global connection pools
db_pool = None
test2_db_pool = None

# Context managers for pooled database connections
@contextmanager
def get_db_connection():
    """Get a connection from the main database pool."""
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

@contextmanager
def get_test2_db_connection():
    """Get a connection from the Test2 database pool."""
    conn = test2_db_pool.getconn()
    try:
        yield conn
    finally:
        test2_db_pool.putconn(conn)

### Database Functions

def user_exists(telegram_id: int) -> bool:
    """Check if a user exists in the database."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                return cursor.fetchone() is not None
    except Exception as e:
        logger.error(f"Error in user_exists: {e}")
        return False

def generate_confirmation_code() -> str:
    """Generate a 6-digit confirmation code."""
    return ''.join(random.choices('0123456789', k=6))

def send_confirmation_email(email: str, code: str) -> bool:
    """Send a confirmation email with the given code."""
    try:
        msg = EmailMessage()
        msg.set_content(f"Your confirmation code is: {code}")
        msg['Subject'] = "Confirmation Code"
        msg['From'] = config.EMAIL_FROM
        msg['To'] = email

        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)
            return True
    except Exception as e:
        logger.error(f"Failed to send email to {email}: {e}")
        return False

def store_message_id(telegram_id: int, chat_id: int, link_id: int, message_id: int) -> None:
    """Store a Telegram message ID with user and chat context."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO link_messages 
                        (telegram_id, chat_id, link_id, message_id) 
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (telegram_id, chat_id, link_id) 
                    DO UPDATE SET message_id = EXCLUDED.message_id
                """, (telegram_id, chat_id, link_id, message_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error storing message ID: {e}")

def get_message_id(telegram_id: int, chat_id: int, link_id: int) -> int:
    """Retrieve a message ID for a specific user and chat."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT message_id FROM link_messages 
                    WHERE telegram_id = %s AND chat_id = %s AND link_id = %s
                """, (telegram_id, chat_id, link_id))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting message ID: {e}")
        return None

def get_allowed_links(telegram_id: int) -> list:
    """Retrieve links available for the user."""
    try:
        allow_link = 0
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT l.id, l.youtube_link, l.description, l.adder, l.channel_id
                    FROM links l
                    LEFT JOIN user_link_status uls 
                        ON l.channel_id = uls.channel_id AND uls.telegram_id = %s
                    WHERE (uls.processed IS NULL OR uls.processed = 0) AND l.allow_link != %s
                    ORDER BY l.id DESC
                """
                cursor.execute(query, (telegram_id, allow_link,))
                return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error in get_allowed_links: {e}")
        return []

async def block_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and update user block status."""
    user_lang = update.effective_user.language_code or 'en'
    telegram_id = update.effective_user.id

    chat_id = update.message.chat_id if update.message else update.callback_query.message.chat_id

    BLOCK_CONFIG = {
        5: {'duration': timedelta(days=1), 'penalty': timedelta(days=1)}
    }

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT block_num, date_block 
                    FROM users 
                    WHERE telegram_id = %s
                """, (telegram_id,))
                user_data = cursor.fetchone()
                if not user_data:
                    return False

                block_num, date_block = user_data
                current_time = datetime.now()

                if block_num >= 10:
                    cursor.execute("""
                        UPDATE users 
                        SET is_banned = True
                        WHERE telegram_id = %s
                    """, (telegram_id,))
                    conn.commit()
                    return False

                if block_num != 5:
                    return False

                config = BLOCK_CONFIG[5]
                block_duration = config['duration']
                penalty_duration = config['penalty']
                release_time = date_block + block_duration
                penalty_threshold = current_time - penalty_duration

                if date_block < penalty_threshold:
                    return False

                localized_time = release_time.strftime("%Y-%m-%d %H:%M:%S")
                msg = (
                    "⚠️ تم حظرك حتى تاريخ {} بسبب انتهاكك الشروط وسياسة البوت والمصداقية بالعمل"
                    if user_lang.startswith('ar')
                    else "⚠️ You're blocked until {} Due to violation of the terms and conditions, bot policy and credibility of work"
                )
                await context.bot.send_message(chat_id=chat_id, text=msg.format(localized_time))
                return True
    except Exception as e:
        logger.error(f"Block check error: {e}")
        return False

def mark_link_processed(telegram_id: int, user_name: str, res_name, link_id: int, res) -> None:
    """Mark a link as processed for the user."""
    date_mation = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO user_link_status (telegram_id, user_name, channel_name, link_id, channel_id, date_mation, processed)
                    VALUES (%s, %s, %s, %s, %s, %s, 1)
                    ON CONFLICT (telegram_id, link_id, channel_id) 
                    DO UPDATE SET processed = EXCLUDED.processed
                """, (telegram_id, user_name, res_name, link_id, res, date_mation))
                conn.commit()
    except Exception as e:
        logger.error(f"Error in mark_link_processed: {e}")

def update_user_points(telegram_id: int, points: int) -> None:
    """Update user's points balance."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE users 
                    SET points = points + %s
                    WHERE telegram_id = %s
                """, (points, telegram_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error in update_user_points: {e}")

def update_likes(link_id: int, points: int = 1) -> None:
    """Update likes count and manage link status."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE likes SET channel_likes = channel_likes + %s
                    WHERE id = %s
                """, (1, link_id))

                cursor.execute(
                    "SELECT channel_likes, subscription_count FROM likes WHERE id = %s",
                    (link_id,)
                )
                user_data = cursor.fetchone()

                if user_data and user_data[0] == user_data[1]:
                    cursor.execute("DELETE FROM links WHERE id = %s", (link_id,))
                    cursor.execute("""
                        UPDATE likes SET status = %s
                        WHERE id = %s
                    """, (True, link_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error in update_likes: {e}")

### Command Handlers

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the main menu."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        keyboard = [
            ["👋 Start", "📝 Register"],
            ["📋 Profile", "🔍 View Links"],
            ["💵 Withdraw", "Educational video 📹"],
            ["SUPPORT", "Help"]
        ] if not user_lang.startswith('ar') else [
            ["بدء 👋", "تسجيل الدخول 📝"],
            ["الملف الشخصي 📋", "عرض المهام 🔍"],
            ["سحب الأرباح 💵", "فيديو تعليمي 📹"],
            ["الدعم", "مساعدة"]
        ]
        menu_text = "Choose a command From The Menu Below:" if not user_lang.startswith('ar') else "اختر أمرا من القائمة أدناه"
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        if update.message:
            await update.message.reply_text(menu_text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(chat_id=update.effective_chat.id, text=menu_text, reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in show_menu: {e}")
        msg = "⚠️ تعذر عرض القائمة" if user_lang.startswith('ar') else "⚠️ Couldn't display menu"
        await update.effective_message.reply_text(msg)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command."""
    try:
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        user_lang = update.effective_user.language_code or 'en'
        context.user_data.clear()

        if await is_banned(user_id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(f"{user_name} {msg}")
            return

        if user_exists(user_id):
            msg = "أهلا بعودتك 🎉" if user_lang.startswith('ar') else "Welcome back! 🎉"
            if user_id in config.ADMIN_IDS:
                msg = "أهلا وسهلا بك أدمن! 🛡️" if user_lang.startswith('ar') else "Welcome back Admin! 🛡️"
            await update.message.reply_text(f"{user_name} {msg}")
        else:
            msg = "أهلا وسهلا بك من فضلك قم بالتسجيل أولا " if user_lang.startswith('ar') else "Welcome! Please Register First"
            await update.message.reply_text(f"{user_name} {msg}")
        await show_menu(update, context)
    except Exception as e:
        logger.error(f"Error in start: {e}")
        msg = "⚠️ لا يمكن معالجة طلبك حاليا يرجى المحاولة لاحقا" if user_lang.startswith('ar') else "⚠️ Couldn't process your request. Please try again."
        await update.message.reply_text(msg)

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the registration process."""
    try:
        user_id = update.effective_user.id
        user_lang = update.effective_user.language_code or 'en'
        context.user_data.clear()

        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        if user_exists(user_id):
            msg = "لا حاجة لإعادة التسجيل أنت مسجل بالفعل ✅ " if user_lang.startswith('ar') else "You're already registered! ✅"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        keyboard = [["إلغاء ❌"]] if user_lang.startswith('ar') else [["Cancel ❌"]]
        msg = "من فضلك قم بإدخال بريدك الإلكتروني لإرسال رمز التأكيد والمتابعة" if user_lang.startswith('ar') else "Please enter your email address:"
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return EMAIL
    except Exception as e:
        logger.error(f"Error in register: {e}")
        msg = "⚠️ لا يمكنك التسجيل الآن حاول لاحقا" if user_lang.startswith('ar') else "⚠️ Couldn't start registration. Please try again."
        await update.message.reply_text(msg)
        return ConversationHandler.END

async def process_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the user's email during registration."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        email = update.message.text.strip().lower()

        if email in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
            error_msg = "❌ صيغة البريد الإلكتروني غير صحيحة" if user_lang.startswith('ar') else "❌ Invalid email format"
            await update.message.reply_text(error_msg)
            return EMAIL

        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM users WHERE email = %s", (email,))
                if cursor.fetchone():
                    error_msg = "❌ هذا البريد الإلكتروني مستخدم بالفعل أدخل بريد آخر" if user_lang.startswith('ar') else "❌ Your Email has Already Exists Change To A Different Email"
                    await update.message.reply_text(error_msg)
                    return EMAIL

        code = generate_confirmation_code()
        context.user_data['confirmation_code'] = code
        context.user_data['email'] = email

        if not send_confirmation_email(email, code):
            error_msg = "فشل إرسال الرمز" if user_lang.startswith('ar') else "Failed to send code"
            await update.message.reply_text(error_msg)
            return EMAIL

        success_msg = (
            "📧 تم إرسال رمز التأكيد إلى بريدك الإلكتروني أو في رسائل البريد العشوائي (سبام). الرجاء إدخاله هنا أو إضغط إلغاء من القائمة لإلغاء التسجيل:"
            if user_lang.startswith('ar')
            else "📧 A confirmation code has been sent to your email or in spam. Please enter it here Or Press Cancel from the Menu For Cancel Registration:"
        )
        await update.message.reply_text(success_msg)
        return CODE_VERIFICATION
    except Exception as e:
        logger.error(f"Email processing error: {e}")
        error_msg = "⚠️ خطأ في معالجة البريد" if user_lang.startswith('ar') else "⚠️ Error processing email"
        await update.message.reply_text(error_msg)
        return EMAIL

async def verify_confirmation_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Verify the confirmation code entered by the user."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_code = update.message.text.strip()
        stored_code = context.user_data.get('confirmation_code')

        if user_code in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        if not stored_code:
            error_msg = "انتهت الجلسة" if user_lang.startswith('ar') else "Session expired"
            await update.message.reply_text(error_msg)
            return ConversationHandler.END

        if user_code == stored_code:
            keyboard = [
                [KeyboardButton("⬇️ مشاركة رقم الهاتف هنا" if user_lang.startswith('ar') else "Share your phone number ⬇️:\n(If you choose to skip, your phone number will not be recorded)", request_contact=True)],
                ["تخطي" if user_lang.startswith('ar') else "Skip", "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"]
            ]
            contact_msg = "📱 شارك رقم هاتفك ⬇️ أو اضغط تخطي:\n(في حال اخترت التخطي لن يتم تسجيل رقم هاتفك)" if user_lang.startswith('ar') else "📱 Share your phone number ⬇️ or skip:"
            await update.message.reply_text(contact_msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True))
            return PHONE
        else:
            error_msg = "❌ رمز غير صحيح" if user_lang.startswith('ar') else "❌ Invalid code"
            await update.message.reply_text(error_msg)
            return CODE_VERIFICATION
    except Exception as e:
        logger.error(f"Code verification error: {e}")
        error_msg = "⚠️ فشل التحقق أعد المحاولة" if user_lang.startswith('ar') else "⚠️ Verification failed try again"
        await update.message.reply_text(error_msg)
        return CODE_VERIFICATION

async def process_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the user's phone number."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user = update.effective_user

        if update.message.text in ["Skip", "تخطي"]:
            context.user_data['phone'] = "+0000000000"
            context.user_data['full_name'] = user.name
            context.user_data['country'] = "Syria"
            await prompt_cash_number(update, context, user_lang)
            return CASH_NUMBER

        if update.message.text in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        if update.message.contact:
            contact = update.message.contact
            if contact.user_id != user.id:
                msg = "من فضلك شارك رقمك الخاص ❌" if user_lang.startswith('ar') else "❌ Please share your own number!"
                await update.message.reply_text(msg)
                return PHONE

            phone_number = "+" + contact.phone_number
            try:
                # Corrected line
                parsed_number = phonenumbers.parse(phone_number, None)
                country = geocoder.description_for_number(parsed_number, "en") or "Unknown"
            except phonenumbers.NumberParseException:
                country = "Unknown"
        else:
            msg = "من فضلك شارك رقمك الخاص أو اضغط (تخطي) أو إلغاء العملية ❌" if user_lang.startswith('ar') else "❌ Please share your private number or press (skip) or cancel the process!"
            await update.message.reply_text(msg)
            return PHONE

        context.user_data['phone'] = phone_number
        context.user_data['country'] = country
        await prompt_cash_number(update, context, user_lang)
        return CASH_NUMBER
    except Exception as e:
        logger.error(f"Phone processing error: {e}")
        error_msg = "⚠️ خطأ في معالجة رقم الهاتف" if user_lang.startswith('ar') else "⚠️ Error processing phone number"
        await update.message.reply_text(error_msg)
        return PHONE

async def prompt_cash_number(update: Update, context: ContextTypes.DEFAULT_TYPE, user_lang: str):
    """Prompt the user for their cash number."""
    try:
        keyboard = [["تخطي" if user_lang.startswith('ar') else "Skip", "إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"]]
        msg = (
            "الرجاء إدخال رقم الكاش الخاص بك (أرقام فقط) وتأكد منه قبل المتابعة لأنه الرقم الذي سيتم تحويل الأرباح عليه وهذا على مسؤليتك الشخصية لكي لا يضيع تعبك أو أضغط على تخطي وعند سحب الأرباح سوف تقوم بإدخاله:"
            if user_lang.startswith('ar')
            else "Please enter your cash number (digits only) And Make sure of it before proceeding because it is the number to which the profits will be transferred and this is your personal responsibility so that your efforts are not wasted Or click skip and when withdrawing the profits you will enter it:"
        )
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    except Exception as e:
        logger.error(f"Error prompting cash number: {e}")

async def process_cash_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the user's cash number and complete registration."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        cash_number = update.message.text.strip()

        if cash_number in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        if cash_number in ["Skip", "تخطي"]:
            cash_number = None
        elif not cash_number.isdigit():
            error_msg = "❌ يرجى إدخال أرقام فقط" if user_lang.startswith('ar') else "❌ Please enter digits only"
            await update.message.reply_text(error_msg)
            return CASH_NUMBER

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO users 
                            (telegram_id, full_name, email, phone, country, registration_date, cash_number)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (
                        update.effective_user.id,
                        update.effective_user.name,
                        context.user_data['email'],
                        context.user_data['phone'],
                        context.user_data['country'],
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        cash_number
                    ))
                    conn.commit()
        except psycopg2.IntegrityError:
            msg = "أنت مسجل بالفعل! ✅" if user_lang.startswith('ar') else "✅ You're already registered!"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        display_cash = cash_number if cash_number else "N/A"
        success_msg = (
            f"✅ تم إكمال التسجيل بنجاح :\n"
            f"👤 أسمك : {escape_markdown(update.effective_user.name)}\n"
            f"📧 بريدك الإلكتروني : {escape_markdown_2(context.user_data['email'])}\n"
            f"📱 رقم هاتفك : {escape_markdown_2(context.user_data['phone'])}\n"
            f"💳 رقم الكاش: {display_cash}\n"
            f"🌍 بلدك : {escape_markdown(context.user_data['country'])}\n"
            f"⭐ تاريخ التسجيل : {escape_markdown(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}"
            if user_lang.startswith('ar')
            else
            f"✅ Registration Complete:\n"
            f"👤 Name: {escape_markdown(update.effective_user.name)}\n"
            f"📧 Email: {escape_markdown_2(context.user_data['email'])}\n"
            f"📱 Phone: {escape_markdown_2(context.user_data['phone'])}\n"
            f"💳 Cash number: {display_cash}\n"
            f"🌍 Country: {escape_markdown(context.user_data['country'])}\n"
            f"⭐ Registration Date: {escape_markdown(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}"
        )
        await update.message.reply_text(success_msg, reply_markup=ReplyKeyboardRemove())
        await show_menu(update, context)
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Cash number error: {e}")
        error_msg = "⚠️ خطأ في معالجة البيانات" if user_lang.startswith('ar') else "⚠️ Error processing data"
        await update.message.reply_text(error_msg)
        return CASH_NUMBER

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display the user's profile."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id

        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return

        profile = get_profile(user_id)
        if profile:
            _, name, email, phone, country, reg_date, points, cash_number, block_num, total_withdrawals, res_name = profile
            msg = (
                f"📋 *ملفك الشخصي :*\n"
                f"👤 أسمك : {escape_markdown(name)}\n"
                f"📧 بريدك الإلكتروني : {escape_markdown(email)}\n"
                f"📱 رقم هاتفك : {escape_markdown(phone)}\n"
                f"💳 رقم الكاش: {cash_number}\n"
                f"🌍 بلدك : {escape_markdown(country)}\n"
                f"⭐ تاريخ التسجيل : {escape_markdown(str(reg_date))}\n"
                f"🏆 نقاطك : {points} نقطة\n"
                f"💰 إجمالي السحوبات : {total_withdrawals} نقطة\n\n"
                f"سوف يتم إضافة رصيد مهماتك الحديثة التي قمت بإنجازها في أقرب وقت وأي مهمة تقوم بإلغاء تنفيذها من تلقاء نفسك سوف يتم خصم رصيدها عند سحب الأرباح\n\n"
                f"هناك مهمات قمت بالاشتراك بها ولكن لم تنجزها من المرة الأولى وتم وضع إشارة حظر عليك وحتى لو قمت بإنجازها للمرة الثانية سوف تبقى إشارة الحظر عليك ويجب الانتباه عندما تصل إشارة الحظر للرقم ٥ سوف يتم حظرك لمدة يوم واحد وعندما تصبح إشارة الحظر ١٠ سيتم حظرك نهائيا عن استخدام البوت وعندها لفك الحظر يرجى التواصل مع فؤيق الدعم:\n"
                f"إجمالي الحظر لحد هذه اللحظة : {block_num}\n\n"
                f"أسماء القنوات التي لم يتم إنجازها ويجب إعادة الاشتراك بها قبل أن تختفي من قائمة المهمات :\n {res_name}"
                if user_lang.startswith('ar')
                else
                f"📋 *Profile Information*\n"
                f"👤 Name: {escape_markdown(name)}\n"
                f"📧 Email: {escape_markdown(email)}\n"
                f"📱 Phone: {escape_markdown(phone)}\n"
                f"💳 Cash number: {cash_number}\n"
                f"🌍 Country: {escape_markdown(country)}\n"
                f"⭐ Registration Date: {escape_markdown(str(reg_date))}\n"
                f"🏆 Points: {points} points\n"
                f"💰 Total Withdrawals: {total_withdrawals} points\n\n"
                f"Your recently completed tasks will be credited as soon as possible, and any task you cancel on your own will have its balance deducted when withdrawing profits\n\n"
                f"There are tasks that you have subscribed to but did not complete them the first time and a ban mark was placed on you and even if you complete them the second time the ban mark will remain on you and you must be careful when the ban mark reaches number 5 you will be banned for one day and when the ban mark reaches 10 you will be permanently banned from using the bot and then to lift the ban please contact the support team:\n"
                f"Total Blocks to date: {block_num}\n\n"
                f"Names of channels that have not been completed and must be resubscribed to before they disappear from the to do list:\n{res_name}"
            )
            await update.message.reply_text(msg, parse_mode="MarkdownV2")
        else:
            msg = "أنت لست مسجل قم بالتسجيل أولا ❌ " if user_lang.startswith('ar') else "❌ You're not registered! Register First"
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Profile error: {e}")
        msg = "⚠️ لا يمكن عرض الملف الشخصي حاليا يرجى إعادة المحاولة لاحقا" if user_lang.startswith('ar') else "⚠️ Couldn't load profile. Please try again."
        await update.message.reply_text(msg)

def get_profile(telegram_id: int) -> tuple:
    """Retrieve user profile data."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM user_link_status WHERE date_mation < CURRENT_TIMESTAMP - INTERVAL '3 days' AND telegram_id = %s AND points_status = %s",
                    (telegram_id, False)
                )
                user_date_data = cursor.fetchone()
                points = user_date_data[0] if user_date_data else 0
                if points:
                    cursor.execute("""
                        UPDATE user_link_status 
                        SET points_status = %s
                        WHERE date_mation < CURRENT_TIMESTAMP - INTERVAL '3 days'
                        AND telegram_id = %s
                        AND points_status = %s
                    """, (True, telegram_id, False))
                    update_user_points(telegram_id, points)
                    conn.commit()

                cursor.execute(
                    "SELECT telegram_id, full_name, email, phone, country, registration_date, points, cash_number, block_num FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                user_data = cursor.fetchone()
                if not user_data:
                    return None

                cursor.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id = %s",
                    (telegram_id,)
                )
                total_withdrawals = cursor.fetchone()[0] or 0

                cursor.execute(
                    "SELECT DISTINCT channel_name FROM users_block WHERE telegram_id = %s",
                    (telegram_id,)
                )
                results = cursor.fetchall()
                res_name = '\n'.join(row[0] for row in results) if results else ''

                return (*user_data, total_withdrawals, res_name)
    except Exception as e:
        logger.error(f"Error in get_profile: {e}")
        return None

async def view_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display available links for the user."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id

        msg = ""
        if await block_check(update, context):
            return
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫"
        if not user_exists(user_id):
            msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
        if msg:
            await update.message.reply_text(msg)
            return

        user_pages[user_id] = 0
        await send_links_page(user_lang, update.effective_chat.id, user_id, 0, context)
    except Exception as e:
        logger.error(f"View links error: {e}")
        msg = "⚠️ لا يمكن تحميل المهمات حاليا يرجى المحاولة لاحقا" if user_lang.startswith('ar') else "⚠️ Couldn't load links. Please try again."
        await update.message.reply_text(msg)

### Link Management

async def send_links_page(user_lang: str, chat_id: int, user_id: int, page: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send paginated links to the user."""
    try:
        links, total_pages = get_paginated_links(user_id, page)

        if not links:
            msg = "لايوجد مهمات لك الآن قم بتحديث المهمات لرؤية المزيد في حال وجودها 🎉" if user_lang.startswith('ar') else "🎉 No more links available!"
            await context.bot.send_message(chat_id, msg)
            return

        for link in links:
            link_id, yt_link, desc, adder, channel_id = link
            text = (
                f"📛 {escape_markdown(desc)}\n"
                f"[🔗 رابط الذهاب للمهمة انقر هنا]({yt_link})"
                if user_lang.startswith('ar')
                else
                f"📛 {escape_markdown(desc)}\n"
                f"[🔗 YouTube Link]({yt_link})"
            )
            keyboard = [[InlineKeyboardButton(
                "تنفيذ المهمة وبعد الانتهاء تحميل لقطة الشاشة لتأكيدها مبدئيا 📸" if user_lang.startswith('ar')
                else "📸 Accept And Subscribed And Then Submit Screenshot",
                callback_data=f"submit_{link_id}"
            )]]
            message = await context.bot.send_message(
                chat_id, text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="MarkdownV2"
            )
            store_message_id(user_id, chat_id, link_id, message.message_id)

        if total_pages > 1:
            buttons = []
            current_page = page + 1
            page_info = f"{current_page} / {total_pages}"
            if page > 0:
                buttons.append(InlineKeyboardButton("الصفحة السابقة ⬅️" if user_lang.startswith('ar') else "⬅️ Previous", callback_data=f"prev_{page-1}"))
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("➡️ الصفحة التالية" if user_lang.startswith('ar') else "Next ➡️", callback_data=f"next_{page+1}"))
            if buttons:
                await context.bot.send_message(chat_id, page_info, reply_markup=InlineKeyboardMarkup([buttons]))
    except Exception as e:
        logger.error(f"Error sending links: {e}")
        msg = "⚠️ لا يمكن عرض المهمات الآن يرجى تحديث المهمات لرؤيتها" if user_lang.startswith('ar') else "⚠️ Couldn't load links. Please try later."
        await context.bot.send_message(chat_id, msg)

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu text commands."""
    try:
        text = update.message.text
        user_lang = update.effective_user.language_code or 'en'
        command_map = {
            "👋 Start": "start", "📝 Register": "register", "📋 Profile": "profile", "🔍 View Links": "view_links",
            "Educational video 📹": "educational_video", "Help": "help",
            "بدء 👋": "start", "تسجيل الدخول 📝": "register", "الملف الشخصي 📋": "profile", "عرض المهام 🔍": "view_links",
            "فيديو تعليمي 📹": "educational_video", "مساعدة": "help"
        }
        action = command_map.get(text)

        if action == "start":
            await start(update, context)
        elif action == "register":
            await update.message.reply_text("جاري بدء التسجيل..." if user_lang.startswith('ar') else "Starting registration...")
            await register(update, context)
        elif action == "profile":
            await profile_command(update, context)
        elif action == "view_links":
            await view_links(update, context)
        elif action == "help":
            await help_us(update, context)
        else:
            msg = "❌ أمر غير معروف. يرجى استخدام أزرار القائمة" if user_lang.startswith('ar') else "❌ Unknown command. Please use the menu buttons."
            await update.message.reply_text(msg)
            await show_menu(update, context)
    except Exception as e:
        logger.error(f"Text command error: {e}")
        error_msg = "⚠️ تعذر معالجة الأمر. يرجى المحاولة مرة أخرى" if user_lang.startswith('ar') else "⚠️ Couldn't process command. Please try again."
        await update.message.reply_text(error_msg)

async def help_us(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display help message."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id

        # msg = ""
        # if await block_check(update, context):
        #     return
        # if await is_banned(user_id):
        #     msg = "تم إلغاء وصولك 🚫"
        if not user_exists(user_id):
            msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
        # if msg:
            await update.message.reply_text(msg)
            return

        lang_detail = "ar" if user_lang.startswith('ar') else "en"
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT message_help FROM help_us WHERE lang = %s AND bot = %s", (lang_detail, "user"))
                result = cursor.fetchone()
                res = result[0] if result else "Help Message"
                await update.message.reply_text(res)
                await show_menu(update, context)
    except Exception as e:
        logger.error(f"Help error: {e}")
        msg = "⚠️ لا يمكن تحميل رسالة المساعدة حاليا" if user_lang.startswith('ar') else "⚠️ Error in Help us"
        await update.message.reply_text(msg)

async def navigate_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination navigation for links."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        action, page_str = query.data.split('_')
        new_page = int(page_str)
        user_pages[user_id] = new_page
        await send_links_page(user_lang, query.message.chat_id, user_id, new_page, context)
        await query.message.delete()
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        error_msg = "⚠️ تعذر تحميل الصفحة. يرجى المحاولة مرة أخرى" if user_lang.startswith('ar') else "⚠️ Couldn't load page. Please try again."
        await query.message.reply_text(error_msg)

### Image Submission

async def handle_submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image submission requests."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        query = update.callback_query
        await query.answer()


        user_id = query.from_user.id
        
        
        msg = ""
        if await block_check(update, context):
            return
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫"
        if not user_exists(user_id):
            msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
        if msg:
            await update.message.reply_text(msg)
            return

        chat_id = query.message.chat_id
        link_id = int(query.data.split("_")[1])
        message_id = get_message_id(user_id, chat_id, link_id)

        if not message_id:
            msg = "⚠️ تم تعطيل الجلسة يرجى تحديث قائمة المهام" if user_lang.startswith('ar') else "⚠️ Session expired. Please reload links."
            await query.message.reply_text(msg)
            return

        allowed_links = get_allowed_links(user_id)
        if not any(link[0] == link_id for link in allowed_links):
            msg = "⚠️ هذه المهمة لم تعد متاحة لك" if user_lang.startswith('ar') else "⚠️ This link is no longer available."
            await query.message.reply_text(msg)
            return

        description = get_link_description(link_id)
        if not description:
            msg = "❌ خطأ في تفاصيل المهمة قم بتحديث المهمات" if user_lang.startswith('ar') else "❌ Link details missing"
            await query.message.reply_text(msg)
            return



        text = f"📸 خذ لقطة الشاشة للقناة وأرسلها هنا : {description}" if user_lang.startswith('ar') else f"📸 Submit image for: {description}"
        text1 = f"يرجى تحميل لقطات الشاشة فقط. لايستجيب البوت سوى للقطات الشاشة" if user_lang.startswith('ar') else f"Please upload screenshots only. The bot only responds to screenshots."
        text_msg = await context.bot.send_message(
            chat_id=chat_id, 
            text=text, 
            reply_to_message_id=message_id
        )
        text1_msg = await query.message.reply_text(text1)


        pending_submissions[user_id] = {'link_id': link_id, 'chat_id': chat_id, 'message_id': message_id, 'description': description,'text_msg_id': text_msg.message_id,'text1_msg_id': text1_msg.message_id}
        # await context.bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=message_id)
        # await query.message.reply_text(text1)

    except Exception as e:
        logger.error(f"Submit error: {e}")
        msg = "❌ خطأ في تفاصيل المهمة قم بتحديث المهمات" if user_lang.startswith('ar') else "❌ Link details missing"
        await query.message.reply_text(msg)

def get_link_description(link_id: int) -> str:
    """Get the description for a specific link."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT description FROM links WHERE id = %s", (link_id,))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error in get_link_description: {e}")
        return None

async def process_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image upload and verification."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id
        user_name = update.effective_user.name
        chat_id = update.effective_chat.id

        if user_id not in pending_submissions:
            msg = "❌ خطأ يرجى تحديث المهمات من جديد" if user_lang.startswith('ar') else "❌ No active submission!"
            await update.message.reply_text(msg)
            return

        submission = pending_submissions[user_id]
        link_id = submission['link_id']
        message_id = submission['message_id']
        description = submission['description']

        os.makedirs("image_process", exist_ok=True)
        filename = f"user_{user_id}_link_{link_id}_{uuid.uuid4().hex}.jpg"
        image_path = os.path.join("image_process", filename)

        photo_file = await update.message.photo[-1].get_file()
        await photo_file.download_to_drive(image_path)

        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT channel_id FROM links WHERE id = %s", (link_id,))
                    result = cursor.fetchone()
                    res = result[0] if result else None
                    cursor.execute("UPDATE links SET allow_link = allow_link - 1 WHERE id = %s", (link_id,))
                    conn.commit()

            with get_test2_db_connection() as conn2:
                with conn2.cursor() as cursor2:
                    cursor2.execute("""
                        INSERT INTO images (
                            user_id, user_name, channel_name, channel_id, date, link_id, image_path
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """, (user_id, user_name, description, res, datetime.now(), link_id, image_path))
                    conn2.commit()

            mark_link_processed(user_id, user_name, description, link_id, res)
        except Exception as e:
            logger.error(f"Database error: {e}")
            if os.path.exists(image_path):
                os.remove(image_path)
            msg = "⚠️ خطأ في شبكة النت يرجى إعادة تحميل المهمات" if user_lang.startswith('ar') else "⚠️ Internet error, please reload the missions"
            await update.message.reply_text(msg)
            return

        msg = (
            "✅ سيتم التحقق من إتمامك للمهمة، وفي حال إتمامها، ستُضاف نقطة +1 إلى نقاطك، وسيتم إضافتها إلى نقاطك في أسرع وقت ممكن. احرص على عدم إلغاء الاشتراك حتى لا تفقد الرصيد عند السحب. في حال عدم إتمام 5 مهمات سيتم حظرك لمدة يوم في المرة الأولى، وفي المرة الثانية سيتم حظرك نهائيًا في حال تكرارها ل10 مهمات. سيتم إبلاغك بالنتيجة. يرجى متابعة ملفك الشخصي، والآن انتقل إلى مهمة أخرى."
            if user_lang.startswith('ar')
            else
            "✅ Your completion of the task will be verified, and if completed, +1 point will be added to your points, and it will be added to your points as soon as possible. Make sure not to unsubscribe so that you do not lose the balance when withdrawing. If you do not complete 5 tasks, you will be banned for a day the first time, and the second time you will be banned permanently if you repeat it for 10 tasks. You will be informed of the result. Please follow your profile, now move on to another task."
        )
        await update.message.reply_text(msg)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=submission['text_msg_id'])
        await context.bot.delete_message(chat_id=chat_id, message_id=submission['text1_msg_id'])
            
    except Exception as e:
        logger.error(f"Image processing error: {e}")
        if 'image_path' in locals() and os.path.exists(image_path):
            os.remove(image_path)
        error_msg = "⚠️ خطأ في معالجة الصورة" if user_lang.startswith('ar') else "⚠️ Image processing error"
        await update.message.reply_text(error_msg)
    finally:
        if user_id in pending_submissions:
            del pending_submissions[user_id]

### Helper Functions

def escape_markdown(text: str) -> str:
    """Escape MarkdownV2 special characters."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + c if c in escape_chars else c for c in str(text)])

def escape_markdown_2(text: str) -> str:
    """Escape additional MarkdownV2 special characters."""
    escape_chars = r'_*[]()~`>#-=|{}!'
    return ''.join(['\\' + char if char in escape_chars else char for char in str(text)])

def get_paginated_links(user_id: int, page: int = 0, per_page: int = 5) -> tuple:
    """Get a paginated list of links."""
    try:
        links = get_allowed_links(user_id)
        total_pages = (len(links) - 1) // per_page + 1
        start = page * per_page
        end = start + per_page
        return links[start:end], total_pages
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        return [], 0

async def is_banned(telegram_id: int) -> bool:
    """Check if a user is banned."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT is_banned FROM users WHERE telegram_id = %s", (telegram_id,))
                result = cursor.fetchone()
                return bool(result and result[0])
    except Exception as e:
        logger.error(f"Ban check error: {e}")
        return False

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uncaught exceptions."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        logger.error("Unhandled exception:", exc_info=context.error)
        if update and update.effective_message:
            msg = "⚠️ خطأ غير متوقع يرجى إعادة المحاولة لاحقا" if user_lang.startswith('ar') else "⚠️ An unexpected error occurred. Please try again later."
            await update.effective_message.reply_text(msg)
            await show_menu(update, context)
    except Exception as e:
        logger.error(f"Error in error handler: {e}")

### Withdrawals

def get_user_points(telegram_id: int) -> int:
    """Get the user's current points balance."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT points FROM users WHERE telegram_id = %s", (telegram_id,))
                result = cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error in get_user_points: {e}")
        return 0

def deduct_points(telegram_id: int, amount: int) -> None:
    """Deduct points from the user's balance."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET points = points - %s WHERE telegram_id = %s", (amount, telegram_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error deducting points: {e}")

def create_withdrawal(telegram_id: int, amount: int, carrier: str) -> None:
    """Record a withdrawal request."""
    try:
        profile = get_full_profile(telegram_id)
        if not profile:
            raise ValueError("User profile not found")
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO withdrawals (
                        user_id, amount_before, carrier, full_name, email, phone, country, registration_date, cash_number
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    telegram_id, amount, carrier, profile['full_name'], profile['email'], profile['phone'],
                    profile['country'], profile['registration_date'], profile['cash_number']
                ))
                conn.commit()
    except Exception as e:
        logger.error(f"Withdrawal creation error: {e}")
        raise

def get_current_cash_number(telegram_id: int) -> str:
    """Get the user's current cash number."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT cash_number FROM users WHERE telegram_id = %s", (telegram_id,))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting cash number: {e}")
        return None

def update_cash_number(telegram_id: int, cash_number: str) -> None:
    """Update the user's cash number."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("UPDATE users SET cash_number = %s WHERE telegram_id = %s", (cash_number, telegram_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error updating cash number: {e}")

def get_full_profile(telegram_id: int) -> dict:
    """Get the user's full profile data."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT full_name, email, phone, country, registration_date, points, cash_number
                    FROM users WHERE telegram_id = %s
                """, (telegram_id,))
                result = cursor.fetchone()
                if result:
                    return {
                        'full_name': result[0], 'email': result[1], 'phone': result[2], 'country': result[3],
                        'registration_date': result[4], 'points': result[5], 'cash_number': result[6]
                    }
                return None
    except Exception as e:
        logger.error(f"Error getting full profile: {e}")
        return None

async def start_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the withdrawal process."""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id

    msg = ""
    if await is_banned(user_id):
        msg = "تم إلغاء وصولك 🚫"
    if not user_exists(user_id):
        msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
    if msg:
        await update.message.reply_text(msg)
        return ConversationHandler.END

    points = get_user_points(user_id)
    if points < 100:
        msg = "⚠️ تحتاج إلى 100 نقطة على الأقل لسحب الأرباح" if user_lang.startswith('ar') else "⚠️ You need at least 100 points to withdraw."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    keyboard = [["إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"]]
    msg = "كم عدد النقاط التي تريد سحبها؟ (أدخل رقماً)" if user_lang.startswith('ar') else "Enter the number of points units to withdraw:"
    await update.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return WITHDRAW_AMOUNT

async def process_withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the withdrawal amount."""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id
    amount_text = update.message.text.strip()

    if amount_text in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_withdrawal(update, context)
        return ConversationHandler.END

    if not amount_text.isdigit():
        error_msg = "❌ يرجى إدخال أرقام فقط" if user_lang.startswith('ar') else "❌ Please enter numbers only"
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    amount = int(amount_text)
    if amount <= 0:
        error_msg = "❌ الرجاء إدخال رقم صحيح موجب" if user_lang.startswith('ar') else "❌ Please enter a positive integer"
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    available_points = get_user_points(user_id)
    max_withdrawal = (available_points // 100) * 100

    if max_withdrawal < 100:
        error_msg = "⚠️ تحتاج إلى 100 نقطة على الأقل للسحب" if user_lang.startswith('ar') else "⚠️ You need at least 100 points to withdraw"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

    if amount > max_withdrawal:
        error_msg = f"❌ الحد الأقصى للسحب هو {max_withdrawal}" if user_lang.startswith('ar') else f"❌ Maximum withdrawal is {max_withdrawal} units"
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    if amount % 100 != 0:
        error_msg = "❌ لاتستطيع سحب سوى نقاط من فئة المئات أو أضعافها (100,200...)" if user_lang.startswith('ar') else "❌ Withdrawal must be in units of 100 (100, 200...)"
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    context.user_data['withdrawal_amount'] = amount
    return await select_carrier(update, context)

async def select_carrier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display carrier selection options."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        buttons = [
            [InlineKeyboardButton("MTN", callback_data="carrier_MTN"),
             InlineKeyboardButton("سيرياتيل" if user_lang.startswith('ar') else "SYRIATEL", callback_data="carrier_SYRIATEL")]
        ]
        prompt_text = (
            "الرجاء اختيار شركة الاتصالات أو أضغط إلغاء من القائمة لإلغاء العملية:"
            if user_lang.startswith('ar')
            else "Please select your mobile carrier or Cancel from the Menu to Cancel the Process:"
        )
        await update.message.reply_text(prompt_text, reply_markup=InlineKeyboardMarkup(buttons))
        return CARRIER_SELECTION
    except Exception as e:
        logger.error(f"Error in select_carrier: {e}")
        error_msg = "❌ حدث خطأ يرجى المحاولة من جديد" if user_lang.startswith('ar') else "❌ There is an Error Try again please"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle invalid input during carrier selection."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        buttons = [
            [InlineKeyboardButton("MTN", callback_data="carrier_MTN"),
             InlineKeyboardButton("سيرياتيل" if user_lang.startswith('ar') else "SYRIATEL", callback_data="carrier_SYRIATEL")]
        ]
        error_text = (
            "❌ اختيار غير صحيح، الرجاء استخدام الأزرار أعلاه أو إلغاء العملية:"
            if user_lang.startswith('ar')
            else "❌ Invalid selection, please use the buttons above or cancel the process:"
        )
        await update.message.reply_text(error_text, reply_markup=InlineKeyboardMarkup(buttons))
        return CARRIER_SELECTION
    except Exception as e:
        logger.error(f"Error handling invalid input: {e}")
        error_msg = "❌ حدث خطأ، يرجى المحاولة مرة أخرى" if user_lang.startswith('ar') else "❌ An error occurred, please try again"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

async def process_carrier_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process the selected carrier."""
    user_lang = update.effective_user.language_code or 'en'
    query = update.callback_query
    await query.answer()

    try:
        await context.bot.delete_message(chat_id=query.message.chat_id, message_id=query.message.message_id)
    except Exception as e:
        logger.error(f"Error deleting carrier message: {e}")

    carrier = query.data.split('_')[1]
    context.user_data['carrier'] = carrier
    current_cash = get_current_cash_number(query.from_user.id)

    keyboard = [["تخطي" if user_lang.startswith('ar') else "Skip"], ["إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"]]
    msg = (
        f"أدخل رقم الكاش الجديد أو 'تخطي' للحفاظ على الرقم الحالي.\nالرقم الحالي هو: {current_cash}\nملاحظة في حال أنك تريد إدخال رقم جديد\nالرجاء إدخال رقم الكاش الخاص بك (أرقام فقط) وتأكد منه قبل المتابعة لأنه الرقم الذي سيتم تحويل الأرباح عليه وهذا على مسؤليتك الشخصية لكي لا يضيع تعبك"
        if user_lang.startswith('ar')
        else
        f"Enter new cash number or 'Skip' to keep current.\nThe Current Cash Number Is: {current_cash}\nNote: If you want to get a new number:\nPlease enter your cash number (digits only) And Make sure of it before proceeding because it is the number to which the profits will be transferred and this is your personal responsibility so that your efforts are not wasted."
    )
    await query.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return UPDATE_CASH

async def process_cash_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process cash number update and complete withdrawal."""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id
    user_input = update.message.text.strip()

    if user_input in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_withdrawal(update, context)
        return ConversationHandler.END

    if user_input in ["Skip", "تخطي"]:
        cash_number = get_current_cash_number(user_id)
    elif not user_input.isdigit():
        error_msg = "❌ أرقام فقط" if user_lang.startswith('ar') else "❌ Digits only"
        await update.message.reply_text(error_msg)
        return UPDATE_CASH
    else:
        cash_number = user_input
        update_cash_number(user_id, cash_number)

    try:
        amount = context.user_data['withdrawal_amount']
        carrier = context.user_data['carrier']
        deduct_points(user_id, amount)
        create_withdrawal(user_id, amount, carrier)
        success_msg = (
            f"✅ تم طلب سحب {amount} نقطة إلى {carrier}\nرقم الكاش: {cash_number} وسوف يتم إعلامك عند تحويلها وإضافتها إلى إجمالي السحوبات"
            if user_lang.startswith('ar')
            else f"✅ Withdrawal request for {amount} points to {carrier} requested\nCash number: {cash_number} And You will be notified when it is transferred and added to the total withdrawals."
        )
        await update.message.reply_text(success_msg)
    except Exception as e:
        logger.error(f"Withdrawal error: {e}")
        error_msg = "❌ فشل السحب" if user_lang.startswith('ar') else "❌ Withdrawal failed"
        await update.message.reply_text(error_msg)

    context.user_data.clear()
    await show_menu(update, context)
    return ConversationHandler.END

### Support Functions

async def start_support_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start a support conversation."""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id

    # msg = ""
    # if await block_check(update, context):
    #     return
    # if await is_banned(user_id):
    #     msg = "تم إلغاء وصولك 🚫"
    if not user_exists(user_id):
        msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
    # if msg:
        await update.message.reply_text(msg)
        return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM support WHERE telegram_id = %s AND who_is = %s",
                    (user_id, "user")
                )
                if cursor.fetchone():
                    msg = (
                        "⏳ أنت بالفعل أرسلت رسالة للدعم مسبقا يرجى الانتظار حتى يجيب فريق الدعم على رسالتك السابقة ثم بعد ذلك أرسل رسالة جديدة مرة أخرى شكرا لتفهمك."
                        if user_lang.startswith('ar')
                        else "⏳ You have already sent a message to support before. Please wait until the support team responds to your previous message and then send a new message again. Thank you for your understanding."
                    )
                    await update.message.reply_text(msg)
                    await show_menu(update, context)
                    return ConversationHandler.END

        keyboard = [["إلغاء ❌" if user_lang.startswith('ar') else "Cancel ❌"]]
        msg = "📩 يرجى كتابة رسالتك إلى الدعم:" if user_lang.startswith('ar') else "📩 Please write your support message:"
        await update.message.reply_text(msg, reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        return SUPPORT_MESSAGE
    except Exception as e:
        logger.error(f"Support message error: {e}")
        error_msg = "⚠️ فشل الإرسال للدعم" if user_lang.startswith('ar') else "⚠️ Failed In Support"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

async def save_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save the support message to the database."""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id
    message_text = update.message.text

    if message_text in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_support(update, context)
        return ConversationHandler.END

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT email FROM users WHERE telegram_id = %s", (user_id,))
                email = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO support 
                        (telegram_id, message, user_name, message_date, email, who_is)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, message_text, update.effective_user.name, datetime.now(), email, "user"))
                conn.commit()
                success_msg = (
                    f"✅ تم إرسال رسالتك إلى الدعم يرجى تفقد إيميلك\n📧 Email: {email}\nسوف يقوم فريق الدعم الخاص بنا بالتواصل معك في أقرب وقت ممكن."
                    if user_lang.startswith('ar')
                    else f"✅ Your message has been sent to support. Please check your email.\n{email}\nOur support team will contact you as soon as possible."
                )
                await update.message.reply_text(success_msg, reply_markup=ReplyKeyboardRemove())
                await show_menu(update, context)
    except Exception as e:
        logger.error(f"Support message error: {e}")
        error_msg = "⚠️ فشل إرسال الرسالة" if user_lang.startswith('ar') else "⚠️ Failed to send message"
        await update.message.reply_text(error_msg)
    return ConversationHandler.END

async def cancel_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the support request."""
    user_lang = update.effective_user.language_code or 'en'
    await update.message.reply_text(
        "❌ تم إلغاء إرسال الرسالة" if user_lang.startswith('ar') else "❌ Message cancelled",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_menu(update, context)
    return ConversationHandler.END

### Educational Video

async def send_educational_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an educational video to the user."""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id

        msg = ""
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫"
        if not user_exists(user_id):
            msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌"
        if msg:
            await update.message.reply_text(msg)
            return

        video_path = get_random_video()
        if not video_path or not os.path.exists(video_path):
            error_msg = "⚠️ الفيديو غير متوفر حالياً" if user_lang.startswith('ar') else "⚠️ Video not available"
            await update.message.reply_text(error_msg)
            return

        caption = "🎓 فيديو تعليمي" if user_lang.startswith('ar') else "🎓 Educational Video"
        await context.bot.send_video(chat_id=update.effective_chat.id, video=open(video_path, 'rb'), caption=caption, supports_streaming=True)
    except Exception as e:
        logger.error(f"Video sending error: {e}")
        error_msg = "⚠️ تعذر إرسال الفيديو" if user_lang.startswith('ar') else "⚠️ Couldn't send video"
        await update.message.reply_text(error_msg)

def get_random_video() -> str:
    """Get a random video from the videos folder."""
    try:
        video_dir = "user_educational_videos"
        if not os.path.exists(video_dir):
            return None
        videos = [f for f in os.listdir(video_dir) if f.endswith(('.mp4', '.mov', '.avi'))]
        if not videos:
            return None
        return os.path.join(video_dir, random.choice(videos))
    except Exception as e:
        logger.error(f"Error getting video: {e}")
        return None

### Cancellation Handlers

async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the registration process."""
    user_lang = update.effective_user.language_code or 'en'
    context.user_data.clear()
    msg = "❌ تم إلغاء التسجيل" if user_lang.startswith('ar') else "❌ Registration cancelled"
    await update.message.reply_text(msg)
    await show_menu(update, context)
    return ConversationHandler.END

async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the withdrawal process."""
    user_lang = update.effective_user.language_code or 'en'
    await update.message.reply_text(
        "❌ تم إلغاء عملية السحب" if user_lang.startswith('ar') else "❌ Withdrawal cancelled",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_menu(update, context)
    return ConversationHandler.END

async def restart_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Restart the registration process."""
    user_lang = update.effective_user.language_code or 'en'
    context.user_data.clear()
    msg = "جاري إعادة بدء عملية التسجيل..." if user_lang.startswith('ar') else "Restarting registration..."
    await update.message.reply_text(msg)
    return await register(update, context)

### Main Application

def main() -> None:
    """Configure and start the bot."""
    global db_pool, test2_db_pool
    db_pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=config.DATABASE_URL)
    test2_db_pool = ThreadedConnectionPool(minconn=1, maxconn=10, dsn=config.TEST2_DATABASE_URL)

    application = ApplicationBuilder().token(config.TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('register', register),
            MessageHandler(filters.Regex(r'^📝 Register$|^تسجيل الدخول 📝$'), register),
            MessageHandler(filters.Regex(r'^/register$'), register)
        ],
        states={
            EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_email),
                CommandHandler('cancel', cancel_registration),
                MessageHandler(filters.Regex(r'^(/start|/register)$'), restart_registration),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_registration)
            ],
            CODE_VERIFICATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_confirmation_code),
                CommandHandler('cancel', cancel_registration)
            ],
            PHONE: [
                MessageHandler(filters.CONTACT | filters.TEXT, process_phone),
                CommandHandler('cancel', cancel_registration),
                MessageHandler(filters.Regex(r'^(/start|/register)$'), restart_registration)
            ],
            CASH_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_cash_number),
                CommandHandler('cancel', cancel_registration)
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_registration),
            MessageHandler(filters.Regex(r'^(/start|/register)$'), restart_registration)
        ],
        allow_reentry=True,
          # <-- Add this line
    )

    support_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r'^SUPPORT$|^الدعم$'), start_support_conversation)
        ],
        states={
            SUPPORT_MESSAGE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, save_support_message),
                CommandHandler('cancel', cancel_support),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_support)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_support)],
        allow_reentry=True,
          # <-- Add this line
    )

    withdrawal_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r'^💵 Withdraw$|^سحب الأرباح 💵$'), start_withdrawal)
        ],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal_amount)],
            CARRIER_SELECTION: [
                CallbackQueryHandler(process_carrier_selection, pattern="^carrier_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invalid_input),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_withdrawal)
            ],
            UPDATE_CASH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_cash_update),
                CommandHandler('cancel', cancel_withdrawal),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_withdrawal)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_withdrawal)],
        allow_reentry=True,
          # <-- Add this line
    )

    handlers = [
        CommandHandler('start', start),
        CommandHandler('menu', show_menu),
        CommandHandler('profile', profile_command),
        CommandHandler('viewlinks', view_links),
        conv_handler,
        support_conv,
        withdrawal_conv,
        MessageHandler(filters.Regex(r'^(Educational video 📹|فيديو تعليمي 📹)$'), send_educational_video),
        MessageHandler(filters.Regex(r'^Help$|^مساعدة$'), help_us),
        CallbackQueryHandler(handle_submit_callback, pattern=r"^submit_\d+$"),
        CallbackQueryHandler(navigate_links, pattern=r"^(prev|next)_\d+$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands),
        MessageHandler(filters.PHOTO, process_image_upload)
    ]

    for handler in handlers:
        application.add_handler(handler)
    application.add_error_handler(error_handler)

    logger.info("Starting bot...")
    application.run_polling(close_loop=False, stop_signals=(SIGINT, SIGTERM))

if __name__ == '__main__':
    main()
