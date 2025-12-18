import logging
import psycopg2

import config
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters
)
from telegram.error import BadRequest
from telegram.warnings import PTBUserWarning
import warnings

# Keep PTB warnings visible
warnings.filterwarnings("ignore", category=PTBUserWarning)

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ===== Start logging: save who pressed /start for this bot =====
BOT_NAME = "SendMoney"

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
    conn = connect_db()
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
            conn.close()
        except Exception:
            pass


# Configuration
BOT_TOKEN = config.SEND_MONEY_BOT_TOKEN
DB_URL = config.DATABASE_URL
ADMIN_IDS = [6936321897, 1130152311, 6106281772, 1021796797, 2050036668, 1322069113]
PER_PAGE = 5

# Conversation states
VIEWING, DETAILS = range(2)
SHOWALL_PER_PAGE = 5  # You can make this different from pending withdrawals

def connect_db():
    """Create and return a PostgreSQL database connection"""
    return psycopg2.connect(DB_URL)


async def is_admins(admins_id: int) -> bool:
    """Check if user is banned with DB connection handling"""
    try:
        with connect_db() as conn:
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

# Update the start menu
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command with admin menu"""
    # if isinstance(update, Update):
    #     message = update.message
    # else:
    #     message = update
    
    user_id = update.effective_user.id
    log_bot_start(update.effective_user)
    await is_admins(user_id)
    if is_admins:
        context.user_data.clear()
        
        menu = ReplyKeyboardMarkup(
            [["🔄 Refresh", "📋 View Withdrawals"], 
             ["📋 Show Processed", "🏠 Start"],  # New button
             ["/start"]],
            resize_keyboard=True
        )
        await update.message.reply_text(
            "Admin Dashboard:",
            reply_markup=menu
        )
        return VIEWING
    else:
        await update.message.reply_text("Welcome to our service!")
        return ConversationHandler.END

# Update handle_menu function
async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle admin menu selections"""
    text = update.message.text
    if text == "📋 View Withdrawals":
        await show_withdrawals(update, context, page=0)
    elif text == "📋 Show Processed":  # New handler
        await show_processed_withdrawals(update, context, page=0)
    elif text == "🔄 Refresh":
        await show_withdrawals(update, context, page=0)
    elif text in ("🏠 Start", "/start"):
        await start(update, context)
    else:
        await start(update, context)
    return VIEWING

# Add new function for processed withdrawals
async def show_processed_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """Show paginated processed withdrawals list"""
    try:
        context.user_data.pop('processed_list_message_id', None)
        withdrawals, total_pages = get_withdrawals(page, status='processed')
        page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0
        
        message = f"📋 Processed Withdrawals (Page {page+1}/{total_pages}):\n\n" if withdrawals else "No processed withdrawals found"
        buttons = []
        
        for wd in withdrawals:
            message += (
                f"✅ #{wd['id']} - {wd['amount']} pts\n" f"Email: {wd['email']}\n" f"Phone: {wd['phone']}\n"
                f"👤 {wd['full_name']} | 📅 {wd['processed_date'].strftime('%Y-%m-%d')}\n\n"
            )
        
        # Pagination controls
        pagination = []
        if page > 0:
            pagination.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"processed_page_{page-1}"))
        if total_pages > 1 and page < total_pages - 1:
            pagination.append(InlineKeyboardButton("Next ➡️", callback_data=f"processed_page_{page+1}"))
        
        if pagination:
            buttons.append(pagination)
        
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text=message, reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            msg = await update.message.reply_text(text=message, reply_markup=reply_markup)
            context.user_data['processed_list_message_id'] = msg.message_id
        
        context.user_data['current_processed_page'] = page
        return VIEWING
        
    except Exception as e:
        logger.error(f"Error showing processed withdrawals: {e}")
        await update.effective_message.reply_text("⚠️ Error loading processed withdrawals")
        return ConversationHandler.END


