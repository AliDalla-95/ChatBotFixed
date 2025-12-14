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
import smtplib
import random
from email.message import EmailMessage
import psycopg2
import scan_image10
import config
import sys
import phonenumbers
from phonenumbers import geocoder


# Keep PTB warnings visible
# warnings.filterwarnings("once", category=PTBUserWarning)


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

# Add HTTPX logging configuration HERE
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("httpcore").setLevel(logging.INFO)

# Global dictionaries for state management
pending_submissions = {}  # Format: {user_id: {link_id, chat_id, message_id, description}}
user_pages = {}

# Conversation states
# Original: EMAIL, PHONE = range(2)
EMAIL, CODE_VERIFICATION, PHONE, CASH_NUMBER = range(4)
WITHDRAW_AMOUNT, CARRIER_SELECTION, UPDATE_CASH = range(4, 7)

def connect_db():
    """Create and return a PostgreSQL database connection"""
    try:
        return psycopg2.connect(config.DATABASE_URL)
    except psycopg2.Error as e:
        logger.error(f"Database connection failed: {e}")
        raise

##########################
#    Database Functions  #
##########################
def user_exists(telegram_id: int) -> bool:
    """Check if user exists in database"""
    try:
        with connect_db() as conn:
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
    return ''.join(random.choices('0123456789', k=6))

def send_confirmation_email(email: str, code: str) -> bool:
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
    """Store Telegram message ID with user and chat context"""
    try:
        with connect_db() as conn:
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
    finally:
        conn.close()
        
def get_message_id(telegram_id: int, chat_id: int, link_id: int) -> int:
    """Get message ID for specific user and chat"""
    try:
        with connect_db() as conn:
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
    finally:
        conn.close()
        
def get_allowed_links(telegram_id: int) -> list:
    """Retrieve links available for the user"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT l.id, l.youtube_link, l.description, l.adder, l.channel_id
                    FROM links l
                    LEFT JOIN user_link_status uls 
                        ON l.channel_id = uls.channel_id  AND uls.telegram_id = %s
                    WHERE uls.processed IS NULL OR uls.processed = 0
                    ORDER BY l.id DESC
                """
                cursor.execute(query, (telegram_id,))
                return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error in get_allowed_links: {e}")
        return []
    finally:
        conn.close()



async def block_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check and update user block status using connection pooling."""
    user_lang = update.effective_user.language_code or 'en'
    telegram_id = update.effective_user.id

    # Get the correct chat ID for sending messages
    if update.message:
        chat_id = update.message.chat_id
    elif update.callback_query:
        chat_id = update.callback_query.message.chat_id
    else:
        return False  # Can't send message if no chat ID

    # Configuration for different block levels
    BLOCK_CONFIG = {
        1: {'duration': timedelta(hours=3), 'penalty': timedelta(hours=3)},
        2: {'duration': timedelta(days=3), 'penalty': timedelta(days=1)},
        3: {'duration': timedelta(days=7), 'penalty': timedelta(days=3)},
    }

    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                # Fetch user's block status
                cursor.execute("""
                    SELECT block_num, date_block 
                    FROM users 
                    WHERE telegram_id = %s
                """, (telegram_id,))
                user_data = cursor.fetchone()
                if not user_data:
                    return False  # User not found

                block_num, date_block = user_data
                current_time = datetime.now()
                if block_num == 3:
                        # Unblock user
                        cursor.execute("""
                            UPDATE users 
                            SET is_banned = True
                            WHERE telegram_id = %s
                        """, (telegram_id,))
                        conn.commit()
                        return False
                else:
                    if block_num == 0 or not date_block:
                        return False  # Not blocked

                    # Get block configuration
                    config = BLOCK_CONFIG.get(block_num)
                    if not config:
                        logger.warning(f"Invalid block number {block_num} for user {telegram_id}")
                        return False

                    # Calculate time thresholds
                    penalty_duration = config['penalty']
                    block_duration = config['duration']
                    release_time = date_block + block_duration
                    penalty_threshold = current_time - penalty_duration

                    # Check if penalty period has expired
                    if date_block < penalty_threshold:
                        return False

                    # User remains blocked
                    localized_time = release_time.strftime("%Y-%m-%d %H:%M:%S")
                    msg = (
                        "⚠️ تم حظرك حتى تاريخ {} بسبب انتهاكك الشروط وسياسة البوت والمصداقية بالعمل" 
                        if user_lang.startswith('ar') 
                        else "⚠️ You're blocked until {} Due to violation of the terms and conditions, bot policy and credibility of work"
                    )
                    # Use context.bot.send_message instead of update.message.reply_text
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=msg.format(localized_time)
                    )
                    return True

    except Exception as e:
        logger.error(f"Block check error: {e}", exc_info=True)
        return False  # Assume not blocked on error

async def block_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a link as processed for the user"""
    telegram_id = update.effective_user.id
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT block_num FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                user_data = cursor.fetchone()[0]
                # if user_data == 0:
                #     cursor.execute("""
                #         UPDATE users 
                #         SET block_num = block_num + %s
                #         WHERE telegram_id = %s
                #     """, (1,date_now, telegram_id,))
                #     conn.commit()
                    
                if user_data < 3:
                    cursor.execute("""
                        UPDATE users 
                        SET block_num = block_num + %s, date_block = %s
                        WHERE telegram_id = %s
                    """, (1, date_now, telegram_id,))
                    conn.commit()
                # else:
                #     await block(update, context)
    except Exception as e:
        logger.error(f"Error in update_user_points: {e}")
        conn.rollback()
    finally:
        conn.close()



# def block(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
#     """Mark a link as processed for the user"""
#     telegram_id = update.effective_user.id
#     try:
#         with connect_db() as conn:
#             with conn.cursor() as cursor:
#                 cursor.execute("""
#                     UPDATE users 
#                     SET is_banned = True
#                     WHERE telegram_id = %s
#                 """, (telegram_id,))
#                 conn.commit()
#     except Exception as e:
#         logger.error(f"Error in update_user_points: {e}")
#         conn.rollback()
#     finally:
#         conn.close()

 
def mark_link_processed(telegram_id: int,user_name: str,res_name, link_id: int, res) -> None:
    """Mark a link as processed for the user"""
    date_mation = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
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
        conn.rollback()
    finally:
        conn.close()

