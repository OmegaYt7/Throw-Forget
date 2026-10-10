import asyncio
import ipaddress
import itertools
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dateparser.search import search_dates

logging.basicConfig(level=logging.INFO)

# Всё секретное берётся из переменных окружения, а не из кода
TOKEN = os.getenv("BOT_TOKEN")
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
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Кнопки главного меню (внизу экрана)
B_ADD = "➕ Добавить"
B_LIST = "📋 Мои дела"
B_REM = "🔔 Напоминания"
B_TZ = "🌍 Часовой пояс"
B_HELP = "❓ Помощь"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_ADD), KeyboardButton(text=B_LIST)],
              [KeyboardButton(text=B_REM), KeyboardButton(text=B_TZ)],
              [KeyboardButton(text=B_HELP)]],
    resize_keyboard=True, is_persistent=True,
)

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

dp = Dispatcher()


# ---------- мелкие помощники ----------

def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


TZ_PROMPT = (
    "🌍 Выбери свой часовой пояс. По нему я буду ставить время и присылать напоминания.\n\n"
    "Нет в списке? Нажми «Ввести вручную»: напиши город, пояс (+3 или UTC+5) "
    "или просто сколько сейчас у тебя времени (например 15:30)."
)


def tz_kb():
    rows = chunk([btn(label, f"tz:{name}") for label, name in PRESETS], 2)
    rows.append([btn("✍️ Ввести вручную", "tz:manual")])
    return markup(rows)


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


def ymd(d):
    return d.strftime("%Y%m%d")


def parse_day(s):
    return datetime.strptime(s, "%Y%m%d").date()


def fmt_time(ts, tz):
    dt = datetime.fromtimestamp(ts, zone(tz))
    return f"{WD[dt.weekday()]} {dt:%d.%m.%Y} в {dt:%H:%M}"


def fmt_date(ts, tz):
    dt = datetime.fromtimestamp(ts, zone(tz))
    return f"{WD[dt.weekday()]} {dt:%d.%m.%Y}"


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


# ---------- карточка и кнопки ----------

def remind_text(e, tz):
    if e["kind"] == "event":
        parts = [EV_LABELS[o] for o in sorted(e["offsets"], reverse=True)]
        return ", ".join(parts) if parts else "без напоминаний"
    parts = [TK_LABELS[o] for o in sorted(e["offsets"], reverse=True)]
    s = ", ".join(parts) if parts else "без напоминаний"
    if parts and e["due"]:
        s += f" (в {datetime.fromtimestamp(e['due'], zone(tz)):%H:%M})"
    if e["repeat"]:
        s += f"\n🔁 Повтор: каждые {e['repeat']} ч, пока не «Готово»"
    return s


def card(e, tz):
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
    if e["place"]:
        lines.append("📍 " + e["place"])
    if e["note"]:
        lines.append("💬 " + e["note"])
    if e["due"]:
        lines.append("🔔 " + remind_text(e, tz))
    if e["checks"]:
        done = sum(1 for c in e["checks"] if c["d"])
        lines.append(f"📋 Чек-лист: {done}/{len(e['checks'])}")
        lines += [("✅ " if c["d"] else "⬜️ ") + c["t"] for c in e["checks"][:10]]
    if e["done"]:
        lines.append("✅ Выполнено")
    return "\n".join(lines)


def kb(e):
    eid = e["id"]
    if e["done"]:
        return markup([[btn("↩️ Вернуть", f"undo:{eid}"), btn("🗑 Удалить", f"del:{eid}")]])
    top = [btn("✅ Готово", f"done:{eid}"), btn("🗑 Удалить", f"del:{eid}")]
    if e["kind"] == "event":
        row2 = [btn("🕐 Изменить время" if e["due"] else "🕐 Задать время", f"tp:{eid}:back")]
        if e["due"]:
            row2.append(btn("🔔 Напоминания", f"cfg:{eid}:e:main"))
        return markup([
            top, row2,
            [btn("📍 Место", f"pl:{eid}"), btn("📆 В календарь", f"ics:{eid}")],
            [btn("📂 Раздел", f"mv:{eid}"), btn("🔄 Сменить тип", f"kind:{eid}")],
        ])
    row2 = [btn("📅 Изменить срок" if e["due"] else "📅 Задать срок", f"tp:{eid}:back")]
    if e["due"]:
        row2.append(btn("🔔 Напоминания", f"cfg:{eid}:t:main"))
    return markup([
        top, row2,
        [btn("📋 Чек-лист", f"ck:{eid}:open"), btn("📂 Раздел", f"mv:{eid}")],
        [btn("🔄 Сменить тип", f"kind:{eid}")],
    ])


def kb_remind(e):
    eid = e["id"]
    return markup([[btn("✅ Готово", f"done:{eid}"),
                    btn("⏰ +15 м", f"snz:{eid}:15"),
                    btn("⏰ +1 ч", f"snz:{eid}:60")]])


def item_response(e, tz, prefix=""):
    """Мероприятие без времени сразу ведёт к выбору времени, остальное показывает карточкой."""
    if e["kind"] == "event" and not e["due"] and not e["done"]:
        return picker_days(e, tz, prefix)
    head = prefix + "\n" if prefix else ""
    return head + card(e, tz), kb(e)


# ---------- выбор времени (мероприятие) и срока (задача) кнопками ----------

def picker_start(e, tz, prefix=""):
    return picker_days(e, tz, prefix) if e["kind"] == "event" else picker_dates(e, tz, prefix)


def picker_dates(e, tz, prefix=""):
    """Задача: только дата срока, время напоминания берётся из настроек."""
    today = datetime.now(zone(tz)).date()
    eid = e["id"]
    rows = [[btn("Сегодня", f"tp:{eid}:dd:{ymd(today)}"),
             btn("Завтра", f"tp:{eid}:dd:{ymd(today + timedelta(days=1))}")]]
    days = [today + timedelta(days=i) for i in range(2, 14)]
    rows += chunk([btn(f"{WD[d.weekday()]} {d:%d.%m}", f"tp:{eid}:dd:{ymd(d)}") for d in days], 4)
    rows.append([btn("✍️ Написать дату", f"tp:{eid}:txt")])
    rows.append([btn("◀️ Отмена", f"tp:{eid}:x")])
    head = prefix + "\n\n" if prefix else ""
    text = (f"{head}📝 {e['title']}\n\n📅 До какого числа нужно сделать?\n"
            "Выбери срок. Напомню утром в выбранное в настройках время.")
    return text, markup(rows)


