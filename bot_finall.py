import os
import re
from signal import SIGINT, SIGTERM
import logging
from datetime import datetime
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
import ocr_processor
import scan_image3
import image_processing
import config
import phonenumbers
from phonenumbers import geocoder

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    filename='bot_errors.log'
)
logger = logging.getLogger(__name__)

# Global dictionaries for state management
pending_submissions = {}
user_pages = {}

# Conversation states
EMAIL, PHONE = range(2)

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

def get_profile(telegram_id: int) -> tuple:
    """Retrieve user profile data"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT telegram_id, full_name, email, phone, country, registration_date, points FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                return cursor.fetchone()
    except Exception as e:
        logger.error(f"Error in get_profile: {e}")
        return None

def store_message_id(link_id: int, message_id: int) -> None:
    """Store Telegram message ID for a link"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "UPDATE links SET telegram_message_id = %s WHERE id = %s",
                    (message_id, link_id)
                )
                conn.commit()
    except Exception as e:
        logger.error(f"Error storing message ID: {e}")

def get_message_id(link_id: int) -> int:
    """Get stored Telegram message ID for a link"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT telegram_message_id FROM links WHERE id = %s",
                    (link_id,)
                )
                result = cursor.fetchone()
                return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting message ID: {e}")
        return None

def get_allowed_links(telegram_id: int) -> list:
    """Retrieve links available for the user"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                query = """
                    SELECT l.id, l.youtube_link, l.description, l.adder
                    FROM links l
                    LEFT JOIN user_link_status uls 
                        ON l.id = uls.link_id AND uls.telegram_id = %s
                    WHERE uls.processed IS NULL OR uls.processed = 0
                    ORDER BY l.id DESC
                """
                cursor.execute(query, (telegram_id,))
                return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error in get_allowed_links: {e}")
        return []

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
    
    
def mark_link_processed(telegram_id: int, link_id: int) -> None:
    """Mark a link as processed for the user"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO user_link_status (telegram_id, link_id, processed)
                    VALUES (%s, %s, 1)
                    ON CONFLICT (telegram_id, link_id) 
                    DO UPDATE SET processed = EXCLUDED.processed
                """, (telegram_id, link_id))
                conn.commit()
    except Exception as e:
        logger.error(f"Error in mark_link_processed: {e}")
        conn.rollback()

def update_user_points(telegram_id: int, points: int = 1) -> None:
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
        
        
def update_likes(link_id: int, points: int = 1) -> None:
    """Update user's points balance"""
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE likes SET channel_likes = channel_likes + %s
            WHERE id = %s
            """, (1,link_id))
            conn.commit()
    except Exception as e:
        logger.error(f"Error in update_likes: {e}")
        conn.rollback()
        
        
##########################
#    Command Handlers    #
##########################
async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display main menu keyboard"""
    try:
        keyboard = [
            ["👋 Start", "📝 Register"],
            ["📋 Profile", "🔍 View Links"]
        ]
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("Choose a command:", reply_markup=reply_markup)
    except Exception as e:
        logger.error(f"Error in show_menu: {e}")
        await update.message.reply_text("⚠️ Couldn't display menu. Please try again.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    try:
        user_id = update.effective_user.id
        if await is_banned(user_id):
            await update.message.reply_text("🚫 Your access has been revoked")
            return ConversationHandler.END
        if user_exists(user_id):
            if user_id in config.ADMIN_IDS:
                await update.message.reply_text("Welcome back Admin! 🛡️")
            else:
                await update.message.reply_text("Welcome back! 🎉")
            await show_menu(update, context)
        else:
            await update.message.reply_text("Welcome! Please Register First")
            await show_menu(update, context)
    except Exception as e:
        logger.error(f"Error in start: {e}")
        await update.message.reply_text("⚠️ Couldn't process your request. Please try again.")

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start registration process"""
    try:
        user_id = update.effective_user.id
        if await is_banned(user_id):
            await update.message.reply_text("🚫 Your access has been revoked")
            return ConversationHandler.END
        if user_exists(user_id):
            await update.message.reply_text("You're already registered! ✅")
            return ConversationHandler.END
        
        await update.message.reply_text("Please enter your email address:")
        return EMAIL
    except Exception as e:
        logger.error(f"Error in register: {e}")
        await update.message.reply_text("⚠️ Couldn't start registration. Please try again.")
        return ConversationHandler.END
    
async def process_email(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process email input and request phone number"""
    try:
        email = update.message.text.strip()
        if not email or '@' not in email or '.' not in email or len(email) > 100:
            raise ValueError("Invalid email format")
            
        context.user_data['email'] = email
        
        contact_keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton("📱 Share Phone Number", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        
        await update.message.reply_text(
            "Please share your phone number:",
            reply_markup=contact_keyboard
        )
        return PHONE
    except Exception as e:
        logger.warning(f"Invalid email input: {e}")
        await update.message.reply_text("❌ Invalid email format! Please enter a valid email.")
        return EMAIL

async def process_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process contact information and complete registration"""
    try:
        user = update.effective_user
        contact = update.message.contact
        
        if contact.user_id != user.id:
            await update.message.reply_text("❌ Please share your own phone number!")
            return PHONE

        phone_number = "+" + contact.phone_number
        full_name = user.name
        
        try:
            parsed_number = phonenumbers.parse(phone_number, None)
            country = geocoder.description_for_number(parsed_number, "en") or "Unknown"
        except phonenumbers.NumberParseException:
            country = "Unknown"

        email = context.user_data.get('email')
        registration_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO users (telegram_id, full_name, email, phone, country, registration_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (user.id, full_name, email, phone_number, country, registration_date)
                )
                conn.commit()

        await update.message.reply_text(
            f"✅ Registration Complete:\n"
            f"👤 Name: {escape_markdown(full_name)}\n"
            f"📧 Email: {escape_markdown_2(email)}\n"
            f"📱 Phone: {escape_markdown_2(phone_number)}\n"
            f"🌍 Country: {escape_markdown(country)}\n"
            f"⭐ Registration Date: {escape_markdown(registration_date)}",
            reply_markup=ReplyKeyboardRemove()
        )
        await show_menu(update, context)
        
    except psycopg2.IntegrityError:
        await update.message.reply_text("⚠️ You're already registered!")
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await update.message.reply_text("⚠️ Registration failed. Please try again.")
    finally:
        context.user_data.clear()
    return ConversationHandler.END

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display user profile"""
    try:
        user_id = update.effective_user.id
        if await is_banned(user_id):
            await update.message.reply_text("🚫 Your access has been revoked")
            return
        profile = get_profile(user_id)
        if profile:
            _, name, email, phone, country, reg_date, points = profile
            response = (
                f"📋 *Profile Information*\n"
                f"👤 Name: {escape_markdown(name)}\n"
                f"📧 Email: {escape_markdown(email)}\n"
                f"📱 Phone: {escape_markdown(phone)}\n"
                f"🌍 Country: {escape_markdown(country)}\n"
                f"⭐ Registration Date: {escape_markdown(reg_date)}\n"
                f"🏆 Points: {points}"
            )
            await update.message.reply_text(response, parse_mode="MarkdownV2")
        else:
            await update.message.reply_text("❌ You're not registered! Register First")
    except Exception as e:
        logger.error(f"Profile error: {e}")
        await update.message.reply_text("⚠️ Couldn't load profile. Please try again.")

async def view_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display available links"""
    try:
        user_id = update.effective_user.id
        if await is_banned(user_id):
            await update.message.reply_text("🚫 Your access has been revoked")
            return
        if not user_exists(user_id):
            await update.message.reply_text("❌ Please register first!")
            return

        user_pages[user_id] = 0
        await send_links_page(update.effective_chat.id, user_id, 0, context)
    except Exception as e:
        logger.error(f"View links error: {e}")
        await update.message.reply_text("⚠️ Couldn't load links. Please try again.")

##########################
#    Link Management     #
##########################
async def send_links_page(chat_id: int, user_id: int, page: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send paginated links with message ID tracking"""
    try:
        links, total_pages = get_paginated_links(user_id, page)
        
        if not links:
            await context.bot.send_message(chat_id, "🎉 No more links available!")
            return

        for link in links:
            link_id, yt_link, desc, adder = link
            text = (
                f"📛 {escape_markdown(desc)}\n"
                f"👤 *By:* {escape_markdown(adder)}\n"
                f"[🔗 YouTube Link]({yt_link})"
            )
            keyboard = [[InlineKeyboardButton("📸 Submit Image", callback_data=f"submit_{link_id}")]]

            # await context.bot.send_message(
            #     chat_id,
            #     text,
            #     reply_markup=InlineKeyboardMarkup(keyboard),
            #     parse_mode="MarkdownV2"
            # )
            
            
            message = await context.bot.send_message(
                chat_id,
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2"
            )
            store_message_id(link_id, message.message_id)

        if total_pages > 1:
            buttons = []
            if page > 0:
                buttons.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"prev_{page-1}"))
            if page < total_pages - 1:
                buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"next_{page+1}"))
            
            if buttons:
                await context.bot.send_message(
                    chat_id,
                    "Navigate between pages:",
                    reply_markup=InlineKeyboardMarkup([buttons])
                )
                
    except Exception as e:
        logger.error(f"Error sending links: {e}")
        await context.bot.send_message(chat_id, "⚠️ Couldn't load links. Please try later.")