def update_user_points(telegram_id: int, points: int) -> None:
    """Update user's points balance"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE users 
                    SET points = points + %s
                    WHERE telegram_id = %s
                """, (points, telegram_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error in update_user_points: {e}")
        conn.rollback()
    finally:
        conn.close()
        
def update_likes(link_id: int, points: int = 1) -> None:
    """Update user's points balance"""
    try:
        with connect_db() as conn:
            cursor = conn.cursor()            
            cursor.execute("""
            UPDATE likes SET channel_likes = channel_likes + %s
            WHERE id = %s
            """, (1,link_id))
            
            cursor.execute(
                "SELECT channel_likes,subscription_count FROM likes WHERE id = %s",
                (link_id,)
            )
            user_data = cursor.fetchone()            
            # cursor.execute(
            #     "SELECT subscription_count FROM links WHERE id = %s",
            #     (link_id,)
            # )
            # user_data1 = cursor.fetchone()
            if user_data[0] == user_data[1]:
                cursor.execute(
                    "DELETE FROM links WHERE id = %s",
                    (link_id,)
                )
                cursor.execute("""
                UPDATE likes SET status = %s
                WHERE id = %s
                """, (True,link_id))
                print(f"{link_id}")
            conn.commit()

    except Exception as e:
        logger.error(f"Error in update_likes: {e}")
        conn.rollback()
    finally:
        conn.close()

##########################
#    Command Handlers    #
##########################
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display main menu keyboard based on user language"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        
        if user_lang.startswith('ar'):
            # Arabic menu
            keyboard = [
                ["بدء 👋", "تسجيل الدخول 📝"],
                ["الملف الشخصي 📋", "عرض المهام 🔍"],
                ["سحب الأرباح 💵", "فيديو تعليمي 📹"]  # Added Arabic command# New Arabic withdrawal button
            ]
            menu_text = "اختر أمرا من القائمة أدناه"
        else:
            # English menu (default)
            keyboard = [
                ["👋 Start", "📝 Register"],
                ["📋 Profile", "🔍 View Links"],
                ["💵 Withdraw", "Educational video 📹"]  # New English withdrawal button
            ]
            menu_text = "Choose a command From The Menu Below:"
            
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        # Handle both messages and callback queries
        if update.message:
            await update.message.reply_text(menu_text, reply_markup=reply_markup)
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=menu_text,
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"Error in show_menu: {e}")
        error_msg = "⚠️ تعذر عرض القائمة" if user_lang.startswith('ar') else "⚠️ Couldn't display menu"
        await update.effective_message.reply_text(error_msg)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    try:
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name
        user_lang = update.effective_user.language_code or 'en'
        # Clear any existing conversation state
        context.user_data.clear()
        if await is_banned(user_id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(user_name+" "+msg)
            return
        if user_exists(user_id):
            if user_id in config.ADMIN_IDS:
                msg = "أهلا وسهلا بك أدمن! 🛡️" if user_lang.startswith('ar') else "Welcome back Admin! 🛡️"
                await update.message.reply_text(msg)
            else:
                msg = "أهلا بعودتك 🎉" if user_lang.startswith('ar') else "Welcome back ! 🎉"
                await update.message.reply_text(user_name+" "+msg)
            await show_menu(update, context)
        else:
            msg = "أهلا وسهلا بك من فضلك قم بالتسجيل أولا " if user_lang.startswith('ar') else "Welcome ! Please Register First"
            await update.message.reply_text(user_name+" "+msg)
            await show_menu(update, context)
        # Force end any existing conversations
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in start: {e}")
        msg = "! لا يمكن معالجة طلبك حاليا يرجى المحاولة لاحقا ⚠️" if user_lang.startswith('ar') else "⚠️ Couldn't process your request. Please try again."
        await update.message.reply_text(msg)

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start registration process with state cleanup"""
    try:
        user_id = update.effective_user.id
        user_lang = update.effective_user.language_code or 'en'
        
        # Clear previous state
        context.user_data.clear()
        
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 "  if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        if user_exists(user_id):
            msg = "لا حاجة لإعادة التسجيل أنت مسجل بالفعل ✅ " if user_lang.startswith('ar') else "You're already registered! ✅"
            await update.message.reply_text(msg)
            return ConversationHandler.END
        if user_lang.startswith('ar'):
            keyboard = [["إلغاء ❌"]]
            msg = "من فضلك قم بإدخال بريدك الإلكتروني لإرسال رمز التأكيد والمتابعة"
        else:
            keyboard = [["Cancel ❌"]]
            msg = "Please enter your email address:"
            
        await update.message.reply_text(
            msg,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
        return EMAIL
    except Exception as e:
        logger.error(f"Error in register: {e}")
        msg = "يمكنك التسجيل الآن حاول لاحقا ⚠️ " if user_lang.startswith('ar') else "⚠️ Couldn't start registration. Please try again."
        await update.message.reply_text(msg)
        return ConversationHandler.END

async def process_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_lang = update.effective_user.language_code or 'en'
        email = update.message.text.strip()
        email_check = email.lower()
        if email in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END
        try:
            with connect_db() as conn:
                with conn.cursor() as cursor:
                    # Get user data
                    cursor.execute(
                        "SELECT 1 FROM users WHERE email = %s",
                        (email_check,)
                    )
                    user_data = cursor.fetchone()
                    if user_data:
                        error_msg = "❌ Your Email has Already Exists Change To A Deferent Email" if user_lang != 'ar' else "❌ هذا البريد الإلكتروني مستخدم بالفعل أدخل بريد آخر"
                        await update.message.reply_text(error_msg)
                        return EMAIL 
        except Exception as e:
            error_msg = "❌ Invalid email format" if user_lang != 'ar' else "❌ صيغة البريد الإلكتروني غير صحيحة"
            await update.message.reply_text(error_msg)
            return EMAIL
        
        if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
            error_msg = "❌ Invalid email format" if user_lang != 'ar' else "❌ صيغة البريد الإلكتروني غير صحيحة"
            await update.message.reply_text(error_msg)
            return EMAIL

        # Generate and send confirmation code
        code = generate_confirmation_code()
        context.user_data['confirmation_code'] = code
        context.user_data['email'] = email

        if not send_confirmation_email(email, code):
            error_msg = "Failed to send code" if user_lang != 'ar' else "فشل إرسال الرمز"
            await update.message.reply_text(error_msg)
            return EMAIL

        success_msg = (
            "📧 A confirmation code has been sent to your email or in spam. Please enter it here Or Press Cancel from the Menu For Cancel Registration:" 
            if user_lang != 'ar' else 
            "📧 تم إرسال رمز التأكيد إلى بريدك الإلكتروني أو في رسائل البريد العشوائي (سبام) . الرجاء إدخاله هنا أو إضغط إلغاء من القائمة لإلغاء التسجيل:"
        )
        await update.message.reply_text(success_msg)
        return CODE_VERIFICATION

    except Exception as e:
        logger.error(f"Email processing error: {e}")
        error_msg = "⚠️ Error processing email" if user_lang != 'ar' else "⚠️ خطأ في معالجة البريد"
        await update.message.reply_text(error_msg)
        await show_menu(update, context)
        return EMAIL



async def verify_confirmation_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_code = update.message.text.strip()
        stored_code = context.user_data.get('confirmation_code')

        if user_code in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        if not stored_code:
            error_msg = "Session expired" if user_lang != 'ar' else "انتهت الجلسة"
            await update.message.reply_text(error_msg)
            return ConversationHandler.END

        if user_code == stored_code:
            # Create phone number keyboard with skip option
            if user_lang.startswith('ar'):
                keyboard = [
                    [KeyboardButton("⬇️ مشاركة رقم الهاتف هنا", request_contact=True)],
                    ["تخطي", "إلغاء ❌"]
                ]
                contact_msg = "شارك رقم هاتفك ⬇️ أو اضغط تخطي: 📱\n(في حال اخترت التخطي لن يتم تسجيل رقم هاتفك)"
            else:
                keyboard = [
                    [KeyboardButton("Share your phone number ⬇️:\n(If you choose to skip, your phone number will not be recorded)", request_contact=True)],
                    ["Skip", "Cancel ❌"]
                ]
                contact_msg = "📱 Share your phone number ⬇️ or skip:"

            reply_markup = ReplyKeyboardMarkup(
                keyboard, 
                resize_keyboard=True,
                one_time_keyboard=True
            )
            
            # Use update.message instead of query
            await update.message.reply_text(contact_msg, reply_markup=reply_markup)
            return PHONE
            
        else:
            error_msg = "❌ Invalid code" if user_lang != 'ar' else "❌ رمز غير صحيح"
            await update.message.reply_text(error_msg)
            return CODE_VERIFICATION

    except Exception as e:
        logger.error(f"Code verification error: {e}")
        error_msg = "⚠️ Verification failed try again" if user_lang != 'ar' else "⚠️ فشل التحقق أعد المحاولة"
        await update.message.reply_text(error_msg)
        return CODE_VERIFICATION

    except Exception as e:
        logger.error(f"Code verification error: {e}")
        error_msg = "⚠️ Verification failed try again" if user_lang != 'ar' else "⚠️ فشل التحقق أعد المحاولة"
        await update.message.reply_text(error_msg)
        await show_menu(update, context)
        return CODE_VERIFICATION


async def process_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_lang = update.effective_user.language_code or 'en'
        user = update.effective_user

        # Handle skip
        if update.message.text in ["Skip", "تخطي"]:
            context.user_data['phone'] = "+0000000000"
            context.user_data['full_name'] = user.name
            context.user_data['country'] = "Syria"
            await prompt_cash_number(update, context, user_lang)
            return CASH_NUMBER

        # Handle cancellation
        if update.message.text in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END

        # Handle contact sharing
        if update.message.contact:
            contact = update.message.contact
            if contact.user_id != user.id:
                msg = "من فضلك شارك رقمك الخاص ❌" if user_lang.startswith('ar') else "❌ Please share your own number!"
                await update.message.reply_text(msg)
                return PHONE

            phone_number = "+" + contact.phone_number
            try:
                parsed_number = phonenumbers.parse(phone_number, None)
                country = geocoder.description_for_number(parsed_number, "en") or "Unknown"
            except phonenumbers.NumberParseException:
                country = "Unknown"
        else:
            msg = "من فضلك شارك رقمك الخاص أو اضغط (تخطي) أو إلغاء العملية ❌" if user_lang.startswith('ar') else "❌ Please share your private number or press (skip) or cancel the process!"
            await update.message.reply_text(msg)
            return PHONE
            # phone_number = "+0000000000"
            # country = "Syria"

        context.user_data['phone'] = phone_number
        context.user_data['country'] = country
        await prompt_cash_number(update, context, user_lang)
        return CASH_NUMBER

    except Exception as e:
        logger.error(f"Phone processing error: {e}")
        error_msg = "⚠️ خطأ في معالجة رقم الهاتف" if user_lang.startswith('ar') else "⚠️ Error processing phone number"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END


async def prompt_cash_number(update: Update, context: ContextTypes.DEFAULT_TYPE, user_lang: str):
    try:
        if user_lang.startswith('ar'):
            msg = "الرجاء إدخال رقم الكاش الخاص بك (أرقام فقط) وتأكد منه قبل المتابعة لأنه الرقم الذي سيتم تحويل الأرباح عليه وهذا على مسؤليتك الشخصية لكي لا يضيع تعبك أو أضغط على تخطي وعند سحب الأرباح سوف تقوم بإدخاله:"
            cancel_btn = ["تخطي", "إلغاء ❌"]
        else:
            msg = "Please enter your cash number (digits only) And Make sure of it before proceeding because it is the number to which the profits will be transferred and this is your personal responsibility so that your efforts are not wasted Or click skip and when withdrawing the profits you will enter it:"
            cancel_btn = ["Skip", "Cancel ❌"]

        keyboard = [cancel_btn]
        await update.message.reply_text(
            msg,
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    except Exception as e:
        logger.error(f"Error prompting cash number: {e}")

async def process_cash_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        user_lang = update.effective_user.language_code or 'en'
        cash_number = update.message.text.strip()
    
        
        if cash_number in ["Cancel ❌", "إلغاء ❌"]:
            await cancel_registration(update, context)
            return ConversationHandler.END


        
        # Handle skip
        if cash_number in ["Skip", "تخطي"]:
            cash_number = None
        else:
            if not cash_number.isdigit():
                error_msg = "❌ يرجى إدخال أرقام فقط" if user_lang.startswith('ar') else "❌ Please enter digits only"
                await update.message.reply_text(error_msg)
                return CASH_NUMBER
        # Save to database
        try:
            with connect_db() as conn:
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
        except Exception as e:
            logger.error(f"Database error: {e}")
            msg = "⚠️ فشل التسجيل" if user_lang.startswith('ar') else "⚠️ Registration failed"
            await update.message.reply_text(msg)
            return ConversationHandler.END

        # # Success message
        # success_msg = (
        #     f"✅ تم التسجيل بنجاح!\n"
        #     f"📱 الهاتف: {context.user_data['phone']}\n"
        #     f"💳 رقم الكاش: {cash_number}"
        #     if user_lang.startswith('ar') else
        #     f"✅ Registration successful!\n"
        #     f"📱 Phone: {context.user_data['phone']}\n"
        #     f"💳 Cash number: {cash_number}"
        # )
        
        
        full_name = update.effective_user.name
        email = context.user_data['email']
        phone_number = context.user_data['phone']
        country = context.user_data['country']
        registration_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Handle display of cash number (show N/A if skipped)
        display_cash = cash_number if cash_number is not None else "N/A"
        
        success_msg = (
        f"✅ تم إكمال التسجيل بنجاح :\n"
        f"👤 أسمك : {escape_markdown(full_name)}\n"
        f"📧 بريدك الإلكتروني : {escape_markdown_2(email)}\n"
        f"📱 رقم هاتفك : {escape_markdown_2(phone_number)}\n"
        f"💳 رقم الكاش: {display_cash}\n"
        f"🌍 بلدك : {escape_markdown(country)}\n"
        f"⭐ تاريخ التسجيل : {escape_markdown(registration_date)}"
        if user_lang.startswith('ar') else
        f"✅ Registration Complete:\n"
        f"👤 Name: {escape_markdown(full_name)}\n"
        f"📧 Email: {escape_markdown_2(email)}\n"
        f"📱 Phone: {escape_markdown_2(phone_number)}\n"
        f"💳 Cash number: {display_cash}\n"
        f"🌍 Country: {escape_markdown(country)}\n"
        f"⭐ Registration Date: {escape_markdown(registration_date)}"
        )

        await update.message.reply_text(success_msg, reply_markup=ReplyKeyboardRemove())
        await show_menu(update, context)
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Cash number error: {e}")
        error_msg = "⚠️ خطأ في معالجة البيانات" if user_lang.startswith('ar') else "⚠️ Error processing data"
        await update.message.reply_text(error_msg)
        return ConversationHandler.END



async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user profile"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return
        profile = get_profile(user_id)
        # print(f"{profile}")
        if profile:
            _, name, email, phone, country, reg_date, points, cash_number, total_withdrawals = profile
            if user_lang.startswith('ar'):
                msg = (f"📋 *ملفك الشخصي :*\n"
                    f"👤 أسمك : {escape_markdown(name)}\n"
                    f"📧 بريدك الإلكتروني : {escape_markdown(email)}\n"
                    f"📱 رقم هاتفك : {escape_markdown(phone)}\n"
                    f"💳 رقم الكاش: {cash_number}\n"
                    f"🌍 بلدك : {escape_markdown(country)}\n"
                    f"⭐ تاريخ التسجيل : {escape_markdown(str(reg_date))}\n"
                    f"🏆 نقاطك : {points} نقطة\n"
                    f"💰 إجمالي السحوبات : {total_withdrawals} نقطة\n\n"
                    f"سوف يتم إضافة رصيد مهماتك الحديثة التي قمت بإنجازها في أقرب وقت وأي مهمة تقوم بإلغاء تنفيذها من تلقاء نفسك سوف يتم خصم رصيدها عند سحب الأرباح")
            else:
                msg = (f"📋 *Profile Information*\n"
                    f"👤 Name: {escape_markdown(name)}\n"
                    f"📧 Email: {escape_markdown(email)}\n"
                    f"📱 Phone: {escape_markdown(phone)}\n"
                    f"💳 Cash number: {cash_number}\n"
                    f"🌍 Country: {escape_markdown(country)}\n"
                    f"⭐ Registration Date: {escape_markdown(str(reg_date))}\n"
                    f"🏆 Points: {points} points\n"
                    f"💰 Total Withdrawals: {total_withdrawals} points\n\n"
                    f"Your recently completed tasks will be credited as soon as possible, and any task you cancel on your own will have its balance deducted when withdrawing profits")              
            response = (msg)
            await update.message.reply_text(response, parse_mode="MarkdownV2")
        else:
            msg = "أنت لست مسجل قم بالتسجيل أولا ❌ " if user_lang.startswith('ar') else "❌ You're not registered! Register First"
            await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"Profile error: {e}")
        msg = "لا يمكن عرض الملف الشخصي حاليا يرجى إعادة المحاولة لاحقا ⚠️ " if user_lang.startswith('ar') else "⚠️ Couldn't load profile. Please try again."
        await update.message.reply_text(msg)
        
def get_profile(telegram_id: int) -> tuple:
    """Retrieve user profile data"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                # Get user data
                cursor.execute(
                    "SELECT COUNT(*) FROM user_link_status WHERE date_mation < CURRENT_TIMESTAMP - INTERVAL '3 days' and telegram_id = %s and points_status = %s",
                    (telegram_id, False)
                )
                user_date_data = cursor.fetchone()
                points = user_date_data[0] if user_date_data else 0
                if user_date_data:                 
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
                    "SELECT telegram_id, full_name, email, phone, country, registration_date, points, cash_number FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                user_data = cursor.fetchone()
                if not user_data:
                    return None

                # Get total withdrawals
                cursor.execute(
                    "SELECT COALESCE(SUM(amount), 0) FROM withdrawals WHERE user_id = %s",
                    (telegram_id,)
                )
                total_withdrawals = cursor.fetchone()[0] or 0

                return (*user_data, total_withdrawals)
    except Exception as e:
        logger.error(f"Error in get_profile: {e}")
        return None
    
async def view_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display available links"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id
        
        if await block_check(update, context):
            return  # User is blocked, stop processing
        
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return
        if not user_exists(user_id):
            msg = "من فضلك قم بالتسجيل أولا للمتابعة ❌ " if user_lang.startswith('ar') else "❌ Please register first!"
            await update.message.reply_text(msg)
            return

        
        user_pages[user_id] = 0
        await send_links_page(user_lang,update.effective_chat.id, user_id, 0, context)
    except Exception as e:
        logger.error(f"View links error: {e}")
        msg = " لا يمكن تحميل المهمات حاليا يرجى المحاولة لاحقا ⚠️" if user_lang.startswith('ar') else "⚠️ Couldn't load links. Please try again."
        await update.message.reply_text(msg)

##########################
#    Link Management     #
##########################
async def send_links_page(user_lang: str,chat_id: int, user_id: int, page: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send paginated links with user-specific message tracking"""
    try:
        links, total_pages = get_paginated_links(user_id, page)
        
        if not links:
            msg = " لايوجد مهمات لك الآن قم بتحديث المهمات لرؤية المزيد في حال وجودها 🎉" if user_lang.startswith('ar') else "🎉 No more links available!"
            await context.bot.send_message(chat_id, msg)
            return

        for link in links:
            link_id, yt_link, desc, adder,channel_id = link
            if user_lang.startswith('ar'):
                text = (
                    f"📛 {escape_markdown(desc)}\n"
                    f"👤 *بواسطة* {escape_markdown(adder)}\n"
                    f"[🔗 رابط الذهاب للمهمة انقر هنا]({yt_link})"
                    )
                keyboard = [[InlineKeyboardButton(" تنفيذ المهمة وبعد الانتهاء تحميل لقطة الشاشة لتأكيدها مبدئيا 📸", callback_data=f"submit_{link_id}")]]
            else:
                text = (
                    f"📛 {escape_markdown(desc)}\n"
                    f"👤 *By:* {escape_markdown(adder)}\n"
                    f"[🔗 YouTube Link]({yt_link})"
                )
                keyboard = [[InlineKeyboardButton("📸 Accept And  Subscribed And Then Submit Screenshot", callback_data=f"submit_{link_id}")]]

            message = await context.bot.send_message(
                chat_id,
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2"
            )
            store_message_id(user_id, chat_id, link_id, message.message_id)

        if total_pages > 1:
            buttons = []
            current_page = page + 1  # Convert 0-based to 1-based
            
            # Add page info to message
            if user_lang.startswith('ar'):
                page_info = f"{current_page} / {total_pages}"
                if page > 0:
                    buttons.append(InlineKeyboardButton(" الصفحة السابقة ⬅️", callback_data=f"prev_{page-1}"))
                if page < total_pages - 1:
                    buttons.append(InlineKeyboardButton("➡️ الصفحة التالية ", callback_data=f"next_{page+1}"))
            else:
                page_info = f"{current_page} / {total_pages}"
                if page > 0:
                    buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"prev_{page-1}"))
                if page < total_pages - 1:
                    buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"next_{page+1}"))

            if buttons:
                await context.bot.send_message(
                    chat_id,
                    page_info,  # Updated message with page numbers
                    reply_markup=InlineKeyboardMarkup([buttons])
                )
                
    except Exception as e:
        logger.error(f"Error sending links: {e}")
        msg = " لا يمكن عرض المهمات الآن يرجى تحديث المهمات لرؤيتها ⚠️" if user_lang.startswith('ar') else "⚠️ Couldn't load links. Please try later."
        await context.bot.send_message(chat_id, msg)

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu text commands in both languages"""
    try:
        text = update.message.text
        user_lang = update.effective_user.language_code or 'en'
        
        # Command mapping for both languages
        command_map = {
            # English commands
            "👋 Start": "start",
            "📝 Register": "register",
            "📋 Profile": "profile",
            "🔍 View Links": "view_links",
            "Educational video 📹": "educational_video",
            # Arabic commands
            "بدء 👋" : "start",
            "تسجيل الدخول 📝": "register",
            "الملف الشخصي 📋": "profile",
            "عرض المهام 🔍": "view_links",
            "فيديو تعليمي 📹": "educational_video"
        }

        action = command_map.get(text)
        
        if action == "start":
            await start(update, context)
        elif action == "register":
            msg = "جاري بدء التسجيل..." if user_lang.startswith('ar') else "Starting registration..."
            await update.message.reply_text(msg)
            await register(update, context)
        elif action == "profile":
            await profile_command(update, context)
        elif action == "view_links":
            await view_links(update, context)
        else:
            msg = "أمر غير معروف. يرجى استخدام أزرار القائمة ❌ " if user_lang.startswith('ar') else "❌ Unknown command. Please use the menu buttons."
            await update.message.reply_text(msg)
            await show_menu(update,context)
            
    except Exception as e:
        logger.error(f"Text command error: {e}")
        error_msg = "تعذر معالجة الأمر. يرجى المحاولة مرة أخرى ⚠️ " if user_lang.startswith('ar') else "⚠️ Couldn't process command. Please try again."
        await update.message.reply_text(error_msg)

async def navigate_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination navigation for links list"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        action, page_str = query.data.split('_')
        new_page = int(page_str)
        user_pages[user_id] = new_page
        await send_links_page(user_lang,query.message.chat_id, user_id, new_page, context)
        await query.message.delete()
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        if 'query' in locals():
            error_msg = "تعذر تحميل الصفحة. يرجى المحاولة مرة أخرى ⚠️ " if user_lang.startswith('ar') else "⚠️ Couldn't load page. Please try again."
            await query.message.reply_text(error_msg)

##########################
#    Image Submission    #
##########################
async def handle_submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image submission requests with user-specific context"""
    try:
        user_lang = update.effective_user.language_code or 'en'

        query = update.callback_query
        await query.answer()
        if await block_check(update, context):
            return  # User is blocked, stop processing
                
        user_id = query.from_user.id
        
        if await is_banned(user_id):
            msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await query.message.reply_text(msg)
            return ConversationHandler.END
        
        chat_id = query.message.chat_id
        link_id = int(query.data.split("_")[1])
        
        message_id = get_message_id(user_id, chat_id, link_id)
        if not message_id:
            msg = " تم تعطيل الجلسة يرجى تحديث قائمة المهام ⚠️" if user_lang.startswith('ar') else "⚠️ Session expired. Please reload links."
            await query.message.reply_text(msg)
            return
            
        allowed_links = get_allowed_links(user_id)
        if not any(link[0] == link_id for link in allowed_links):
            msg = " هذه المهمة لم تعد متاحة لك ⚠️" if user_lang.startswith('ar') else "⚠️ This link is no longer available."
            await query.message.reply_text(msg)
            return
            
        description = get_link_description(link_id)
        if not description:
            msg = " خطأ في تفاصيل المهمة قم بتحديث المهمات ❌" if user_lang.startswith('ar') else "❌ Link details missing"
            await query.message.reply_text("❌ Link details missing")
            return
            
        pending_submissions[user_id] = {
            'link_id': link_id,
            'chat_id': chat_id,
            'message_id': message_id,
            'description': description
        }
        
        if user_lang.startswith('ar'):
            textt=f"📸 خذ لقطة الشاشة للقناة وأرسلها هنا : {description}"
        else:
            textt=f"📸 Submit image for: {description}"
            
        await context.bot.send_message(
            chat_id=chat_id,
            text=textt,
            reply_to_message_id=message_id
        )

    except Exception as e:
        logger.error(f"Submit error: {e}")
        msg = " خطأ في تفاصيل المهمة قم بتحديث المهمات ❌" if user_lang.startswith('ar') else "❌ Link details missing"
        await query.message.reply_text(msg)


def get_link_description(link_id: int) -> str:
    """Get description for a specific link"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT description FROM links WHERE id = %s",
                    (link_id,))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error in get_link_description: {e}")
        return None
    
    
async def process_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image verification with user-specific context"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id
        user_name = update.effective_user.name
        chat_id = update.effective_chat.id
        
        if user_id not in pending_submissions:
            msg = " خطأ يرجى تحديث المهمات من جديد ❌" if user_lang.startswith('ar') else "❌ No active submission!"
            await update.message.reply_text(msg)
            return
            
        submission = pending_submissions[user_id]
        link_id = submission['link_id']
        message_id = submission['message_id']
        description = submission['description']
        
        photo_file = await update.message.photo[-1].get_file()
        image_path = f"temp_{user_id}_{link_id}.jpg"
        await photo_file.download_to_drive(image_path)
        
        msg = " جاري التحقق يرجى الانتظار٫٫٫٫٫٫٫٫ 🔍" if user_lang.startswith('ar') else "🔍 Verifying..."
        processing_msg = await update.message.reply_text(
            msg,
            reply_to_message_id=message_id
        )

        verification_passed = False
        try:
            if scan_image10.check_text_in_image(image_path, description):
                verification_passed = True

        except Exception as e:
            logger.error(f"Image processing error: {e}")
        
        if verification_passed:
            try:
                with connect_db() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            "SELECT channel_id,description FROM links WHERE id = %s",
                            (link_id,)
                        )
                        result = cursor.fetchone()
                        res= result[0]
                        res_name = result[1]
            except Exception as e:
                logger.error(f"Ban check error: {e}")
                return False
            youtube_link = description
            mark_link_processed(user_id, user_name, res_name, link_id, res)
            # update_user_points(user_id)
            update_likes(link_id)
            msg = "✅ تهانينا لقد كسبت نقطة واحدة +١ وسوف يتم إضافتها إلى نقاطك في أقرب وقت تأكد من عدم إلغاء الاشتراك لكي لاتخسر الرصيد عند السحب يرجى الانتقال لمهمة أخرى " if user_lang.startswith('ar') else "✅ Congratulations, you have earned 1 point +1 and it will be added to your points as soon as possible. Make sure not to cancel your subscription so that you do not lose the balance when withdrawing. Please move on to another task."
            await update.message.reply_text(
                msg,
                reply_to_message_id=message_id
            )
        else:
            msg = " فشل التحقق يبدو أنك غير مشترك بالقناة يرجى الاشتراك ثم إعادة المحاولة ❌" if user_lang.startswith('ar') else "❌ Verification failed. Try again."
            await update.message.reply_text(
                msg,
                reply_to_message_id=message_id
            )
            # List of admin user IDs
            admins_id = [7168120805, 6106281772, 1130152311]

            # Check if user is NOT in admin list
            if user_id not in admins_id:
                await block_add(update, context)

        await context.bot.delete_message(
            chat_id=chat_id,
            message_id=processing_msg.message_id
        )

    except Exception as e:
        logger.error(f"Image error: {e}")
        msg = " خطأ في معالجة الطلب يرجى تحديث المهمات وإعادة المحاولة لاحقا ⚠️" if user_lang.startswith('ar') else "⚠️ Processing error. Please try again."
        await update.message.reply_text("⚠️ Processing error. Please try again.")
    finally:
        if 'image_path' in locals() and os.path.exists(image_path):
            os.remove(image_path)
        if user_id in pending_submissions:
            del pending_submissions[user_id]

