import os
import asyncio
import random
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.errors import FloodWaitError

import storage
import logger
import watchdog
import analytics
import mailbroadcast as mbc
import find_contacts as fc
from persona import match_template, is_lawyer_request, is_urgent, LAWYER_REPLY_TEMPLATE
from ai import ai_reply
from mailer import send_lead_email

# ─── НАСТРОЙКИ ───────────────────────────────────────────────────
API_ID            = int(os.environ.get("API_ID", "0"))
API_HASH          = os.environ.get("API_HASH", "")
SESSION_STRING    = os.environ.get("SESSION_STRING", "")
ADMIN_ID          = int(os.environ.get("ADMIN_ID", "0"))
REPORT_CHAT_ID    = int(os.environ.get("REPORT_CHAT_ID", "0"))
LAWYER_TG_USERNAME = os.environ.get("LAWYER_TG_USERNAME", "")
DEBOUNCE_SECONDS  = float(os.environ.get("DEBOUNCE_SECONDS", "6"))
REPLY_DELAY_MIN   = float(os.environ.get("REPLY_DELAY_MIN", "4"))
REPLY_DELAY_MAX   = float(os.environ.get("REPLY_DELAY_MAX", "9"))
# Куда слать найденные номера с Telegram-профилем: "me" = Избранное (по умолчанию),
# либо id группы/канала (например -1001234567890), если создашь отдельный чат под это.
FOUND_TG_CHAT_ID  = os.environ.get("FOUND_TG_CHAT_ID", "me")
# ─────────────────────────────────────────────────────────────────

client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)

# Debounce-буфер: user_id -> {texts, event, sender, task}
pending = {}


# ─── УТИЛИТЫ ─────────────────────────────────────────────────────
IMPORT_CHUNK_SIZE = 500  # Telegram режет запросы больше ~1МБ — шлём пачками


async def import_known_contacts():
    clients = storage.all_clients()
    if not clients:
        return
    contacts = [
        InputPhoneContact(client_id=i, phone=c["phone"],
                          first_name=c.get("name") or "Client", last_name="")
        for i, c in enumerate(clients)
    ]
    imported = 0
    for i in range(0, len(contacts), IMPORT_CHUNK_SIZE):
        chunk = contacts[i:i + IMPORT_CHUNK_SIZE]
        try:
            await client(ImportContactsRequest(chunk))
            imported += len(chunk)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                await client(ImportContactsRequest(chunk))
                imported += len(chunk)
            except Exception as e2:
                await logger.error(e2, f"import_contacts chunk {i}")
        except Exception as e:
            await logger.error(e, f"import_contacts chunk {i}")
        await asyncio.sleep(1)  # небольшая пауза между пачками, чтобы не словить FloodWait
    await logger.tg(f"Импортировано контактов: {imported}/{len(contacts)}", "info")


async def safe_send(chat_id, text: str):
    """Отправка с защитой от FloodWait."""
    try:
        await client.send_message(chat_id, text, parse_mode="markdown")
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        await client.send_message(chat_id, text, parse_mode="markdown")
    except Exception as e:
        await logger.error(e, f"safe_send to {chat_id}")


async def safe_respond(event, text: str):
    """Имитация набора текста + задержка + отправка."""
    try:
        async with client.action(event.chat_id, "typing"):
            await asyncio.sleep(random.uniform(REPLY_DELAY_MIN, REPLY_DELAY_MAX))
        await event.respond(text)
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        await event.respond(text)
    except Exception as e:
        await logger.error(e, "safe_respond")


def find_client_by_sender(sender) -> tuple:
    phone = getattr(sender, "phone", None)
    if not phone:
        return None, None
    data = storage.find_client_by_phone(phone)
    return phone, data


# ─── КОМАНДЫ ИЗ ИЗБРАННОГО (Saved Messages) ──────────────────────
# Если ADMIN_ID совпадает с аккаунтом, на который залогинен юзербот
# (тот же номер), команды, написанные себе в Избранное, идут как
# outgoing и не ловятся incoming=True хендлером ниже — обрабатываем
# их отдельно.
@client.on(events.NewMessage(outgoing=True))
async def on_saved_message(event):
    if not event.is_private:
        return
    me = await client.get_me()
    if event.chat_id != me.id:
        return  # это не Избранное, а обычная переписка от своего лица — игнор
    await handle_admin(event)


# ─── ГЛАВНЫЙ ОБРАБОТЧИК ──────────────────────────────────────────
@client.on(events.NewMessage(incoming=True))
async def on_message(event):
    if not event.is_private:
        return
    watchdog.touch()

    sender = await event.get_sender()
    if sender is None or sender.bot:
        return
    if sender.id == ADMIN_ID:
        await handle_admin(event)
        return

    text = (event.raw_text or "").strip()
    if not text:
        return

    uid = str(sender.id)

    if storage.is_paused(uid):
        return  # Admin взял на себя этого клиента

    # Debounce — склеиваем сообщения идущие подряд
    buf = pending.get(uid)
    if buf:
        buf["texts"].append(text)
        buf["event"] = event
        buf["task"].cancel()
    else:
        buf = {"texts": [text], "event": event, "sender": sender}
        pending[uid] = buf

    buf["task"] = asyncio.create_task(_process(uid))