async def handle_text_commands(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle menu text commands"""
    try:
        text = update.message.text
        if text == "👋 Start":
            await start(update, context)
        elif text == "📝 Register":
            await update.message.reply_text("Starting registration...")
            await register(update, context)
        elif text == "📋 Profile":
            await profile_command(update, context)
        elif text == "🔍 View Links":
            await view_links(update, context)
        else:
            await update.message.reply_text("❌ Unknown command. Please use the menu buttons.")
    except Exception as e:
        logger.error(f"Text command error: {e}")
        await update.message.reply_text("⚠️ Couldn't process command. Please try again.")


##########################
#    Image Submission    #
##########################
# async def navigate_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
#     """Handle pagination navigation for links list"""
#     try:
#         query = update.callback_query
#         await query.answer()
#         user_id = query.from_user.id
#         action, page_str = query.data.split('_')
#         new_page = int(page_str)
#         # Update user's current page
#         user_pages[user_id] = new_page
#       # Send updated page
#         await send_links_page(query.message.chat_id, user_id, new_page, context)
#         await query.message.delete()
#     except Exception as e:
#         logger.error(f"Pagination error: {e}")
#         if 'query' in locals():
#             await query.message.reply_text("⚠️ Couldn't load page. Please try again.")

def process_text(req_text):
    # Extract all words (alphanumeric + underscores)
    words = re.findall(r'\b\w+\b', req_text)
    
    # Case 1: No words found
    if not words:
        return "No valid words found"
    
    # Case 2: Only one word
    if len(words) == 1:
        return words[0]
    
    # Case 3: Two or more words
    return words[0] + " " + words[1]

async def process_image_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image verification with threaded replies"""
    try:
        user_id = update.effective_user.id
        if user_id not in pending_submissions:
            await update.message.reply_text("❌ No active submission!")
            return

        link_id, req_text, msg_id = pending_submissions[user_id]
        # print(f"{req_text} and {link_id}")
        # cleaned_text = re.sub(r'[^\w\s]', '', req_text)
        # words = re.findall(r'\b\w+\b', req_text)
        text_finall = process_text(req_text)
        photo_file = await update.message.photo[-1].get_file()
        image_path = f"temp_{user_id}_{link_id}.jpg"
        
        await photo_file.download_to_drive(image_path)
        processing_msg = await update.message.reply_text(
            "🔍 Verifying...",
            reply_to_message_id=msg_id
        )

        verification_passed = False
        try:
            if scan_image3.check_text_in_image(image_path, text_finall):
                verification_passed = True
        except Exception as e:
            logger.error(f"Image processing error: {e}")
        teeeest = scan_image3.check_text_in_image(image_path, text_finall)
        # print(f"teeeest{teeeest}")
        # print(f"verification_passed: {verification_passed}")
        if verification_passed:
            mark_link_processed(user_id, link_id)
            update_user_points(user_id)
            update_likes(link_id)
            await update.message.reply_text(
                "✅ Verification successful! +1 point",
                reply_to_message_id=msg_id
            )
        else:
            await update.message.reply_text(
                "❌ Verification failed. Try again.",
                reply_to_message_id=msg_id
            )

        await context.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=processing_msg.message_id
        )

    except Exception as e:
        logger.error(f"Image error: {e}")
        await update.message.reply_text("⚠️ Processing error. Please try again.")
    finally:
        if 'image_path' in locals() and os.path.exists(image_path):
            os.remove(image_path)
        pending_submissions.pop(user_id, None)