async def show_withdrawals(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """Show paginated withdrawals list with state management"""
    try:
        # # Delete previous processed message if exists
        # if 'processed_list_message_id' in context.user_data:
        #     try:
        #         await context.bot.delete_message(
        #             chat_id=update.effective_chat.id,
        #             message_id=context.user_data['processed_list_message_id']
        #         )
        #     except BadRequest:
        #         pass
        context.user_data.pop('list_message_id', None)
        withdrawals, total_pages = get_withdrawals(page)
        page = max(0, min(page, total_pages - 1)) if total_pages > 0 else 0
        
        message = f"📋 Pending Withdrawals (Page {page+1}/{total_pages}):\n\n" if withdrawals else "No pending withdrawals found"
        buttons = []
        
        for wd in withdrawals:
            message += f"🔹 #{wd['id']} - {wd['amount_before']} pts - {wd['full_name']}\n"
            buttons.append([InlineKeyboardButton(
                f"Detail #{wd['id']}", callback_data=f"detail_{wd['id']}_{page}"
            )])
        
        # Pagination controls
        pagination = []
        if page > 0:
            pagination.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"page_{page-1}"))
        if total_pages > 1 and page < total_pages - 1:
            pagination.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{page+1}"))
        
        if pagination:
            buttons.append(pagination)
        
        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(
                    text=message, reply_markup=reply_markup
                )
            except BadRequest as e:
                if "Message is not modified" not in str(e):
                    raise
        else:
            msg = await update.message.reply_text(text=message, reply_markup=reply_markup)
            context.user_data['list_message_id'] = msg.message_id
        
        context.user_data['current_page'] = page
        return VIEWING
        
    except Exception as e:
        logger.error(f"Error showing withdrawals: {e}")
        await update.effective_message.reply_text("⚠️ Error loading withdrawals")
        return ConversationHandler.END


async def handle_pagination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle pagination callbacks for both pending and processed"""
    query = update.callback_query
    await query.answer()
    
    try:
        if query.data.startswith('processed_page_'):
            # Processed withdrawals pagination
            page = int(query.data.split('_')[2])
            return await show_processed_withdrawals(update, context, page=page)
        else:
            # Pending withdrawals pagination
            page = int(query.data.split('_')[1])
            return await show_withdrawals(update, context, page=page)
    except Exception as e:
        logger.error(f"Pagination error: {e}")
        await query.edit_message_text("⚠️ Error changing page")
        return VIEWING

async def mark_as_sent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark withdrawal as processed and update list"""
    user_name = update.effective_user.name
    query = update.callback_query
    try:
        await query.answer("⏳ Processing request...")
        wd_id = query.data.split('_')[1]
        withdrawal = get_withdrawal_detail(wd_id)
        
        if not withdrawal:
            await query.edit_message_text("❌ Withdrawal not found")
            return

        # Update database
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT amount_before FROM withdrawals WHERE id = %s", (wd_id,))
                am = cursor.fetchone()
                am_finall = int(am[0]) + int(withdrawal['amount']) 
                cursor.execute("""
                    UPDATE withdrawals 
                    SET status = 'processed', amount = %s, user_sent = %s,
                        processed_date = NOW()
                    WHERE id = %s
                """, (am_finall,user_name,wd_id,))
                cursor.execute(
                    "UPDATE withdrawals SET amount_before = 0 WHERE id = %s",
                    (wd_id,)
                )
                conn.commit()

        # Prepare user message
        user_message = (
            "🎉 Withdrawal Processed!\n"
            f"📆 Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"💎 Amount: {withdrawal['amount_before']} pts\n"
            f"👤 Receiver: {withdrawal['full_name']}\n"
            f"👤 Sender: {user_name}\n"
            f"📱 Phone: {withdrawal['phone']}\n"
            f"💳 Cash Number: {withdrawal['cash_number']}\n"
        )

        # Try to send notification
        notification_status = "✅ User notified"
        try:
            await context.bot.send_message(
                chat_id=withdrawal['user_id'],
                text=user_message
            )
        except BadRequest as e:
            if "bot can't initiate conversation" in str(e):
                notification_status = "❌ User hasn't started the bot"
            else:
                notification_status = f"❌ Notification failed: {e.message}"
            logger.error(f"Failed to send message: {e}")

        # Update detail message with processing results
        processed_message = (
            f"✅ Processed Withdrawal #{wd_id}\n"
            "────────────────────\n"
            f"👤 Name: {withdrawal['full_name']}\n"
            f"📱 Phone: {withdrawal['phone']}\n"
            f"💳 Cash Number: {withdrawal['cash_number']}\n"
            f"💸 Amount: {withdrawal['amount_before']} points\n"
            f"📡 Carrier: {withdrawal['carrier']}\n"
            f"📅 Date: {withdrawal['withdrawal_date'].strftime('%Y-%m-%d %H:%M')}\n"
            "────────────────────\n"
            f"Status: ✅ PROCESSED\n"
            f"Notification: {notification_status}"
        )

        await query.edit_message_text(
            text=processed_message,
            reply_markup=None
        )

        # # Refresh withdrawals list
        current_page = context.user_data.get('current_page', 0)
        await show_withdrawals(update, context, page=current_page)
        return VIEWING
    
    except Exception as e:
        logger.error(f"Processing error: {e}")
        if query.from_user:
            try:
                await query.edit_message_text("⚠️ Processing failed")
                return VIEWING
            except BadRequest:
                return VIEWING

