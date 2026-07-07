import sqlite3
import os
import json
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", "blc.db")
HISTORY_LIMIT = 10
NOTIFY_AFTER = 15
DEFAULT_DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "100"))

# Если папка для БД ещё не существует (например, Volume в Railway не смонтирован
# или DB_PATH указывает на вложенный путь) — создаём её, чтобы не падать с
# sqlite3.OperationalError: unable to open database file.
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS clients (
            phone TEXT PRIMARY KEY,
            name TEXT DEFAULT '',
            has_anketa INTEGER DEFAULT 0,
            note TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            msg_count INTEGER DEFAULT 0,
            notified INTEGER DEFAULT 0,
            paused INTEGER DEFAULT 0,
            history TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS subscribers (
            email TEXT PRIMARY KEY,
            sent INTEGER DEFAULT 0,
            sent_at TEXT
        );
        """)
    # На случай если clients уже существовала без этих колонок (старый деплой) —
    # добавляем миграцией, без потери данных.
    with get_conn() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(clients)").fetchall()]
        if "tg_sent" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_sent INTEGER DEFAULT 0")
        if "tg_sent_at" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_sent_at TEXT")
        if "has_tg" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN has_tg INTEGER DEFAULT NULL")
        if "tg_username" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_username TEXT")
        if "tg_user_id" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_user_id TEXT")
        if "tg_checked" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_checked INTEGER DEFAULT 0")
        if "tg_checked_at" not in cols:
            conn.execute("ALTER TABLE clients ADD COLUMN tg_checked_at TEXT")
    # Загружаем clients.json если база клиентов пустая
    _seed_clients_from_json()
    # Загружаем subscribers.json если база подписчиков пустая (один раз)
    _seed_subscribers_from_json()


def _seed_clients_from_json():
    path = "clients.json"
    if not os.path.exists(path):
        return
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        if count > 0:
            return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    with get_conn() as conn:
        for phone, info in data.items():
            conn.execute(
                "INSERT OR IGNORE INTO clients (phone,name,has_anketa,note) VALUES (?,?,?,?)",
                (phone, info.get("name",""), int(info.get("has_anketa", False)), info.get("note",""))
            )
    print(f"[DB] Импортировано клиентов из clients.json: {len(data)}")


def _seed_subscribers_from_json(path: str = "subscribers.json"):
    """
    Разовый импорт email-адресов в БД. Если файл переименован иначе — передай
    путь явно или переименуй в subscribers.json рядом с ботом.
    Безопасно вызывать повторно: если в базе уже есть подписчики, ничего не делает,
    поэтому повторный деплой/рестарт не затирает прогресс рассылки (sent/sent_at).
    """
    if not os.path.exists(path):
        return
    with get_conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
        if count > 0:
            return
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    with get_conn() as conn:
        for email, info in data.items():
            sent = int(bool(info.get("sent", False))) if isinstance(info, dict) else 0
            sent_at = info.get("sent_at") if isinstance(info, dict) else None
            conn.execute(
                "INSERT OR IGNORE INTO subscribers (email,sent,sent_at) VALUES (?,?,?)",
                (email.strip().lower(), sent, sent_at)
            )
    print(f"[DB] Импортировано подписчиков из {path}: {len(data)}")


# ─── ПОДПИСЧИКИ (ПОЧТОВАЯ РАССЫЛКА) ─────────────────────────────
def subscribers_total() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]


def subscribers_sent_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM subscribers WHERE sent=1").fetchone()[0]


def subscribers_sent_today_count() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM subscribers WHERE sent=1 AND sent_at LIKE ?",
            (f"{today}%",)
        ).fetchone()[0]


def get_unsent_subscribers(limit: int) -> list:
    """Следующая пачка неотправленных адресов — детерминированный порядок,
    поэтому рестарт всегда продолжает ровно с того же места."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT email FROM subscribers WHERE sent=0 ORDER BY email ASC LIMIT ?",
            (limit,)
        ).fetchall()
        return [r[0] for r in rows]


def mark_subscriber_sent(email: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE subscribers SET sent=1, sent_at=? WHERE email=?",
            (datetime.now().isoformat(timespec="seconds"), email)
        )


# ─── TELEGRAM-РАССЫЛКА (клиенты с телефоном) ────────────────────
def tg_broadcast_sent_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM clients WHERE tg_sent=1"
        ).fetchone()[0]


def tg_broadcast_sent_in_last_days(days: int) -> int:
    """Сколько отправлено за последние N дней (скользящее окно, не календарная неделя) —
    так рестарт посреди недели не сбрасывает лимит и не даёт его обойти."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM clients WHERE tg_sent=1 AND tg_sent_at >= ?",
            (cutoff,)
        ).fetchone()[0]


def get_unsent_tg_clients(limit: int) -> list:
    """Следующая пачка клиентов с телефоном, кому ещё не отправляли Telegram-рассылку."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone FROM clients WHERE tg_sent=0 AND phone IS NOT NULL AND phone != '' "
            "ORDER BY phone ASC LIMIT ?",
            (limit,)
        ).fetchall()
        return [r[0] for r in rows]


def mark_tg_client_sent(phone: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET tg_sent=1, tg_sent_at=? WHERE phone=?",
            (datetime.now().isoformat(timespec="seconds"), phone)
        )