def picker_days(e, tz, prefix=""):
    today = datetime.now(zone(tz)).date()
    eid = e["id"]
    rows = [
        [btn("⏳ Через 15 м", f"tp:{eid}:in:15"), btn("⏳ Через 1 ч", f"tp:{eid}:in:60"),
         btn("⏳ Через 3 ч", f"tp:{eid}:in:180")],
        [btn("Сегодня", f"tp:{eid}:d:{ymd(today)}"),
         btn("Завтра", f"tp:{eid}:d:{ymd(today + timedelta(days=1))}")],
    ]
    days = [today + timedelta(days=i) for i in range(2, 14)]
    rows += chunk([btn(f"{WD[d.weekday()]} {d:%d.%m}", f"tp:{eid}:d:{ymd(d)}") for d in days], 4)
    rows.append([btn("✍️ Написать дату и время", f"tp:{eid}:txt")])
    rows.append([btn("◀️ Отмена", f"tp:{eid}:x")])
    head = prefix + "\n\n" if prefix else ""
    text = (f"{head}📅 {e['title']}\n\n🗓 Когда будет мероприятие?\n"
            "Выбери день или быстрый вариант «через…». Точное время выберешь следующим шагом.")
    return text, markup(rows)


def picker_hours(e, tz, day):
    now = datetime.now(zone(tz))
    eid = e["id"]
    start = 0
    if day == now.date():
        start = now.hour if now.minute < 55 else now.hour + 1
    hours = list(range(start, 24))
    rows = chunk([btn(f"{h:02d}:00", f"tp:{eid}:h:{ymd(day)}:{h:02d}") for h in hours], 4)
    rows.append([btn("◀️ Другой день", f"tp:{eid}:back")])
    body = "Выбери час. Минуты выберешь следующим шагом." if hours else "Сегодня уже не успеть. Вернись и выбери другой день."
    return f"{e['title']}\n\n🗓 {WD[day.weekday()]} {day:%d.%m.%Y}\n🕐 Во сколько? {body}", markup(rows)


def picker_minutes(e, tz, day, hour):
    now = datetime.now(zone(tz))
    eid = e["id"]
    mins = list(range(0, 60, 5))
    if day == now.date() and hour == now.hour:
        mins = [x for x in mins if x > now.minute]
    rows = chunk([btn(f"{hour:02d}:{x:02d}", f"tp:{eid}:ok:{ymd(day)}:{hour:02d}{x:02d}") for x in mins], 4)
    rows.append([btn("◀️ Другой час", f"tp:{eid}:d:{ymd(day)}")])
    text = (f"{e['title']}\n\n🗓 {WD[day.weekday()]} {day:%d.%m.%Y}, {hour:02d}:xx\n"
            "Выбери точное время. Нужно другое, например 18:07? Вернись назад и нажми «Написать дату и время».")
    return text, markup(rows)


# ---------- настройки напоминаний (для всех дел и для одного дела) ----------

SETTINGS_TEXT = (
    "🔔 Напоминания\n\n"
    "Это настройки по умолчанию для новых дел. Для отдельного дела их можно поменять "
    "на его карточке кнопкой «🔔 Напоминания».\n\n"
    "📅 Мероприятия: предупреждаю заранее (за 10 м, 1 ч, 1 д и так далее).\n"
    "📝 Задачи: напоминаю утром в день срока, можно повторять каждые N ч, пока не нажмёшь «Готово»."
)


def settings_kb():
    return markup([
        [btn("📅 Мероприятия", "cfg:0:e:main"), btn("📝 Задачи", "cfg:0:t:main")],
        [btn("👌 Закрыть", "cfg:0:e:close")],
    ])


def cfg_screen(uid, e, kind, scr):
    """Один экран настроек. e = дело или None (тогда настройки по умолчанию)."""
    s = get_set(uid)
    z = zone(get_tz(uid))
    eid = e["id"] if e else 0
    k = "e" if kind == "event" else "t"
    offs = e["offsets"] if e else s["ev_offsets" if kind == "event" else "tk_offsets"]
    rep = e["repeat"] if e else s["repeat"]
    head = (card(e, get_tz(uid)) + "\n\n") if e else ""
    close = btn("👌 Закрыть", f"cfg:{eid}:{k}:close") if e else btn("◀️ Назад", f"cfg:0:{k}:hub")
    back = btn("◀️ Назад", f"cfg:{eid}:{k}:main")

    if kind == "event":
        rows = chunk([btn(("✅ " if o in offs else "▫️ ") + lbl, f"cfg:{eid}:e:o{o}")
                      for o, lbl in EV_LABELS.items()], 2)
        rows.append([close])
        intro = ("👇 Отметь, когда предупредить о начале (можно несколько)" if e else
                 "📅 Напоминания о мероприятиях\n\nПредупрежу о начале заранее. "
                 "Отметь, когда напомнить (можно несколько).")
        return head + intro, markup(rows)

    if scr == "so":
        rows = [[btn(("✅ " if o in offs else "▫️ ") + lbl, f"cfg:{eid}:t:o{o}")] for o, lbl in TK_LABELS.items()]
        rows.append([back])
        return head + "🔔 Когда напомнить о сроке? Отметь нужное (можно несколько).", markup(rows)

    if scr == "sh":
        if e:
            cur = datetime.fromtimestamp(e["due"], z)
            cur_h = cur.hour if cur.minute == 0 else None
        else:
            cur_h = s["tk_hour"]
        rows = chunk([btn(("✅ " if h == cur_h else "") + f"{h:02d}:00", f"cfg:{eid}:t:h{h}") for h in TK_HOURS], 4)
        rows.append([back])
        return head + "🕘 Во сколько напоминать? Это время суток, когда я напомню о сроке.", markup(rows)

    if scr == "sr":
        row = [btn(("✅ " if rep == 0 else "") + "Выкл", f"cfg:{eid}:t:r0")]
        row += [btn(("✅ " if rep == n else "") + f"{n} ч", f"cfg:{eid}:t:r{n}") for n in REPEATS]
        custom = rep > 0 and rep not in REPEATS
        row.append(btn(f"✅ {rep} ч (изменить)" if custom else "✍️ Своё", f"cfg:{eid}:t:rx"))
        rows = chunk(row, 4)
        rows.append([back])
        return (head + "🔁 Повторять напоминание, пока не нажмёшь «Готово»?\n"
                f"Выбери, как часто. Минимум 1 ч, не больше {MAX_NAGS} раз."), markup(rows)

    # главный экран настроек задач
    summary = ", ".join(TK_LABELS[o] for o in sorted(offs, reverse=True)) or "не напоминать"
    hour_txt = datetime.fromtimestamp(e["due"], z).strftime("%H:%M") if e else f"{s['tk_hour']:02d}:00"
    rows = [
        [btn(f"🔔 Когда: {summary}", f"cfg:{eid}:t:so")],
        [btn(f"🕘 Время: {hour_txt}", f"cfg:{eid}:t:sh")],
        [btn("🔁 Повтор: " + (f"каждые {rep} ч" if rep else "выкл"), f"cfg:{eid}:t:sr")],
        [close],
    ]
    intro = ("👇 Выбери, что поменять" if e else
             "📝 Напоминания о задачах\n\nУ задачи есть срок (дата). Напоминаю утром в выбранное время. "
             "С повтором буду напоминать снова и снова, пока не нажмёшь «Готово».\n\nВыбери, что настроить:")
    return head + intro, markup(rows)