##########################
#    Helper Functions    #
##########################
def escape_markdown(text: str) -> str:
    """Escape MarkdownV2 special characters"""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(['\\' + c if c in escape_chars else c for c in text])

def escape_markdown_2(text: str) -> str:
    """Escape all MarkdownV2 special characters"""
    escape_chars = r'_*[]()~`>#-=|{}!'
    return ''.join(['\\' + char if char in escape_chars else char for char in text])

def get_paginated_links(user_id: int, page: int = 0, per_page: int = 5) -> tuple:
    """Get paginated list of links"""
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
    """Check if user is banned"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT is_banned FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                result = cursor.fetchone()
                return bool(result and result[0])
    except Exception as e:
        logger.error(f"Ban check error: {e}")
        return False


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for all uncaught exceptions"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        logger.error("Unhandled exception:", exc_info=context.error)
        
        if update is not None and update.effective_message:
            msg = " خطأ غير متوقع يرجى إعادة المحاولة لاحقا ⚠️" if user_lang.startswith('ar') else "⚠️ An unexpected error occurred. Please try again later."
            await update.effective_message.reply_text(
                msg
            )
            await show_menu(update, context)

    except Exception as e:
        logger.error(f"Error in error handler: {e}")
        

##########################
#      Withdrawals       #
##########################
def get_user_points(telegram_id: int) -> int:
    """Get current points balance"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT points FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                result = cursor.fetchone()
                return result[0] if result else 0
    except Exception as e:
        logger.error(f"Error in get_user_points: {e}")
        return 0

