"""
Поиск клиентов, у которых есть Telegram-профиль — по номеру телефона из базы.

Как это работает: Telegram даёт способ проверить пачку номеров разом
(ImportContactsRequest) — для тех, кто зарегистрирован (и не спрятал себя
от поиска по номеру в настройках приватности), в ответе приходит user_id
и профиль. Дальше мы сразу удаляем эти временные контакты из аккаунта,
чтобы не захламлять реальную адресную книгу.

Найденные номера (с юзернеймом/id) шлются в чат (по умолчанию — Избранное,
см. FOUND_TG_CHAT_ID в main.py).

Устойчиво к рестартам: каждый проверенный номер сразу помечается в БД
(storage.mark_tg_checked), поэтому следующий /findtg продолжит с номеров,
которые ещё не проверялись, а не по новой.
"""
import asyncio
import os

from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact
from telethon.errors import FloodWaitError

import storage
import logger

CHUNK_SIZE = 500  # лимит Telegram на ImportContactsRequest за один запрос
PAUSE_BETWEEN_CHUNKS = float(os.environ.get("FINDTG_PAUSE_SECONDS", "3"))
REPORT_EVERY = 20  # шлём накопленные находки в чат раз в столько штук, чтобы не спамить

_running = [False]
_task = [None]


def is_running() -> bool:
    return _running[0]


def stop():
    _running[0] = False


def get_status() -> dict:
    total = storage.tg_check_total()
    checked = storage.tg_checked_count()
    return {
        "total": total,
        "checked": checked,
        "found": storage.tg_found_count(),
        "remaining": max(0, total - checked),
    }


async def run_find_contacts(client, target_chat_id, admin_id: int, limit: int = None):
    """
    limit — если указан, проверяет максимум limit номеров за этот запуск
    (удобно для тестовой пачки перед тем как гнать по всей базе).
    """
    _running[0] = True
    checked_this_run = 0
    found_this_run = 0
    buffer = []

    await client.send_message(admin_id, "🔍 Начинаю поиск клиентов с Telegram-профилем...")

    while _running[0]:
        if limit and checked_this_run >= limit:
            break

        batch = storage.get_unchecked_clients(CHUNK_SIZE)
        if not batch:
            break

        contacts = [
            InputPhoneContact(client_id=i, phone=phone, first_name="Client", last_name="")
            for i, phone in enumerate(batch)
        ]

        try:
            result = await client(ImportContactsRequest(contacts))
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                result = await client(ImportContactsRequest(contacts))
            except Exception as e2:
                await logger.error(e2, "find_contacts import chunk (retry)")
                await asyncio.sleep(PAUSE_BETWEEN_CHUNKS)
                continue
        except Exception as e:
            await logger.error(e, "find_contacts import chunk")
            await asyncio.sleep(PAUSE_BETWEEN_CHUNKS)
            continue

        users_by_id = {u.id: u for u in result.users}
        found_client_ids = {imp.client_id: imp.user_id for imp in result.imported}

        for i, phone in enumerate(batch):
            user_id = found_client_ids.get(i)
            has_tg = user_id is not None
            username = None
            if has_tg:
                u = users_by_id.get(user_id)
                username = u.username if u else None
                name = " ".join(filter(None, [u.first_name, u.last_name])) if u else ""
                tag = f"@{username}" if username else f"id{user_id}"
                line = f"📱 +{phone} → {tag}"
                if name:
                    line += f" ({name})"
                buffer.append(line)
                found_this_run += 1
            storage.mark_tg_checked(phone, has_tg, user_id, username)

        checked_this_run += len(batch)

        # Убираем только что добавленные контакты из аккаунта — они были нужны
        # только чтобы Telegram сопоставил номер с профилем, реальными
        # контактами их делать незачем.
        try:
            uids = [uid for uid in found_client_ids.values() if uid]
            if uids:
                await client(DeleteContactsRequest(id=uids))
        except Exception as e:
            await logger.error(e, "find_contacts cleanup contacts")

        if len(buffer) >= REPORT_EVERY:
            await _flush_buffer(client, target_chat_id, buffer)
            buffer = []

        await logger.tg(
            f"🔍 Поиск TG: проверено {checked_this_run} в этом запуске, найдено {found_this_run}. "
            f"Всего по базе: {storage.tg_checked_count()}/{storage.tg_check_total()}",
            "info"
        )

        await asyncio.sleep(PAUSE_BETWEEN_CHUNKS)

    if buffer:
        await _flush_buffer(client, target_chat_id, buffer)

    was_stopped = not _running[0]
    _running[0] = False
    status = get_status()
    summary = (
        f"{'🛑 Остановлено' if was_stopped else '✅ Поиск завершён'}\n"
        f"Проверено в этом запуске: {checked_this_run}\n"
        f"Найдено с Telegram в этом запуске: {found_this_run}\n"
        f"Всего найдено: {status['found']} / проверено {status['checked']} из {status['total']}"
    )
    await logger.tg(summary, "info")
    await client.send_message(admin_id, summary)


async def _flush_buffer(client, target_chat_id, buffer):
    text = "\n".join(buffer)
    # Telegram режет сообщения примерно по 4096 символов — режем сами с запасом
    for i in range(0, len(text), 3500):
        await client.send_message(target_chat_id, text[i:i + 3500])
        await asyncio.sleep(1)
