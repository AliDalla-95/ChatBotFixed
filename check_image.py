import psycopg2
import time
from psycopg2 import OperationalError
import config
import scan_image
from datetime import datetime, timedelta
import os

def connect_db():
    """Create and return a PostgreSQL database connection"""
    try:
        return psycopg2.connect(config.DATABASE_URL)
    except psycopg2.Error as e:
        print(f"Database connection failed: {e}")
        raise
    
    
def main():
    while True:
        conn = None
        try:
            conn = psycopg2.connect(config.TEST2_DATABASE_URL)
            conn.autocommit = False

            with conn.cursor() as cur:
                # Modified SELECT query with WHERE locked = False
                cur.execute("""
                    SELECT id, image_path, channel_name, user_id, link_id 
                    FROM images 
                    WHERE locked = False 
                    ORDER BY date ASC 
                    LIMIT 1 
                    FOR UPDATE SKIP LOCKED
                """)
                row = cur.fetchone()

                if row:
                    image_id, image_path, channel_name, user_id, link_id = row
                    print(f"Processing image {image_id}...")

                    try:
                        # Process the image
                        result = scan_image.check_text_in_image(image_path, channel_name)
                        # Handle results after cleanup
                        if result:
                            update_likes(user_id, link_id)
                            print("True")
                            try:
                                with conn.cursor() as del_cur:
                                    del_cur.execute("""
                                        DELETE FROM images 
                                        WHERE id = %s
                                    """, (image_id,))
                                    conn.commit()
                            except Exception as e:
                                print(f"Database deletion error: {str(e)}")
                                                    # Always attempt cleanup
                            try:
                                if os.path.exists(image_path):
                                    os.remove(image_path)
                                    print(f"Deleted file: {image_path}")
                                else:
                                    try:
                                        with conn.cursor() as del_cur:
                                            del_cur.execute("""
                                                DELETE FROM images 
                                                WHERE id = %s
                                            """, (image_id,))
                                            conn.commit()
                                    except Exception as e:
                                        print(f"Database deletion error: {str(e)}")
                            except Exception as e:
                                print(f"File deletion error: {str(e)}")
                        else:
                            print("False")

                            # 6936321897
                            # admins_id = [7168120805, 6106281772, 1130152311]

                            # # Check if user is NOT in admin list
                            # if user_id not in admins_id:
                            #     block_add(user_id, channel_name, link_id)
                            # mark_link_processed(user_id, link_id)
                            # allow_links(link_id)

                        # UPDATE locked = True
                        try:
                            with conn.cursor() as update_cur:
                                update_cur.execute("""
                                    UPDATE images 
                                    SET locked = True 
                                    WHERE id = %s
                                """, (image_id,))
                                conn.commit()
                        except Exception as e:
                            print(f"Database update error: {str(e)}")
                            conn.rollback()
                    
                    except Exception as e:
                        print(f"Error during processing: {str(e)}")
                        result = False
                        # try:
                        #     with conn.cursor() as update_cur:
                        #         update_cur.execute("""
                        #             UPDATE images 
                        #             SET locked = True 
                        #             WHERE id = %s
                        #         """, (image_id,))
                        #         conn.commit()
                        # except Exception as e:
                        #     print(f"Database update error: {str(e)}")
                        #     conn.rollback()
                        conn.rollback()  # Rollback to keep row unlocked if processing fails
                    finally:
                            print(f"Finished")
                else:
                    print("No images to process. Sleeping...")
                    time.sleep(5)

        except OperationalError as e:
            print(f"Database connection error: {str(e)}. Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()
            time.sleep(1)



def block_add(user_id: int, channel_name :str, link_id :int):
    """Mark a link as processed for the user"""
    telegram_id = user_id
    channel_name = channel_name
    link_id = link_id
    block_num = 1
    date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE users 
                    SET block_num = block_num + %s, date_block = %s
                    WHERE telegram_id = %s
                """, (1, date_now, telegram_id,))
                cursor.execute(
                    "SELECT full_name FROM users WHERE telegram_id = %s",
                    (telegram_id,)
                )
                user_name = cursor.fetchone()[0]
                cursor.execute("""
                    INSERT INTO users_block (
                        telegram_id, user_name, channel_name, link_id, block_num
                    ) VALUES (%s, %s, %s, %s, %s)
                """, (
                    telegram_id,
                    user_name,
                    channel_name,
                    link_id,
                    block_num
                ))
                conn.commit()
    except Exception as e:
        print(f"Error in update_user_points: {e}")
        conn.rollback()
    finally:
        conn.close()



def update_likes(user_id: int, link_id: int, points: int = 1):
    """Update user's points balance"""
    try:
        with connect_db() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM users_block WHERE link_id = %s AND telegram_id = %s",(link_id, user_id,))            
            cursor.execute("""
            UPDATE likes SET channel_likes = channel_likes + %s
            WHERE id = %s
            """, (1,link_id))
            
            cursor.execute(
                "SELECT channel_likes,subscription_count FROM likes WHERE id = %s",
                (link_id,)
            )
            user_data = cursor.fetchone()            
            # cursor.execute(
            #     "SELECT subscription_count FROM links WHERE id = %s",
            #     (link_id,)
            # )
            # user_data1 = cursor.fetchone()
            if user_data[0] == user_data[1]:
                cursor.execute(
                    "DELETE FROM links WHERE id = %s",
                    (link_id,)
                )
                cursor.execute("DELETE FROM users_block WHERE link_id = %s",(link_id,))            
                cursor.execute("""
                UPDATE likes SET status = %s
                WHERE id = %s
                """, (True,link_id))
                print(f"{link_id}")
            conn.commit()

    except Exception as e:
        print(f"Error in update_likes: {e}")
        conn.rollback()
    finally:
        conn.close()


def mark_link_processed(user_id: int, link_id: int):
    """Mark a link as processed for the user"""
    try:
        telegram_id = user_id
        link = link_id
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM user_link_status WHERE telegram_id = %s And link_id = %s",
                    (telegram_id,link,)
                )
                conn.commit()
    except Exception as e:
        print(f"Error in mark_link_processed: {e}")
        conn.rollback()
    finally:
        conn.close()


def allow_links(link_id: int):
    """Mark a link as processed for the user"""
    try:
        link = link_id
        with connect_db() as conn:
            with conn.cursor() as cursor:
                cursor.execute("""
                    UPDATE links 
                    SET allow_link = allow_link + 1
                    WHERE id = %s
                """, (link,))
                conn.commit()
    except Exception as e:
        print(f"Error in mark_link_processed: {e}")
        conn.rollback()
    finally:
        conn.close()



if __name__ == "__main__":
    main()