def deduct_points(telegram_id: int, amount: int) -> None:
    """Deduct points from user's balance"""
    points_to_deduct = amount
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET points = points - %s WHERE telegram_id = %s",
                    (points_to_deduct, telegram_id)
                )
                conn.commit()
    except Exception as e:
        logger.error(f"Error deducting points: {e}")
        conn.rollback()
        raise

def create_withdrawal(telegram_id: int, amount: int, carrier: str) -> None:
    """Record withdrawal with current cash number"""
    try:
        profile = get_full_profile(telegram_id)
        if not profile:
            raise ValueError("User profile not found")

        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO withdrawals (
                        user_id, amount_before, carrier,
                        full_name, email, phone, country,
                        registration_date, cash_number
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    telegram_id,
                    amount,
                    carrier,
                    profile['full_name'],
                    profile['email'],
                    profile['phone'],
                    profile['country'],
                    profile['registration_date'],
                    profile['cash_number']  # Now reflects any updates
                ))
                conn.commit()
    except Exception as e:
        logger.error(f"Withdrawal creation error: {e}")
        raise

def get_current_cash_number(telegram_id: int) -> str:
    """Get user's current cash number from database"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT cash_number FROM users WHERE telegram_id = %s",
                    (telegram_id,))
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting cash number: {e}")
        return None

def update_cash_number(telegram_id: int, cash_number: str) -> None:
    """Update user's cash number in database"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE users SET cash_number = %s WHERE telegram_id = %s",
                    (cash_number, telegram_id)
                )
                conn.commit()
    except Exception as e:
        logger.error(f"Error updating cash number: {e}")
        conn.rollback()
        raise


