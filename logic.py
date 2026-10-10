"""Логика бота без Telegram: время, дела, разделы, календарь, разбор текста, ИИ.

Файл bot.py подключает всё отсюда и отвечает только за кнопки и сообщения.
"""
import asyncio
import ipaddress
import itertools
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from dateparser.search import search_dates

GROQ_KEY = os.getenv("GROQ_API_KEY")  # бесплатный ключ с console.groq.com
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DAILY_AI_LIMIT = int(os.getenv("DAILY_AI_LIMIT", "10"))
DEFAULT_TZ = "Europe/Moscow"

# Мероприятие: предупреждения до начала (минуты -> подпись)
EV_LABELS = {0: "в момент", 10: "за 10 м", 30: "за 30 м", 60: "за 1 ч", 180: "за 3 ч", 1440: "за 1 д"}
EV_LEFT = {10: "10 м", 30: "30 м", 60: "1 ч", 180: "3 ч", 1440: "1 д"}
# Задача: напоминания относительно срока (минуты -> подпись)
TK_LABELS = {0: "в день срока", 1440: "за 1 д", 4320: "за 3 д"}
TK_HOURS = [7, 8, 9, 10, 12, 15, 18, 20]   # время напоминания о задачах
REPEATS = [1, 2, 3, 6, 12, 24]             # повтор для задач, в часах (минимум 1 ч)
MAX_REPEAT = 72
MAX_NAGS = 12
MAX_SECTIONS = 15
MAX_CHECKS = 20
EVENT_MIN = 60                              # мероприятие занимает 1 ч (для пересечений и календаря)
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
KIND_NAME = {"task": "📝 Задачи", "event": "📅 Мероприятия"}

PRESETS = [
    ("🇷🇺 Москва +3", "Europe/Moscow"), ("🇺🇦 Киев", "Europe/Kyiv"),
    ("🇧🇾 Минск +3", "Europe/Minsk"), ("🇷🇺 Калининград +2", "Europe/Kaliningrad"),
    ("🇷🇺 Самара +4", "Europe/Samara"), ("🇷🇺 Екатеринбург +5", "Asia/Yekaterinburg"),
    ("🇰🇿 Алматы +5", "Asia/Almaty"), ("🇷🇺 Омск +6", "Asia/Omsk"),
    ("🇷🇺 Новосибирск +7", "Asia/Novosibirsk"), ("🇷🇺 Иркутск +8", "Asia/Irkutsk"),
    ("🇷🇺 Якутск +9", "Asia/Yakutsk"), ("🇷🇺 Владивосток +10", "Asia/Vladivostok"),
]
CITIES = {
    "москва": "Europe/Moscow", "санкт-петербург": "Europe/Moscow", "спб": "Europe/Moscow",
    "питер": "Europe/Moscow", "казань": "Europe/Moscow", "нижний новгород": "Europe/Moscow",
    "краснодар": "Europe/Moscow", "воронеж": "Europe/Moscow", "ростов-на-дону": "Europe/Moscow",
    "киев": "Europe/Kyiv", "минск": "Europe/Minsk", "калининград": "Europe/Kaliningrad",
    "самара": "Europe/Samara", "екатеринбург": "Asia/Yekaterinburg", "уфа": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg", "пермь": "Asia/Yekaterinburg", "омск": "Asia/Omsk",
    "новосибирск": "Asia/Novosibirsk", "красноярск": "Asia/Krasnoyarsk", "иркутск": "Asia/Irkutsk",
    "якутск": "Asia/Yakutsk", "владивосток": "Asia/Vladivostok", "хабаровск": "Asia/Vladivostok",
    "алматы": "Asia/Almaty", "астана": "Asia/Almaty", "ташкент": "Asia/Tashkent",
    "баку": "Asia/Baku", "тбилиси": "Asia/Tbilisi", "ереван": "Asia/Yerevan",
    "кишинев": "Europe/Chisinau", "вильнюс": "Europe/Vilnius", "рига": "Europe/Riga",
    "таллин": "Europe/Tallinn", "варшава": "Europe/Warsaw", "берлин": "Europe/Berlin",
    "лондон": "Europe/London", "стамбул": "Europe/Istanbul", "дубай": "Asia/Dubai",
    "нью-йорк": "America/New_York",
}

