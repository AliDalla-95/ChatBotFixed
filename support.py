import logging
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import psycopg2.pool
from config import BOT_TOKEN, DATABASE_URL

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Single DB pool (Test DB): requests + users + likes + support tables
db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📋 Show Requests", "🆕 Refresh Support"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)

REQUESTS_PAGE_SIZE = 5
SUPPORT_PAGE_SIZE = 7


def _msg(update: Update):
    # Works for both normal messages and callback queries
    return update.effective_message


async def is_admin(user_id: int) -> bool:
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM admins WHERE admins_id = %s", (user_id,))
            return bool(cur.fetchone())
    except Exception as e:
        logger.error(f"Admin check error: {e}")
        return False
    finally:
        db_pool.putconn(conn)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Support System\nChoose an option:",
        reply_markup=MAIN_KEYBOARD,
    )


async def clear_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    msg_ids = context.user_data.get("messages", [])
    for msg_id in list(msg_ids):
        try:
            await context.bot.delete_message(chat_id, msg_id)
        except Exception as e:
            # Message might be already deleted; log at debug to reduce noise
            logger.debug(f"Delete message error: {e}")
    context.user_data["messages"] = []


async def get_pending_requests(page: int = 0, limit: int = REQUESTS_PAGE_SIZE):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, user_id, link_id, channel_name, channel_id, user_name, date
                FROM requests
                WHERE locked = FALSE
                ORDER BY date ASC
                LIMIT %s OFFSET %s
                """,
                (limit, page * limit),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        db_pool.putconn(conn)


async def get_pending_requests_count() -> int:
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM requests WHERE locked = FALSE")
            return int(cur.fetchone()[0])
    finally:
        db_pool.putconn(conn)


async def handle_show_requests(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    if not await is_admin(update.effective_user.id):
        await _msg(update).reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    chat_id = update.effective_chat.id
    await clear_chat(context, chat_id)

    try:
        reqs = await get_pending_requests(page=page)
        if not reqs:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="📭 No pending requests!",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data["messages"] = [msg.message_id]
            return

        message_ids = []
        for r in reqs:
            action_kb = InlineKeyboardMarkup(
                [[
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{r['id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"reject_{r['id']}"),
                ]]
            )

            dt = r.get("date")
            dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt)

            text_msg = (
                f"📌 {r['channel_name']}\n"
                f"👤 {r['user_name']} (ID: {r['user_id']})\n"
                f"🔗 Link ID: {r['link_id']}\n"
                f"🆔 Channel ID: {r['channel_id']}\n"
                f"🕒 {dt_str}\n"
                f"📝 User confirmed subscription (no screenshot)."
            )

            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text_msg,
                reply_markup=action_kb,
            )
            message_ids.append(msg.message_id)

        total = await get_pending_requests_count()
        total_pages = max(1, (total + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE)

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⏪ Previous", callback_data=f"page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ⏩", callback_data=f"page_{page+1}"))

        if nav_buttons:
            nav_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 Page {page+1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav_buttons]),
            )
            message_ids.append(nav_msg.message_id)

        # Keep also the menu prompt message tracked so it can be cleared next refresh
        prompt_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Choose next action:",
            reply_markup=MAIN_KEYBOARD,
        )
        message_ids.append(prompt_msg.message_id)

        context.user_data.update({"messages": message_ids, "current_page": page})

    except Exception as e:
        logger.error(f"Show requests error: {e}")
        await show_menu(update, context)


async def _reserve_request(cur, request_id: int):
    cur.execute(
        """
        UPDATE requests
        SET locked = TRUE
        WHERE id = %s AND locked = FALSE
        RETURNING user_id, link_id, channel_name, channel_id
        """,
        (request_id,),
    )
    return cur.fetchone()


async def handle_approval(cur, user_id: int, link_id: int, channel_id: str, channel_name: str):
    # 1) Remove any previous block mark for this link/user
    cur.execute(
        "DELETE FROM users_block WHERE link_id = %s AND telegram_id = %s",
        (link_id, user_id),
    )

    # 2) Update likes counters
    cur.execute(
        "UPDATE likes SET channel_likes = channel_likes + 1 WHERE id = %s",
        (link_id,),
    )

    cur.execute(
        "SELECT channel_likes, subscription_count FROM likes WHERE id = %s",
        (link_id,),
    )
    result = cur.fetchone()
    if result and result[0] >= result[1]:
        cur.execute("DELETE FROM links WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users_block WHERE link_id = %s", (link_id,))
        cur.execute("UPDATE likes SET status = TRUE WHERE id = %s", (link_id,))

    # 3) Credit points immediately, but prevent double-crediting
    cur.execute(
        """
        SELECT points_status
        FROM user_link_status
        WHERE telegram_id = %s AND link_id = %s AND channel_id = %s
        LIMIT 1
        """,
        (user_id, link_id, channel_id),
    )
    row = cur.fetchone()
    already_credited = bool(row and row[0])

    if not already_credited:
        cur.execute(
            "UPDATE users SET points = points + 1 WHERE telegram_id = %s",
            (user_id,),
        )

    # 4) Ensure user_link_status exists and marked as credited + processed
    cur.execute("SELECT full_name FROM users WHERE telegram_id = %s", (user_id,))
    user_full_name_row = cur.fetchone()
    user_full_name = user_full_name_row[0] if user_full_name_row else str(user_id)

    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(
        """
        INSERT INTO user_link_status
            (telegram_id, user_name, channel_name, link_id, channel_id, date_mation, processed, points_status)
        VALUES (%s, %s, %s, %s, %s, %s, 1, TRUE)
        ON CONFLICT (telegram_id, link_id, channel_id)
        DO UPDATE SET processed = 1, points_status = TRUE
        """,
        (user_id, user_full_name, channel_name, link_id, channel_id, date_now),
    )


async def handle_rejection(cur, user_id: int, link_id: int, channel_name: str):
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(
        """
        UPDATE users
        SET block_num = block_num + 1, date_block = %s
        WHERE telegram_id = %s
        """,
        (date_now, user_id),
    )

    cur.execute("SELECT full_name FROM users WHERE telegram_id = %s", (user_id,))
    user_name_row = cur.fetchone()
    user_name = user_name_row[0] if user_name_row else str(user_id)

    cur.execute(
        """
        INSERT INTO users_block (telegram_id, user_name, channel_name, link_id, block_num)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (user_id, user_name, channel_name, link_id, 1),
    )

    cur.execute("DELETE FROM user_link_status WHERE telegram_id = %s AND link_id = %s", (user_id, link_id))
    cur.execute("UPDATE links SET allow_link = allow_link + 1 WHERE id = %s", (link_id,))


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await is_admin(query.from_user.id):
        await query.message.reply_text("⛔ Access Expired", reply_markup=MAIN_KEYBOARD)
        return

    data = query.data

    try:
        if data.startswith("page_"):
            new_page = int(data.split("_", 1)[1])
            await handle_show_requests(update, context, new_page)
            return

        if data.startswith("support_page_"):
            new_page = int(data.split("_", 2)[2])
            await handle_support_refresh(update, context, new_page)
            return

        if data.startswith(("approve_", "reject_")):
            action, request_id_str = data.split("_", 1)
            request_id = int(request_id_str)

            conn = db_pool.getconn()
            try:
                conn.autocommit = False
                with conn.cursor() as cur:
                    reserved = await _reserve_request(cur, request_id)
                    if not reserved:
                        conn.rollback()
                        await context.bot.send_message(
                            chat_id=query.message.chat.id,
                            text="⏳ This request is already being handled or was processed.",
                            reply_markup=MAIN_KEYBOARD,
                        )
                        return

                    user_id, link_id, channel_name, channel_id = reserved

                    if action == "approve":
                        await handle_approval(cur, user_id, link_id, channel_id, channel_name)
                        result_text = "✅ Approved (points added) and removed."
                    else:
                        admins_id = [7168120805, 1130152311, 6106281772]
                        if user_id not in admins_id:
                            await handle_rejection(cur, user_id, link_id, channel_name)
                        result_text = "❌ Rejected and removed."

                    cur.execute("DELETE FROM requests WHERE id = %s", (request_id,))
                    conn.commit()

                await context.bot.send_message(
                    chat_id=query.message.chat.id,
                    text=result_text,
                    reply_markup=MAIN_KEYBOARD,
                )
            except Exception as e:
                conn.rollback()
                logger.error(f"Approve/Reject error: {e}")
                await context.bot.send_message(
                    chat_id=query.message.chat.id,
                    text="⚠️ DB error while processing request.",
                    reply_markup=MAIN_KEYBOARD,
                )
            finally:
                db_pool.putconn(conn)

            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if data.startswith("confirm_"):
            request_id = int(data.split("_", 1)[1])
            admin_name = query.from_user.full_name

            conn = db_pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM support WHERE id = %s", (request_id,))
                    conn.commit()
            finally:
                db_pool.putconn(conn)

            try:
                await query.message.delete()
            except Exception:
                pass

            await context.bot.send_message(
                chat_id=query.message.chat.id,
                text=f"✅ Request #{request_id} confirmed by {admin_name}",
                reply_markup=MAIN_KEYBOARD,
            )
            return

    except Exception as e:
        logger.error(f"Button handler error: {e}")
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text="Please choose an option:",
            reply_markup=MAIN_KEYBOARD,
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        await _msg(update).reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    text = update.message.text
    if text == "📋 Show Requests":
        await handle_show_requests(update, context, page=0)
    elif text == "🆕 Refresh Support":
        await clear_chat(context, update.effective_chat.id)
        await handle_support_refresh(update, context, page=0)
    else:
        await update.message.reply_text("Please use the menu buttons below:", reply_markup=MAIN_KEYBOARD)