async def show_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show withdrawal details"""
    query = update.callback_query
    await query.answer()
    
    try:
        parts = query.data.split('_')
        wd_id, page = parts[1], parts[2]
        withdrawal = get_withdrawal_detail(wd_id)
        
        if not withdrawal:
            await query.edit_message_text("❌ Withdrawal not found")
            return
        
        message = (
            f"⚠️ Withdrawal #{wd_id}\n"
            "────────────────────\n"
            f"👤 Name: {withdrawal['full_name']}\n"
            f"📱 Phone: {withdrawal['phone']}\n"
            f"💳 Cash Number: {withdrawal['cash_number']}\n"
            f"💸 Amount: {withdrawal['amount_before']} points\n"
            f"📡 Carrier: {withdrawal['carrier']}\n"
            f"📅 Date: {withdrawal['withdrawal_date'].strftime('%Y-%m-%d %H:%M')}\n"
            "────────────────────\n"
            "Status: ❌ PENDING"
        )
        
        keyboard = [
            [InlineKeyboardButton("✅ Mark Sent", callback_data=f"approve_{wd_id}")],
            [InlineKeyboardButton("🔙 Back", callback_data=f"page_{page}")]
        ]
        
        await query.edit_message_text(
            message, reply_markup=InlineKeyboardMarkup(keyboard))
        return DETAILS
        
    except Exception as e:
        logger.error(f"Error showing detail: {e}")
        await query.edit_message_text("⚠️ Error loading details")
        return VIEWING

# Update get_withdrawals function
def get_withdrawals(page: int, status='pending'):
    """Get paginated withdrawals from database with status filter"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT * FROM withdrawals
                    WHERE status = %s
                    ORDER BY processed_date DESC
                    LIMIT %s OFFSET %s
                """, (status, SHOWALL_PER_PAGE, page * SHOWALL_PER_PAGE))
                
                results = cursor.fetchall()
                columns = [desc[0] for desc in cursor.description]
                
                cursor.execute("SELECT COUNT(*) FROM withdrawals WHERE status = %s", (status,))
                total = cursor.fetchone()[0]
                total_pages = (total + SHOWALL_PER_PAGE - 1) // SHOWALL_PER_PAGE if total > 0 else 0
                
                return [dict(zip(columns, row)) for row in results], total_pages
    except Exception as e:
        logger.error(f"Database error: {e}")
        return [], 0

def get_withdrawal_detail(wd_id: int):
    """Get single withdrawal details"""
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM withdrawals WHERE id = %s", (wd_id,))
                row = cursor.fetchone()
                columns = [desc[0] for desc in cursor.description]
                return dict(zip(columns, row)) if row else None
    except Exception as e:
        logger.error(f"Database error: {e}")
        return None

# Ensure bot_starts table exists
# try:
#     _c = connect_db()
#     try:
#         ensure_bot_starts_table(_c)
#         _c.commit()
#     finally:
#         _c.close()
# except Exception as e:
#     logger.error(f"Failed to ensure bot_starts table: {e}")
def main():
    """Configure and start the bot"""
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            MessageHandler(filters.Regex(r'^(🏠 Start|/start)$'), start)
        ],
        states={
            VIEWING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu),
                # Modified pattern to match both pagination types
                CallbackQueryHandler(handle_pagination, pattern=r"^(page|processed_page)_\d+"),
                CallbackQueryHandler(show_detail, pattern=r"^detail_")
            ],
            DETAILS: [
                CallbackQueryHandler(mark_as_sent, pattern=r"^approve_"),
                CallbackQueryHandler(handle_pagination, pattern=r"^page_")
            ]
        },
        fallbacks=[CommandHandler('start', start)],
        
    )

    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == "__main__":
    main()