# ---------- чек-лист, место, календарь ----------

def check_view(e):
    eid = e["id"]
    checks = e["checks"]
    rows = [[btn(("✅ " if c["d"] else "⬜️ ") + c["t"][:50], f"ck:{eid}:t{i}")] for i, c in enumerate(checks)]
    rows.append([btn("➕ Пункт", f"ck:{eid}:add")])
    if any(c["d"] for c in checks):
        rows.append([btn("🧹 Убрать отмеченные", f"ck:{eid}:clr")])
    rows.append([btn("◀️ К задаче", f"ck:{eid}:back")])
    if checks:
        done = sum(1 for c in checks if c["d"])
        body = f"Отмечено {done} из {len(checks)}. Нажми на пункт, чтобы отметить."
    else:
        body = "Пока пусто. Нажми «➕ Пункт» и напиши пункты, каждый с новой строки."
    return f"📋 Чек-лист\n«{e['title']}»\n\n{body}", markup(rows)


def ics_escape(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(e):
    fmt = "%Y%m%dT%H%M%SZ"
    start = datetime.fromtimestamp(e["due"], timezone.utc)
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Kin i zabud//RU", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT", f"UID:{e['id']}-{e['user_id']}-{e['due']}@kinzabud",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime(fmt)}",
        f"DTSTART:{start.strftime(fmt)}", f"DTEND:{(start + timedelta(hours=1)).strftime(fmt)}",
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


# ---------- экраны «Мои дела» ----------

KIND_NAME = {"task": "📝 Задачи", "event": "📅 Мероприятия"}


def hub_view(uid):
    t, ev, dn = len(items_of(uid, "task")), len(items_of(uid, "event")), len(items_of(uid, done=True))
    text = (
        "📋 Мои дела\n\n"
        "📝 Задачи: дела со сроком-датой. Напоминаю утром, можно вести чек-лист, "
        "просроченные подсвечиваются 🔴.\n"
        "📅 Мероприятия: события в точное время и с местом. Предупреждаю заранее "
        "и могу добавить в календарь телефона.\n\n"
        "В каждой группе можно создавать свои разделы: «Работа», «Дом», «Учёба».\n\n"
        "Что открыть?"
    )
    return text, markup([
        [btn(f"📝 Задачи ({t})", "sk:task")],
        [btn(f"📅 Мероприятия ({ev})", "sk:event")],
        [btn(f"✅ Выполненные ({dn})", "dn")],
    ])


def kind_view(uid, kind):
    ss = secs(uid, kind)
    rows = [[btn(f"📂 {s['name']} ({len(items_of(uid, kind, s['id']))})", f"sv:{kind}:{s['id']}")] for s in ss]
    rows.append([btn(f"📋 Без раздела ({len(items_of(uid, kind, 0))})", f"sv:{kind}:0")])
    rows.append([btn("➕ Новый раздел", f"sn:{kind}")])
    rows.append([btn("◀️ Назад", "hub")])
    intro = ("Выбери раздел или создай свой." if ss else
             "Своих разделов пока нет. Нажми «➕ Новый раздел», чтобы создать первый, и назови его как хочешь.")
    return f"{KIND_NAME[kind]}\n\n{intro}", markup(rows)


def item_label(e, tz):
    z = zone(tz)
    if e["kind"] == "event":
        when = datetime.fromtimestamp(e["due"], z).strftime("%d.%m %H:%M  ") if e["due"] else "без времени  "
        return when + e["title"]
    if e["due"]:
        mark = "🔴 " if is_overdue(e, tz) else ""
        return f"{mark}до {datetime.fromtimestamp(e['due'], z):%d.%m}  {e['title']}"
    return e["title"]


def section_view(uid, kind, sid, tz):
    s = get_sec(sid, uid) if sid else None
    items = items_of(uid, kind, sid)
    items.sort(key=lambda e: (e["due"] is None, e["due"] or 0))
    rows = [[btn(item_label(e, tz)[:60], f"open:{e['id']}")] for e in items[:15]]
    rows.append([btn("➕ Добавить сюда", f"sa:{kind}:{sid}")])
    if s:
        rows.append([btn("✏️ Переименовать", f"sren:{sid}"), btn("🗑 Удалить раздел", f"sdel:{sid}")])
    rows.append([btn("◀️ Назад", f"sk:{kind}")])
    name = s["name"] if s else "Без раздела"
    if items:
        more = f"\n(показаны первые 15 из {len(items)})" if len(items) > 15 else ""
        body = "Нажми на дело, чтобы открыть его." + more
    else:
        body = "Здесь пока пусто. Нажми «➕ Добавить сюда» или просто отправь мне текст."
    return f"{KIND_NAME[kind]} → {name}\n\n{body}", markup(rows)


def done_view(uid):
    items = items_of(uid, done=True)[-15:]
    rows = [[btn(("📅 " if e["kind"] == "event" else "📝 ") + e["title"][:55], f"open:{e['id']}")] for e in items]
    if items:
        rows.append([btn("🧹 Очистить выполненные", "dnclr")])
    rows.append([btn("◀️ Назад", "hub")])
    body = "Нажми на дело, чтобы вернуть его в список или удалить." if items else "Выполненных дел пока нет."
    return f"✅ Выполненные\n\n{body}", markup(rows)


def choose_section_view(uid, kind, intro, pick, new_cb, back=None):
    rows = [[btn(f"📂 {s['name']}", f"{pick}:{s['id']}")] for s in secs(uid, kind)]
    rows.append([btn("📋 Без раздела", f"{pick}:0")])
    rows.append([btn("➕ Новый раздел", new_cb)])
    if back:
        rows.append([btn("◀️ Назад", back)])
    return intro, markup(rows)


# ---------- служебные ----------

async def safe_edit(msg, text, mk):
    try:
        await msg.edit_text(text, reply_markup=mk)
    except Exception:
        pass


async def retire(bot, ref, text):
    """Меняем старое сообщение с кнопками на короткую пометку."""
    if not ref:
        return
    try:
        await bot.edit_message_text(text=text, chat_id=ref[0], message_id=ref[1], reply_markup=None)
    except Exception:
        pass


async def need_tz(m):
    if m.from_user.id in USER_TZ:
        return False
    STATE[m.from_user.id] = {"type": "tz"}
    await m.answer(TZ_PROMPT, reply_markup=tz_kb())
    return True


async def guard(m):
    STATE.pop(m.from_user.id, None)
    return await need_tz(m)


async def tz_done(message, uid):
    name = get_tz(uid)
    now = datetime.now(zone(name)).strftime("%H:%M")
    await message.answer(
        f"✅ Часовой пояс: {name}\nСейчас у тебя {now}. Если время неверное, "
        f"нажми «{B_TZ}» и выбери другой.\n\n"
        "Теперь можно добавлять дела: нажми «➕ Добавить» или просто отправь мне текст, "
        "например «Встреча с Олей завтра в 18:00».",
        reply_markup=MAIN_KB,
    )


def add_prompt(kind, sid, uid):
    s = get_sec(sid, uid) if sid else None
    where = f" в раздел «{s['name']}»" if s else ""
    if kind == "task":
        return (f"📝 Напиши задачу{where}.\n\nНапример: «купить молоко» или «сдать отчёт завтра». "
                "Если назовёшь срок, я поставлю напоминание. Если нет, срок можно задать кнопкой потом.")
    return (f"📅 Напиши мероприятие{where} и когда оно.\n\nНапример: «Встреча с Олей завтра в 18:00». "
            "Если время не назовёшь, я предложу выбрать его кнопками.")


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


def draft_view(did, tz):
    d = DRAFTS[did]
    lines = ["🤔 Что это?", "", f"«{d['title']}»"]
    if d["due"]:
        lines.append("🕐 Нашёл время: " + fmt_time(d["due"], tz))
    if d["place"]:
        lines.append("📍 " + str(d["place"]))
    lines += [
        "",
        "📝 Задача: дело со сроком-датой. Напомню утром, можно вести чек-лист и повторять напоминания.",
        "📅 Мероприятие: событие в точное время и с местом. Предупрежу заранее и добавлю в календарь телефона.",
        "",
        "Выбери, как сохранить 👇",
    ]
    return "\n".join(lines), markup([
        [btn("📝 Задача", f"dk:{did}:task"), btn("📅 Мероприятие", f"dk:{did}:event")],
        [btn("✖️ Не сохранять", f"dx:{did}")],
    ])


def finalize_draft(did, sid):
    d = DRAFTS.pop(did)
    return add_item(d["uid"], d["kind"], d["title"], d["due"], d["place"], d["note"], sid)


# ---------- команды и кнопки меню ----------

@dp.message(CommandStart())
async def start(m: Message):
    STATE.pop(m.from_user.id, None)
    if m.from_user.id not in USER_TZ:
        await m.answer(
            "Привет! 👋 Я помогаю не забывать дела. Отправь мне текст, пересланное сообщение "
            "или ссылку, а я сделаю из этого задачу или мероприятие и напомню вовремя.\n\n"
            "Для начала скажи, какой у тебя часовой пояс, чтобы я не путал время.",
        )
        await need_tz(m)
        return
    await m.answer("С возвращением! Выбирай действие в меню внизу или просто отправь мне текст.",
                   reply_markup=MAIN_KB)


@dp.message(Command("menu"))
async def menu(m: Message):
    if await guard(m):
        return
    await m.answer("Меню 👇", reply_markup=MAIN_KB)


@dp.message(Command("help"))
@dp.message(F.text == B_HELP)
async def help_cmd(m: Message):
    STATE.pop(m.from_user.id, None)
    await m.answer(
        "❓ Как пользоваться\n\n"
        "Отправь мне любой текст: «Встреча с Олей завтра в 18:00», «купить молоко», "
        "пересланное сообщение или ссылку на афишу. Я спрошу, что это.\n\n"
        "📝 Задача: дело со сроком-датой.\n"
        "• напомню утром в день срока (время можно поменять)\n"
        "• чек-лист с галочками\n"
        "• повтор каждые N ч, пока не нажмёшь «Готово»\n"
        "• просроченные подсвечиваются 🔴\n\n"
        "📅 Мероприятие: событие в точное время.\n"
        "• время выбираешь кнопками: день, ч, м\n"
        "• место\n"
        "• предупреждение заранее: за 10 м, 1 ч, 1 д\n"
        "• кнопка «📆 В календарь» добавит его в календарь телефона\n\n"
        "Разделы («Работа», «Дом» и так далее) создаются отдельно для задач и мероприятий в «📋 Мои дела».\n\n"
        "Команды: /add /list /settings /tz /menu /help"
    )


@dp.message(Command("add"))
@dp.message(F.text == B_ADD)
async def add_menu(m: Message):
    if await guard(m):
        return
    await m.answer(
        "➕ Что добавляем?\n\n"
        "📝 Задача: дело со сроком-датой (напомню утром, есть чек-лист).\n"
        "📅 Мероприятие: событие в точное время (встреча, поездка, концерт).\n"
        "🤖 Из текста или ссылки: пришли длинный текст или ссылку, я найду в них все дела.\n\n"
        "Можно и без кнопок: просто отправь мне текст, и я спрошу, что это.",
        reply_markup=markup([
            [btn("📝 Задача", "add:task"), btn("📅 Мероприятие", "add:event")],
            [btn("🤖 Из текста или ссылки", "add:ai")],
        ]),
    )


@dp.message(Command("list"))
@dp.message(F.text == B_LIST)
async def list_cmd(m: Message):
    if await guard(m):
        return
    text, mk = hub_view(m.from_user.id)
    await m.answer(text, reply_markup=mk)


@dp.message(Command("settings"))
@dp.message(F.text == B_REM)
async def settings(m: Message):
    if await guard(m):
        return
    await m.answer(SETTINGS_TEXT, reply_markup=settings_kb())


@dp.message(Command("tz"))
@dp.message(F.text == B_TZ)
async def tz_cmd(m: Message):
    uid = m.from_user.id
    STATE.pop(uid, None)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0].startswith("/"):
        name = parse_tz_input(parts[1])
        if name:
            USER_TZ[uid] = name
            await tz_done(m, uid)
        else:
            STATE[uid] = {"type": "tz"}
            await m.answer("Не понял такой пояс. Выбери кнопкой или напиши город, +3 или время.",
                           reply_markup=tz_kb())
        return
    STATE[uid] = {"type": "tz"}
    if uid in USER_TZ:
        now = datetime.now(zone(get_tz(uid))).strftime("%H:%M")
        await m.answer(f"Сейчас: {get_tz(uid)} (у тебя {now}).\n\n{TZ_PROMPT}", reply_markup=tz_kb())
    else:
        await m.answer(TZ_PROMPT, reply_markup=tz_kb())