def get_full_profile(telegram_id: int) -> dict:
    """Get complete user profile data"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT 
                        full_name,
                        email,
                        phone,
                        country,
                        registration_date,
                        points,
                        cash_number
                    FROM users 
                    WHERE telegram_id = %s
                """, (telegram_id,))
                result = cursor.fetchone()
                if result:
                    return {
                        'full_name': result[0],
                        'email': result[1],
                        'phone': result[2],
                        'country': result[3],
                        'registration_date': result[4],
                        'points': result[5],
                        'cash_number': result[6]
                    }
                return None
    except Exception as e:
        logger.error(f"Error getting full profile: {e}")
        return None

# Add new functions
async def start_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id

    if await is_banned(user_id):
        msg = "🚫 تم إلغاء وصولك " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
        await update.message.reply_text(msg)
        return ConversationHandler.END

    if not user_exists(user_id):
        msg = "من فضلك قم بالتسجيل أولا ❌" if user_lang.startswith('ar') else "❌ Please register first!"
        await update.message.reply_text(msg)
        return ConversationHandler.END

    points = get_user_points(user_id)
    if points < 100:
        msg = "تحتاج إلى 100 نقطة على الأقل لسحب الأرباح ⚠️" if user_lang.startswith('ar') else "⚠️ You need at least 100 points to withdraw."
        await update.message.reply_text(msg)
        return ConversationHandler.END

    msg = "كم عدد المئات التي تريد سحبها؟ (أدخل رقماً)" if user_lang.startswith('ar') else "Enter the number of 100-point units to withdraw:"
    if user_lang.startswith('ar'):
        keyboard = [["إلغاء ❌"]]
        msg = "كم عدد النقاط التي تريد سحبها؟ (أدخل رقماً)"
    else:
        keyboard = [["Cancel ❌"]]
        msg = "Enter the number of points units to withdraw:"
        
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )
    return WITHDRAW_AMOUNT

