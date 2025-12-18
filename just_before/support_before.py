import logging
import asyncio
from datetime import datetime, timedelta
import os
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)
import psycopg2.pool
from config import DATABASE_CONFIG, BOT_TOKEN, DATABASE_URL

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database connection pools
main_db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_CONFIG)
user_db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["🖼 Show Images", "🆕 Refresh Support"]],
    resize_keyboard=True,
    one_time_keyboard=False
)

async def is_admin(user_id: int) -> bool:
    """Check if user exists in admins table."""
    conn = user_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE admins_id = %s", (user_id,))
            return bool(cur.fetchone())
    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return False
    finally:
        user_db_pool.putconn(conn)

async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Display main menu with persistent visibility."""
    await update.message.reply_text(
        "Support System\nChoose an option:",
        reply_markup=MAIN_KEYBOARD
    )

async def clear_chat(context, chat_id):
    """Delete all tracked messages while preserving menu."""
    if 'messages' in context.user_data:
        for msg_id in context.user_data['messages']:
            try:
                await context.bot.delete_message(chat_id, msg_id)
            except Exception as e:
                logger.error(f"Delete message error: {e}")
        context.user_data['messages'] = []

async def get_pending_images(page=0, limit=5):
    """Retrieve paginated locked images."""
    conn = main_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, user_id, link_id, channel_name, 
                user_name, image_path FROM images 
                WHERE locked = TRUE 
                ORDER BY date DESC 
                LIMIT %s OFFSET %s""",
                (limit, page * limit)
            )
            return [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]
    finally:
        main_db_pool.putconn(conn)

async def handle_show_images(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """Display images with persistent menu."""
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    chat_id = update.effective_chat.id
    await clear_chat(context, chat_id)

    try:
        images = await get_pending_images(page)
        valid_images = []
        
        # Validate and clean images
        for img in images:
            if os.path.exists(img['image_path']):
                valid_images.append(img)
            else:
                # Cleanup invalid entries
                conn = main_db_pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM images WHERE id = %s", (img['id'],))
                        conn.commit()
                finally:
                    main_db_pool.putconn(conn)

        if not valid_images:
            msg = await context.bot.send_message(
                chat_id, 
                "📭 No pending images!", 
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data['messages'] = [msg.message_id]
            return

        # Send images with inline buttons
        message_ids = []
        for img in valid_images:
            try:
                with open(img['image_path'], 'rb') as photo:
                    msg = await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=photo,
                        caption=f"📌 {img['channel_name']}\n👤 {img['user_name']}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{img['id']}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{img['id']}")
                        ]])
                    )
                    message_ids.append(msg.message_id)
            except Exception as e:
                logger.error(f"Image send error: {e}")

        # Navigation controls
        total_images = await get_pending_images_count()
        total_pages = (total_images + 4) // 5  # Keep existing calculation
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⏪ Previous", callback_data=f"page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ⏩", callback_data=f"page_{page+1}"))

        if nav_buttons:
            nav_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 Page {page+1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav_buttons])
            )
            message_ids.append(nav_msg.message_id)

        context.user_data.update({
            'messages': message_ids,
            'current_page': page
        })

        # Show persistent menu
        await context.bot.send_message(
            chat_id=chat_id,
            text="Choose next action:",
            reply_markup=MAIN_KEYBOARD
        )

    except Exception as e:
        logger.error(f"Show images error: {e}")
        await show_menu(update, context)



