"""Хранение данных бота: облачная база Neon (Postgres) или файл SQLite, плюс резервные копии.

Если задана переменная окружения DATABASE_URL (строка подключения из консоли Neon), данные
хранятся в Neon. Иначе используется файл SQLite (путь в DB_PATH, по умолчанию bot_data.db).
Всё состояние хранится как набор JSON-значений в таблице kv.
"""
import json
import os
import sqlite3
import tempfile
import time

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
SQLITE_HEADER = b"SQLite format 3\x00"

_last = {}          # что уже записано в Postgres (чтобы не слать неизменившееся)
_last_full = 0.0    # когда в последний раз писали всё целиком
FULL_EVERY = 6 * 3600


def backend_name():
    return "Neon (Postgres)" if DATABASE_URL else f"SQLite ({DB_PATH})"


# ---------- SQLite ----------

def _sqlite_connect(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return con


def _sqlite_save(state, path):
    con = _sqlite_connect(path)
    try:
        with con:
            for key, value in state.items():
                con.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
    finally:
        con.close()


def _sqlite_load(path):
    if not os.path.exists(path):
        return {}
    con = _sqlite_connect(path)
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM kv")}
    finally:
        con.close()


# ---------- Neon / Postgres ----------

_CREATE = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
_UPSERT = ("INSERT INTO kv(key, value) VALUES (%s, %s) "
           "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value")


def _pg_connect():
    """Соединение с Neon. Neon засыпает без активности, поэтому первое подключение пробуем несколько раз."""
    try:
        import psycopg2
    except ImportError as e:
        raise RuntimeError("Для DATABASE_URL нужна библиотека psycopg2-binary (добавь её в requirements.txt)") from e
    dsn = DATABASE_URL
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    last = None
    for attempt in range(4):
        try:
            return psycopg2.connect(dsn, connect_timeout=15)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Не удалось подключиться к базе Neon: {last}") from last


def _pg_save(state, only_changed=True):
    global _last_full
    now = time.time()
    if now - _last_full > FULL_EVERY:
        _last.clear()
        only_changed = True
    blobs = {k: json.dumps(v, ensure_ascii=False) for k, v in state.items()}
    todo = {k: b for k, b in blobs.items() if _last.get(k) != b} if only_changed else blobs
    if not todo:
        return
    con = _pg_connect()
    try:
        with con:
            with con.cursor() as cur:
                cur.execute(_CREATE)
                for k, b in todo.items():
                    cur.execute(_UPSERT, (k, b))
    finally:
        con.close()
    _last.update(todo)
    if now - _last_full > FULL_EVERY:
        _last_full = now


def _pg_load():
    con = _pg_connect()
    try:
        with con:
            with con.cursor() as cur:
                cur.execute(_CREATE)
                cur.execute("SELECT key, value FROM kv")
                return {k: json.loads(v) for k, v in cur.fetchall()}
    finally:
        con.close()


# ---------- общий интерфейс ----------

def save(state, path=None):
    """Записывает состояние. Ошибка подключения вылетает наружу: вызывающий решает, что делать."""
    if path is None and DATABASE_URL:
        _pg_save(state)
    else:
        _sqlite_save(state, path or DB_PATH)


def load(path=None):
    """Читает состояние. Пусто: возвращает {}. Не получилось подключиться к Neon: исключение
    (так бот не запустится с пустыми данными и не затрёт ими базу)."""
    if path is None and DATABASE_URL:
        state = _pg_load()
        if not state and os.path.exists(DB_PATH):  # первый переход с файла SQLite на Neon
            old = _sqlite_load(DB_PATH)
            if old:
                _pg_save(old, only_changed=False)
                state = old
        _last.clear()
        _last.update({k: json.dumps(v, ensure_ascii=False) for k, v in state.items()})
        return state
    return _sqlite_load(path or DB_PATH)


def export_bytes(path=None):
    """Копия всех данных одним файлом SQLite (для резервной копии, одинаково для Neon и файла)."""
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        if path is None and DATABASE_URL:
            os.remove(tmp)
            _sqlite_save(_pg_load(), tmp)
        else:
            p = path or DB_PATH
            _sqlite_connect(p).close()  # создаст файл, если его ещё нет
            src, dst = sqlite3.connect(p), sqlite3.connect(tmp)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def validate_bytes(data):
    """Похож ли файл на нашу копию: заголовок SQLite, таблица kv, значения читаются как JSON."""
    if not data.startswith(SQLITE_HEADER):
        return False
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        con = sqlite3.connect(tmp)
        try:
            for _, v in con.execute("SELECT key, value FROM kv").fetchall():
                json.loads(v)
            return True
        finally:
            con.close()
    except Exception:
        return False
    finally:
        os.remove(tmp)


def import_bytes(data, path=None):
    """Заменяет данные присланной копией (после проверки). Возвращает True, если получилось."""
    if not validate_bytes(data):
        return False
    if path is None and DATABASE_URL:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            _pg_save(_sqlite_load(tmp), only_changed=False)
            _last.clear()
        finally:
            os.remove(tmp)
        return True
    p = path or DB_PATH
    tmp = p + ".restore"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return True
