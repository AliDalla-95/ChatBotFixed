""" ali """
# Standard Library Imports
import os
import sqlite3
# Third-Party Imports
import telebot
import database
from telebot import types
from telebot.types import InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton
import ocr_processor
import image_processing
import logging
import config
import psycopg2

# Initialize bot with your token
bot = telebot.TeleBot(config.TOKEN)

# Global dictionaries for pending submissions and pagination
pending_submissions = {}
user_pages = {}
pending_registrations = {}


def connect_db():
    """Returns a connection to the PostgreSQL database."""
    return psycopg2.connect(config.DATABASE_URL)

##########################
#      SHOW MENU         #
##########################
@bot.message_handler(commands=['menu'])
def show_menu(message):
    # Create a custom keyboard with friendly text labels and emoji
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_start = types.KeyboardButton("👋 Start")
    btn_register = types.KeyboardButton("📝 Register")
    btn_profile = types.KeyboardButton("📋 Profile")
    btn_viewlinks = types.KeyboardButton("🔍 View Links")
    
    # Add buttons to the keyboard
    markup.add(btn_start, btn_register, btn_profile, btn_viewlinks)
    
    # Send a message with the custom keyboard
    bot.send_message(message.chat.id, "Choose a command:", reply_markup=markup)

##########################
#  COMMAND HANDLERS      #
##########################
@bot.message_handler(commands=['start'])
def start(message):
    """Handle /start command."""
    if user_exists(message.from_user.id):
        if message.from_user.id in config.ADMIN_IDS:
            bot.reply_to(message, "Welcome, Admin!\nUse Menu to see all commands.")
        elif is_authorized_link_adder(message.from_user.id):
            bot.reply_to(message, "Welcome, friend!\nUse Menu to see all commands.")
        else:
            bot.reply_to(message, "Welcome! \nUse Menu to see available commands.")
    elif not user_exists(message.from_user.id):
        # For new users, set commands and prompt registration.
        user_commands = [
            types.BotCommand("start", "Start interacting with the bot"),
            types.BotCommand("register", "Register your account"),
        ]
        bot.set_my_commands(user_commands, scope=types.BotCommandScopeChat(message.chat.id))
        bot.reply_to(message, "Welcome to the YouTube Points Bot!\nUse Menu to start.")
    else:
        register(message)
    show_menu(message)
@bot.message_handler(commands=['register'])
def register(message):
    """Handle /register command."""
    try:
        if user_exists(message.from_user.id):
            bot.reply_to(message, "You are already registered!")
        else:
            msg = bot.send_message(message.chat.id, "Please enter your email:")
            bot.register_next_step_handler(msg, process_email)
    except Exception as e:
        bot.reply_to(message, f"An error occurred: {e}")

def process_email(message):
    """Process email input and ask for full name."""
    try:
        email = message.text.strip()
        
        # Simple email validation
        if '@' not in email or '.' not in email:
            raise ValueError("Invalid email format")
            
        # Store email temporarily
        pending_registrations[message.from_user.id] = {'email': email}
        
        # Ask for full name
        msg = bot.send_message(message.chat.id, "Please enter your full name:")
        bot.register_next_step_handler(msg, process_full_name)
        
    except Exception as e:
        bot.reply_to(message, f"Invalid email: {e}\nPlease try again with /register")
        pending_registrations.pop(message.from_user.id, None)

def process_full_name(message):
    """Process full name input and complete registration."""
    try:
        user_id = message.from_user.id
        full_name = message.text.strip()
        
        if not full_name:
            raise ValueError("Full name cannot be empty")
            
        # Get stored email
        user_data = pending_registrations.get(user_id)
        if not user_data:
            raise ValueError("Registration session expired")
            
        email = user_data['email']
        
        # Add to database
        database.add_user(user_id, full_name, email)
        
        # Cleanup
        pending_registrations.pop(user_id, None)
        
        bot.reply_to(message, "✅ Registration successful!\nWelcome to the YouTube Points Bot! Use /menu to start.")
        
    except Exception as e:
        bot.reply_to(message, f"Registration failed: {e}\nPlease try again with /register")
        pending_registrations.pop(message.from_user.id, None)
        
        
        
@bot.message_handler(commands=['profile'])
def profile_command(message):
    """Handle /profile command."""
    profile = get_profile(message.from_user.id)
    if profile:
        telegram_id, full_name, email, points = profile
        profile_text = (
            f"Profile Information:\n"
            f"Telegram ID: {telegram_id}\n"
            f"Full Name: {full_name}\n"
            f"Email: {email}\n"
            f"Points: {points}"
        )
        bot.reply_to(message, profile_text)
    else:
        bot.reply_to(message, "You are not registered. Please register using /register.")

