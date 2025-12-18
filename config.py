"""
Configuration file for the Telegram bot.

This file contains API keys, bot tokens, and other global settings.
"""

TOKEN = "8078121735:AAFN_wdDGW16o8EMaxqFSyuBV2rzaZZjXtI"
ADMIN_IDS = [6106281772, 6106281772]  # Replace with actual Telegram user IDs of admins

DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/Test"
TEST2_DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/Test"

# Email Configuration
SMTP_SERVER = 'smtp.gmail.com'
SMTP_PORT = 587
SMTP_USERNAME = 'ironm2249@gmail.com'  # Your actual email
SMTP_PASSWORD = 'bevu ggwh ohmp eihh '    # The 16-digit app password
EMAIL_FROM = 'ironm2249@gmail.com'    # Same as username

DATABASE_CONFIG = "postgresql://postgres:postgres@localhost:5432/Test2"


BOT_TOKEN = "7555220650:AAEHsC6R0HM44dQ7-1uv_xsfGcYc-KWCWy4"



VERIFY_BOT_TOKEN = "8506009760:AAGmNjlNM5mRdROIrQuVzCsA6gDqBIJVae8"
# ===== Tokens for each bot (recommended: move to ENV vars) =====
# User bot:
USER_BOT_TOKEN = TOKEN

# Support bot:
SUPPORT_BOT_TOKEN = BOT_TOKEN

# Verify bot:
VERIFY_BOT_TOKEN_ALIAS = VERIFY_BOT_TOKEN

# Client/Admin bot:
CLIENT_BOT_TOKEN = "7861338140:AAG3w1f7UBcwKpdYh0ipfLB3nMZM3sLasP4"

# Send money / withdrawals bot:
SEND_MONEY_BOT_TOKEN = "8062800182:AAGwnhGinAaa-0oM2El2KMuuf3fu17Mbl_E"

# Client paid bot:
CLIENT_PAID_BOT_TOKEN = "7328995633:AAF4pY4xlW68RhfX43wJ3AJXfUITKpe0q8s"

# Optional: a single mapping if you prefer
BOT_TOKENS = {
    "User": USER_BOT_TOKEN,
    "Support": SUPPORT_BOT_TOKEN,
    "Verify": VERIFY_BOT_TOKEN,
    "Client": CLIENT_BOT_TOKEN,
    "SendMoney": SEND_MONEY_BOT_TOKEN,
    "ClientPaid": CLIENT_PAID_BOT_TOKEN,
}
