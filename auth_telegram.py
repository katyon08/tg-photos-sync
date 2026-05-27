#!/usr/bin/env python3
"""
Two-step Telegram auth (no PTY needed):
  Step 1: python auth_telegram.py --request   → sends OTP to your phone
  Step 2: python auth_telegram.py --code 12345 → completes sign-in
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TG_API_ID   = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_PHONE    = os.environ["TG_PHONE"]
SESSION     = str(BASE_DIR / "tg")
HASH_FILE   = BASE_DIR / ".phone_code_hash"


async def request_code():
    client = TelegramClient(SESSION, TG_API_ID, TG_API_HASH)
    await client.connect()
    result = await client.send_code_request(TG_PHONE)
    HASH_FILE.write_text(json.dumps({"phone_code_hash": result.phone_code_hash}))
    print(f"OTP sent to {TG_PHONE}. Check Telegram.")
    print(f"Now run: python auth_telegram.py --code YOUR_CODE")
    await client.disconnect()


async def sign_in(code: str):
    if not HASH_FILE.exists():
        sys.exit("Run --request first to get the OTP.")
    data = json.loads(HASH_FILE.read_text())
    phone_code_hash = data["phone_code_hash"]

    client = TelegramClient(SESSION, TG_API_ID, TG_API_HASH)
    await client.connect()
    try:
        await client.sign_in(TG_PHONE, code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        sys.exit("2FA password required — not supported in this flow.")

    me = await client.get_me()
    print(f"Telegram auth OK: {me.first_name} (@{me.username})")
    print(f"Session saved: {SESSION}.session")
    HASH_FILE.unlink(missing_ok=True)
    await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--request", action="store_true", help="Send OTP to phone")
    group.add_argument("--code", help="Sign in with received OTP code")
    args = parser.parse_args()

    if args.request:
        asyncio.run(request_code())
    else:
        asyncio.run(sign_in(args.code))
