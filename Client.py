import logging
import re
import sqlite3
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)
import os
import sys
from pathlib import Path
import psutil
from telegram.error import Conflict
import cachetools
from googleapiclient.errors import HttpError
from googleapiclient.discovery import build
import warnings
import psycopg2
from psycopg2 import pool
from psycopg2 import errors
import phonenumbers
from phonenumbers import geocoder

warnings.filterwarnings("ignore", category=UserWarning, module="googleapiclient.discovery_cache")
# ========== CONFIGURATION ==========admin
TELEGRAM_TOKEN = "7861338140:AAG3w1f7UBcwKpdYh0ipfLB3nMZM3sLasP4"
YOUTUBE_API_KEY = "AIzaSyCH0lUUlI-u1ziHsHiSl8aTC2J0nFU2l2Q"
ADMIN_TELEGRAM_ID = "6106281772"  # Get this from @userinfobot
DATABASE_NAME = "Test.db"

# Configure logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
(
    EMAIL, 
    PHONE, 
    FULLNAME, 
    COUNTRY, 
    CHANNEL_URL
) = range(5)

# ========== MENU SYSTEM ==========
# ========== UPDATED MENU SYSTEM ==========
MAIN_MENU = [
    ["📝 Register","Start"],
    ["🔍 Input Your YouTube URL Channel"],
    ["📋 My Profile"],  # Added new menu item
    ["📌 My Channels", "🗑 Delete Channel"]  # Added new menu item
]

ADMIN_MENU = [
    ["Start", "👑 Admin Panel"],
    ["🔍 Input Your YouTube URL Channel"],
    ["📋 My Profile"],  # Added new menu item
    ["📌 My Channels","🗑 Delete Channel"]  # Added for admin too
]


# Database configuration
POSTGRES_CONFIG = {
    "user": "postgres",
    "password": "postgres",
    "host": "localhost",
    "port": "5432",
    "database": "Test"
}

# Create connection pool
connection_pool = psycopg2.pool.SimpleConnectionPool(
    minconn=1,
    maxconn=1000,
    **POSTGRES_CONFIG
)

def get_conn():
    return connection_pool.getconn()

def put_conn(conn):
    connection_pool.putconn(conn)