# Пока без базы: всё в памяти, при перезапуске пропадает
ITEMS = []       # дела: kind = "task" | "event"
SECTIONS = []    # разделы: {id, user_id, kind, name}
DRAFTS = {}      # черновики, ждущие выбора «задача / мероприятие»
STATE = {}       # user_id -> что бот ждёт от человека следующим сообщением
USER_TZ = {}
USER_SET = {}    # настройки по умолчанию
AI_USED = {}
_ids, _sids, _dids = itertools.count(1), itertools.count(1), itertools.count(1)
URL_RE = re.compile(r"https?://\S+")

TZ_PROMPT = (
    "🌍 Выбери свой часовой пояс. По нему я буду ставить время и присылать напоминания.\n\n"
    "Нет в списке? Нажми «Ввести вручную»: напиши город, пояс (+3 или UTC+5) "
    "или просто сколько сейчас у тебя времени (например 15:30)."
)
SETTINGS_TEXT = (
    "🔔 Напоминания\n\n"
    "Это настройки по умолчанию для новых дел. Для отдельного дела их можно поменять "
    "на его карточке кнопкой «🔔 Напоминания».\n\n"
    "📅 Мероприятия: предупреждаю заранее (за 10 м, 1 ч, 1 д и так далее).\n"
    "📝 Задачи: напоминаю утром в день срока, можно повторять каждые N ч, пока не нажмёшь «Готово»."
)


# ---------- настройки и общие помощники ----------

def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def get_tz(uid):
    return USER_TZ.get(uid, DEFAULT_TZ)


def get_set(uid):
    return USER_SET.setdefault(uid, {
        "ev_offsets": [60, 0],   # мероприятия: за 1 ч и в момент
        "tk_offsets": [0],       # задачи: в день срока
        "tk_hour": 9,            # во сколько напоминать о задачах
        "repeat": 0,             # повтор для задач, часы (0 = выкл)
    })


def ai_allowed(uid):
    today = date.today()
    d, n = AI_USED.get(uid, (today, 0))
    if d != today:
        n = 0
    if n >= DAILY_AI_LIMIT:
        return False
    AI_USED[uid] = (today, n + 1)
    return True


# ---------- часовые пояса ----------

def zone(name):
    """Поддерживает названия вроде Europe/Moscow и смещения вроде UTC+5:30."""
    try:
        return ZoneInfo(name)
    except Exception:
        pass
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", name or "")
    if not m:
        return ZoneInfo(DEFAULT_TZ)
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0)))


def offset_name(minutes):
    sign = "+" if minutes >= 0 else "-"
    h, mi = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{mi:02d}" if mi else "")


def parse_tz_input(text):
    """Понимает: «Москва», «+3», «UTC+5:30», «15:30» (твоё время сейчас), «Europe/Kyiv»."""
    raw = text.strip()
    t = raw.lower().replace("ё", "е")
    m = re.fullmatch(r"(?:utc|gmt)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", t)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h > 14 or mi > 59:
            return None
        return offset_name((1 if m.group(1) == "+" else -1) * (h * 60 + mi))
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            return None
        utc = datetime.now(timezone.utc)
        diff = round((utc.replace(hour=h, minute=mi, second=0, microsecond=0) - utc).total_seconds() / 60)
        while diff > 14 * 60:
            diff -= 1440
        while diff < -12 * 60:
            diff += 1440
        return offset_name(round(diff / 15) * 15)
    if t in CITIES:
        return CITIES[t]
    try:
        ZoneInfo(raw)
        return raw
    except Exception:
        return None


# ---------- время: разбор текста ----------