# ---------- текстовые сообщения ----------

@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = m.text.strip()
    st = STATE.get(uid)

    # Пока нет пояса (или человек его меняет), читаем текст как пояс
    if uid not in USER_TZ or (st and st["type"] == "tz"):
        name = parse_tz_input(text)
        if name:
            USER_TZ[uid] = name
            STATE.pop(uid, None)
            await tz_done(m, uid)
        else:
            STATE[uid] = {"type": "tz"}
            await m.answer("Не понял. Выбери пояс кнопкой или напиши город, «+3» "
                           "или сколько сейчас у тебя времени (например 15:30).",
                           reply_markup=tz_kb())
        return

    tz = get_tz(uid)
    if st:
        STATE.pop(uid, None)
        t = st["type"]

        if t == "add":  # «Добавить» -> тип и раздел уже выбраны
            title, due = text, None
            loc = safe_local(text, tz) if len(text) < 200 else None
            if loc:
                title, due = loc
            e = add_item(uid, st["kind"], title[:200], due, sec=st["sec"])
            resp, mk = item_response(e, tz, "✅ Добавлено")
            await m.answer(resp, reply_markup=mk)
            return

        if t == "ai":
            await handle_free_text(m, text, force_ai=True)
            return

        if t == "sec_new":
            try:
                s = make_section(uid, st["kind"], text)
            except ValueError as ex:
                STATE[uid] = st
                await m.answer(f"⚠️ {ex}\nНапиши другое название.")
                return
            await after_section_created(m, st, s)
            return

        if t == "sec_rename":
            s = get_sec(st["sid"], uid)
            name = " ".join(text.split())[:30]
            if s and name:
                s["name"] = name
                resp, mk = section_view(uid, s["kind"], s["id"], tz)
                await m.answer(f"✅ Раздел переименован в «{name}»\n\n" + resp, reply_markup=mk)
            return

        if t == "when":
            e = find(st["eid"], uid)
            if not e:
                return
            ts = parse_when(text, tz)
            if ts is None:
                loc = safe_local(text, tz)
                ts = loc[1] if loc else None
            if ts is not None and e["kind"] == "task" and not has_explicit_time(text):
                ts = task_ts(datetime.fromtimestamp(ts, zone(tz)).date(), uid, tz)
            if ts is None or ts <= time.time():
                STATE[uid] = st
                hint = ("• завтра\n• 25.10\n• через 3 д" if e["kind"] == "task" else
                        "• 18:30\n• завтра 9:00\n• 25.10 14:00\n• через 40 м")
                await m.answer("⚠️ Не понял дату или она уже прошла. Напиши, например:\n" + hint)
                return
            e["due"] = ts
            reset_fired(e)
            word = "Срок поставлен" if e["kind"] == "task" else "Время поставлено"
            await m.answer(f"✅ {word}\n" + card(e, tz), reply_markup=kb(e))
            await retire(m.bot, st.get("msg"), "✅ Готово")
            return

        if t == "repeat":
            digits = re.sub(r"\D", "", text)
            n = int(digits) if digits else 0
            if not 1 <= n <= MAX_REPEAT:
                STATE[uid] = st
                await m.answer(f"⚠️ Напиши число ч от 1 до {MAX_REPEAT}, например 4.")
                return
            if st["eid"] == 0:
                get_set(uid)["repeat"] = n
                await m.answer(f"✅ Повтор для задач по умолчанию: каждые {n} ч, пока не нажмёшь «Готово».")
            else:
                e = find(st["eid"], uid)
                if e:
                    e["repeat"] = n
                    e["nags"] = 0
                    e["nag_at"] = None
                    await m.answer("✅ Повтор настроен\n" + card(e, tz), reply_markup=kb(e))
            return

        if t == "place":
            e = find(st["eid"], uid)
            if e:
                e["place"] = None if text in ("-", "—") else text[:100]
                await m.answer("✅ Место сохранено\n" + card(e, tz), reply_markup=kb(e))
            return

        if t == "check_add":
            e = find(st["eid"], uid)
            if e:
                lines = [ln.strip(" -•*\t") for ln in text.splitlines()]
                for ln in [x for x in lines if x][:MAX_CHECKS]:
                    if len(e["checks"]) < MAX_CHECKS:
                        e["checks"].append({"t": ln[:60], "d": False})
                ctext, cmk = check_view(e)
                await m.answer(ctext, reply_markup=cmk)
            return

    await handle_free_text(m, text)


