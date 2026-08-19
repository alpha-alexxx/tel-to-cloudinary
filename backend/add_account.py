"""Headless account sign-in — the CLI equivalent of the browser login.

    cd backend && python add_account.py

The dashboard's own OTP flow is the normal path. Use this when you can't reach
the UI (a server with no port forwarding, a first-run setup script, or a
recovery situation). It performs the same phone → code → 2FA flow and writes
the resulting session into the same SQLite database the app reads.
"""

from __future__ import annotations

import asyncio
import getpass
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from config import ConfigError, load_settings
from db import Database


async def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return 1

    phone = input("Phone number in international format (+91…): ").strip()
    if not phone.startswith("+"):
        print("The number must start with a country code, e.g. +919876543210.", file=sys.stderr)
        return 1

    client = TelegramClient(StringSession(), settings.tg_api_id, settings.tg_api_hash)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        code = input("Login code Telegram just sent: ").strip()
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass.getpass("Two-step verification password: ")
            await client.sign_in(password=password)

        me = await client.get_me()
        display_name = (
            f"{me.first_name or ''} {me.last_name or ''}".strip()
            or me.username
            or str(me.id)
        )

        db = Database(settings.db_path)
        db.connect()
        try:
            account = await db.upsert_account(
                tg_user_id=me.id,
                phone=me.phone or phone,
                display_name=display_name,
                username=me.username,
                session_string=client.session.save(),
            )
        finally:
            db.close()

        print(f"\nSaved account #{account['id']}: {display_name}")
        print("Open the dashboard, unlock it, and pick this account from the list.")
        print("Add its Cloudinary target there — credentials are per account.")
        return 0
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
