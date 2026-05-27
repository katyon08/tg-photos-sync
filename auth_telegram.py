#!/usr/bin/env python3
"""Interactive Telegram auth — run once via: ssh -t ubuntu@VM python auth_telegram.py"""
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TG_API_ID   = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_PHONE    = os.environ["TG_PHONE"]
SESSION     = str(BASE_DIR / "tg")


async def main():
    client = TelegramClient(SESSION, TG_API_ID, TG_API_HASH)
    await client.start(phone=TG_PHONE)
    me = await client.get_me()
    print(f"\nTelegram auth OK: {me.first_name} (@{me.username})")
    print(f"Session saved to: {SESSION}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