async def after_section_created(m, st, s):
    uid = m.from_user.id
    tz = get_tz(uid)
    then = st.get("then")
    if then == "draft":
        d = DRAFTS.get(st["ref"])
        if not d:
            await m.answer(f"✅ Раздел «{s['name']}» создан, но черновик уже закрыт.")
            return
        e = finalize_draft(st["ref"], s["id"])
        resp, mk = item_response(e, tz, f"✅ Сохранено в раздел «{s['name']}»")
        await m.answer(resp, reply_markup=mk)
        await retire(m.bot, st.get("msg"), "✅ Сохранено")
    elif then == "move":
        e = find(st["ref"], uid)
        if e:
            e["sec"] = s["id"]
            await m.answer(f"✅ Раздел «{s['name']}» создан, дело перенесено\n" + card(e, tz), reply_markup=kb(e))
    elif then == "add":
        STATE[uid] = {"type": "add", "kind": st["kind"], "sec": s["id"]}
        await m.answer(f"✅ Раздел «{s['name']}» создан.\n\n" + add_prompt(st["kind"], s["id"], uid))
    else:
        await m.answer(
            f"✅ Раздел «{s['name']}» создан.",
            reply_markup=markup([[btn("➕ Добавить сюда", f"sa:{s['kind']}:{s['id']}")],
                                 [btn("📂 Открыть раздел", f"sv:{s['kind']}:{s['id']}")]]),
        )