@bot.message_handler(commands=['viewlinks'])
def view_links(message):
    """Handle /viewlinks command."""
    try:
        if user_exists(message.from_user.id):
            user_id = message.from_user.id
            user_pages[user_id] = 0  # Start from page 0
            send_links_page(message.chat.id, user_id, 0)
        else:
            bot.reply_to(message, "You are not registered. Please register using /register.")
            bot.register_next_step_handler(message, register)
    except psycopg2.Error as e:
        bot.reply_to(message, f"An error occurred: {e}")

##########################
# Unified Text Handler   #
##########################
@bot.message_handler(func=lambda message: message.text in ["👋 Start", "📝 Register", "📋 Profile", "🔍 View Links"])
def handle_text_commands(message):
    """
    If a user sends text matching one of the friendly button labels,
    call the corresponding command function.
    """
    text = message.text.strip()
    if text == "👋 Start":
        start(message)
    elif text == "📝 Register":
        register(message)
    elif text == "📋 Profile":
        profile_command(message)
    elif text == "🔍 View Links":
        view_links(message)
    else:
        bot.reply_to(message, "I don't understand that command.")

##########################
#    DATABASE FUNCTIONS  #
##########################
# def connect_db():
#     """Returns a connection to the SQLite database."""
#     return sqlite3.connect('bot_base.db')  # Adjust the path as needed

def user_exists(telegram_id):
    """Check if the user already exists in the database."""
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user = cursor.fetchone()
            return user is not None
    except psycopg2.Error as e:
        print(f"SQLite error occurred: {e}")
        return False

# def save_registration(telegram_id, full_name, email):
#         """Add a new user to the database."""
#         try:
#             with connect_db() as conn:
#                 cursor = conn.cursor()
#                 cursor.execute(
#                     "INSERT INTO users (telegram_id, full_name, email) VALUES (%s, %s, %s)",
#                     (telegram_id, full_name, email)
#                 )
#                 conn.commit()
#         except psycopg2.Error as e:
#             print(f"Database error occurred: {e}")
#             return False
#     # try:
#     #     user_data = message.text.split(",")
#     #     email, full_name = map(str.strip, user_data)
#     #     database.add_user(message.from_user.id, full_name, email)
#     #     bot.reply_to(message, "Registration successful!\nWelcome to the YouTube Points Bot! Use /menu to start.")
#     #     bot.register_next_step_handler(message, start)
#     # except:
#     #     bot.reply_to(message, "Invalid format! Use: email, full name /register")

def get_profile(telegram_id):
    """Retrieves a user’s profile data from the users table."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, full_name, email, points FROM users WHERE telegram_id = %s",
            (telegram_id,)
        )
        return cursor.fetchone()

def link_exists(link):
    """Check if the link already exists in the database."""
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM links WHERE youtube_link = %s", (link,))
            user = cursor.fetchone()
            return user is not None
    except psycopg2.Error as e:
        print(f"SQLite error occurred: {e}")
        return False

def get_adder(user):
    """Get the full name of the user who added the link."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT full_name FROM users WHERE telegram_id = %s", (user,))
        return cursor.fetchone()

def get_allowed_links(telegram_id):
    """
    Returns a list of links that the user has not yet processed.
    Each returned row is a tuple: (id, youtube_link, description)
    """
    with connect_db() as conn:
        cursor = conn.cursor()
        query = """
            SELECT l.id, l.youtube_link, l.description
            FROM links l
            LEFT JOIN user_link_status uls 
                ON l.id = uls.link_id AND uls.telegram_id = %s
            WHERE uls.processed IS NULL OR uls.processed = 0
        """
        cursor.execute(query, (telegram_id,))
        return cursor.fetchall()

def mark_link_processed(telegram_id, link_id):
    """Marks the given link as processed for the user."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_link_status (telegram_id, link_id, processed)
            VALUES (%s, %s, 1)
            ON CONFLICT (telegram_id, link_id) 
            DO UPDATE SET processed = EXCLUDED.processed
        """, (telegram_id, link_id))
        conn.commit()

def update_user_points(telegram_id, points=1):
    """Updates the user's points in the users table."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE users SET points = points + %s
            WHERE telegram_id = %s
        """, (points, telegram_id))
        conn.commit()

