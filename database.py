""" ali """
import sqlite3
import psycopg2
import config

DB_PATH = "bot_base.db"

def connect_db():
    """Returns a connection to the PostgreSQL database."""
    return psycopg2.connect(config.DATABASE_URL)

# def setup_database():
#     """ ali """
#     with connect_db() as conn:
#         cursor = conn.cursor()
#         cursor.execute("""
#             CREATE TABLE IF NOT EXISTS users (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 telegram_id INTEGER UNIQUE,
#                 full_name TEXT,
#                 email TEXT,
#                 points INTEGER DEFAULT 0
#             );

#             CREATE TABLE IF NOT EXISTS links (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 youtube_link TEXT,
#                 description TEXT,
#                 added_by INTEGER
#             );

#             CREATE TABLE IF NOT EXISTS uploaded_images (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 user_id INTEGER,
#                 image_path TEXT,
#                 linked_link TEXT,
#                 FOREIGN KEY(user_id) REFERENCES users(telegram_id)
#             );
            
#                 CREATE TABLE IF NOT EXISTS user_link_status (
#                 id INTEGER PRIMARY KEY AUTOINCREMENT,
#                 telegram_id INTEGER,
#                 link_id INTEGER,
#                 processed INTEGER DEFAULT 0,
#                 UNIQUE(telegram_id, link_id)
#             );
            
#                 CREATE TABLE IF NOT EXISTS authorized_link_adders (
#                 telegram_id INTEGER PRIMARY KEY,
#                 full_name TEXT,
#                 email TEXT,
#                 added_by INTEGER
#             )
#         """)
#         conn.commit()

def add_user(telegram_id, full_name, email):
    """ ali """
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (telegram_id, full_name, email) VALUES (%s, %s, %s)", 
                        (telegram_id, full_name, email))
            conn.commit()
    except psycopg2.Error as e:
        # Catch any SQLite errors and print them
        print(f"Database error occurred: {e}")


def add_link(channel_id, channel_name, admin_id):
    """ ali """
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO likes (channel_id, channel_name) VALUES (%s, %s)",
                       (channel_id, channel_name, admin_id))
        conn.commit()

def get_links():
    """ ali """
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, channel_name FROM likes")
        return cursor.fetchall()

def add_points(user_id, points=1):
    """ ali """
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET points = points + %s WHERE telegram_id = %s",
                       (points, user_id))
        conn.commit()



def get_links_for_user(user_id):
    """Fetches all YouTube links for a specific user."""
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT channel_id, channel_name FROM likes WHERE user_id = %s", (user_id,))
        return cursor.fetchall()  # Returns a list of (link, description) tuples