async def get_pending_images_count():
    """Get total count of locked images."""
    conn = main_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM images WHERE locked = TRUE")
            return cur.fetchone()[0]
    finally:
        main_db_pool.putconn(conn)


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process button clicks with menu persistence."""
    user_name = update.effective_user.name
    query = update.callback_query
    await query.answer()
    
    if not await is_admin(query.from_user.id):
        await query.message.reply_text("⛔ Access Expired", reply_markup=MAIN_KEYBOARD)
        return

    try:
        data = query.data
        
        # Handle image pagination
        if data.startswith("page_"):
            new_page = int(data.split("_")[1])
            await handle_show_images(update, context, new_page)
        
        # Add support pagination handler
        elif data.startswith("support_page_"):
            new_page = int(data.split("_")[2])
            await handle_support_refresh(update, context, new_page)
            
        # Handle image approval/rejection
        elif data.startswith(("approve_", "reject_")):
            action, image_id = data.split("_")
            image_id = int(image_id)
            
            conn = main_db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """SELECT user_id, link_id, channel_name, image_path 
                        FROM images WHERE id = %s""",
                        (image_id,)
                    )
                    img_data = cur.fetchone()
                    
                    if not img_data:
                        await query.message.reply_text("❌ Image not found!", reply_markup=MAIN_KEYBOARD)
                        return

                    user_id, link_id, channel_name, path = img_data

                    # Process action
                    if action == "approve":
                        await handle_approval(user_id, link_id)
                        text = "✅ Approved and removed"
                    else:
                        admins_id = [7168120805, 1130152311, 6106281772]
                        if user_id not in admins_id:
                            await handle_rejection(user_id, link_id, channel_name)
                        text = "❌ Rejected and tracked"
                    
                    # Cleanup
                    cur.execute("DELETE FROM images WHERE id = %s", (image_id,))
                    if os.path.exists(path):
                        os.remove(path)
                    conn.commit()
                    
                    await context.bot.send_message(
                        query.message.chat_id,
                        text,
                        reply_markup=MAIN_KEYBOARD
                    )
                    
            finally:
                main_db_pool.putconn(conn)
            
            await query.message.delete()
            if 'messages' in context.user_data:
                context.user_data['messages'].remove(query.message.message_id)

        # Handle support request confirmation
        elif data.startswith("confirm_"):
            request_id = int(data.split("_")[1])
            # admin_name = query.from_user.full_name
            # print(f"{admin_name}")

            conn = user_db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM support WHERE id = %s", (request_id,))
                    conn.commit()
                    
                    await query.message.delete()
                    if 'messages' in context.user_data:
                        context.user_data['messages'].remove(query.message.message_id)
                        
                    await context.bot.send_message(
                        query.message.chat_id,
                        f"✅ Request #{request_id} confirmed by {user_name}",
                        reply_markup=MAIN_KEYBOARD
                    )
            finally:
                user_db_pool.putconn(conn)

    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await context.bot.send_message(
            query.message.chat_id,
            "Please choose an option:",
            reply_markup=MAIN_KEYBOARD
        )

async def handle_approval(user_id: int, link_id: int):
    """Process approval actions."""
    conn = user_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users_block WHERE link_id = %s AND telegram_id = %s", (link_id, user_id))
            cur.execute("UPDATE likes SET channel_likes = channel_likes + 1 WHERE id = %s", (link_id,))
            
            # Check subscription status
            cur.execute("SELECT channel_likes, subscription_count FROM likes WHERE id = %s", (link_id,))
            result = cur.fetchone()
            if result and result[0] >= result[1]:
                cur.execute("DELETE FROM links WHERE id = %s", (link_id,))
                cur.execute("DELETE FROM users_block WHERE link_id = %s", (link_id,))
                cur.execute("UPDATE likes SET status = TRUE WHERE id = %s", (link_id,))
            
            conn.commit()
    finally:
        user_db_pool.putconn(conn)

async def handle_rejection(user_id: int, link_id: int, channel_name: str):
    """Process rejection actions."""
    telegram_id = user_id
    channel_name = channel_name
    link_id = link_id
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = user_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            # Block non-admin users
            # if not await is_admin(user_id):
            cur.execute("""
                UPDATE users 
                SET block_num = block_num + 1, date_block = %s
                WHERE telegram_id = %s
            """, (date_now, user_id,))
            cur.execute(
                "SELECT full_name FROM users WHERE telegram_id = %s",
                (telegram_id,)
            )
            user_name = cur.fetchone()[0]
            cur.execute("""
                INSERT INTO users_block (
                    telegram_id, user_name, channel_name, link_id, block_num
                ) VALUES (%s, %s, %s, %s, %s)
            """, (
                telegram_id,
                user_name,
                channel_name,
                link_id,
                1
            ))
            # Update link status
            cur.execute("DELETE FROM user_link_status WHERE telegram_id = %s AND link_id = %s", (user_id, link_id))
            cur.execute("UPDATE links SET allow_link = allow_link + 1 WHERE id = %s", (link_id,))
            conn.commit()
    finally:
        user_db_pool.putconn(conn)

# Update the message handler to reset to first page
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process all messages with menu persistence."""
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    text = update.message.text
    if text == "🖼 Show Images":
        await handle_show_images(update, context)
    elif text == "🆕 Refresh Support":
        await clear_chat(context, update.effective_chat.id)
        await handle_support_refresh(update, context, page=0)  # Start from first page
    else:
        await update.message.reply_text(
            "Please use the menu buttons below:",
            reply_markup=MAIN_KEYBOARD
        )

