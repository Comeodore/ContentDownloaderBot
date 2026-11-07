"""The owner's "Send to Claude" queue for failure reports.

The button under a failure report writes it to <inbox>/<id>.json. A Claude Code
session watches that dir, takes a request, fixes what it can and answers under
the report in Telegram:

    python inbox.py list
    python inbox.py take <id>
    echo "text" | python inbox.py reply <id>
    python inbox.py done <id>
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import sys
import time

from config import Config, load_config

STATES = ("", "taken", "done")  # "" = pending, in the inbox dir itself


def inbox_dir(config: Config) -> str:
    """Next to the downloads dir: on the data volume, so it outlives the container."""
    return os.path.join(os.path.dirname(os.path.abspath(config.download_dir)), "claude-inbox")


def add(config: Config, chat_id: int, message_id: int, report: str) -> str:
    """Queues a report; returns its id."""
    root = inbox_dir(config)
    os.makedirs(root, exist_ok=True)
    rid = f"{time.strftime('%Y%m%d-%H%M%S')}-{message_id}"
    path = os.path.join(root, rid + ".json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"id": rid, "chat_id": chat_id, "message_id": message_id,
                   "sent_at": time.strftime("%Y-%m-%d %H:%M:%S %z"), "report": report},
                  f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)  # watchers only ever see complete *.json files
    return rid


def _path(config: Config, rid: str) -> str:
    for state in STATES:
        path = os.path.join(inbox_dir(config), state, rid + ".json")
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(f"no request {rid}")


def load(config: Config, rid: str) -> dict:
    with open(_path(config, rid), encoding="utf-8") as f:
        return json.load(f)


def pending(config: Config) -> list[dict]:
    reqs = []
    for path in sorted(glob.glob(os.path.join(inbox_dir(config), "*.json"))):
        with open(path, encoding="utf-8") as f:
            reqs.append(json.load(f))
    return reqs


def move(config: Config, rid: str, state: str) -> None:
    """Moves a request to "taken" or "done"."""
    target = os.path.join(inbox_dir(config), state)
    os.makedirs(target, exist_ok=True)
    os.replace(_path(config, rid), os.path.join(target, rid + ".json"))


async def reply(config: Config, rid: str, text: str) -> None:
    """Answers under the report in Telegram (plain text)."""
    from aiogram import Bot
    from aiogram.client.session.aiohttp import AiohttpSession
    from aiogram.client.telegram import TelegramAPIServer

    req = load(config, rid)
    session = None
    if config.telegram_api_base:  # never the cloud API: the bot is logged out of it
        server = TelegramAPIServer.from_base(config.telegram_api_base, is_local=True)
        session = AiohttpSession(api=server)
    bot = Bot(config.bot_token, session=session)
    try:
        await bot.send_message(req["chat_id"], text, reply_to_message_id=req["message_id"])
    finally:
        await bot.session.close()


def main(argv: list[str]) -> int:
    config = load_config()
    cmd, args = (argv[0], argv[1:]) if argv else ("list", [])
    if cmd == "list":
        for req in pending(config):
            print(json.dumps(req, ensure_ascii=False, indent=1))
    elif cmd in ("take", "done") and len(args) == 1:
        move(config, args[0], "taken" if cmd == "take" else "done")
    elif cmd == "reply" and len(args) == 1:
        asyncio.run(reply(config, args[0], sys.stdin.read().strip()))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