async def handle_free_text(m, text, force_ai=False):
    """Текст без выбранного типа: показываем, что поняли, и спрашиваем «задача или мероприятие»."""
    uid = m.from_user.id
    tz = get_tz(uid)
    url_match = URL_RE.search(text)
    use_ai = force_ai or url_match or len(text) >= 200

    if not use_ai:
        title, due = text, None
        loc = safe_local(text, tz)
        if loc:
            title, due = loc
        did = new_draft(uid, title[:200], due)
        dtext, mk = draft_view(did, tz)
        await m.answer(dtext, reply_markup=mk)
        return

    await m.bot.send_chat_action(m.chat.id, "typing")
    source = text
    if url_match:
        page = await fetch_page(url_match.group(0))
        if not page:
            await m.answer("Не смог открыть ссылку. Скинь текст мероприятия сообщением, и я его разберу.")
            return
        comment = text.replace(url_match.group(0), "").strip()
        source = f"{comment}\nСсылка: {url_match.group(0)}\nСтраница: {page}"

    items = None
    if GROQ_KEY:
        if ai_allowed(uid):
            items = await ask_llm(source, tz)
        else:
            await m.answer("Лимит разбора ИИ на сегодня закончился. Напиши коротко с датой, например «завтра в 18:00».")
            return
    elif url_match:
        await m.answer("Разбор ссылок пока выключен. Скинь текст мероприятия сообщением.")
        return

    drafts = []
    for it in (items or [])[:10]:
        if it.get("title"):
            drafts.append((str(it["title"])[:200], to_ts(it.get("datetime"), tz), it.get("place"), it.get("note")))
    if not drafts:
        title, due = text[:200], None
        loc = safe_local(text, tz) if len(text) < 200 else None
        if loc:
            title, due = loc[0][:200], loc[1]
        drafts = [(title, due, None, None)]

    if len(drafts) == 1:
        did = new_draft(uid, *drafts[0])
        dtext, mk = draft_view(did, tz)
        await m.answer(dtext, reply_markup=mk)
        return

    await m.answer(f"🤖 Нашёл дел: {len(drafts)}. Сохраняю все. Тип определил сам: "
                   "со временем это мероприятие, без времени задача. Любое можно поменять кнопкой на карточке.")
    for title, due, place, note in drafts:
        e = add_item(uid, "event" if due else "task", title, due, place, note)
        await m.answer("✅ Сохранено\n" + card(e, tz), reply_markup=kb(e))


# ---------- инлайн-кнопки ----------

@dp.callback_query()
async def on_cb(c: CallbackQuery):
    try:
        await dispatch(c)
    except Exception:
        logging.exception("callback error: %s", c.data)
        try:
            await c.answer("Что-то пошло не так, попробуй ещё раз")
        except Exception:
            pass


ITEM_CMDS = ("open", "done", "undo", "del", "kind", "mv", "mvs", "mvn", "back", "tp", "snz", "pl", "ics", "ck")


