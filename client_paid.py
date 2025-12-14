import logging
import psycopg2
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
    CallbackContext
)
from datetime import datetime

# ========== CONFIGURATION ==========
TELEGRAM_TOKEN = "7328995633:AAF4pY4xlW68RhfX43wJ3AJXfUITKpe0q8s"
ADMIN_IDS = ["6936321897", "1130152311", "6106281772", "1021796797", "2050036668", "1322069113"]  # Add your admin IDs
DATABASE_CONFIG = {
    "host": "localhost",
    "database": "Test",
    "user": "postgres",
    "password": "postgres",
    "port": "5432"
}

# ========== STATES ==========
(
    AWAIT_ID_PAY,
    AWAIT_PRICE,  # New state
    AWAIT_COMPANY,
    AWAIT_CONFIRMATION,
    AWAIT_USER_ID,
    AWAIT_USER_MESSAGE
) = range(6)  # Updated to 6 states

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== DATABASE HELPERS ==========
def get_conn():
    return psycopg2.connect(**DATABASE_CONFIG)

def create_tables():
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS links_success (
                id SERIAL PRIMARY KEY,
                youtube_link TEXT,
                description TEXT,
                added_by BIGINT,
                adder TEXT,
                submission_date TEXT,
                channel_id TEXT,
                subscription_count BIGINT,
                id_pay VARCHAR(255),
                telecom_company VARCHAR(255)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_client_id_chosser (
                id SERIAL PRIMARY KEY,
                admin_id BIGINT,
                id_pay VARCHAR(255),
                telecom_company VARCHAR(255),
                action_date TIMESTAMP
            )
        """)
        conn.commit()
    finally:
        conn.close()

# ========== ADMIN MENUS ==========
ADMIN_MAIN_MENU = [
    ["🔍 Process Payment ID", "📨 Send Message"],
    ["📊 View Statistics", "🔙 Main Menu"]
]




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
        logger.error(f"Admin check failed: {str(e)}")
        return False
    finally:
        conn.close()


def get_admin_menu():
    return ReplyKeyboardMarkup(ADMIN_MAIN_MENU, resize_keyboard=True)

# ========== CORE FUNCTIONALITY ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await is_admins(user.id):
        await update.message.reply_text(
            "👑 Admin Panel",
            reply_markup=get_admin_menu()
        )
    else:
        await update.message.reply_text("Welcome to the bot!")
    return ConversationHandler.END

async def process_payment_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Enter Payment ID:",
        reply_markup=ReplyKeyboardMarkup([["Cancel ❌"]], resize_keyboard=True)
    )
    return AWAIT_ID_PAY

# Modified handlers
async def handle_payment_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    id_pay = update.message.text.strip()
    user_lang = update.effective_user.language_code or 'en'

    if id_pay in ["Cancel ❌", "إلغاء ❌"]:
        cancel_msg = "🚫 Operation cancelled" if user_lang != 'ar' else "🚫 تم الإلغاء"
        await update.message.reply_text(cancel_msg, reply_markup=get_admin_menu())
        return ConversationHandler.END
    context.user_data['id_pay'] = id_pay
    
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT EXISTS(
                SELECT 1 FROM links_success 
                WHERE id_pay = %s
            )
        """, (id_pay,))
        exists = cur.fetchone()[0]
        
        if not exists:
            await update.message.reply_text(
                "❌ No records found for this payment ID",
                reply_markup=get_admin_menu()
            )
            return ConversationHandler.END
            
        await update.message.reply_text(
            "💵 Enter the price:",
            reply_markup=ReplyKeyboardMarkup([["Cancel ❌"]], resize_keyboard=True)
        )
        return AWAIT_PRICE
    finally:
        conn.close()


