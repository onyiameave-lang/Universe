"""Read the private chat ID for an Oracle Telegram bot without saving its token."""
from __future__ import annotations

import getpass
import json
import urllib.error
import urllib.parse
import urllib.request


def main() -> int:
    token = getpass.getpass("Paste the Telegram bot token (input is hidden): ").strip()
    if not token:
        print("No token entered.")
        return 1

    encoded_token = urllib.parse.quote(token, safe=":_")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{encoded_token}/getUpdates?limit=100",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"Telegram returned HTTP {exc.code}. Check the token and try again.")
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"Could not read Telegram updates ({type(exc).__name__}).")
        return 1

    if not payload.get("ok"):
        print(f"Telegram rejected the request (error {payload.get('error_code', 'unknown')}).")
        return 1

    chats: dict[tuple[str, int], None] = {}
    for update in payload.get("result", []):
        for field in ("message", "edited_message", "channel_post", "edited_channel_post"):
            message = update.get(field)
            chat = message.get("chat") if isinstance(message, dict) else None
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                chats[(str(chat.get("type", "unknown")), chat["id"])] = None

    if not chats:
        print("No chat messages found. Open your bot in Telegram, press Start, send /start, "
              "then run this helper again.")
        return 0

    print("Chat IDs found:")
    for (chat_type, chat_id) in chats:
        print(f"  type={chat_type}  chat_id={chat_id}")
    print("Copy your private chat_id into ORACLE_TELEGRAM_CHAT_ID in Universal_AI/.env.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