async def dispatch(c: CallbackQuery):
    uid = c.from_user.id
    p = c.data.split(":")
    cmd = p[0]
    msg = c.message
    tz = get_tz(uid)

    if cmd == "noop":
        await c.answer()
        return

    # --- часовой пояс ---
    if cmd == "tz":
        if p[1] == "manual":
            STATE[uid] = {"type": "tz"}
            await msg.answer("Напиши город (например, Москва), пояс (+3 или UTC+5) "
                             "или сколько сейчас у тебя времени (например 15:30).")
            await c.answer()
            return
        USER_TZ[uid] = ":".join(p[1:])
        STATE.pop(uid, None)
        await c.answer("Готово")
        await tz_done(msg, uid)
        return

    # --- настройки напоминаний ---
    if cmd == "cfg":
        await cfg_cb(c, int(p[1]), p[2], p[3])
        return

    # --- «Добавить» ---
    if cmd == "add":
        kind = p[1]
        if kind == "ai":
            STATE[uid] = {"type": "ai"}
            await msg.answer("🤖 Отправь длинный текст, пересланное сообщение или ссылку на мероприятие. "
                             "Я сам найду в них дела, даты и места, а ты выберешь, как сохранить.")
        elif secs(uid, kind):
            text, mk = choose_section_view(
                uid, kind, f"📂 В какой раздел добавим?\n\nВыбери раздел для {KIND_NAME[kind].lower()} или создай новый.",
                f"ak:{kind}", f"akn:{kind}")
            await msg.answer(text, reply_markup=mk)
        else:
            STATE[uid] = {"type": "add", "kind": kind, "sec": 0}
            await msg.answer(add_prompt(kind, 0, uid))
        await c.answer()
        return

    if cmd == "ak":
        kind, sid = p[1], int(p[2])
        STATE[uid] = {"type": "add", "kind": kind, "sec": sid}
        await safe_edit(msg, add_prompt(kind, sid, uid), None)
        await c.answer()
        return

    if cmd == "akn":
        STATE[uid] = {"type": "sec_new", "kind": p[1], "then": "add"}
        await msg.answer("✏️ Напиши название нового раздела (до 30 символов), например «Работа» или «Дом».")
        await c.answer()
        return

    # --- черновики ---
    if cmd in ("dk", "ds", "dsn", "dx"):
        did = int(p[1])
        d = DRAFTS.get(did)
        if not d or d["uid"] != uid:
            await safe_edit(msg, "Это уже сохранено или отменено.", None)
            await c.answer()
            return
        if cmd == "dx":
            DRAFTS.pop(did, None)
            await safe_edit(msg, "❌ Не сохранил.", None)
            await c.answer()
            return
        if cmd == "dk":
            d["kind"] = p[2]
            if secs(uid, d["kind"]):
                text, mk = choose_section_view(
                    uid, d["kind"], f"📂 В какой раздел сохранить?\n\n«{d['title']}»",
                    f"ds:{did}", f"dsn:{did}")
                await safe_edit(msg, text, mk)
                await c.answer()
                return
            sid = 0
        elif cmd == "ds":
            sid = int(p[2])
        else:  # dsn
            STATE[uid] = {"type": "sec_new", "kind": d["kind"], "then": "draft", "ref": did,
                          "msg": (msg.chat.id, msg.message_id)}
            await msg.answer("✏️ Напиши название нового раздела (до 30 символов).")
            await c.answer()
            return
        e = finalize_draft(did, sid)
        resp, mk = item_response(e, tz, "✅ Сохранено")
        await safe_edit(msg, resp, mk)
        await c.answer()
        return

    # --- «Мои дела» ---
    if cmd == "hub":
        text, mk = hub_view(uid)
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "sk":
        text, mk = kind_view(uid, p[1])
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "sv":
        text, mk = section_view(uid, p[1], int(p[2]), tz)
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "sn":
        STATE[uid] = {"type": "sec_new", "kind": p[1], "then": "hub"}
        await msg.answer("✏️ Напиши название нового раздела (до 30 символов), например «Работа», «Дом» или «Учёба».")
        await c.answer()
        return
    if cmd == "sa":
        kind, sid = p[1], int(p[2])
        STATE[uid] = {"type": "add", "kind": kind, "sec": sid}
        await msg.answer(add_prompt(kind, sid, uid))
        await c.answer()
        return
    if cmd == "sren":
        if get_sec(int(p[1]), uid):
            STATE[uid] = {"type": "sec_rename", "sid": int(p[1])}
            await msg.answer("✏️ Напиши новое название раздела.")
        await c.answer()
        return
    if cmd == "sdel":
        s = get_sec(int(p[1]), uid)
        if s:
            await safe_edit(
                msg, f"🗑 Удалить раздел «{s['name']}»?\n\nСами дела не пропадут, они перейдут в «Без раздела».",
                markup([[btn("Да, удалить", f"sdy:{s['id']}"), btn("Отмена", f"sv:{s['kind']}:{s['id']}")]]))
        await c.answer()
        return
    if cmd == "sdy":
        s = get_sec(int(p[1]), uid)
        if s:
            for e in ITEMS:
                if e["sec"] == s["id"]:
                    e["sec"] = 0
            SECTIONS.remove(s)
            text, mk = kind_view(uid, s["kind"])
            await safe_edit(msg, "✅ Раздел удалён\n\n" + text, mk)
        await c.answer()
        return
    if cmd == "dn":
        text, mk = done_view(uid)
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "dnclr":
        ITEMS[:] = [e for e in ITEMS if not (e["user_id"] == uid and e["done"])]
        text, mk = done_view(uid)
        await safe_edit(msg, text, mk)
        await c.answer("Очистил")
        return

    # --- действия с делом ---
    e = find(int(p[1]), uid) if cmd in ITEM_CMDS else None
    if e is None:
        await c.answer("Это дело уже удалено")
        return

    if cmd == "open":
        await msg.answer(card(e, tz), reply_markup=kb(e))
    elif cmd == "done":
        e["done"] = True
        await safe_edit(msg, "✅ Выполнено: " + e["title"], None)
        await c.answer("Готово!")
        return
    elif cmd == "undo":
        e["done"] = False
        reset_fired(e)
        text, mk = item_response(e, tz, "↩️ Вернул в список")
        await safe_edit(msg, text, mk)
    elif cmd == "del":
        ITEMS.remove(e)
        await safe_edit(msg, "🗑 Удалено", None)
        await c.answer("Удалено")
        return
    elif cmd == "back":
        await safe_edit(msg, card(e, tz), kb(e))
    elif cmd == "kind":
        s = get_set(uid)
        if e["kind"] == "event":
            e["kind"] = "task"
            e["offsets"] = list(s["tk_offsets"])
            e["repeat"] = s["repeat"]
        else:
            e["kind"] = "event"
            e["offsets"] = list(s["ev_offsets"])
            e["repeat"] = 0
        e["sec"] = 0
        reset_fired(e)
        name = "мероприятие" if e["kind"] == "event" else "задача"
        text, mk = item_response(e, tz, f"🔄 Теперь это {name}")
        await safe_edit(msg, text, mk)
    elif cmd == "mv":
        text, mk = choose_section_view(
            uid, e["kind"], f"📂 Куда перенести?\n\n«{e['title']}»", f"mvs:{e['id']}", f"mvn:{e['id']}",
            back=f"back:{e['id']}")
        await safe_edit(msg, text, mk)
    elif cmd == "mvs":
        e["sec"] = int(p[2])
        await safe_edit(msg, "✅ Перенёс\n" + card(e, tz), kb(e))
    elif cmd == "mvn":
        STATE[uid] = {"type": "sec_new", "kind": e["kind"], "then": "move", "ref": e["id"]}
        await msg.answer("✏️ Напиши название нового раздела (до 30 символов).")
    elif cmd == "snz":
        mins = int(p[2])
        e["snooze"] = int(time.time()) + mins * 60
        label = f"{mins // 60} ч" if mins >= 60 else f"{mins} м"
        await safe_edit(msg, f"⏰ Напомню через {label}: {e['title']}", None)
    elif cmd == "pl":
        STATE[uid] = {"type": "place", "eid": e["id"]}
        await msg.answer("📍 Напиши место мероприятия, например «Кофейня на Ленина, 5». "
                         "Чтобы убрать место, напиши «-».")
    elif cmd == "ics":
        if not e["due"]:
            await c.answer("Сначала задай время мероприятия", show_alert=True)
            return
        await msg.answer_document(
            BufferedInputFile(build_ics(e), filename="meropriyatie.ics"),
            caption="📆 Открой этот файл на телефоне, и мероприятие добавится в календарь "
                    "вместе с напоминаниями.")
    elif cmd == "tp":
        await picker_cb(c, e, p[2:])
        return
    elif cmd == "ck":
        await check_cb(c, e, p[2])
        return
    await c.answer()


async def picker_cb(c, e, args):
    uid = c.from_user.id
    tz = get_tz(uid)
    z = zone(tz)
    msg = c.message
    act = args[0]
    if act == "back":
        text, mk = picker_start(e, tz)
    elif act == "d":
        text, mk = picker_hours(e, tz, parse_day(args[1]))
    elif act == "h":
        text, mk = picker_minutes(e, tz, parse_day(args[1]), int(args[2]))
    elif act == "dd":  # срок задачи датой
        e["due"] = task_ts(parse_day(args[1]), uid, tz)
        reset_fired(e)
        text, mk = "✅ Срок поставлен\n" + card(e, tz), kb(e)
    elif act in ("ok", "in"):
        if act == "in":
            ts = int((datetime.now(z) + timedelta(minutes=int(args[1]))).timestamp())
        else:
            d = parse_day(args[1])
            ts = int(datetime.combine(d, dtime(int(args[2][:2]), int(args[2][2:])), tzinfo=z).timestamp())
            if ts <= time.time():
                await c.answer("Это время уже прошло, выбери другое", show_alert=True)
                return
        e["due"] = ts
        reset_fired(e)
        text, mk = "✅ Время поставлено\n" + card(e, tz), kb(e)
    elif act == "txt":
        STATE[uid] = {"type": "when", "eid": e["id"], "msg": (msg.chat.id, msg.message_id)}
        if e["kind"] == "task":
            await msg.answer("✍️ Напиши срок, например:\n• завтра\n• 25.10\n• через 3 д")
        else:
            await msg.answer("✍️ Напиши дату и время, например:\n• 18:30\n• завтра 9:00\n"
                             "• 25.10 14:00\n• через 40 м")
        await c.answer()
        return
    else:  # x: отмена
        text, mk = card(e, tz), kb(e)
    await safe_edit(msg, text, mk)
    await c.answer()


