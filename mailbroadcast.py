"""
Массовая рассылка по email из storage.subscribers (импортируется один раз
из subscribers.json при первом старте бота).

Ключевое требование — устойчивость к перезапускам: каждый успешный отправленный
адрес сразу помечается sent=1 в БД (storage.mark_subscriber_sent). Если процесс
упадёт или Railway перезапустит контейнер, следующий вызов /mailbroadcast
получит из storage.get_unsent_subscribers() ровно тех, кто ещё не отправлен —
рассылка НЕ начинается заново.

Дневной лимит считается по факту (storage.subscribers_sent_today_count),
а не по счётчику в памяти — поэтому лимит не сбрасывается при рестарте
посреди дня.
"""
import asyncio
import os
from datetime import datetime, timedelta

import storage
import logger
import watchdog
from mailer import send_html_email

DEFAULT_DAILY_LIMIT = int(os.environ.get("MAIL_DAILY_LIMIT", "250"))
PAUSE_BETWEEN = float(os.environ.get("MAIL_PAUSE_SECONDS", "170"))  # ~300/день на 14ч
HOUR_START = int(os.environ.get("MAIL_HOUR_START", "9"))
HOUR_END = int(os.environ.get("MAIL_HOUR_END", "19"))

SITE_LINK = "https://xaruem.github.io/Rassilka/"
SUBJECT = "Business Law Consulting — правовая защита вашего бизнеса в Узбекистане"

HTML_TEMPLATE = f"""\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;
            color:#1a1a1a;line-height:1.5;">
  <div style="background:#0b1f3a;color:#fff;padding:24px;text-align:center;">
    <div style="font-size:20px;font-weight:bold;letter-spacing:0.5px;">BUSINESS LAW CONSULTING</div>
    <div style="font-size:12px;color:#c9a24b;letter-spacing:1px;margin-top:4px;">
      АДВОКАТСКАЯ ФИРМА · ТАШКЕНТ
    </div>
  </div>

  <div style="padding:24px;background:#ffffff;">
    <p>Уважаемые предприниматели, учредители и руководство компании!</p>
    <p>
      Адвокатская фирма «Business Law Consulting» предлагает надёжную правовую
      защиту вашего бизнеса — от консультаций до комплексного юридического
      сопровождения.
    </p>

    <p style="font-weight:bold;margin-top:20px;">Мы предлагаем:</p>
    <ul style="padding-left:18px;">
      <li>Адвокатская защита вашего бизнеса</li>
      <li>Юридическое обслуживание и сопровождение</li>
      <li>Разработка договоров, экспорт/импорт контрактов</li>
      <li>Взыскание дебиторской задолженности</li>
      <li>Due diligence — проверка контрагентов</li>
      <li>Юридический аудит по разработанной нами системе</li>
    </ul>

    <div style="text-align:center;margin:28px 0;">
      <a href="{SITE_LINK}"
         style="background:#c9a24b;color:#0b1f3a;text-decoration:none;
                padding:12px 28px;border-radius:4px;font-weight:bold;display:inline-block;">
        Подробнее о наших услугах →
      </a>
    </div>

    <p>Или позвоните: <b>+998 90 888-44-66</b></p>
  </div>

  <div style="background:#f4f4f4;color:#666;font-size:11px;padding:16px 24px;text-align:center;">
    © 2026 Business Law Consulting · г. Ташкент, пр. Мустакиллик, 107<br>
  </div>
</div>
"""

TEXT_TEMPLATE = (
    "Business Law Consulting — Адвокатская фирма, Ташкент\n\n"
    "Правовая защита бизнеса: регистрация и сопровождение, налоговые вопросы, "
    "договоры, взыскание задолженности, due diligence, судебные споры.\n\n"
    f"Подробнее: {SITE_LINK}\n"
    "Телефон: +998 90 888-44-66"
)

_running = [False]
_task = [None]


def is_running() -> bool:
    return _running[0]


def stop_mail_broadcast():
    _running[0] = False


def get_status() -> dict:
    return {
        "total": storage.subscribers_total(),
        "sent": storage.subscribers_sent_count(),
        "remaining": storage.subscribers_total() - storage.subscribers_sent_count(),
        "sent_today": storage.subscribers_sent_today_count(),
        "daily_limit": DEFAULT_DAILY_LIMIT,
    }


async def _sleep_until_tomorrow():
    now = datetime.now()
    next_run = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    await asyncio.sleep((next_run - now).total_seconds())