async def handle_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        user_lang = update.effective_user.language_code or 'en'
        if text in ["Cancel ❌", "إلغاء ❌"]:
            cancel_msg = "🚫 Operation cancelled" if user_lang != 'ar' else "🚫 تم الإلغاء"
            await update.message.reply_text(cancel_msg, reply_markup=get_admin_menu())
            return ConversationHandler.END
        price = float(update.message.text.strip())
        context.user_data['price'] = price
        
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT DISTINCT telecom_company FROM links_success
                        WHERE id_pay = %s
                    """, (context.user_data['id_pay'],))
                    companies = [row[0] for row in cur.fetchall()]
                    cur.execute("""
                        SELECT price FROM links_success
                        WHERE id_pay = %s
                    """, (context.user_data['id_pay'],))
                    price_find = cur.fetchone()[0]
                    keyboard = [[company] for company in companies]
                    keyboard.append(["Cancel ❌"])
                    
                    await update.message.reply_text(
                        "🏢 Select telecom company:",
                        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
                    )
                    if price == price_find:
                        return AWAIT_COMPANY
                    else:
                        await update.message.reply_text(
                            "❌ the price is not the real please sure you send the real price",
                            reply_markup=get_admin_menu()
                        )
                        return ConversationHandler.END
        finally:
            conn.close()
    except ValueError:
        await update.message.reply_text("❌ Invalid price format. Please enter a number.")
        return AWAIT_PRICE

async def handle_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    company = update.message.text.strip()
    user_lang = update.effective_user.language_code or 'en'
    if company in ["Cancel ❌", "إلغاء ❌"]:
        cancel_msg = "🚫 Operation cancelled" if user_lang != 'ar' else "🚫 تم الإلغاء"
        await update.message.reply_text(cancel_msg, reply_markup=get_admin_menu())
        return ConversationHandler.END
    context.user_data['company'] = company
    
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM links_success
            WHERE id_pay = %s AND telecom_company = %s
        """, (context.user_data['id_pay'], company))
        # Get column names from cursor description
        columns = [desc[0] for desc in cur.description]
        record = cur.fetchone()
        
        if not record:
            await update.message.reply_text("❌ Record not found")
            return ConversationHandler.END

        # Create dictionary with column names as keys
        context.user_data['record'] = dict(zip(columns, record))
        # context.user_data['record'] = record
        
        await update.message.reply_text(
            f"Confirm processing:\n\n"
            f"Payment ID: {record['id_pay']}\n"
            f"Company: {record['telecom_company']}\n"
            f"Channel: {record['description']}\n",
            reply_markup=ReplyKeyboardMarkup([["✅ Confirm", "Cancel ❌"]], resize_keyboard=True)
        )
        return AWAIT_CONFIRMATION
    finally:
        conn.close()


async def handle_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "✅ Confirm":
        record = context.user_data['record']
        conn = get_conn()
        try:
            cur = conn.cursor()
            admin_id = update.effective_user.id  # <-- Fix here
            # Insert to links
            cur.execute("""
                INSERT INTO links (
                    youtube_link, description, added_by, adder, 
                    submission_date, channel_id, id_pay, subscription_count 
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (record['youtube_link'],
                  record['description'],
                  record['added_by'],
                  record['adder'],
                  record['submission_date'],
                  record['channel_id'],
                  record['id_pay'],
                  record['subscription_count']))

            
            # Insert to admin log with price
            cur.execute("""
                INSERT INTO admin_client_id_chosser
                (admin_id, id_pay, telecom_company, price, action_date)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                admin_id,
                record['id_pay'], # id_pay
                record['telecom_company'], # telecom_company
                context.user_data['price'],
                datetime.now()
            ))
            
            # Delete from links_success
            cur.execute("DELETE FROM links_success WHERE id = %s", (record['id'],))
            
            conn.commit()
            # r = record[1:9]
            # print(f"{r}")
            # Notify user with price
            try:
                await context.bot.send_message(
                    chat_id=record['added_by'],
                    text=f"✅ Payment processed!\n"
                        f"📋 Details:\n"
                        f"ID: {record['id_pay']}\n"
                        f"Company: {record['telecom_company']}\n"
                        f"Channel: {record['description']}\n"
                        f"Required: {record['subscription_count']}\n"
                        f"Price: {context.user_data['price']}"
                )
            except Exception as e:
                logger.error(f"User notification failed: {str(e)}")
            
            await update.message.reply_text(
                "✅ Processing complete!",
                reply_markup=get_admin_menu()
            )
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error: {str(e)}")
            await update.message.reply_text("❌ Processing failed!")
        finally:
            conn.close()
    else:
        await update.message.reply_text("🚫 Operation cancelled", reply_markup=get_admin_menu())
        return ConversationHandler.END
    
    context.user_data.clear()
    return ConversationHandler.END

