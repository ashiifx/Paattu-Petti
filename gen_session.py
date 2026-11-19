# gen_session.py — Paattu Petti assistant-account session generator
# Run this YOURSELF on your phone (Termux) or laptop. Do NOT share your
# phone number, login code, or 2FA password with anyone — the script asks
# for them locally and they never leave your device.
#
# Steps:
#   1. pip install pyrogram
#   2. python gen_session.py
#   3. Enter the API ID and API Hash (from my.telegram.org, alt account)
#   4. Enter the ALT account's phone number, then the login code Telegram
#      sends to that account, then its 2FA password if it has one
#   5. Copy the session string it prints and send ONLY that string to broski

import asyncio

from pyrogram import Client


async def main():
    api_id = int(input("API ID: ").strip())
    api_hash = input("API Hash: ").strip()
    async with Client(
        "paattu_petti_assistant", api_id=api_id, api_hash=api_hash
    ) as app:
        session_string = await app.export_session_string()
        me = await app.get_me()
        print("\nLogged in as:", me.first_name, f"(@{me.username})" if me.username else "")
        print("\n=== YOUR SESSION STRING — keep it secret, send only this to broski ===\n")
        print(session_string)
        print("\n=== done — you can close this now ===")


if __name__ == "__main__":
    asyncio.run(main())
