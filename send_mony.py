import logging
import psycopg2
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

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = "8062800182:AAGwnhGinAaa-0oM2El2KMuuf3fu17Mbl_E"
DB_URL = "postgres://postgres:postgres@localhost:5432/Test"
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
            message += f"🔹 #{wd['id']} - {wd['amount']} pts - {wd['full_name']}\n"
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
                cursor.execute("""
                    UPDATE withdrawals 
                    SET status = 'processed',
                        processed_date = NOW()
                    WHERE id = %s
                """, (wd_id,))
                conn.commit()

        # Prepare user message
        user_message = (
            "🎉 Withdrawal Processed!\n"
            f"📆 Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
            f"💎 Amount: {withdrawal['amount']} pts\n"
            f"👤 Receiver: {withdrawal['full_name']}\n"
            f"📱 Phone: {withdrawal['phone']}"
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
            f"💸 Amount: {withdrawal['amount']} points\n"
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
            f"💸 Amount: {withdrawal['amount']} points\n"
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
        fallbacks=[CommandHandler('start', start)]
    )

    application.add_handler(conv_handler)
    application.run_polling()

if __name__ == "__main__":
    main()