def parse_local(text, tz):
    """Естественный язык через dateparser: «завтра в 18:00», «в пятницу». Возвращает (название, ts)."""
    z = zone(tz)
    now = datetime.now(z)
    found = search_dates(
        text, languages=["ru"],
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None)},
    )
    if not found:
        return None
    fragment, dt = found[0]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=z)
    if not re.search(r"\d|через|час|минут|секунд|утр|вечер|дн[её]м|ночь|полдень|полночь", fragment.lower()):
        dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if dt <= now:
            dt = now + timedelta(hours=1)
    title = text.replace(fragment, "").strip(" ,.-—:") or text
    return title, int(dt.timestamp())


def safe_local(text, tz):
    try:
        return parse_local(text, tz)
    except Exception:
        return None


def parse_when(text, tz):
    """Свой разбор для ввода руками: «18:30», «завтра 9:00», «25.10 14:00», «через 40 м»."""
    z = zone(tz)
    now = datetime.now(z)
    today = now.date()
    t = text.lower().strip()

    m = re.search(r"через\s+(\d+)\s*(мин\w*|м\b|час\w*|ч\b|дн\w*|д\b)", t)
    if m:
        n, u = int(m.group(1)), m.group(2)
        if u.startswith("м"):
            delta = timedelta(minutes=n)
        elif u.startswith(("час", "ч")):
            delta = timedelta(hours=n)
        else:
            delta = timedelta(days=n)
        return int((now + delta).timestamp())

    hh = mm = None
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        t = t.replace(m.group(0), " ")
    else:
        m = re.search(r"\bв\s*(\d{1,2})\b(?![.:\d])", t)
        if m:
            hh, mm = int(m.group(1)), 0
            t = t.replace(m.group(0), " ")
    if hh is not None and (hh > 23 or mm > 59):
        return None

    day = None
    if "послезавтра" in t:
        day = today + timedelta(days=2)
    elif "завтра" in t:
        day = today + timedelta(days=1)
    elif "сегодня" in t:
        day = today
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b", t)
        if m:
            d_, mo = int(m.group(1)), int(m.group(2))
            y = int(m.group(3)) if m.group(3) else today.year
            if y < 100:
                y += 2000
            try:
                day = date(y, mo, d_)
            except ValueError:
                return None
            if not m.group(3) and day < today:
                day = date(y + 1, mo, d_)

    if hh is None and day is None:
        return None
    if hh is None:
        hh, mm = 9, 0
    if day is None:
        day = today
        if datetime.combine(day, dtime(hh, mm), tzinfo=z) <= now:
            day = today + timedelta(days=1)
    return int(datetime.combine(day, dtime(hh, mm), tzinfo=z).timestamp())


def has_explicit_time(text):
    return bool(re.search(r"\d{1,2}:\d{2}|\bв\s*\d|через", text.lower()))