async def send_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Enter user's Telegram ID:",
        reply_markup=ReplyKeyboardMarkup([["Cancel ❌"]], resize_keyboard=True)
    )
    return AWAIT_USER_ID

async def handle_user_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text in ["Cancel ❌", "إلغاء ❌"]:
            cancel_msg = "🚫 Operation cancelled"
            await update.message.reply_text(cancel_msg, reply_markup=get_admin_menu())
            return ConversationHandler.END
        user_id = int(update.message.text)
        context.user_data['user_id'] = user_id
        await update.message.reply_text(
            "Enter message to send:",
            reply_markup=ReplyKeyboardMarkup([["Cancel ❌"]], resize_keyboard=True)
        )
        return AWAIT_USER_MESSAGE
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID. Must be a number.")
        return AWAIT_USER_ID

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        text = update.message.text.strip()
        if text in ["Cancel ❌", "إلغاء ❌"]:
            cancel_msg = "🚫 Operation cancelled"
            await update.message.reply_text(cancel_msg, reply_markup=get_admin_menu())
            return ConversationHandler.END
        await context.bot.send_message(
            chat_id=context.user_data['user_id'],
            text=update.message.text
        )
        await update.message.reply_text(
            "✅ Message sent successfully!",
            reply_markup=get_admin_menu()
            )
    except Exception as e:
        logger.error(f"Failed to send message: {e}")
        await update.message.reply_text("❌ Failed to send message")
    return ConversationHandler.END

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process menu button selections"""
    text = update.message.text
    user = update.effective_user
    
    # ["🔍 Process Payment ID", "📨 Send Message"],
    # ["📊 View Statistics", "🔙 Main Menu"]
    

    if text == "🔍 Process Payment ID":
        await process_payment_start(update, context)
    elif text == "📨 Send Message":
        await send_message_start(update, context)
    elif text == "📊 View Statistics":
        await start(update, context)
    elif text == "🔙 Main Menu":  # New handler
        await start(update, context)
    else:
        msg = "❌ Unknown command. Please use the menu buttons."
        await update.message.reply_text(msg)
        await start(update,context)



async def cancel_operation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Operation cancelled",
        reply_markup=get_admin_menu()
    )
    context.user_data.clear()
    return ConversationHandler.END

# ========== APPLICATION SETUP ==========
def main():
    create_tables()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Admin conversation handler
    admin_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🔍 Process Payment ID$"), process_payment_start),
            MessageHandler(filters.Regex(r"^📨 Send Message$"), send_message_start)
        ],
        states={
            AWAIT_ID_PAY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_payment_id)],
            AWAIT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price)],
            AWAIT_COMPANY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_company)],
            AWAIT_CONFIRMATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_confirmation)],
            AWAIT_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_id)],
            AWAIT_USER_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_user_message)]
        },
        fallbacks=[
            CommandHandler('cancel', cancel_operation),
            MessageHandler(filters.Regex(r"^Cancel ❌$"), cancel_operation)
        ]
    )


    handlers = [
        MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler),
    ]


    # Main handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(admin_conv)
    for handler in handlers:
        application.add_handler(handler)

    application.run_polling()

if __name__ == '__main__':
    main()



