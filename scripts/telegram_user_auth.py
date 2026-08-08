"""Two-step Telegram user-account login, driveable from the CLI.

Normal interactive flow (user runs in their own terminal):

    python scripts/telegram_user_auth.py

Scripted flow (values via env vars — what Claude uses):

    # Step 1: request code
    TELEGRAM_PHONE=+15125551234 python scripts/telegram_user_auth.py

    # Step 2: verify code
    TELEGRAM_PHONE=+15125551234 TELEGRAM_CODE=12345 \\
        python scripts/telegram_user_auth.py

    # Step 3 (only if 2FA is on):
    TELEGRAM_PASSWORD=hunter2 python scripts/telegram_user_auth.py

On success a session file is written to ~/.polysim/telegram.session.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from polysim.config import load_secrets

SESSION_DIR = Path.home() / ".polysim"
SESSION_PATH = SESSION_DIR / "telegram.session"
# Transient hash file so step 2 (verify code) can inherit the
# phone_code_hash from step 1 (send code). Deleted on successful sign-in.
HASH_PATH = SESSION_DIR / "telegram_auth_pending.json"


async def main() -> int:
    secrets = load_secrets()
    if not secrets.TELEGRAM_API_ID or not secrets.TELEGRAM_API_HASH:
        print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env")
        print("       See https://my.telegram.org/apps")
        return 2

    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from telethon import TelegramClient
        from telethon.errors import (
            PhoneCodeInvalidError,
            SessionPasswordNeededError,
        )
    except ImportError:
        print("ERROR: telethon not installed.")
        return 2

    client = TelegramClient(
        str(SESSION_PATH),
        int(secrets.TELEGRAM_API_ID),
        secrets.TELEGRAM_API_HASH,
    )

    phone = os.environ.get("TELEGRAM_PHONE", "").strip()
    code = os.environ.get("TELEGRAM_CODE", "").strip()
    password = os.environ.get("TELEGRAM_PASSWORD", "").strip()
    scripted = bool(phone or code or password)

    if scripted:
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"OK already logged in as @{me.username or me.id}")
                print(f"   session={SESSION_PATH}")
                return 0

            # Step 1: send the code
            if phone and not code and not password:
                sent = await client.send_code_request(phone)
                HASH_PATH.write_text(
                    json.dumps({"phone": phone, "phone_code_hash": sent.phone_code_hash}),
                    encoding="utf-8",
                )
                print(f"OK code sent to {phone}")
                print(f"   hash-stash -> {HASH_PATH}")
                print("   Check Telegram for a 5-digit code, then re-run with TELEGRAM_CODE.")
                return 0

            # Step 2: verify the code — read stashed phone_code_hash.
            if code:
                if not HASH_PATH.exists():
                    print("ERROR: no pending auth; run with TELEGRAM_PHONE first.")
                    return 2
                stash = json.loads(HASH_PATH.read_text(encoding="utf-8"))
                effective_phone = phone or stash.get("phone") or ""
                code_hash = stash.get("phone_code_hash") or ""
                try:
                    await client.sign_in(
                        phone=effective_phone, code=code,
                        phone_code_hash=code_hash,
                    )
                except SessionPasswordNeededError:
                    print("2FA password required; re-run with TELEGRAM_PASSWORD set.")
                    return 3
                except PhoneCodeInvalidError:
                    print("ERROR: code invalid or expired — request a new one.")
                    return 4
                me = await client.get_me()
                print(f"OK signed in as @{me.username or me.id}")
                print(f"   session={SESSION_PATH}")
                HASH_PATH.unlink(missing_ok=True)
                return 0

            # Step 3: supply password (if 2FA tripped us in step 2)
            if password:
                await client.sign_in(password=password)
                me = await client.get_me()
                print(f"OK signed in (2FA) as @{me.username or me.id}")
                print(f"   session={SESSION_PATH}")
                HASH_PATH.unlink(missing_ok=True)
                return 0

            print("ERROR: scripted mode needs TELEGRAM_PHONE first.")
            return 2
        finally:
            await client.disconnect()

    # Interactive fallback — tty prompts for phone, code, password.
    async with client:
        me = await client.get_me()
        print(f"OK logged in as @{me.username or me.id}  (session={SESSION_PATH})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