def mark_tg_client_failed(phone: str):
    """Номер не резолвится в Telegram-пользователя (не зарегистрирован/невалиден).
    Помечаем отдельным статусом (2), чтобы get_unsent_tg_clients() его больше
    не выдавал — иначе рассылка каждый раз заново упирается в одни и те же
    мёртвые номера в начале отсортированного списка и не продвигается дальше."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET tg_sent=2, tg_sent_at=? WHERE phone=?",
            (datetime.now().isoformat(timespec="seconds"), phone)
        )


def tg_broadcast_failed_count() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM clients WHERE tg_sent=2"
        ).fetchone()[0]


# ─── ПОИСК TELEGRAM-ПРОФИЛЕЙ ПО НОМЕРУ ───────────────────────────
def get_unchecked_clients(limit: int) -> list:
    """Следующая пачка номеров, которые ещё не проверялись на наличие Telegram."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone FROM clients WHERE tg_checked=0 AND phone IS NOT NULL AND phone != '' "
            "ORDER BY phone ASC LIMIT ?",
            (limit,)
        ).fetchall()
        return [r[0] for r in rows]


def mark_tg_checked(phone: str, has_tg: bool, user_id=None, username: str = None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE clients SET tg_checked=1, has_tg=?, tg_user_id=?, tg_username=?, tg_checked_at=? "
            "WHERE phone=?",
            (
                int(has_tg),
                str(user_id) if user_id else None,
                username,
                datetime.now().isoformat(timespec="seconds"),
                phone,
            )
        )


def tg_check_total() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM clients WHERE phone IS NOT NULL AND phone != ''"
        ).fetchone()[0]


def tg_checked_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM clients WHERE tg_checked=1").fetchone()[0]


def tg_found_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM clients WHERE has_tg=1").fetchone()[0]


def get_found_tg_clients() -> list:
    """Все номера, у которых нашёлся Telegram-профиль (для выгрузки/повторной отправки)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT phone, tg_username, tg_user_id FROM clients WHERE has_tg=1 ORDER BY phone ASC"
        ).fetchall()
        return [dict(r) for r in rows]


# ─── КЛИЕНТЫ ────────────────────────────────────────────────────
def normalize_phone(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit())


def find_client_by_phone(phone: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM clients WHERE phone=?", (normalize_phone(phone),)).fetchone()
        return dict(row) if row else None


def add_client(phone: str, name: str = "", has_anketa: bool = False, note: str = ""):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO clients (phone,name,has_anketa,note) VALUES (?,?,?,?)",
            (normalize_phone(phone), name, int(has_anketa), note)
        )


def all_clients():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM clients").fetchall()]


# ─── ПОЛЬЗОВАТЕЛИ / ИСТОРИЯ ─────────────────────────────────────
def _get_state(key: str, default: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return row[0] if row else default


def _set_state(key: str, value: str):
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO state (key,value) VALUES (?,?)", (key, value))


def get_daily_limit() -> int:
    return int(_get_state("daily_limit", str(DEFAULT_DAILY_LIMIT)))


def set_limit(new_limit: int):
    _set_state("daily_limit", str(new_limit))


def get_sent_today() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    last_date = _get_state("last_date", "")
    if last_date != today:
        _set_state("sent_today", "0")
        _set_state("last_date", today)
        return 0
    return int(_get_state("sent_today", "0"))


def can_send() -> bool:
    return get_sent_today() < get_daily_limit()


def register_sent():
    today = datetime.now().strftime("%Y-%m-%d")
    sent = get_sent_today()
    _set_state("sent_today", str(sent + 1))
    _set_state("last_date", today)


# ─── ЮЗЕРЫ ──────────────────────────────────────────────────────
def _load_user(uid: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
        if row:
            d = dict(row)
            d["history"] = json.loads(d["history"])
            return d
        return {"user_id": uid, "msg_count": 0, "notified": 0, "paused": 0, "history": []}


def _save_user(u: dict):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id,msg_count,notified,paused,history) VALUES (?,?,?,?,?)",
            (u["user_id"], u["msg_count"], u["notified"], u["paused"], json.dumps(u["history"], ensure_ascii=False))
        )


def get_user(uid: str) -> dict:
    return _load_user(uid)


def register_user_message(uid: str) -> dict:
    u = _load_user(uid)
    u["msg_count"] += 1
    _save_user(u)
    return u


def append_history(uid: str, role: str, text: str):
    u = _load_user(uid)
    u["history"].append({"role": role, "text": text})
    u["history"] = u["history"][-HISTORY_LIMIT:]
    _save_user(u)


def get_history(uid: str) -> list:
    return _load_user(uid)["history"]


def should_notify_admin(u: dict) -> bool:
    return u["msg_count"] >= NOTIFY_AFTER and not u["notified"]


def pause_user(uid: str):
    u = _load_user(uid)
    u["notified"] = 1
    u["paused"] = 1
    _save_user(u)


def unpause_user(uid: str):
    u = _load_user(uid)
    u["paused"] = 0
    u["notified"] = 0
    u["msg_count"] = 0
    _save_user(u)


def is_paused(uid: str) -> bool:
    return bool(_load_user(uid)["paused"])