async def process_withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process withdrawal amount and initiate carrier selection"""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id
    amount_text = update.message.text.strip()
    if amount_text in ["Cancel ❌", "إلغاء ❌"]:
        msg = "تم إلغاء العملية" if user_lang.startswith('ar') else "Process Canceled"
        await update.message.reply_text(msg)
        await show_menu(update, context)
        return ConversationHandler.END
    # Validate numeric input
    if not amount_text.isdigit():
        error_msg = (
            "❌ يرجى إدخال أرقام فقط" if user_lang.startswith('ar') 
            else "❌ Please enter numbers only"
        )
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    try:
        amount = int(amount_text)
        if amount <= 0:
            raise ValueError("Negative value")
    except ValueError:
        error_msg = (
            "❌ الرجاء إدخال رقم صحيح موجب" if user_lang.startswith('ar')
            else "❌ Please enter a positive integer"
        )
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT

    # Check available points
    available_points = get_user_points(user_id)
    max_withdrawal_units = available_points // 100
    max_withdrawal_units_allow = max_withdrawal_units * 100

    if max_withdrawal_units_allow < 100:
        error_msg = (
            "⚠️ تحتاج إلى 100 نقطة على الأقل للسحب" if user_lang.startswith('ar')
            else "⚠️ You need at least 100 points to withdraw"
        )
        await update.message.reply_text(error_msg)
        await show_menu(update, context)
        return ConversationHandler.END

    if amount > max_withdrawal_units_allow:
        error_msg = (
            f"❌ الحد الأقصى للسحب هو {max_withdrawal_units_allow}" if user_lang.startswith('ar')
            else f"❌ Maximum withdrawal is {max_withdrawal_units_allow} units"
        )
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT
    
    if amount < 100:
        error_msg = (
            f"❌ (100,200.....)لاتستطيع سحب سوى نقاط من فئة المئات أو أضعافها" if user_lang.startswith('ar')
            else f"❌ withdrawal is 100 or 200 or...... units"
        )
        await update.message.reply_text(error_msg)
        return WITHDRAW_AMOUNT
    # Store valid amount and proceed to carrier selection
    context.user_data['withdrawal_amount'] = amount
    return await select_carrier(update, context)



async def select_carrier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display carrier selection buttons"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        
        buttons = [
            [
                InlineKeyboardButton("MTN", callback_data="carrier_MTN"),
                InlineKeyboardButton(
                    "سيرياتيل" if user_lang.startswith('ar') else "SYRIATEL", 
                    callback_data="carrier_SYRIATEL"
                )
            ]
        ]
        
        prompt_text = (
            "الرجاء اختيار شركة الاتصالات أو أضغط إلغاء من القائمة لإلغاء العملية:" 
            if user_lang.startswith('ar')
            else "Please select your mobile carrier or Cancel from the Menu to Cancel the Process:"
        )

        await update.message.reply_text(
            prompt_text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return CARRIER_SELECTION

    except Exception as e:
        logger.error(f"Error getting full profile: {e}")
        error_msg = (
            f"❌حدث خطأ يرجى من المحاولة من جديد " 
            if user_lang.startswith('ar')
            else f"❌ there is an Error Try again please"
        )
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

async def handle_invalid_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle invalid input during carrier selection"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        
        # Recreate the selection buttons
        buttons = [
            [
                InlineKeyboardButton("MTN", callback_data="carrier_MTN"),
                InlineKeyboardButton(
                    "سيرياتيل" if user_lang.startswith('ar') else "SYRIATEL", 
                    callback_data="carrier_SYRIATEL"
                )
            ]
        ]

        error_text = (
            "❌ اختيار غير صحيح، الرجاء استخدام الأزرار أعلاه أو إلغاء العملية:"
            if user_lang.startswith('ar')
            else "❌ Invalid selection, please use the buttons above or cancel the process:"
        )

        await update.message.reply_text(
            error_text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return CARRIER_SELECTION  # Stay in the same state

    except Exception as e:
        logger.error(f"Error handling invalid input: {e}")
        error_msg = (
            "❌ حدث خطأ، يرجى المحاولة مرة أخرى"
            if user_lang.startswith('ar')
            else "❌ An error occurred, please try again"
        )
        await update.message.reply_text(error_msg)
        return ConversationHandler.END

async def process_carrier_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle carrier selection and prompt for cash number update"""
    user_lang = update.effective_user.language_code or 'en'
    query = update.callback_query
    await query.answer()
    
    try:
        # Delete the carrier selection message
        await context.bot.delete_message(
            chat_id=query.message.chat_id,
            message_id=query.message.message_id
        )
    except Exception as e:
        logger.error(f"Error deleting carrier message: {e}")

    # Store selected carrier
    carrier = query.data.split('_')[1]
    context.user_data['carrier'] = carrier
    
    # Get current cash number
    current_cash = get_current_cash_number(query.from_user.id)

    # Prepare message
    if user_lang.startswith('ar'):
        msg = f"أدخل رقم الكاش الجديد أو 'تخطي' للحفاظ على الرقم الحالي.\nالرقم الحالي هو: {current_cash}\nملاحظة في حال أنك تريد إدخال رقم جديد\nالرجاء إدخال رقم الكاش الخاص بك (أرقام فقط) وتأكد منه قبل المتابعة لأنه الرقم الذي سيتم تحويل الأرباح عليه وهذا على مسؤليتك الشخصية لكي لا يضيع تعبك"
        keyboard = [["تخطي"], ["إلغاء ❌"]]
    else:
        msg = f"Enter new cash number or 'Skip' to keep current.\nThe Current Cash Number Is: {current_cash}\nNote: If you want to get a new number:\nPlease enter your cash number (digits only) And Make sure of it before proceeding because it is the number to which the profits will be transferred and this is your personal responsibility so that your efforts are not wasted."
        keyboard = [["Skip"], ["Cancel ❌"]]
    
    await query.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    return UPDATE_CASH