async def get_pending_support_requests(page: int = 0, limit: int = SUPPORT_PAGE_SIZE):
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, telegram_id, message, user_name, message_date, email, who_is
                FROM support
                ORDER BY message_date ASC
                LIMIT %s OFFSET %s
                """,
                (limit, page * limit),
            )
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        db_pool.putconn(conn)


async def get_pending_support_count() -> int:
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM support")
            return int(cur.fetchone()[0])
    finally:
        db_pool.putconn(conn)


async def handle_support_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    if not await is_admin(update.effective_user.id):
        await _msg(update).reply_text("⛔ Access Denied", reply_markup=MAIN_KEYBOARD)
        return

    chat_id = update.effective_chat.id
    await clear_chat(context, chat_id)

    try:
        reqs = await get_pending_support_requests(page=page)
        if not reqs:
            msg = await context.bot.send_message(
                chat_id=chat_id,
                text="📭 No pending support requests!",
                reply_markup=MAIN_KEYBOARD,
            )
            context.user_data["messages"] = [msg.message_id]
            return

        message_ids = []
        for r in reqs:
            dt = r.get("message_date")
            dt_str = dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt)

            text = (
                f"📨 Request #{r['id']}\n"
                f"👤 User: {r['user_name']} (ID: {r['telegram_id']})\n"
                f"👤 Who IS: {r['who_is']}\n"
                f"📧 Email: {r['email']}\n"
                f"📆 Date: {dt_str}\n"
                f"✉️ Message: {r['message']}"
            )

            msg = await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Confirm", callback_data=f"confirm_{r['id']}")]]
                ),
            )
            message_ids.append(msg.message_id)

        total = await get_pending_support_count()
        total_pages = max(1, (total + SUPPORT_PAGE_SIZE - 1) // SUPPORT_PAGE_SIZE)

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⏪ Previous", callback_data=f"support_page_{page-1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton("Next ⏩", callback_data=f"support_page_{page+1}"))

        if nav_buttons:
            nav_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 Support Page {page+1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav_buttons]),
            )
            message_ids.append(nav_msg.message_id)

        prompt_msg = await context.bot.send_message(
            chat_id=chat_id,
            text="Choose next action:",
            reply_markup=MAIN_KEYBOARD,
        )
        message_ids.append(prompt_msg.message_id)

        context.user_data["messages"] = message_ids

    except Exception as e:
        logger.error(f"Support refresh error: {e}")
        await show_menu(update, context)


def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", show_menu))
    application.add_handler(CallbackQueryHandler(handle_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    application.run_polling()


if __name__ == "__main__":
    try:
        main()
    finally:
        db_pool.closeall()
