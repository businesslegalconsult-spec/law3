"""
Еженедельный отчёт и статистика по обращениям.
"""
import asyncio
from datetime import datetime, timedelta
import storage
import logger

REPORT_DAY = 0   # 0=понедельник, отправляем еженедельный отчёт


async def weekly_report_loop(client, report_chat_id: int):
    """Раз в неделю (в понедельник утром) шлёт сводку в отчётный чат."""
    while True:
        now = datetime.now()
        # Следующий понедельник 09:00
        days_ahead = (7 - now.weekday()) % 7 or 7
        next_run = (now + timedelta(days=days_ahead)).replace(hour=9, minute=0, second=0, microsecond=0)
        wait_sec = (next_run - now).total_seconds()
        await asyncio.sleep(wait_sec)
        await send_weekly_report(report_chat_id)


async def send_weekly_report(report_chat_id: int):
    try:
        clients = storage.all_clients()
        total = len(clients)
        hot = sum(1 for c in clients if c["has_anketa"])

        from storage import get_conn
        with get_conn() as conn:
            users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            paused = conn.execute("SELECT COUNT(*) FROM users WHERE paused=1").fetchone()[0]

        sent_today = storage.get_sent_today()
        limit = storage.get_daily_limit()

        text = (
            f"📊 *Еженедельный отчёт BLC Userbot*\n"
            f"Дата: {datetime.now().strftime('%d.%m.%Y')}\n\n"
            f"👥 Клиентов в базе: {total} (с анкетой: {hot})\n"
            f"💬 Уникальных собеседников: {users_count}\n"
            f"⏸ На паузе (переданы админу): {paused}\n"
            f"📨 Отправлено автоответов сегодня: {sent_today} / {limit}"
        )
        await logger.tg(text, "info")
    except Exception as e:
        await logger.error(e, "weekly_report")