def _in_working_hours() -> bool:
    return HOUR_START <= datetime.now().hour < HOUR_END


async def _sleep_until_working_hours():
    """Если сейчас вне окна 09:00-19:00 — спим до ближайшего HOUR_START."""
    now = datetime.now()
    if now.hour < HOUR_START:
        next_run = now.replace(hour=HOUR_START, minute=0, second=0, microsecond=0)
    else:
        next_run = (now + timedelta(days=1)).replace(hour=HOUR_START, minute=0, second=0, microsecond=0)
    wait_sec = (next_run - now).total_seconds()
    await logger.tg(f"🌙 Вне рабочего окна ({HOUR_START}:00–{HOUR_END}:00). Продолжу в {next_run.strftime('%d.%m %H:%M')}.", "info")
    await asyncio.sleep(wait_sec)


async def run_mail_broadcast(client, report_chat_id: int, admin_id: int, daily_limit: int = None):
    """
    Бесконечный цикл (пока не остановят /stopmailbroadcast или не кончатся адреса):
    - шлёт по одному письму с паузой PAUSE_BETWEEN
    - работает только в окне HOUR_START-HOUR_END (по умолчанию 09:00-19:00)
    - как только упирается в дневной лимит — засыпает до следующего утра
    - при рестарте процесса просто вызови эту функцию заново (например, /mailbroadcast) —
      она продолжит с первого ещё не отправленного адреса.
    """
    limit = daily_limit or DEFAULT_DAILY_LIMIT
    _running[0] = True

    total = storage.subscribers_total()
    already_sent = storage.subscribers_sent_count()
    await logger.tg(
        f"📧 Запускаю почтовую рассылку\nВсего адресов: {total}\n"
        f"Уже было отправлено ранее: {already_sent}\nЛимит в день: {limit}\n"
        f"Рабочее окно: {HOUR_START}:00–{HOUR_END}:00",
        "info"
    )
    await client.send_message(
        admin_id,
        f"📧 Почтовая рассылка запущена\nВсего: {total} | Уже отправлено: {already_sent}\n"
        f"Лимит: {limit}/день | Окно: {HOUR_START}:00–{HOUR_END}:00\n\nОстановить: /stopmailbroadcast"
    )

    sent_this_run = 0
    failed_this_run = 0

    while _running[0]:
        if not _in_working_hours():
            await _sleep_until_working_hours()
            if not _running[0]:
                break
            continue

        sent_today = storage.subscribers_sent_today_count()
        if sent_today >= limit:
            await logger.tg(f"⏸ Дневной лимит почты исчерпан ({sent_today}/{limit}). Продолжу завтра.", "info")
            await _sleep_until_tomorrow()
            if not _running[0]:
                break
            continue

        batch = storage.get_unsent_subscribers(limit - sent_today)
        if not batch:
            await logger.tg(f"✅ Почтовая рассылка полностью завершена. Отправлено всего: {storage.subscribers_sent_count()}", "info")
            await client.send_message(admin_id, "✅ Почтовая рассылка завершена — все адреса обработаны.")
            _running[0] = False
            break

        for email in batch:
            if not _running[0]:
                break
            if not _in_working_hours():
                break  # выходим во внешний цикл — там сработает _sleep_until_working_hours

            watchdog.touch()  # рассылка тоже считается активностью, не даём watchdog ложно ругаться

            ok = send_html_email(email, SUBJECT, HTML_TEMPLATE, TEXT_TEMPLATE)
            if ok:
                storage.mark_subscriber_sent(email)  # коммитится сразу — это и даёт устойчивость к рестарту
                sent_this_run += 1
            else:
                failed_this_run += 1

            if (sent_this_run + failed_this_run) % 25 == 0:
                await logger.tg(
                    f"📨 Почта: {sent_this_run} отправлено, {failed_this_run} ошибок в этом запуске "
                    f"(всего отправлено: {storage.subscribers_sent_count()}/{total})",
                    "info"
                )
            await asyncio.sleep(PAUSE_BETWEEN)

    await logger.tg(
        f"🛑 Почтовая рассылка остановлена. В этом запуске: {sent_this_run} отправлено, {failed_this_run} ошибок.",
        "warn"
    )
    await client.send_message(
        admin_id,
        f"🛑 Почтовая рассылка остановлена (или лимит/адреса кончились).\n"
        f"В этом запуске отправлено: {sent_this_run}, ошибок: {failed_this_run}\n"
        f"Всего в базе отправлено: {storage.subscribers_sent_count()}/{total}"
    )
    _running[0] = False