def extract_time(text):
    """Достаёт время из текста: «Встреча в 18:30» -> («Встреча», 18, 30). Нет времени -> None."""
    m = re.search(r"(?:\bв\s+)?\b(\d{1,2}):(\d{2})\b", text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(r"\bв\s*(\d{1,2})\b(?![.:\d])", text)
        if not m:
            return None
        hh, mm = int(m.group(1)), 0
    if hh > 23 or mm > 59:
        return None
    title = " ".join((text[:m.start()] + " " + text[m.end():]).split()).strip(" ,.-—:")
    return (title or text), hh, mm


def ymd(d):
    return d.strftime("%Y%m%d")


def parse_day(s):
    return datetime.strptime(s, "%Y%m%d").date()


def fmt_day(day):
    return f"{WD[day.weekday()]} {day:%d.%m.%Y}"


def fmt_time(ts, tz):
    dt = datetime.fromtimestamp(ts, zone(tz))
    return f"{WD[dt.weekday()]} {dt:%d.%m.%Y} в {dt:%H:%M}"


def fmt_date(ts, tz):
    return fmt_day(datetime.fromtimestamp(ts, zone(tz)).date())


def task_ts(day, uid, tz):
    """Срок задачи датой: напоминание в выбранное время суток. Если оно сегодня уже прошло, через 1 ч."""
    z = zone(tz)
    ts = int(datetime.combine(day, dtime(get_set(uid)["tk_hour"], 0), tzinfo=z).timestamp())
    return ts if ts > time.time() else int(time.time()) + 3600


def is_overdue(e, tz):
    if e["kind"] != "task" or not e["due"] or e["done"]:
        return False
    z = zone(tz)
    return datetime.now(z).date() > datetime.fromtimestamp(e["due"], z).date()


# ---------- дела и разделы ----------

def secs(uid, kind):
    return [s for s in SECTIONS if s["user_id"] == uid and s["kind"] == kind]


def get_sec(sid, uid):
    return next((s for s in SECTIONS if s["id"] == sid and s["user_id"] == uid), None)


def make_section(uid, kind, name):
    name = " ".join(name.split())[:30]
    if not name:
        raise ValueError("Название не должно быть пустым.")
    for s in secs(uid, kind):
        if s["name"].casefold() == name.casefold():
            return s
    if len(secs(uid, kind)) >= MAX_SECTIONS:
        raise ValueError(f"Можно создать не больше {MAX_SECTIONS} разделов. Удали ненужные.")
    s = {"id": next(_sids), "user_id": uid, "kind": kind, "name": name}
    SECTIONS.append(s)
    return s


def items_of(uid, kind=None, sid=None, done=False):
    return [e for e in ITEMS if e["user_id"] == uid and e["done"] == done
            and (kind is None or e["kind"] == kind) and (sid is None or e["sec"] == sid)]


def find(eid, uid):
    return next((e for e in ITEMS if e["id"] == eid and e["user_id"] == uid), None)


def reset_fired(e):
    """Уже прошедшие напоминания не шлём задним числом."""
    now = int(time.time())
    e["nags"] = 0
    e["nag_at"] = None
    e["snooze"] = None
    e["fired"] = {o for o in e["offsets"] if e["due"] and e["due"] - o * 60 <= now}


def add_item(uid, kind, title, due=None, place=None, note=None, sec=0):
    s = get_set(uid)
    task = kind == "task"
    e = {"id": next(_ids), "user_id": uid, "kind": kind, "title": title, "due": due,
         "place": place, "note": note, "sec": sec, "done": False,
         "offsets": list(s["tk_offsets"] if task else s["ev_offsets"]),
         "repeat": s["repeat"] if task else 0,
         "checks": [], "fired": set(), "nags": 0, "nag_at": None, "snooze": None}
    reset_fired(e)
    ITEMS.append(e)
    return e


# ---------- календарь и пересечения ----------

def conflicts_for(e):
    """Другие мероприятия, которые пересекаются с этим по времени (каждое занимает EVENT_MIN минут)."""
    if e["kind"] != "event" or not e["due"] or e["done"]:
        return []
    span = EVENT_MIN * 60
    out = [o for o in ITEMS
           if o is not e and o["user_id"] == e["user_id"] and o["kind"] == "event"
           and not o["done"] and o["due"]
           and e["due"] < o["due"] + span and o["due"] < e["due"] + span]
    return sorted(out, key=lambda x: x["due"])


def items_on_day(uid, day, tz):
    z = zone(tz)
    res = [e for e in ITEMS if e["user_id"] == uid and e["due"]
           and datetime.fromtimestamp(e["due"], z).date() == day]
    return sorted(res, key=lambda e: e["due"])


def month_counts(uid, year, month, tz):
    """День месяца -> сколько невыполненных дел с этой датой."""
    z = zone(tz)
    res = {}
    for e in ITEMS:
        if e["user_id"] != uid or e["done"] or not e["due"]:
            continue
        d = datetime.fromtimestamp(e["due"], z)
        if d.year == year and d.month == month:
            res[d.day] = res.get(d.day, 0) + 1
    return res


def month_summary(uid, year, month, tz):
    z = zone(tz)
    ev = tk = 0
    for e in ITEMS:
        if e["user_id"] != uid or e["done"] or not e["due"]:
            continue
        d = datetime.fromtimestamp(e["due"], z)
        if d.year == year and d.month == month:
            if e["kind"] == "event":
                ev += 1
            else:
                tk += 1
    return ev, tk


# ---------- тексты карточки ----------

def remind_text(e, tz):
    if e["kind"] == "event":
        parts = [EV_LABELS[o] for o in sorted(e["offsets"], reverse=True)]
        return ", ".join(parts) if parts else "без напоминаний"
    parts = [TK_LABELS[o] for o in sorted(e["offsets"], reverse=True)]
    s = ", ".join(parts) if parts else "без напоминаний"
    if parts and e["due"]:
        s += f" (в {datetime.fromtimestamp(e['due'], zone(tz)):%H:%M})"
    return s


def card(e, tz):
    z = zone(tz)
    event = e["kind"] == "event"
    lines = [("📅 " if event else "📝 ") + e["title"]]
    s = next((x["name"] for x in SECTIONS if x["id"] == e["sec"]), None) if e["sec"] else None
    if s:
        lines.append("📂 Раздел: " + s)
    if e["due"]:
        lines.append(("🕐 " if event else "📅 Срок: ") + (fmt_time(e["due"], tz) if event else fmt_date(e["due"], tz)))
    elif event:
        lines.append("🕐 Время не задано. Нажми «Задать время»")
    else:
        lines.append("📅 Срок не задан")
    if is_overdue(e, tz):
        lines.append("⚠️ Просрочено")
    cf = conflicts_for(e)
    if cf:
        lines.append("⚠️ В это время уже есть другое мероприятие:")
        lines += [f"   • {datetime.fromtimestamp(o['due'], z):%H:%M} {o['title']}" for o in cf[:3]]
        lines.append("   Это не запрет: можно оставить как есть или изменить время.")
    if e["place"]:
        lines.append("📍 " + e["place"])
    if e["note"]:
        lines.append("💬 " + e["note"])
    if e["due"]:
        lines.append("🔔 " + remind_text(e, tz))
        if e["kind"] == "task" and e["repeat"]:
            lines.append(f"🔁 Повтор: каждые {e['repeat']} ч, пока не «Готово»")
    if e["checks"]:
        done = sum(1 for c in e["checks"] if c["d"])
        lines.append(f"📋 Чек-лист: {done}/{len(e['checks'])}")
        lines += [("✅ " if c["d"] else "⬜️ ") + c["t"] for c in e["checks"][:10]]
    if e["done"]:
        lines.append("✅ Выполнено")
    return "\n".join(lines)


def saved_title(e):
    s = next((x["name"] for x in SECTIONS if x["id"] == e["sec"]), None) if e["sec"] else None
    base = "✅ Мероприятие сохранено" if e["kind"] == "event" else "✅ Задача сохранена"
    return base + (f" в раздел «{s}»" if s else "")


def next_hint(e, tz):
    """Подсказка «что дальше» под карточкой, чтобы человек понимал, что делать."""
    if e["kind"] == "event":
        if not e["due"]:
            return ""
        return ("Что будет дальше:\n"
                f"• Я предупрежу: {remind_text(e, tz)}.\n"
                "• Нужны другие предупреждения? Нажми «🔔 Напоминания».\n"
                "• «📍 Место» добавит адрес, а «📆 В календарь» пришлёт файл для календаря телефона.")
    if not e["due"]:
        h = get_set(e["user_id"])["tk_hour"]
        return ("Что дальше:\n"
                f"• Срок пока не задан, поэтому напоминаний не будет. Нажми «📅 Задать срок», "
                f"и я напомню утром в {h:02d}:00 в день срока.\n"
                "• «📋 Чек-лист» поможет разбить дело на пункты с галочками.\n"
                "• «📂 Раздел» разложит дела по папкам, например «Работа» или «Дом».")
    return ("Что будет дальше:\n"
            f"• Я напомню: {remind_text(e, tz)}.\n"
            "• Если дело большое, разбей его на пункты кнопкой «📋 Чек-лист».\n"
            "• Сделаешь раньше? Нажми «✅ Готово».")


def item_label(e, tz):
    z = zone(tz)
    if e["kind"] == "event":
        when = datetime.fromtimestamp(e["due"], z).strftime("%d.%m %H:%M  ") if e["due"] else "без времени  "
        return when + e["title"]
    if e["due"]:
        mark = "🔴 " if is_overdue(e, tz) else ""
        return f"{mark}до {datetime.fromtimestamp(e['due'], z):%d.%m}  {e['title']}"
    return e["title"]


def add_prompt(kind, sid, uid):
    s = get_sec(sid, uid) if sid else None
    where = f" в раздел «{s['name']}»" if s else ""
    if kind == "task":
        return (f"📝 Напиши задачу{where}.\n\nНапример: «купить молоко» или «сдать отчёт завтра». "
                "Если назовёшь срок, я поставлю напоминание. Если нет, срок можно задать кнопкой потом.")
    return (f"📅 Напиши мероприятие{where} и когда оно.\n\nНапример: «Встреча с Олей завтра в 18:00». "
            "Если время не назовёшь, я предложу выбрать его кнопками.")


def day_add_prompt(kind, day):
    d = fmt_day(day)
    if kind == "task":
        return (f"📝 Напиши задачу со сроком на {d}.\n\nНапример: «Сдать отчёт». Я поставлю срок на этот день "
                "и напомню о нём утром в выбранное в настройках время.")
    return (f"📅 Напиши мероприятие на {d}.\n\nМожно сразу указать время, например «Встреча с Олей в 18:30». "
            "Если времени не будет, я предложу выбрать его кнопками. Если в это время уже есть другое "
            "мероприятие, я предупрежу, но добавить всё равно можно.")


# ---------- календарный файл .ics ----------

def ics_escape(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(e):
    fmt = "%Y%m%dT%H%M%SZ"
    start = datetime.fromtimestamp(e["due"], timezone.utc)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kin i zabud//RU", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{e['id']}-{e['user_id']}-{e['due']}@kinzabud",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{(start + timedelta(minutes=EVENT_MIN)).strftime(fmt)}",
        f"SUMMARY:{ics_escape(e['title'])}",
    ]
    if e["place"]:
        lines.append(f"LOCATION:{ics_escape(e['place'])}")
    if e["note"]:
        lines.append(f"DESCRIPTION:{ics_escape(e['note'])}")
    for o in e["offsets"]:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_escape(e['title'])}",
                  f"TRIGGER:-PT{o}M" if o else "TRIGGER:PT0M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ---------- ссылки и ИИ ----------