async def persistent_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ensure menu persists after every interaction."""
    if update.message:
        await show_menu(update, context)


# Modified get_pending_support_requests with pagination
async def get_pending_support_requests(page=0, limit=7):
    """Retrieve paginated support requests"""
    conn = user_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, telegram_id, message, user_name, message_date, email, who_is 
                FROM support 
                ORDER BY message_date ASC 
                LIMIT %s OFFSET %s
            """, (limit, page * limit))
            return [dict(zip([desc[0] for desc in cur.description], row)) for row in cur.fetchall()]
    finally:
        user_db_pool.putconn(conn)


# Modified handle_support_refresh with pagination
async def handle_support_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    """Handle support request refresh with pagination"""
    user_id = update.effective_user.id
    if not await is_admin(user_id):
        await update.message.reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    chat_id = update.effective_chat.id
    await clear_chat(context, chat_id)

    try:
        requests = await get_pending_support_requests(page)
        
        if not requests:
            msg = await context.bot.send_message(
                chat_id, 
                "📭 No pending support requests!", 
                reply_markup=MAIN_KEYBOARD
            )
            context.user_data['messages'] = [msg.message_id]
            return

        message_ids = []
        for req in requests:
            text = (
                f"📨 Request #{req['id']}\n"
                f"👤 User: {req['user_name']} (ID: {req['telegram_id']})\n"
                f"👤 Who IS: {req['who_is']}\n"
                f"📧 Email: {req['email']}\n"
                f"📆 Date: {req['message_date']}\n"
                f"✉️ Message: {req['message']}"
            )
            
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{req['id']}")
                ]])
            )
            message_ids.append(msg.message_id)

        # Add navigation buttons
        total_requests = await get_pending_support_count()
        total_pages = (total_requests + 6) // 7  # Round up for 7 items per page
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⏪ Previous", callback_data=f"support_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ⏩", callback_data=f"support_page_{page+1}"))

        if nav_buttons:
            nav_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 Support Page {page+1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav_buttons])
            )
            message_ids.append(nav_msg.message_id)

        context.user_data['messages'] = message_ids
        
        # Show persistent menu
        await context.bot.send_message(
            chat_id=chat_id,
            text="Choose next action:",
            reply_markup=MAIN_KEYBOARD
        )

    except Exception as e:
        logger.error(f"Support refresh error: {e}")
        await show_menu(update, context)
        
        
        
# Add this new function to get support request count
async def get_pending_support_count():
    """Get total count of pending support requests."""
    conn = user_db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM support")
            return cur.fetchone()[0]
    finally:
        user_db_pool.putconn(conn)
        
        
        
def main():
    """Configure and start the bot."""
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add persistent menu handler
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, persistent_menu), group=-1)
    
    # Register handlers
    application.add_handler(CommandHandler("start", show_menu))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT, handle_message))
    
    application.run_polling()

if __name__ == "__main__":
    try:
        main()
    finally:
        main_db_pool.closeall()
        user_db_pool.closeall()