##########################
#    Error Handling      #
##########################
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global error handler for all uncaught exceptions"""
    try:
        logger.error("Unhandled exception:", exc_info=context.error)
        
        if update is not None and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ An unexpected error occurred. Please try again later."
            )
    except Exception as e:
        logger.error(f"Error in error handler: {e}")
##########################
#    Pagination Handler  #
##########################
async def navigate_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle pagination navigation for links list"""
    try:
        query = update.callback_query
        await query.answer()
        
        user_id = query.from_user.id
        action, page_str = query.data.split('_')
        new_page = int(page_str)
        
        # Update user's current page
        user_pages[user_id] = new_page
        
        # Send updated page
        await send_links_page(query.message.chat_id, user_id, new_page, context)
        await query.message.delete()
        
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        if 'query' in locals():
            await query.message.reply_text("⚠️ Couldn't load page. Please try again.")
            
            
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

async def handle_submit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image submission requests with message threading"""
    try:
        query = update.callback_query
        await query.answer()
        user_id = query.from_user.id
        allowed_links = get_allowed_links(user_id)
        if not allowed_links:
            await query.message.reply_text("⚠️ You have no available links.")
            return   
        link_id = int(query.data.split("_")[1])
        message_id = get_message_id(link_id)
        # Verify the link is in the allowed list
        if not any(link[0] == link_id for link in allowed_links):
            await query.message.reply_text("⚠️ This link is no longer available for submission.")
            return
        description = get_link_description(link_id)
        if not message_id or not description:
            await query.message.reply_text("❌ Link details missing")
            return
        pending_submissions[user_id] = (link_id, description, message_id)
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📸 Submit image for: {description}",
            reply_to_message_id=message_id
        )
    except Exception as e:
        logger.error(f"Submit error: {e}")
        await query.message.reply_text("⚠️ Couldn't process request. Please try again.")
        
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
            
        ],
        states={
            EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_email)],
            PHONE: [
                MessageHandler(filters.CONTACT, process_phone),
                MessageHandler(filters.ALL, lambda u,c: u.message.reply_text("❌ Please use contact button!"))
            ]
        },
        fallbacks=[CommandHandler('cancel', lambda u,c: ConversationHandler.END)],
    )

    # Register handlers
    handlers = [
        CommandHandler('start', start),
        CommandHandler('menu', show_menu),
        CommandHandler('profile', profile_command),
        CommandHandler('viewlinks', view_links),
        conv_handler,
        CallbackQueryHandler(handle_submit_callback, pattern=r"^submit_\d+$"),
        CallbackQueryHandler(lambda u,c: navigate_links(u,c), pattern=r"^(prev|next)_\d+$"),
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_commands),
        MessageHandler(filters.PHOTO, process_image_upload)
    ]

    for handler in handlers:
        application.add_handler(handler)
    application.add_handler(MessageHandler(filters.ALL, lambda u,c: None))  # Workaround
    application.add_error_handler(lambda u,c: error_handler(u,c))

    # Start bot
    application.run_polling(
        close_loop=False,
        stop_signals=(SIGINT, SIGTERM)
    )
if __name__ == '__main__':
    main()