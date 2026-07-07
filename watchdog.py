"""
Watchdog — следит за тем, что бот жив и переподключается при обрыве.
"""
import asyncio
import logger

PING_INTERVAL = 60   # секунд между проверками
MAX_SILENT = 300     # секунд тишины после которых считаем что соединение мертво


_last_event_time = 0


def touch():
    """Вызывается при каждом входящем событии — сбрасывает таймер тишины."""
    global _last_event_time
    import time
    _last_event_time = time.time()


async def run(client):
    import time
    global _last_event_time
    _last_event_time = time.time()
    await logger.tg("✅ Userbot запущен", "info")

    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            if not client.is_connected():
                await logger.tg("🔄 Соединение потеряно — переподключаюсь...", "warn")
                await client.connect()
                await logger.tg("✅ Переподключился", "info")
            else:
                # Лёгкий пинг — получаем свой профиль
                await client.get_me()

            silent_for = time.time() - _last_event_time

            # 3 часа = 10800 секунд
            if silent_for >= 3 * 60 * 60:
                await logger.tg(
                    f"⚠️ Нет входящих событий уже {int(silent_for // 3600)} ч. — бот молчит, проверь",
                    "warn"
                )

        except Exception as e:
            await logger.error(e, "watchdog")
            try:
                await client.connect()
            except Exception:
                pass
