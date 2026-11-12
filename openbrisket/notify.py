"""Leaving the cook a note: Telegram when a bot is configured, the terminal otherwise."""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request


class Notifier:
    def send(self, text: str) -> bool:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    def send(self, text: str) -> bool:
        print(text, file=self.stream, flush=True)
        return True


class TelegramNotifier(Notifier):
    def __init__(self, bot_token: str, chat_id: str, timeout: float = 15.0):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.timeout = timeout

    def send(self, text: str) -> bool:
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.bot_token}/sendMessage", data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return bool(json.load(r).get("ok"))
        except Exception as e:
            print(f"telegram: could not send ({e}); message was: {text}", file=sys.stderr, flush=True)
            return False


class MemoryNotifier(Notifier):
    """Collects messages; used by tests and dry runs."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        return True


def from_settings(cfg: dict) -> Notifier:
    tg = cfg.get("telegram") or {}
    if tg.get("bot_token") and tg.get("chat_id"):
        return TelegramNotifier(tg["bot_token"], tg["chat_id"])
    return ConsoleNotifier()