async def check_cb(c, e, act):
    uid = c.from_user.id
    tz = get_tz(uid)
    msg = c.message
    if act == "back":
        await safe_edit(msg, card(e, tz), kb(e))
    elif act == "add":
        if len(e["checks"]) >= MAX_CHECKS:
            await c.answer(f"Максимум {MAX_CHECKS} пунктов", show_alert=True)
            return
        STATE[uid] = {"type": "check_add", "eid": e["id"]}
        await msg.answer("➕ Напиши пункты чек-листа. Каждый пункт с новой строки, "
                         "можно сразу несколько.")
        await c.answer()
        return
    elif act == "clr":
        e["checks"] = [x for x in e["checks"] if not x["d"]]
        text, mk = check_view(e)
        await safe_edit(msg, text, mk)
    else:  # open или t<i>
        m = re.fullmatch(r"t(\d+)", act)
        if m and int(m.group(1)) < len(e["checks"]):
            ch = e["checks"][int(m.group(1))]
            ch["d"] = not ch["d"]
        text, mk = check_view(e)
        await safe_edit(msg, text, mk)
    await c.answer()


async def cfg_cb(c, eid, k, act):
    uid = c.from_user.id
    tz = get_tz(uid)
    z = zone(tz)
    msg = c.message
    s = get_set(uid)
    e = None
    if eid:
        e = find(eid, uid)
        if not e:
            await c.answer("Это дело уже удалено")
            return
        k = "e" if e["kind"] == "event" else "t"
    kind = "event" if k == "e" else "task"

    if act == "close":
        if e:
            await safe_edit(msg, card(e, tz), kb(e))
        else:
            try:
                await msg.delete()
            except Exception:
                pass
        await c.answer()
        return
    if act == "hub":
        await safe_edit(msg, SETTINGS_TEXT, settings_kb())
        await c.answer()
        return
    if act == "rx":
        STATE[uid] = {"type": "repeat", "eid": eid}
        await msg.answer(f"✍️ Напиши, через сколько ч повторять напоминание (число от 1 до {MAX_REPEAT}), "
                         "например 4.")
        await c.answer()
        return

    scr = "main"
    m = re.fullmatch(r"([ohr])(\d+)", act)
    if m:
        letter, n = m.group(1), int(m.group(2))
        if letter == "o":
            offs = e["offsets"] if e else s["ev_offsets" if kind == "event" else "tk_offsets"]
            if n in offs:
                offs.remove(n)
            else:
                offs.append(n)
            offs.sort(reverse=True)
            if e:
                now = int(time.time())
                e["fired"] |= {x for x in e["offsets"] if e["due"] and e["due"] - x * 60 <= now}
            scr = "so"
        elif letter == "h":
            if e:
                dt = datetime.fromtimestamp(e["due"], z).replace(hour=n, minute=0, second=0, microsecond=0)
                e["due"] = int(dt.timestamp())
                reset_fired(e)
            else:
                s["tk_hour"] = n
            scr = "sh"
        else:  # r: повтор в часах
            if e:
                e["repeat"] = n
                e["nags"] = 0
                e["nag_at"] = None
            else:
                s["repeat"] = n
            scr = "sr"
    elif act in ("so", "sh", "sr"):
        scr = act

    text, mk = cfg_screen(uid, e, kind, scr)
    await safe_edit(msg, text, mk)
    await c.answer()


# ---------- напоминания ----------

async def send_safe(bot, uid, text, mk):
    try:
        await bot.send_message(uid, text, reply_markup=mk)
    except Exception:
        pass


def remind_head(e, o):
    if e["kind"] == "event":
        return "⏰ Пора! Начинается" if o == 0 else f"🔔 Через {EV_LEFT[o]}"
    return {0: "📅 Срок сегодня", 1440: "📅 Срок завтра", 4320: "📅 Срок через 3 д"}[o]


async def tick(bot):
    """Один проход: кому пора напомнить."""
    now = int(time.time())
    for e in list(ITEMS):
        if e["done"]:
            continue
        tzname = get_tz(e["user_id"])
        if e.get("snooze") and now >= e["snooze"]:
            e["snooze"] = None
            await send_safe(bot, e["user_id"], "⏰ Напоминаю ещё раз\n" + card(e, tzname), kb_remind(e))
        if not e["due"]:
            continue
        for o in sorted(e["offsets"], reverse=True):
            if o not in e["fired"] and now >= e["due"] - o * 60:
                e["fired"].add(o)
                await send_safe(bot, e["user_id"], remind_head(e, o) + "\n" + card(e, tzname), kb_remind(e))
        # повтор только для задач: каждые N часов, пока не нажато «Готово»
        if e["kind"] == "task" and e["repeat"] and e["nags"] < MAX_NAGS and now >= e["due"]:
            step = e["repeat"] * 3600
            if e["nag_at"] is None:
                e["nag_at"] = (e["due"] if now - e["due"] < 120 else now) + step
            if now >= e["nag_at"]:
                e["nags"] += 1
                e["nag_at"] = now + step
                await send_safe(bot, e["user_id"], "🔁 Ещё не отмечено «Готово»\n" + card(e, tzname), kb_remind(e))


async def reminder_loop(bot):
    while True:
        try:
            await tick(bot)
        except Exception:
            logging.exception("reminder error")
        await asyncio.sleep(20)


async def main():
    if not TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN")
    bot = Bot(TOKEN)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="menu", description="Показать меню"),
            BotCommand(command="add", description="Добавить дело"),
            BotCommand(command="list", description="Мои дела и разделы"),
            BotCommand(command="settings", description="Напоминания"),
            BotCommand(command="tz", description="Часовой пояс"),
            BotCommand(command="help", description="Помощь"),
        ])
    except Exception:
        pass
    reminders = asyncio.create_task(reminder_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        reminders.cancel()


if __name__ == "__main__":
    asyncio.run(main())
