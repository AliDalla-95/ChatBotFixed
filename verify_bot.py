import os
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
import config

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("verify_bot")

# --- Config ---
DATABASE_URL = getattr(config, "DATABASE_URL", None) or os.getenv("DATABASE_URL")
VERIFY_BOT_TOKEN = (
    getattr(config, "VERIFY_BOT_TOKEN", None)
    or os.getenv("VERIFY_BOT_TOKEN")
)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing in config.py or environment.")
if not VERIFY_BOT_TOKEN:
    raise RuntimeError("VERIFY_BOT_TOKEN is missing in config.py or environment.")

# --- DB Pool ---
db_pool = psycopg2.pool.SimpleConnectionPool(1, 10, DATABASE_URL)

PAGE_SIZE = 5

MAIN_KB = ReplyKeyboardMarkup(
    [["📋 Pending Activations", "🆕 Refresh"]],
    resize_keyboard=True,
    one_time_keyboard=False,
)


def _msg(update: Update):
    return update.effective_message


async def is_support_admin(user_id: int) -> bool:
    """Allow only Support Admins (support_admins table)."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM support_admins WHERE telegram_id=%s", (user_id,))
            return bool(cur.fetchone())
    except Exception as e:
        logger.error(f"Support-admin check error: {e}")
        return False
    finally:
        db_pool.putconn(conn)


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="✅ Verify Bot\nChoose an option:",
        reply_markup=MAIN_KB,
    )


async def clear_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    msg_ids = context.user_data.get("messages", [])
    for mid in list(msg_ids):
        try:
            await context.bot.delete_message(chat_id, mid)
        except Exception:
            pass
    context.user_data["messages"] = []


async def get_pending_users(page: int = 0, limit: int = PAGE_SIZE):
    """Users waiting verification."""
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    telegram_id,
                    full_name,
                    email,
                    phone,
                    country,
                    registration_date,
                    cash_number,
                    facebook_username,
                    instagram_username
                FROM users
                WHERE is_banned = FALSE
                  AND COALESCE(is_verified, FALSE) = FALSE
                  AND COALESCE(verification_pending, TRUE) = TRUE
                ORDER BY registration_date ASC
                LIMIT %s OFFSET %s
                """,
                (limit, page * limit),
            )
            rows = cur.fetchall()
            return rows
    finally:
        db_pool.putconn(conn)


async def get_pending_users_count() -> int:
    conn = db_pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM users
                WHERE is_banned = FALSE
                  AND COALESCE(is_verified, FALSE) = FALSE
                  AND COALESCE(verification_pending, TRUE) = TRUE
                """
            )
            return int(cur.fetchone()[0])
    finally:
        db_pool.putconn(conn)


def _safe(v):
    return v if v is not None and str(v).strip() else "N/A"


async def handle_show_pending(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    uid = update.effective_user.id
    if not await is_support_admin(uid):
        await _msg(update).reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return

    chat_id = update.effective_chat.id
    await clear_chat(context, chat_id)

    try:
        rows = await get_pending_users(page=page)
        if not rows:
            m = await context.bot.send_message(chat_id, "📭 No pending activations.", reply_markup=MAIN_KB)
            context.user_data["messages"] = [m.message_id]
            return

        message_ids = []
        for (
            telegram_id, full_name, email, phone, country, reg_date,
            cash_number, fb, ig
        ) in rows:
            reg_str = reg_date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(reg_date, "strftime") else str(reg_date)

            text = (
                f"🧾 Activation Request\n"
                f"👤 Name: {_safe(full_name)}\n"
                f"🆔 ID: {telegram_id}\n"
                f"📧 Email: {_safe(email)}\n"
                f"📱 Phone: {_safe(phone)}\n"
                f"🌍 Country: {_safe(country)}\n"
                f"💳 Cash: {_safe(cash_number)}\n"
                f"📘 Facebook: {_safe(fb)}\n"
                f"📸 Instagram: {_safe(ig)}\n"
                f"🕒 Registered: {reg_str}\n\n"
                f"✅ Verify if this account is real, then approve/reject."
            )

            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{telegram_id}"),
                InlineKeyboardButton("❌ Reject (Ban)", callback_data=f"reject_{telegram_id}"),
            ]])

            msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            message_ids.append(msg.message_id)

        total = await get_pending_users_count()
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⏪ Prev", callback_data=f"page_{page-1}"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ⏩", callback_data=f"page_{page+1}"))

        if nav:
            nav_msg = await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 Page {page+1}/{total_pages}",
                reply_markup=InlineKeyboardMarkup([nav]),
            )
            message_ids.append(nav_msg.message_id)

        context.user_data["messages"] = message_ids
        await context.bot.send_message(chat_id=chat_id, text="Choose next action:", reply_markup=MAIN_KB)

    except Exception as e:
        logger.error(f"Show pending error: {e}")
        await show_menu(update, context)


async def approve_user(telegram_id: int, admin_id: int, admin_name: str) -> bool:
    conn = db_pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET is_verified = TRUE,
                    verification_pending = FALSE,
                    verified_by = %s
                    verified_at = NOW()
                WHERE telegram_id = %s
                  AND is_banned = FALSE
                  AND COALESCE(is_verified, FALSE) = FALSE
                  AND COALESCE(verification_pending, TRUE) = TRUE
                """,
                (admin_name, telegram_id),
            )

            if cur.rowcount != 1:
                conn.rollback()
                return False

            cur.execute(
                """
                INSERT INTO support (telegram_id, message, user_name, message_date, checks, email, who_is, admin_name)
                VALUES (%s, %s, %s, %s, TRUE, NULL, %s, %s)
                """,
                (
                    telegram_id,
                    "Account verification APPROVED",
                    str(telegram_id),
                    datetime.now(),
                    "verify",
                    admin_name,
                ),
            )

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Approve DB error: {e}")
        return False
    finally:
        db_pool.putconn(conn)