def get_menu(user_id: int) -> ReplyKeyboardMarkup:
    """Return appropriate menu based on user status"""
    if str(user_id) == ADMIN_TELEGRAM_ID:
        return ReplyKeyboardMarkup(ADMIN_MENU, resize_keyboard=True)
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)

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
    if await is_banned(user.id):
        await update.message.reply_text("🚫 Your access has been revoked")
        return ConversationHandler.END
    menu = get_menu(user.id)
    # Auto-register admin if not in database
    if str(user.id) == ADMIN_TELEGRAM_ID and not await is_registered(user.id):
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
        await update.message.reply_text(f"👋 Welcome {user.first_name}!",
        reply_markup=menu)
        return
    
    await update.message.reply_text(
        f"👋 Welcome {user.first_name}!",
        reply_markup=menu
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process menu button selections"""
    text = update.message.text
    user = update.effective_user
    
    if text == "📝 Register":
        await handle_registration(update, context)
    elif text == "🔍 Input Your YouTube URL Channel":
        await handle_channel_verification(update, context)
    elif text == "👑 Admin Panel":
        await handle_admin_panel(update, context)
    elif text == "📌 My Channels":  # New handler
        await list_channels(update, context)
    elif text == "📋 My Profile":
        await profile_command(update, context)
    elif text == "🔙 Main Menu":
        await show_main_menu(update, user)
    elif text == "Start":
        await start(update, context)
    else:
        await update.message.reply_text("Please use the menu buttons")

async def show_main_menu(update: Update, user):
    """Display the appropriate main menu"""
    await update.message.reply_text(
        "Main Menu:",
        reply_markup=get_menu(user.id)
    )

async def handle_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start registration process"""
    user = update.effective_user
    if await is_banned(user.id):
        await update.message.reply_text("🚫 Your access has been revoked")
        return ConversationHandler.END
    if await is_registered(user.id):
        await update.message.reply_text("ℹ️ You're already registered!")
        return
    await update.message.reply_text("📝 Please enter your Google email address:")
    return EMAIL

# ========== YOUTUBE CHANNEL VERIFICATION ==========
async def process_channel_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process YouTube channel URL with duplicate validation and enhanced checks"""
    try:
        user = update.effective_user
        url = update.message.text.strip()
        
        # Validate URL format first
        if not re.match(r'^(https?://)?(www\.)?youtube\.com/', url, re.IGNORECASE):
            await update.message.reply_text("❌ Invalid YouTube URL format. Please try again.")
            return ConversationHandler.END

        # YouTube API initialization with cache
        class MemoryCache:
            def __init__(self):
                self._cache = {}
            def get(self, url):
                return self._cache.get(url)
            def set(self, url, content):
                self._cache[url] = content

        youtube = build(
            "youtube",
            "v3",
            developerKey=YOUTUBE_API_KEY,
            cache=MemoryCache(),
            cache_discovery=False
        )

        # Extract channel identifier
        patterns = [
            (r'/channel/([a-zA-Z0-9_-]{24})', 'id'),  # Channel ID
            (r'/c/([a-zA-Z0-9_-]+)', 'custom'),       # Custom URL
            (r'/user/([a-zA-Z0-9_-]+)', 'user'),       # Legacy username
            (r'/@([a-zA-Z0-9_-]+)', 'handle')          # Channel handle
        ]

        channel_id = None
        channel_name = None
        identifier_type = None

        for pattern, id_type in patterns:
            match = re.search(pattern, url)
            if match:
                identifier = match.group(1)
                identifier_type = id_type
                try:
                    if id_type == 'id':
                        response = youtube.channels().list(
                            part="snippet",
                            id=identifier
                        ).execute()
                    else:
                        response = youtube.search().list(
                            part="snippet",
                            q=identifier,
                            type="channel",
                            maxResults=1
                        ).execute()

                    if response.get('items'):
                        if id_type == 'id':
                            channel = response['items'][0]
                        else:
                            channel_id = response['items'][0]['id']['channelId']
                            channel = youtube.channels().list(
                                part="snippet",
                                id=channel_id
                            ).execute()['items'][0]

                        channel_id = channel['id']
                        channel_name = channel['snippet']['title']
                        channel_name = filter_non_arabic_words(channel_name,url)
                        break

                except HttpError as e:
                    logger.error(f"YouTube API Error: {str(e)}")
                    await update.message.reply_text("❌ Error verifying channel. Please try later.")
                    return ConversationHandler.END

        if not channel_id or not channel_name:
            await update.message.reply_text("❌ Could not verify YouTube channel. Check URL and try again.")
            return ConversationHandler.END

        # Database checks
        conn = get_conn()
        try:
            c = conn.cursor()
            # Check existing submissions
            c.execute("""
                SELECT channel_id, description 
                FROM links 
                WHERE added_by = %s 
                AND (channel_id = %s OR description = %s)
            """, (user.id, channel_id, channel_name))
            
            existing = c.fetchone()
            if existing:
                existing_id, existing_name = existing
                message = []
                if existing_id == channel_id:
                    message.append("⚠️ You already submitted this channel ID")
                if existing_name == channel_name:
                    message.append("⚠️ You already submitted a channel with this name")
                await update.message.reply_text("\n".join(message))
                return ConversationHandler.END


            try:
                cc = conn.cursor()
                cc.execute("""
                    SELECT fullname 
                    FROM clients 
                    WHERE telegram_id = %s
                """, (
                    user.id,
                ))
                exit = cc.fetchone()
                ex = exit[0]
            except Exception as e:
                logger.error(f"Channel processing error: {str(e)}")
                await update.message.reply_text("❌ An error occurred. Please try again.")
                

            #     exit_name = exit
            #     print(f"{exit_name}")
            #     return exit_name
            # Insert new submission
            # c.execute("""
            #     INSERT INTO likes 
            #     (adder, channel_id, channel_name, url, submission_date, adder)
            #     VALUES (%s, %s, %s, %s, %s, %s)
            # """, (
            #     user.id,
            #     channel_id,
            #     channel_name,
            #     url,
            #     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            #     ex,
            # ))
            c.execute("""
                INSERT INTO links 
                (added_by, youtube_link, description, channel_id, submission_date, adder) OVERRIDING SYSTEM VALUE
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                user.id,
                url,
                channel_name,
                channel_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ex,
            ))
            # c.execute("""
            #     SELECT id 
            #     FROM links 
            #     WHERE added_by = %s and youtube_link = %s and description = %s and channel_id = %s and submission_date = %s and adder = %s
            # """, (
            #     user.id,
            #     url,
            #     channel_name,
            #     channel_id,
            #     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            #     ex,
            # ))
            # exit = c.fetchone()
            # link_id_id = exit[0]
            # c.execute("""
            #     INSERT OR REPLACE INTO user_link_status (telegram_id, link_id, processed)
            #     VALUES (%s, %s, 1)
            # """, (
            #     user.id,
            #     link_id_id
            # ))
            conn.commit()
            await update.message.reply_text(
                f"✅ Channel registered successfully!\n\n"
                f"📛 Name: {channel_name}\n"
                f"🆔 ID: {channel_id}\n"
                f"🔗 URL: {url}"
            )
            
        finally:
            conn.close()

    except Exception as e:
        logger.error(f"Channel processing error: {str(e)}")
        await update.message.reply_text("❌ An error occurred. Please try again.")

    return ConversationHandler.END




def filter_non_arabic_words(text, url):    
    # Regex to match only English words and spaces
    english_re = re.compile(r'[a-zA-Z\s]+')
    
    # Find all matching English parts
    matches = english_re.findall(text)
    
    # Join them into a single string and remove extra spaces
    filtered_text = ' '.join(''.join(matches).split())

    # If no English words are found, check for @ in URL
    if not filtered_text.strip():
        at_match = re.search(r'@([a-zA-Z0-9_]+)', url)
        if at_match:
            return at_match.group(1)  # Return the part after @
        else:
            return text  # Return the original text if no @ is found

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
async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all submitted channels for the current user with likes count"""
    user = update.effective_user
    
    # Check if user is banned
    if await is_banned(user.id):
        await update.message.reply_text("🚫 Your access has been revoked")
        return ConversationHandler.END
        
    try:
        # Check if user is registered
        if not await is_registered(user.id):
            await update.message.reply_text("❌ Please Register First.")
            return
            
        conn = get_conn()
        c = conn.cursor()
        
        # Get channels with likes count FOR CURRENT USER ONLY
        c.execute("""
            SELECT l.description, l.youtube_link, l.channel_id, l.submission_date,
                   COALESCE(k.channel_likes, 0) AS likes_count
            FROM links l
            LEFT JOIN likes k ON l.id = k.id
            WHERE l.added_by = %s
            ORDER BY l.submission_date DESC
        """, (user.id,))  # Make sure user.id is correctly passed
        
        channels = c.fetchall()
        conn.close()
        
        if not channels:
            await update.message.reply_text("📭 You haven't submitted any channels yet")
            return
            
        response = ["📋 Your Submitted Channels:"]
        for idx, (name, url, channel_id, date, likes) in enumerate(channels, 1):
            response.append(
                f"{idx}. {name}\n"
                f"🔗 {url}\n"
                f"🆔 Channel ID: {channel_id}\n"
                f"📅 Submitted: {date}\n"
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
        logger.error(f"List channels error: {str(e)}")
        await update.message.reply_text("❌ Error retrieving your channels")
        

# ========== REGISTRATION FLOW HANDLERS ==========
async def email_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and store email"""
    email = update.message.text
    if not re.match(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$", email):
        await update.message.reply_text("❌ Invalid email format. Try again:")
        return EMAIL
    context.user_data["email"] = email
    # Create contact sharing keyboard
    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share Phone Number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await update.message.reply_text(
        "Please share your phone number using the button below:",
        reply_markup=contact_keyboard
    )
    return PHONE

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle received contact information and determine country automatically"""
    contact = update.message.contact
    # datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Verify the contact belongs to the user
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text("❌ Please share your own phone number!")
        return PHONE
    
    phone_number = "+" + contact.phone_number
    # print(f"{phone_number}")
    context.user_data["phone"] = phone_number
    
    try:
        # Validate international format
        if not phone_number.startswith("+"):
            raise ValueError("Phone number must include country code (e.g., +123456789)")
            
        # Parse phone number to determine country
        parsed_number = phonenumbers.parse(phone_number, None)
        country_name = geocoder.description_for_number(parsed_number, "en")
        country_name = country_name if country_name else "Unknown"
        
    except (phonenumbers.NumberParseException, ValueError) as e:
        logger.error(f"Phone number error: {e}")
        await update.message.reply_text(
            "❌ Invalid phone number format. Please share your contact using the button below "
            "and ensure it includes your country code (e.g., +123456789).",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("📱 Share Phone Number", request_contact=True)]],
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
        await update.message.reply_text(
            # "✅ Registration complete!\n\n"
            f"✅ Registration Complete:\n"
            f"👤 Name: {escape_markdown(fullname)}\n"
            f"📧 Email: {escape_markdown_2(str(email))}\n"
            f"📱 Phone: {escape_markdown_2(phone_number)}\n"
            f"🌍 Country: {escape_markdown(country_name)}\n"
            f"⭐ Registration Date: {escape_markdown_2(str(registration_date))}",
            reply_markup=get_menu(update.effective_user.id)
        )
        # Show main menu after registration       
    except Exception as e:
        logger.error(f"Database error: {str(e)}")
        await update.message.reply_text("❌ Registration failed. Please try again.")
        return ConversationHandler.END
    finally:
        conn.close()
    
    return ConversationHandler.END

async def handle_invalid_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle non-contact input in phone number stage"""
    contact_keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Share Phone Number", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await update.message.reply_text(
        "❌ Please use the button below to share your phone number.",
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
#             reply_markup=get_menu(update.effective_user.id)
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
    if await is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Your access has been revoked")
        return ConversationHandler.END
    if not await is_registered(update.effective_user.id):
        await update.message.reply_text("❌ Please Register First.")
        return ConversationHandler.END
    
    await update.message.reply_text("🔗 Please send your YouTube channel URL:")
    return CHANNEL_URL

async def handle_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel access control"""
    user = update.effective_user
    if str(user.id) != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("🚫 Access denied!")
        return
    
    keyboard = [
        # ["📊 User Statistics", "📢 Broadcast Message"],
        
        ["🚫 Ban Client", "✅ UnBan Client"],
        ["🚫 Ban User", "✅ UnBan User"],
        ["🗑 Delete Channel", "🗑 Delete  All Channels"], # Updated buttons
        ["🔙 Main Menu"]
    ]
    await update.message.reply_text(
        "👑 Admin Panel:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# ========== IMPROVED ERROR HANDLING ==========
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle PostgreSQL errors"""
    logger.error("Exception:", exc_info=context.error)
    
    if isinstance(context.error, errors.UniqueViolation):
        await update.message.reply_text("❌ This entry already exists!")
    elif isinstance(context.error, errors.ForeignKeyViolation):
        await update.message.reply_text("❌ Invalid reference!")
    else:
        await update.message.reply_text("⚠️ An error occurred. Please try again.")
        
# ========== ADMIN DELETE CHANNELS ==========
async def delete_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only channel deletion flow"""
    if await is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 Your access has been revoked")
        return ConversationHandler.END
    if not await is_registered(update.effective_user.id):
        await update.message.reply_text("❌ Please Register First.")
        return ConversationHandler.END
    # user = update.effective_user
    # if str(user.id) != ADMIN_TELEGRAM_ID:
    #     await update.message.reply_text("🚫 Access denied!")
    #     return ConversationHandler.END

    await update.message.reply_text("Enter Channel URL to delete:")
    return "AWAIT_CHANNEL_URL"


async def confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm and delete channel"""
    url = update.message.text.strip()
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT description FROM links WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        result = c.fetchone()
        
        if not result:
            await update.message.reply_text("❌ Channel not found")
            return ConversationHandler.END

        channel_name = result[0]
        c.execute("SELECT id FROM links WHERE youtube_link = %s and added_by = %s", (url, update.effective_user.id,))
        result_id = c.fetchone()
        if not result_id:
            await update.message.reply_text("❌ Channel not found")
            return ConversationHandler.END
        result_id_for_link = result_id[0]
        c.execute("DELETE FROM links WHERE youtube_link = %s and added_by = %s", (url,update.effective_user.id,))
        c.execute("DELETE FROM user_link_status WHERE link_id = %s", (result_id_for_link,))
        c.execute("DELETE FROM likes WHERE url = %s and user_id = %s", (url,update.effective_user.id,))
        conn.commit()
        await update.message.reply_text(
            f"✅ Channel deleted:\n"
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
    if str(user.id) != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("🚫 Access denied!")
        return ConversationHandler.END

    await update.message.reply_text("Enter Channel URL to delete:")
    return "AWAIT_CHANNEL_URL_ADMIN"

# Step 2: Receive the Channel URL and prompt for the adder
async def receive_channel_url_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store the channel URL and prompt for the adder."""
    # Save the channel URL in user_data for later retrieval
    context.user_data["channel_url"] = update.message.text.strip()
    await update.message.reply_text("And enter the 'adder' (the user who added the channel):")
    return "AWAIT_ADDER"

# Step 3: Receive the adder and confirm deletion
async def confirm_delete_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Confirm deletion using the stored channel URL and the provided adder."""
    adder = update.message.text.strip()  # Now this is the admin's input text, not a Message object
    url = context.user_data.get("channel_url")
    
    if not url:
        await update.message.reply_text("Channel URL not found. Aborting deletion.")
        return ConversationHandler.END

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT description FROM links WHERE youtube_link = %s and adder = %s", (url, adder))
        result = c.fetchone()
        if not result:
            await update.message.reply_text("❌ Channel not found")
            return ConversationHandler.END
            
        channel_name = result[0]
        c.execute("SELECT id FROM links WHERE youtube_link = %s and adder = %s", (url, adder,))
        result_id = c.fetchone()
        if not result_id:
            await update.message.reply_text("❌ Channel not found")
            return ConversationHandler.END
        result_id_for_link = result_id[0]
        c.execute("DELETE FROM user_link_status WHERE link_id = %s", (result_id_for_link,))
        c.execute("DELETE FROM likes WHERE url = %s and adder = %s", (url,adder,))
        c.execute("DELETE FROM links WHERE youtube_link = %s and adder = %s", (url, adder))
        conn.commit()
        await update.message.reply_text(
            f"✅ Channel deleted:\n"
            f"📛 Name: {channel_name}\n"
            f"🔗 URL: {url}"
        )
    finally:
        conn.close()

    return ConversationHandler.END
async def unban_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if str(admin.id) != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("🚫 Access denied!")
        return

    if context.args:
        target_fullname = " ".join(context.args).strip()
    else:
        await update.message.reply_text("Usage: /unbanclient <full name>")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
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
                    await context.bot.send_message(
                        chat_id=user_data[0],
                        text="✅ Your access to this bot has been restored"
                    )
                except Exception as e:
                    logger.error(f"Unban notification failed: {str(e)}")
        else:
            await update.message.reply_text("❌ Client Already restored")
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
    except sqlite3.Error as e:
        logger.error(f"Ban check failed: {str(e)}")
        return False
    finally:
        conn.close()
                
# ========== Ban Client FUNCTIONALITY ==========
async def ban_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ban a user from using the bot"""
    admin = update.effective_user
    if str(admin.id) != ADMIN_TELEGRAM_ID:
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
            await update.message.reply_text("Usage: /banclient <fullname> or reply to Clients's message")
            return
    else:
        await update.message.reply_text("Usage: /banclient <fullname> or reply to Clients's message")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
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
                await context.bot.send_message(
                    chat_id=user_data[0],
                    text="🚫 Your access to this bot has been revoked"
                )
        else:
            await update.message.reply_text("❌ Client Already revoked")
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
        if await is_banned(user_id):
            await update.message.reply_text("🚫 Your access has been revoked")
            return ConversationHandler.END
        profile = get_profile(user_id)
        if profile:
            fullname, email, phone, country, registration_date = profile
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
            await update.message.reply_text("❌ You're not registered! Register First")
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
    admin = update.effective_user
    if str(admin.id) != ADMIN_TELEGRAM_ID:
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
            await update.message.reply_text("Usage: /banuser <fullname> or reply to Users's message")
            return
    else:
        await update.message.reply_text("Usage: /banuser <fullname> or reply to Users's message")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        # Ban Client
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
            
            # Notify banned user if possible
            c.execute("""
                SELECT telegram_id FROM users
                WHERE full_name = %s
            """, (target,))
            user_data = c.fetchone()
            if user_data and user_data[0]:
                await context.bot.send_message(
                    chat_id=user_data[0],
                    text="🚫 Your access to this bot has been revoked"
                )
        else:
            await update.message.reply_text("❌ User Already revoked")
            return
    except Exception as e:
        logger.error(f"Ban notification failed: {str(e)}")
            
    finally:
        conn.close()

async def unban_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if str(admin.id) != ADMIN_TELEGRAM_ID:
        await update.message.reply_text("🚫 Access denied!")
        return

    if context.args:
        target_fullname = " ".join(context.args).strip()
    else:
        await update.message.reply_text("Usage: /unbanuser <full name>")
        return

    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT telegram_id FROM users
            WHERE is_banned = True And full_name = %s 
        """, (target_fullname,))
        check = c.fetchone()
        if check:
            c = conn.cursor()
            c.execute("""
                UPDATE users 
                SET is_banned = False 
                WHERE full_name = %s
            """, (target_fullname,))
            
            if c.rowcount == 0:
                await update.message.reply_text("❌ User not found in database")
                return
                
            conn.commit()
            await update.message.reply_text(f"✅ User '{target_fullname}' has been unbanned")
            
            c.execute("""
                SELECT telegram_id FROM users
                WHERE full_name = %s
            """, (target_fullname,))
            user_data = c.fetchone()
            if user_data and user_data[0]:
                try:
                    await context.bot.send_message(
                        chat_id=user_data[0],
                        text="✅ Your access to this bot has been restored"
                    )
                except Exception as e:
                    logger.error(f"Unban notification failed: {str(e)}")
        else:
            await update.message.reply_text("❌ User Already restored")
            return
    finally:
        conn.close()
                
        
def main() -> None:
    """Configure and start the bot with comprehensive error handling"""
    pid_file = Path("bot.pid")
    logger = logging.getLogger(__name__)

    try:
        # ========== PID FILE HANDLING ==========
        # Check for existing instances with validation
        if pid_file.exists():
            try:
                content = pid_file.read_text().strip()
                if not content:
                    raise ValueError("Empty PID file")
                
                old_pid = int(content)
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
        application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # ========== HANDLER CONFIGURATION ==========
        # Admin conversation handler
        admin_conv = ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex(r"^🗑 Delete Channel"), delete_channel),
                MessageHandler(filters.Regex(r"^📋 My Profile$"), profile_command),
                MessageHandler(filters.Regex(r"^🗑 Delete  All Channels$"), delete_channel_admin),
                MessageHandler(filters.Regex(r"^🚫 Ban Client$"), ban_client),
                MessageHandler(filters.Regex(r"^✅ UnBan Client$"), unban_client),
                MessageHandler(filters.Regex(r"^🚫 Ban User$"), ban_user),
                MessageHandler(filters.Regex(r"^✅ UnBan User$"), unban_user)
            ],
            states={
                "AWAIT_CHANNEL_URL": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete)],
                "AWAIT_CHANNEL_URL_ADMIN": [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_channel_url_admin)],
                "AWAIT_ADDER": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete_admin)],
            },
            fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
            map_to_parent={ConversationHandler.END: ConversationHandler.END}
        )

        # Main conversation handler
        conv_handler = ConversationHandler(
            entry_points=[
                MessageHandler(filters.Regex(r"^📝 Register$"), handle_registration),
                MessageHandler(filters.Regex(r"^📋 My Profile$"), profile_command),
                MessageHandler(filters.Regex(r"^🔍 Input Your YouTube URL Channel$"), handle_channel_verification),
                MessageHandler(filters.Regex(r"^🗑 Delete Channel$"), delete_channel),
            ],
            states={
                EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, email_handler)],
                PHONE: [
                    MessageHandler(filters.CONTACT, phone_handler),
                    MessageHandler(filters.ALL & ~filters.COMMAND, handle_invalid_contact)
                ],
                # FULLNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_handler)],
                # COUNTRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, country_handler)],
                CHANNEL_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_channel_url)],
                "AWAIT_CHANNEL_URL": [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_delete)],
            },
            fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)]
        )

        # ========== HANDLER REGISTRATION ==========
        handlers = [
            CommandHandler("start", start),
            CommandHandler('profile', profile_command),
            conv_handler,
            admin_conv,
            MessageHandler(filters.Regex(r"^👑 Admin Panel$"), handle_admin_panel),
            MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler),
            CommandHandler("banclient", ban_client),
            CommandHandler("unbanclient", unban_client),
            CommandHandler("banuser", ban_user),
            CommandHandler("unbanuser", unban_user),
            CommandHandler("mychannels", list_channels)
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
                logger.error(f"Ban check failed: {str(e)}")
                return False
            finally:
                conn.close()

        def wrap_handler(handler):
            """Safe handler wrapper with ban checking"""
            if not hasattr(handler, 'callback'):
                return handler
                
            original_callback = handler.callback
            async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
                try:
                    # Allow /start command and Start button even if banned
                    if update.message and update.message.text:
                        text = update.message.text.strip()
                        if text in ("/start", "Start"):
                            return await original_callback(update, context)
                    
                    # Check ban status for all other interactions
                    user = update.effective_user
                    if user and await is_banned(user.id):
                        await update.message.reply_text("🚫 Your access has been revoked")
                        return ConversationHandler.END
                        
                    return await original_callback(update, context)
                except Exception as e:
                    logger.error(f"Handler error: {str(e)}")
                    return ConversationHandler.END

            handler.callback = wrapped
            return handler

        # Apply ban checks to all handlers
        wrapped_handlers = [wrap_handler(h) for h in handlers]
        application.add_handlers(wrapped_handlers)

        # ========== ERROR HANDLING ==========
        application.add_error_handler(error_handler)

        # ========== BOT STARTUP ==========
        logger.info("Starting bot...")
        application.run_polling(
            poll_interval=2,
            timeout=30,
            drop_pending_updates=True
        )

    except Conflict as e:
        logger.critical(f"Bot conflict: {str(e)}")
        print("""
        🔌 Connection conflict detected!
        Possible solutions:
        1. Wait 10 seconds before restarting
        2. Check for other running instances
        3. Verify your bot token is unique
        """)
    except Exception as e:
        logger.critical(f"Fatal error: {str(e)}", exc_info=True)
    finally:
        # ========== CLEANUP ==========
        try:
            pid_file.unlink(missing_ok=True)
            logger.info("Cleanup completed")
        except Exception as e:
            logger.error(f"Cleanup failed: {str(e)}")

        # Ensure database connections are closed
        sqlite3.connect(DATABASE_NAME).close()

if __name__ == "__main__":
    main()