async def process_cash_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process cash number update and complete withdrawal"""
    user_lang = update.effective_user.language_code or 'en'
    user_id = update.effective_user.id
    user_input = update.message.text.strip()

    # Handle cancellation
    if user_input in ["Cancel ❌", "إلغاء ❌"]:
        await cancel_withdrawal(update, context)
        return ConversationHandler.END

    # Handle skip
    if user_input in ["Skip", "تخطي"]:
        cash_number = get_current_cash_number(user_id)
    else:
        # Validate input
        if not user_input.isdigit():
            error_msg = "❌ أرقام فقط" if user_lang.startswith('ar') else "❌ Digits only"
            await update.message.reply_text(error_msg)
            return UPDATE_CASH
        
        # Update cash number
        cash_number = user_input
        update_cash_number(user_id, cash_number)

    # Complete withdrawal
    try:
        amount = context.user_data['withdrawal_amount']
        carrier = context.user_data['carrier']
        
        deduct_points(user_id, amount)
        create_withdrawal(user_id, amount, carrier)
        
        success_msg = (f"✅ تم طلب سحب {amount} نقطة إلى {carrier}\nرقم الكاش: {cash_number} وسوف يتم إعلامك عند تحويلها وإضافتها إلى إجمالي السحوبات"
                        if user_lang.startswith('ar') 
                        else f"✅ Withdrawal request for {amount} points to {carrier} requested\nCash number: {cash_number} And You will be notified when it is transferred and added to the total withdrawals.")
        await update.message.reply_text(success_msg)
        
    except Exception as e:
        logger.error(f"Withdrawal error: {e}")
        error_msg = "❌ فشل السحب" if user_lang.startswith('ar') else "❌ Withdrawal failed"
        await update.message.reply_text(error_msg)

    context.user_data.clear()
    await show_menu(update, context)
    return ConversationHandler.END



async def cancel_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lang = update.effective_user.language_code or 'en'
    await update.message.reply_text(
        "❌ تم إلغاء عملية التسجيل" if user_lang.startswith('ar') else "❌ Registration cancelled",
        reply_markup=ReplyKeyboardRemove()
    )
    return ConversationHandler.END

async def cancel_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allow users to cancel registration at any point"""
    user_lang = update.effective_user.language_code or 'en'
    context.user_data.clear()
    msg = "تم إلغاء التسجيل ❌" if user_lang.startswith('ar') else "❌ Registration cancelled"
    await update.message.reply_text(msg)
    await show_menu(update, context)
    return ConversationHandler.END