async def reject_user_ban(telegram_id: int, admin_id: int, admin_name: str) -> bool:
    conn = db_pool.getconn()
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET is_banned = TRUE,
                    verification_pending = FALSE,
                    verified_by = %s,
                    date_block = NOW()
                WHERE telegram_id = %s
                  AND is_banned = FALSE
                  AND COALESCE(is_verified, FALSE) = FALSE
                  AND COALESCE(verification_pending, TRUE) = TRUE
                """,
                (admin_name, telegram_id),
            )

            if cur.rowcount != 1:
                conn.rollback()
                return False

            cur.execute(
                """
                INSERT INTO support (telegram_id, message, user_name, message_date, checks, email, who_is, admin_name)
                VALUES (%s, %s, %s, %s, TRUE, NULL, %s, %s)
                """,
                (
                    telegram_id,
                    "Account verification REJECTED => BANNED",
                    str(telegram_id),
                    datetime.now(),
                    "verify",
                    admin_name,
                ),
            )

        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f"Reject DB error: {e}")
        return False
    finally:
        db_pool.putconn(conn)



async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    uid = query.from_user.id
    if not await is_support_admin(uid):
        await query.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return

    data = query.data
    admin_name = query.from_user.full_name

    try:
        if data.startswith("page_"):
            page = int(data.split("_", 1)[1])
            await handle_show_pending(update, context, page=page)
            return

        if data.startswith("approve_"):
            telegram_id = int(data.split("_", 1)[1])
            ok = await approve_user(telegram_id, uid, admin_name)
            if ok:
                await context.bot.send_message(query.message.chat_id, f"✅ Approved user {telegram_id}", reply_markup=MAIN_KB)
            else:
                await context.bot.send_message(query.message.chat_id, "⏳ Already processed / not found.", reply_markup=MAIN_KB)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

        if data.startswith("reject_"):
            telegram_id = int(data.split("_", 1)[1])
            ok = await reject_user_ban(telegram_id, uid, admin_name)
            if ok:
                await context.bot.send_message(query.message.chat_id, f"❌ Rejected & banned user {telegram_id}", reply_markup=MAIN_KB)
            else:
                await context.bot.send_message(query.message.chat_id, "⏳ Already processed / not found.", reply_markup=MAIN_KB)
            try:
                await query.message.delete()
            except Exception:
                pass
            return

    except Exception as e:
        logger.error(f"Callback error: {e}")
        await context.bot.send_message(
            query.message.chat_id,
            "⚠️ Error. Try again from menu.",
            reply_markup=MAIN_KB,
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await is_support_admin(uid):
        await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return

    text = (update.message.text or "").strip()
    if text in ["📋 Pending Activations", "🆕 Refresh"]:
        await handle_show_pending(update, context, page=0)
        return

    await update.message.reply_text("Use the menu buttons below:", reply_markup=MAIN_KB)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await is_support_admin(uid):
        await update.message.reply_text("⛔ غير مصرح لك باستخدام هذا البوت.")
        return
    await show_menu(update, context)


def main():
    app = ApplicationBuilder().token(VERIFY_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Verify bot started...")
    app.run_polling()


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            db_pool.closeall()
        except Exception:
            pass