async def _process(uid: str):
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        return

    buf = pending.pop(uid, None)
    if not buf:
        return

    event   = buf["event"]
    sender  = buf["sender"]
    text    = "\n".join(buf["texts"])
    uname   = sender.username or ""
    fname   = " ".join(filter(None, [sender.first_name, sender.last_name]))

    phone, client_data = find_client_by_sender(sender)
    has_anketa = bool(client_data and client_data.get("has_anketa"))

    try:
        # Счётчик сообщений
        user_rec = storage.register_user_message(uid)

        # ── 1. Явный запрос контакта юриста ──
        if is_lawyer_request(text):
            link = f"https://t.me/{LAWYER_TG_USERNAME}" if LAWYER_TG_USERNAME else "(не настроено)"
            reply = LAWYER_REPLY_TEMPLATE.format(lawyer_link=link)
            await safe_respond(event, reply)
            storage.append_history(uid, "assistant", reply)
            await logger.tg(
                f"🔥 *Клиент попросил юриста*\n@{uname or '—'} (id `{sender.id}`)\n{text}", "lead"
            )
            if has_anketa:
                asyncio.create_task(asyncio.to_thread(
                    send_lead_email, client_data.get("name") or fname, phone, text, uname
                ))
            await _maybe_notify_admin(user_rec, uid, uname, sender.id)
            return

        # ── 2. Срочное обращение ──
        if is_urgent(text):
            await logger.tg(
                f"🚨 *СРОЧНО* от @{uname or '—'} (id `{sender.id}`):\n{text}", "lead"
            )
            if ADMIN_ID:
                await safe_send(
                    ADMIN_ID,
                    f"🚨 *Срочное обращение!*\nОт @{uname or '—'} (id `{sender.id}`):\n{text}"
                )

        # ── 3. Клиент с анкетой (горячий лид) ──
        if has_anketa:
            await logger.tg(
                f"🔥 *Заявка с анкетой*\nИмя: {client_data.get('name') or fname}\n"
                f"Тел: `{phone}`  |  @{uname or '—'}\n\n{text}", "lead"
            )
            asyncio.create_task(asyncio.to_thread(
                send_lead_email, client_data.get("name") or fname, phone, text, uname
            ))
            await _maybe_notify_admin(user_rec, uid, uname, sender.id)
            return

        # ── 4. Лимит автоответов ──
        if not storage.can_send():
            await logger.tg(
                f"⏳ Лимит исчерпан — пропущено от @{uname or '—'}:\n{text}", "warn"
            )
            return

        # ── 5. Шаблон → AI ──
        history = storage.get_history(uid)
        reply = match_template(text)
        used_ai = False
        if reply is None:
            reply = ai_reply(history, text)
            used_ai = True

        await safe_respond(event, reply)
        storage.register_sent()
        storage.append_history(uid, "user", text)
        storage.append_history(uid, "assistant", reply)

        await logger.tg(
            f"💬 @{uname or '—'} (id `{sender.id}`)\n"
            f"▸ {text}\n"
            f"◂ {'AI' if used_ai else 'шаблон'}: {reply}", "msg"
        )

        await _maybe_notify_admin(user_rec, uid, uname, sender.id)

    except Exception as e:
        await logger.error(e, f"_process uid={uid}")


async def _maybe_notify_admin(user_rec, uid: str, uname: str, user_id: int):
    if storage.should_notify_admin(user_rec):
        storage.pause_user(uid)
        if ADMIN_ID:
            await safe_send(
                ADMIN_ID,
                f"⚠️ Клиент @{uname or '—'} (id `{user_id}`) написал уже "
                f"{user_rec['msg_count']} сообщений.\n"
                f"Бот замолчал — возможно, стоит ответить лично.\n\n"
                f"Чтобы снова включить автоответы этому клиенту:\n`/unpause {uid}`"
            )


