"""
Разовый скрипт: чистит таблицу clients в blc.db от номеров с невалидным
кодом оператора (домашние/мусорные номера) и перезаливает (upsert) актуальный
clients.json поверх базы.

ЗАПУСК НА RAILWAY:
    railway run python clean_and_reload_clients.py

(или зайди в Railway Shell для сервиса и выполни `python clean_and_reload_clients.py`)

Что делает:
1. Показывает, сколько записей сейчас в clients и сколько из них "мусорных".
2. Удаляет из clients все phone, где первые 2 цифры (после 998) не входят
   в список реальных мобильных кодов Узбекистана.
3. Делает upsert (INSERT OR REPLACE) всех номеров из clients.json — то есть
   новые номера добавятся, существующие обновятся (name/has_anketa/note),
   а поле tg_sent/tg_sent_at НЕ трогается для тех, кто уже есть в базе
   (чтобы не сбросить прогресс рассылки).
4. Печатает итоговую статистику.

Ничего не удаляет из clients.json — файл остаётся как есть, меняется
только содержимое blc.db.
"""
import sqlite3
import json
import os

DB_PATH = os.environ.get("DB_PATH", "blc.db")
CLIENTS_JSON_PATH = "clients.json"

# Действующие коды мобильных операторов Узбекистана (может быть Telegram)
MOBILE_CODES = {"90", "91", "92", "93", "94", "95", "97", "98", "99",
                "33", "50", "55", "77", "88", "87", "80"}


def is_valid_mobile(phone: str) -> bool:
    """phone ожидается в формате '998XXXXXXXXX' (12 цифр, без +)."""
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) != 12 or not digits.startswith("998"):
        return False
    code = digits[3:5]
    return code in MOBILE_CODES


def main():
    if not os.path.exists(DB_PATH):
        print(f"❌ Не найден файл базы: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # ── 1. Статистика ДО чистки ────────────────────────────────
    total_before = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    rows = conn.execute("SELECT phone FROM clients").fetchall()
    bad_phones = [r["phone"] for r in rows if not is_valid_mobile(r["phone"])]
    print(f"Всего в базе сейчас: {total_before}")
    print(f"Найдено мусорных (невалидный код): {len(bad_phones)}")
    if bad_phones:
        print("Примеры мусора:", bad_phones[:10])

    # ── 2. Удаляем мусор ────────────────────────────────────────
    if bad_phones:
        conn.executemany("DELETE FROM clients WHERE phone = ?", [(p,) for p in bad_phones])
        conn.commit()
        print(f"🗑️  Удалено мусорных записей: {len(bad_phones)}")

    total_after_clean = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    print(f"Осталось после чистки: {total_after_clean}")

    # ── 3. Upsert из clients.json ────────────────────────────────
    if not os.path.exists(CLIENTS_JSON_PATH):
        print(f"⚠️  Не найден {CLIENTS_JSON_PATH} — пропускаю загрузку новых номеров.")
    else:
        with open(CLIENTS_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)

        added = 0
        updated = 0
        skipped_invalid = 0

        for phone, info in data.items():
            digits = "".join(ch for ch in phone if ch.isdigit())
            if not is_valid_mobile(digits):
                skipped_invalid += 1
                continue

            existing = conn.execute(
                "SELECT phone FROM clients WHERE phone = ?", (digits,)
            ).fetchone()

            if existing:
                # обновляем только name/has_anketa/note, tg_sent/tg_sent_at не трогаем
                conn.execute(
                    "UPDATE clients SET name = ?, has_anketa = ?, note = ? WHERE phone = ?",
                    (info.get("name", ""), int(info.get("has_anketa", False)),
                     info.get("note", ""), digits)
                )
                updated += 1
            else:
                conn.execute(
                    "INSERT INTO clients (phone, name, has_anketa, note, tg_sent) "
                    "VALUES (?, ?, ?, ?, 0)",
                    (digits, info.get("name", ""), int(info.get("has_anketa", False)),
                     info.get("note", ""))
                )
                added += 1

        conn.commit()
        print(f"✅ Добавлено новых номеров: {added}")
        print(f"🔄 Обновлено существующих: {updated}")
        if skipped_invalid:
            print(f"⏭️  Пропущено невалидных номеров из clients.json: {skipped_invalid}")

    total_final = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    unsent_final = conn.execute(
        "SELECT COUNT(*) FROM clients WHERE tg_sent = 0 OR tg_sent IS NULL"
    ).fetchone()[0]
    print(f"\n📊 ИТОГО в базе: {total_final}")
    print(f"📬 Из них ещё не получали рассылку (tg_sent=0): {unsent_final}")

    conn.close()


if __name__ == "__main__":
    main()