async def restart_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle registration restart during active conversation"""
    user_lang = update.effective_user.language_code or 'en'
    context.user_data.clear()
    msg = "جاري إعادة بدء عملية التسجيل..." if user_lang.startswith('ar') else "Restarting registration..."
    await update.message.reply_text(msg)
    return await register(update, context)

async def cancel_withdrawal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_lang = update.effective_user.language_code or 'en'
    await update.message.reply_text(
        "❌ تم إلغاء عملية السحب" if user_lang.startswith('ar') else "❌ Withdrawal cancelled",
        reply_markup=ReplyKeyboardRemove()
    )
    await show_menu(update, context)  # Add this line to show menu
    return ConversationHandler.END

async def send_educational_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send educational video to user"""
    try:
        user_lang = update.effective_user.language_code or 'en'
        user_id = update.effective_user.id
        
        if await is_banned(user_id):
            msg = "تم إلغاء وصولك 🚫 " if user_lang.startswith('ar') else "🚫 Your access has been revoked"
            await update.message.reply_text(msg)
            return

        # Get random video from database or folder
        video_path = get_random_video()  # Implement this function
        
        if not video_path or not os.path.exists(video_path):
            error_msg = "الفيديو غير متوفر حالياً ⚠️" if user_lang.startswith('ar') else "⚠️ Video not available"
            await update.message.reply_text(error_msg)
            return

        caption = "🎓 فيديو تعليمي" if user_lang.startswith('ar') else "🎓 Educational Video"
        await context.bot.send_video(
            chat_id=update.effective_chat.id,
            video=open(video_path, 'rb'),
            caption=caption,
            supports_streaming=True
        )

    except Exception as e:
        logger.error(f"Video sending error: {e}")
        error_msg = "تعذر إرسال الفيديو ⚠️" if user_lang.startswith('ar') else "⚠️ Couldn't send video"
        await update.message.reply_text(error_msg)
        
        
        
        
def get_random_video() -> str:
    """Get random video from videos folder"""
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
    
    
    
##########################
#    Main Application    #
##########################
def main() -> None:
    """Configure and start the bot"""
    application = ApplicationBuilder().token(config.TOKEN).build()

    # Conversation handler for registration
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('register', register),
            MessageHandler(filters.Regex(r'^📝 Register$'), register),
            MessageHandler(filters.Regex(r'^/register$'), register),
            MessageHandler(filters.Regex(r'^تسجيل الدخول 📝$'), register),
            # MessageHandler(
            #         filters.Regex(r'^(Educational video 📹|فيديو تعليمي 📹)$'),
            #         send_educational_video)
        ],
        states={
            EMAIL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_email),
                CommandHandler('cancel', cancel_registration),
                MessageHandler(filters.Regex(r'^(/start|/register)'), restart_registration),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_email)
            ],
            CODE_VERIFICATION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, verify_confirmation_code),
                # Cancel/retry handlers...
            ],
            CASH_NUMBER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_cash_number),
                CommandHandler('cancel', cancel_registration)
            ],
            PHONE: [
                MessageHandler(filters.CONTACT, process_phone),
                MessageHandler(filters.TEXT | filters.CONTACT, process_phone),
                CommandHandler('cancel', cancel_registration),
                MessageHandler(filters.Regex(r'^(/start|/register)'), restart_registration),
                MessageHandler(filters.ALL, lambda u,c: u.message.reply_text("❌ Please use contact button!"))
            ]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_registration),
            MessageHandler(filters.Regex(r'^(/start|/register)'), restart_registration)
        ],
        allow_reentry=True
    )

    withdrawal_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r'^💵 Withdraw$'), start_withdrawal),
            MessageHandler(filters.Regex(r'^سحب الأرباح 💵$'), start_withdrawal),
        ],
        states={
            WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_withdrawal_amount)],
            CARRIER_SELECTION: [
                CallbackQueryHandler(process_carrier_selection, pattern="^carrier_"),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_withdrawal),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_invalid_input)
            ],
            UPDATE_CASH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_cash_update),
                CommandHandler('cancel', cancel_withdrawal),
                MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_withdrawal)
            ]
            # CARRIER_SELECTION: [
            #     CallbackQueryHandler(process_carrier_selection, pattern=r"^carrier_"),
            #     # Add this line to handle text cancellation
            #     MessageHandler(filters.Regex(r'^(Cancel ❌|إلغاء ❌)$'), cancel_withdrawal)
            # ]
        },
        fallbacks=[CommandHandler('cancel', cancel_withdrawal)]
    )

    # Register handlers
    handlers = [
        CommandHandler('start', start),
        CommandHandler('menu', show_menu),
        CommandHandler('profile', profile_command),
        CommandHandler('viewlinks', view_links),
        conv_handler,
        MessageHandler(
                filters.Regex(r'^(Educational video 📹|فيديو تعليمي 📹)$'),
                send_educational_video),
        withdrawal_conv,  # Add this line
        CallbackQueryHandler(handle_submit_callback, pattern=r"^submit_\d+$"),
        CallbackQueryHandler(navigate_links, pattern=r"^(prev|next)_\d+$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands),
        MessageHandler(filters.PHOTO, process_image_upload)
    ]

    for handler in handlers:
        application.add_handler(handler)
    # application.add_handler(MessageHandler(
    #     filters.Regex(r'^(Educational video 📹|فيديو تعليمي 📹)$'),
    #     send_educational_video
    # ))
    application.add_handler(MessageHandler(filters.ALL, lambda u,c: None))  # Workaround
    application.add_error_handler(lambda u,c: error_handler(u,c))


    logger.info("Starting bot...")
    # Start bot
    application.run_polling(
        close_loop=False,
        stop_signals=(SIGINT, SIGTERM)
    )

if __name__ == '__main__':
    main()