# ─── КОМАНДЫ АДМИНА ──────────────────────────────────────────────
async def handle_admin(event):
    text = (event.raw_text or "").strip()

    if text.startswith("/limit"):
        parts = text.split()
        if len(parts) == 2 and parts[1].isdigit():
            storage.set_limit(int(parts[1]))
            await event.respond(f"✅ Лимит установлен: {parts[1]}/день")
        else:
            await event.respond(
                f"Лимит: {storage.get_daily_limit()}/день\n"
                f"Отправлено сегодня: {storage.get_sent_today()}\n"
                f"Изменить: /limit 150"
            )

    elif text.startswith("/status"):
        clients = storage.all_clients()
        hot = sum(1 for c in clients if c["has_anketa"])
        await event.respond(
            f"📊 Статус\n"
            f"Лимит/день: {storage.get_daily_limit()}\n"
            f"Отправлено сегодня: {storage.get_sent_today()}\n"
            f"Клиентов в базе: {len(clients)} (с анкетой: {hot})"
        )

    elif text.startswith("/unpause"):
        parts = text.split()
        if len(parts) == 2:
            storage.unpause_user(parts[1])
            await event.respond(f"✅ Клиент {parts[1]} снят с паузы, автоответы возобновлены.")

    elif text.startswith("/addclient"):
        # /addclient 998901234567 Иван Иванов
        parts = text.split(maxsplit=2)
        if len(parts) >= 2:
            phone = parts[1]
            name = parts[2] if len(parts) == 3 else ""
            storage.add_client(phone, name, has_anketa=True)
            await event.respond(f"✅ Клиент добавлен: {phone} {name}")
        else:
            await event.respond("Формат: /addclient 998901234567 Имя Фамилия")

    elif text.startswith("/report"):
        await analytics.send_weekly_report(REPORT_CHAT_ID)
        await event.respond("✅ Отчёт отправлен в канал.")

    elif text.startswith("/mailbroadcast"):
        if mbc.is_running():
            await event.respond("⚠️ Рассылка на почту уже идёт. Остановить: /stopmailbroadcast")
            return
        parts = text.split()
        daily_limit = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
        mbc._task[0] = asyncio.create_task(
            mbc.run_mail_broadcast(client, REPORT_CHAT_ID, ADMIN_ID, daily_limit)
        )

    elif text.startswith("/stopmailbroadcast"):
        mbc.stop_mail_broadcast()
        await event.respond("🛑 Останавливаю рассылку на почту (прогресс сохранён, продолжит с этого места)...")

    elif text.startswith("/mailstatus"):
        stats = mbc.get_status()
        await event.respond(
            f"📧 Рассылка на почту\n"
            f"Всего адресов: {stats['total']}\n"
            f"Уже отправлено: {stats['sent']}\n"
            f"Осталось: {stats['remaining']}\n"
            f"Отправлено сегодня: {stats['sent_today']}/{stats['daily_limit']}\n"
            f"Статус: {'идёт рассылка' if mbc.is_running() else 'остановлена'}"
        )

    elif text.startswith("/findtg"):
        if fc.is_running():
            await event.respond("⚠️ Поиск уже идёт. Остановить: /stopfindtg")
            return
        parts = text.split()
        limit = int(parts[1]) if len(parts) == 2 and parts[1].isdigit() else None
        fc._task[0] = asyncio.create_task(
            fc.run_find_contacts(client, FOUND_TG_CHAT_ID, ADMIN_ID, limit)
        )
        await event.respond(
            f"🔍 Запускаю поиск{f' (макс. {limit} номеров)' if limit else ''}. "
            f"Находки будут приходить в чат: {FOUND_TG_CHAT_ID}\nОстановить: /stopfindtg"
        )

    elif text.startswith("/stopfindtg"):
        fc.stop()
        await event.respond("🛑 Останавливаю поиск...")

    elif text.startswith("/findtgstatus"):
        s = fc.get_status()
        await event.respond(
            f"🔍 Поиск Telegram-профилей\n"
            f"Всего номеров в базе: {s['total']}\n"
            f"Проверено: {s['checked']}\n"
            f"Найдено с Telegram: {s['found']}\n"
            f"Осталось проверить: {s['remaining']}\n"
            f"Статус: {'идёт поиск' if fc.is_running() else 'остановлен'}"
        )

    elif text.startswith("/help"):
        await event.respond(
            "📋 Команды:\n"
            "/status — состояние бота\n"
            "/limit 100 — лимит автоответов/день\n"
            "/unpause <user_id> — снять клиента с паузы\n"
            "/addclient <телефон> <Имя> — добавить клиента с анкетой\n"
            "/report — отправить еженедельный отчёт прямо сейчас\n"
            "/mailbroadcast [лимит/день] — рассылка на почту (продолжает с места остановки)\n"
            "/stopmailbroadcast — остановить почтовую рассылку\n"
            "/mailstatus — прогресс почтовой рассылки\n"
            "/findtg [N] — искать клиентов с Telegram-профилем по номеру (резюмируемо)\n"
            "/stopfindtg — остановить поиск\n"
            "/findtgstatus — сколько проверено / найдено\n"
            "/help — эта справка"
        )


# ─── СТАРТ ───────────────────────────────────────────────────────
async def main():
    storage.init_db()
    await client.start()
    await client.get_dialogs()  # прогреваем кэш сущностей — иначе логгер не найдёт REPORT_CHAT_ID
    logger.setup(client, REPORT_CHAT_ID)
    await import_known_contacts()
    asyncio.create_task(watchdog.run(client))
    asyncio.create_task(analytics.weekly_report_loop(client, REPORT_CHAT_ID))
    # Почтовая рассылка автоматически продолжает с того места, где остановилась
    # (прогресс хранится в БД, а не в памяти) — можно просто раскомментировать,
    # если нужно чтобы она стартовала сама при поднятии бота:
    # mbc._task[0] = asyncio.create_task(mbc.run_mail_broadcast(client, REPORT_CHAT_ID, ADMIN_ID))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