async def is_public(url):
    try:
        host = urlparse(url).hostname
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


async def fetch_page(url):
    if not await is_public(url):
        return None
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                raw = await r.content.read(300_000)
    except Exception:
        return None
    html = raw.decode("utf-8", errors="ignore")
    html = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html)
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()[:4000]


async def ask_llm(text, tz):
    now = datetime.now(zone(tz)).strftime("%Y-%m-%d %H:%M, %A")
    system = (
        "Ты помощник-планировщик. Из текста пользователя выдели задачи и события. "
        f"Сейчас {now}, часовой пояс {tz}. Отвечай ТОЛЬКО JSON такого вида: "
        '{"items":[{"title":"коротко","datetime":"YYYY-MM-DDTHH:MM или null",'
        '"place":"строка или null","note":"строка или null"}]}. '
        "Если времени у дела нет, datetime = null. "
        "Если дата есть, а времени нет, ставь 09:00. Ничего не выдумывай."
    )
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": text[:6000]}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"},
            ) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        return json.loads(data["choices"][0]["message"]["content"]).get("items", [])
    except Exception:
        return None


def to_ts(value, tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone(tz))
        return int(dt.timestamp())
    except Exception:
        return None


# ---------- черновики: «Что это: задача или мероприятие?» ----------

def new_draft(uid, title, due, place=None, note=None):
    did = next(_dids)
    DRAFTS[did] = {"uid": uid, "title": title, "due": due, "place": place, "note": note, "kind": None}
    return did


def finalize_draft(did, sid):
    d = DRAFTS.pop(did)
    return add_item(d["uid"], d["kind"], d["title"], d["due"], d["place"], d["note"], sid)
