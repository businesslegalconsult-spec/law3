"""
Централизованный логгер — все ошибки и события пишутся
одновременно в консоль (Railway logs) и в Telegram-чат.
"""
import traceback
import asyncio
from datetime import datetime

_client = None
_report_chat_id = 0


def setup(client, report_chat_id: int):
    global _client, _report_chat_id
    _client = client
    _report_chat_id = report_chat_id


async def tg(text: str, level: str = "info"):
    icons = {"info": "ℹ️", "warn": "⚠️", "error": "🔴", "lead": "🔥", "msg": "💬"}
    icon = icons.get(level, "•")
    ts = datetime.now().strftime("%H:%M:%S")
    full = f"{icon} `[{ts}]` {text}"
    print(f"[{level.upper()}] {text}")
    if _client and _report_chat_id:
        try:
            await _client.send_message(_report_chat_id, full, parse_mode="markdown")
        except Exception as e:
            print(f"[LOGGER SEND ERROR] {e}")


async def error(e: Exception, context: str = ""):
    tb = traceback.format_exc()
    short = f"{context}: {type(e).__name__}: {e}" if context else f"{type(e).__name__}: {e}"
    print(f"[ERROR] {short}\n{tb}")
    if _client and _report_chat_id:
        try:
            await _client.send_message(
                _report_chat_id,
                f"🔴 *Ошибка* | `{short}`\n```\n{tb[-600:]}\n```",
                parse_mode="markdown"
            )
        except Exception as send_err:
            print(f"[LOGGER SEND ERROR] {send_err}")
