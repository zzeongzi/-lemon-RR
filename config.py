# config.py
import os
from dotenv import load_dotenv

load_dotenv()

# ------------ Discord Token ------------
_DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if _DISCORD_TOKEN is None:
    raise RuntimeError("DISCORD_TOKEN is not set in .env")
DISCORD_TOKEN: str = _DISCORD_TOKEN

# ------------ Cashu / Minibits Mint ------------
_raw_mint_url = os.getenv("CASHU_MINT_URL")
if _raw_mint_url is None:
    raise RuntimeError("CASHU_MINT_URL is not set in .env")

CASHU_MINT_URL: str = _raw_mint_url.rstrip("/")
CASHU_MINT_KEYSETS_ENDPOINT: str = os.getenv("CASHU_MINT_KEYSETS_ENDPOINT", "/keys")
CASHU_MINT_REDEEM_ENDPOINT: str = os.getenv("CASHU_MINT_REDEEM_ENDPOINT", "/redeem")
CASHU_MINT_MINT_ENDPOINT: str = os.getenv("CASHU_MINT_MINT_ENDPOINT", "/mint")
