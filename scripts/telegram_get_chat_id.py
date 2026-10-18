#!/usr/bin/env python3
"""
One-shot helper: after creating a bot with @BotFather and /start'ing it,
paste TELEGRAM_BOT_TOKEN then run this to print chat_id from getUpdates.

  export TELEGRAM_BOT_TOKEN='…'
  python3 scripts/telegram_get_chat_id.py

Never commit real tokens. Writes nothing to disk.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request


def main() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first.", file=sys.stderr)
        print("1) Talk to @BotFather → /newbot → copy token", file=sys.stderr)
        print("2) Open your bot chat → send /start", file=sys.stderr)
        print("3) export TELEGRAM_BOT_TOKEN=… && python3 scripts/telegram_get_chat_id.py", file=sys.stderr)
        return 1
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    with urllib.request.urlopen(url, timeout=15) as res:
        data = json.loads(res.read().decode("utf-8"))
    if not data.get("ok"):
        print("getUpdates failed:", data.get("description"), file=sys.stderr)
        return 2
    chats: set[str] = set()
    for upd in data.get("result") or []:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if "id" in chat:
            chats.add(str(chat["id"]))
    if not chats:
        print("No chats yet — open the bot and send /start, then re-run.")
        return 3
    print("TELEGRAM_CHAT_ID candidates:")
    for c in sorted(chats):
        print(c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
