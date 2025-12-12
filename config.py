# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# DB
DB_PATH = os.getenv("DB_PATH", "lemon_lotto.db")

# Blink (Lightning)
BLINK_API_URL = os.getenv("BLINK_API_URL", "https://api.blink.sv/graphql").rstrip("/")
BLINK_API_KEY = os.getenv("BLINK_API_KEY", "")
BLINK_WALLET_ID = os.getenv("BLINK_WALLET_ID", "")

if not BLINK_API_URL:
    print("[WARN] BLINK_API_URL 가 설정되지 않았습니다.")
if not BLINK_API_KEY:
    print("[WARN] BLINK_API_KEY 가 설정되지 않았습니다.")
if not BLINK_WALLET_ID:
    print("[WARN] BLINK_WALLET_ID 가 설정되지 않았습니다.")
