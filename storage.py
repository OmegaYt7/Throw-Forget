"""Хранение данных бота в одном файле SQLite и резервные копии.

Файл базы по умолчанию bot_data.db. Путь можно поменять переменной окружения DB_PATH
(например, на постоянный диск хостинга). Всё состояние хранится как набор JSON-значений.
"""
import json
import os
import sqlite3
import tempfile

DB_PATH = os.getenv("DB_PATH", "bot_data.db")
SQLITE_HEADER = b"SQLite format 3\x00"


def _connect(path=None):
    con = sqlite3.connect(path or DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    return con


def save(state, path=None):
    """Записывает всё состояние одной транзакцией: либо всё сохранилось, либо ничего."""
    con = _connect(path)
    try:
        with con:
            for key, value in state.items():
                con.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))
    finally:
        con.close()


def load(path=None):
    """Читает состояние. Нет файла или он пустой: возвращает {}."""
    p = path or DB_PATH
    if not os.path.exists(p):
        return {}
    con = _connect(p)
    try:
        return {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM kv")}
    finally:
        con.close()


def export_bytes(path=None):
    """Консистентная копия базы одним файлом (для резервной копии)."""
    p = path or DB_PATH
    _connect(p).close()  # создаст файл, если его ещё нет
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src, dst = sqlite3.connect(p), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


def validate_bytes(data):
    """Похож ли файл на нашу базу: заголовок SQLite, таблица kv, значения читаются как JSON."""
    if not data.startswith(SQLITE_HEADER):
        return False
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        con = sqlite3.connect(tmp)
        try:
            rows = con.execute("SELECT key, value FROM kv").fetchall()
            for _, v in rows:
                json.loads(v)
            return True
        finally:
            con.close()
    except Exception:
        return False
    finally:
        os.remove(tmp)


def import_bytes(data, path=None):
    """Заменяет базу присланным файлом (после проверки). Возвращает True, если получилось."""
    if not validate_bytes(data):
        return False
    p = path or DB_PATH
    tmp = p + ".restore"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, p)
    return True