def get_link_description(link_id):
    """Fetches the description for a given link ID."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT description FROM links WHERE id = %s", (link_id,))
        result = cursor.fetchone()
        return result[0] if result else None

##########################
#   CALLBACK HANDLERS    #
##########################
@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_"))
def handle_submit_callback(call):
    """
    Triggered when a user taps the 'Submit Image' button.
    Stores the pending link id and its description, then prompts for image upload.
    """
    telegram_id = call.from_user.id
    allowd_submit = get_allowed_links(telegram_id)
    if allowd_submit:
        try:
            link_id = int(call.data.split("_")[1])
            description = get_link_description(link_id)
            if not description:
                bot.answer_callback_query(call.id, "❌ Error: Link description not found.")
                return
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "❌ Invalid link data.")
            return

        pending_submissions[telegram_id] = (link_id, description)
        bot.answer_callback_query(call.id, "✅ Please upload your image for this link.")
        bot.send_message(call.message.chat.id, "📸 Upload an image now.")
    else:
        bot.send_message(call.message.chat.id, "📸 Not Allowed To Upload an image now.")
        
@bot.callback_query_handler(func=lambda call: call.data.startswith("prev_") or call.data.startswith("next_"))
def navigate_links(call):
    """Handles pagination button clicks."""
    user_id = call.from_user.id
    new_page = int(call.data.split("_")[1])
    send_links_page(call.message.chat.id, user_id, new_page)
    bot.delete_message(call.message.chat.id, call.message.message_id)

##########################
# IMAGE UPLOAD HANDLER   #
##########################
@bot.message_handler(content_types=['photo'])
def process_image_upload(message):
    """
    Handles the image upload.
    Downloads the image, calls the OCR processor, updates points and link status accordingly.
    """
    telegram_id = message.from_user.id
    if telegram_id not in pending_submissions:
        bot.reply_to(message, "❌ No link submission is pending. Please tap 'Submit Image' for a link first.")
        return

    link_id, required_channel = pending_submissions[telegram_id]
    # print(f"{required_channel}")
    photo = message.photo[-1]  # Highest resolution image
    file_info = bot.get_file(photo.file_id)
    downloaded_file = bot.download_file(file_info.file_path)
    image_path = f"temp_{telegram_id}_{link_id}.jpg"
    with open(image_path, "wb") as f:
        f.write(downloaded_file)

    bot.reply_to(message, "🔍 Checking image")
    # print(f"{image_path}")
    result = ocr_processor.check_text_in_image(image_path, required_channel)
    # print(f"{result}")
    if result:
        mark_link_processed(telegram_id, link_id)
        update_user_points(telegram_id, 1)
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
            UPDATE likes SET channel_likes = channel_likes + %s
            WHERE id = %s
            """, (1,link_id))
            conn.commit()
        bot.reply_to(message, "✅ Image verified successfully! You earned +1 point.\n🚫 This link is now blocked for you.\nPerfect Go\nUse /viewlinks to continue.")
    else:
        result = image_processing.check_text_in_image(image_path, required_channel)
        if result:
            mark_link_processed(telegram_id, link_id)
            update_user_points(telegram_id, 1)
            with connect_db() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                UPDATE likes SET channel_likes = channel_likes + %s
                WHERE channel_id = %s
                """, (1,link_id))
                conn.commit()
            bot.reply_to(message, "✅ Image verified successfully! You earned 1 point.\n🚫 This link is now blocked for you.\nPerfect Go\nUse /viewlinks to continue.")
        else:
            bot.reply_to(message, "❌ Image verification failed.\nSorry, try again. Use /viewlinks to continue.")

    if os.path.exists(image_path):
        os.remove(image_path)
    pending_submissions.pop(telegram_id, None)

##########################
#  PAGINATION FUNCTIONS  #
##########################
def get_paginated_links(user_id, page=0, per_page=5):
    """Fetches paginated YouTube links for a user."""
    links = get_allowed_links(user_id)
    total_pages = (len(links) - 1) // per_page + 1
    start = page * per_page
    end = start + per_page
    current_links = links[start:end]
    return current_links, total_pages

def send_links_page(chat_id, user_id, page):
    """Sends a specific page of YouTube links."""
    links, total_pages = get_paginated_links(user_id, page)
    if not links:
        bot.send_message(chat_id, "❌ No available links at the moment.")
        return
    for link in links:
        link_id, youtube_link, description = link
        safe_youtube_link = escape_markdown(youtube_link)
        safe_description = escape_markdown(description)
        text = f"📌 *YouTube Link:* {safe_youtube_link}\n📜 *Description:* {safe_description}"
        markup = types.InlineKeyboardMarkup()
        submit_btn = types.InlineKeyboardButton("📸 Submit Image", callback_data=f"submit_{link_id}")
        markup.add(submit_btn)
        bot.send_message(chat_id, text, reply_markup=markup, parse_mode="Markdown")
    if total_pages > 1:
        markup = types.InlineKeyboardMarkup()
        if page > 0:
            markup.add(types.InlineKeyboardButton("⬅️", callback_data=f"prev_{page-1}"))
            bot.send_message(chat_id, "Previous Page:", parse_mode="Markdown", reply_markup=markup)
        if page < total_pages - 1:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("➡️", callback_data=f"next_{page+1}"))
            bot.send_message(chat_id, "Next Page:", parse_mode="Markdown", reply_markup=markup)



def escape_markdown(text: str) -> str:
    # List of characters that need to be escaped in Markdown v1
    escape_chars = r'\_*[]()~`>#+-=|{}!'
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text

##########################
#  AUTHORIZATION CHECK   #
##########################
def is_authorized_link_adder(telegram_id):
    """Returns True if the user is authorized to add links."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM authorized_link_adders WHERE telegram_id = %s", (telegram_id,))
        result = cursor.fetchone()
        return result is not None

##########################
#      START BOT         #
##########################
bot.polling()
