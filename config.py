"""
Configuration file for the Telegram bot.

This file contains API keys, bot tokens, and other global settings.
"""
import os

TOKEN = "8148810093:AAGIsDDPbtHLbi7B5W3MpgN4aTKAKgu0gVI"
ADMIN_IDS = [6106281772, 6106281772]  # Replace with actual Telegram user IDs of admins
DB_PATH = "bot_base.db"

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/Test"

# Email Configuration
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USERNAME = 'ironm2249@gmail.com'  # Your actual email
SMTP_PASSWORD = 'bevu ggwh ohmp eihh '    # The 16-digit app password
EMAIL_FROM = 'ironm2249@gmail.com'    # Same as username