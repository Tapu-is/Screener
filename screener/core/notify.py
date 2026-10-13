"""Telegram delivery. The token and chat id are kept in telegram.json in the
stk data folder (or STK_TELEGRAM_TOKEN / STK_TELEGRAM_CHAT_ID)."""

from __future__ import annotations

import json
import os
from typing import Optional

import requests

from . import db


class NotifyError(Exception):
    pass


def _api() -> str:
    return os.environ.get("STK_TELEGRAM_URL", "https://api.telegram.org")


def _file():
    return db.data_dir() / "telegram.json"


def load() -> Optional[dict]:
    token = os.environ.get("STK_TELEGRAM_TOKEN")
    chat = os.environ.get("STK_TELEGRAM_CHAT_ID")
    if token and chat:
        return {"token": token, "chat_id": chat}
    try:
        data = json.loads(_file().read_text())
        if data.get("token") and data.get("chat_id"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save(token: str, chat_id) -> None:
    f = _file()
    f.write_text(json.dumps({"token": token.strip(), "chat_id": str(chat_id)}))
    try:
        f.chmod(0o600)
    except OSError:
        pass


def configured() -> bool:
    return load() is not None


def find_chat_id(token: str, session=None) -> Optional[str]:
    """The chat id of whoever last messaged the bot (None if nobody has yet)."""
    s = session or requests
    try:
        r = s.get(f"{_api()}/bot{token}/getUpdates", timeout=15)
    except requests.RequestException as exc:
        raise NotifyError(f"cannot reach Telegram ({exc.__class__.__name__})") from None
    if r.status_code == 401:
        raise NotifyError("Telegram rejected that token - copy it again from BotFather")
    if r.status_code != 200:
        raise NotifyError(f"Telegram answered HTTP {r.status_code}")
    chat = None
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        if msg.get("chat", {}).get("id") is not None:
            chat = str(msg["chat"]["id"])
    return chat


def chunks(text: str, size: int = 4000) -> list[str]:
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            line, rest = line[:size], line[size:]
            out.append(cur + line if cur else line)
            cur, line = "", rest
        if len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send(text: str, session=None) -> None:
    cfg = load()
    if not cfg:
        raise NotifyError("Telegram is not set up - run: stk telegram setup")
    s = session or requests
    for part in chunks(text):
        try:
            r = s.post(
                f"{_api()}/bot{cfg['token']}/sendMessage",
                json={"chat_id": cfg["chat_id"], "text": part, "disable_web_page_preview": True},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise NotifyError(f"cannot reach Telegram ({exc.__class__.__name__})") from None
        if r.status_code != 200:
            raise NotifyError(f"Telegram answered HTTP {r.status_code}")
