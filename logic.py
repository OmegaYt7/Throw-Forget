"""Логика бота без Telegram: время, дела, разделы, календарь, разбор текста, ИИ.

Файл bot.py подключает всё отсюда и отвечает только за кнопки и сообщения.
"""
import asyncio
import calendar
import ipaddress
import itertools
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from html import unescape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
try:  # необязательная библиотека: запасной разбор дат, если свой разбор ничего не нашёл
    from dateparser.search import search_dates
except Exception:  # noqa: BLE001
    search_dates = None

GROQ_KEY = os.getenv("GROQ_API_KEY")  # бесплатный ключ с console.groq.com
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DAILY_AI_LIMIT = int(os.getenv("DAILY_AI_LIMIT", "5"))   # разборов ИИ в день на бесплатном тарифе

# Администраторы (числовые Telegram ID через запятую): у них нет никаких ограничений
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.getenv("ADMIN_IDS", ""))}
PRO_PRICE = int(os.getenv("PRO_PRICE_STARS", "100"))   # цена Pro в Telegram Stars
PRO_DAYS = int(os.getenv("PRO_DAYS", "30"))            # на сколько дней даёт один платёж
SUPPORT = os.getenv("SUPPORT_CONTACT", "")             # контакт для вопросов по оплате, например @username
LIMITS = {
    "free": {"items": 30, "sections": 2, "recurring": 3, "ai": DAILY_AI_LIMIT},
    "pro": {"items": 1000, "sections": 15, "recurring": 100, "ai": int(os.getenv("PRO_AI_LIMIT", "60"))},
}
LIMIT_WORDS = {"items": "активных дел", "sections": "разделов каждого типа",
               "recurring": "повторяющихся дел", "ai": "разборов ИИ в день"}
DEFAULT_TZ = "Europe/Moscow"

# Мероприятие: предупреждения до начала (минуты -> подпись)
EV_LABELS = {0: "в момент", 10: "за 10 м", 30: "за 30 м", 60: "за 1 ч", 180: "за 3 ч", 1440: "за 1 д"}
EV_LEFT = {10: "10 м", 30: "30 м", 60: "1 ч", 180: "3 ч", 1440: "1 д"}
# Задача: напоминания относительно срока (минуты -> подпись)
TK_LABELS = {0: "в срок", 1440: "за 1 д", 4320: "за 3 д"}
REPEATS = [1, 2, 3, 6, 12, 24]             # повтор для задач, в часах (минимум 1 ч)
MAX_REPEAT = 72
MAX_NAGS = 12
MAX_SECTIONS = 15
MAX_CHECKS = 20
DESC_MAX = {"event": 3000, "task": 1000}   # длина описания: у мероприятия больше, чем у задачи
TITLE_MAX = 200
EVENT_MIN = 60                              # мероприятие занимает 1 ч (для пересечений и календаря)
WD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
KIND_NAME = {"task": "📝 Задачи", "event": "📅 Мероприятия"}

# Города для выбора часового пояса: (флаг, город, часовой пояс)
WORLD = [
    ("🇺🇸", "Гонолулу", "Pacific/Honolulu"), ("🇺🇸", "Анкоридж", "America/Anchorage"),
    ("🇺🇸", "Лос-Анджелес", "America/Los_Angeles"), ("🇺🇸", "Денвер", "America/Denver"),
    ("🇺🇸", "Чикаго", "America/Chicago"), ("🇲🇽", "Мехико", "America/Mexico_City"),
    ("🇺🇸", "Нью-Йорк", "America/New_York"), ("🇨🇦", "Торонто", "America/Toronto"),
    ("🇨🇴", "Богота", "America/Bogota"), ("🇵🇪", "Лима", "America/Lima"),
    ("🇻🇪", "Каракас", "America/Caracas"), ("🇨🇱", "Сантьяго", "America/Santiago"),
    ("🇧🇷", "Сан-Паулу", "America/Sao_Paulo"), ("🇦🇷", "Буэнос-Айрес", "America/Argentina/Buenos_Aires"),
    ("🇮🇸", "Рейкьявик", "Atlantic/Reykjavik"), ("🇬🇧", "Лондон", "Europe/London"),
    ("🇵🇹", "Лиссабон", "Europe/Lisbon"), ("🇳🇬", "Лагос", "Africa/Lagos"),
    ("🇫🇷", "Париж", "Europe/Paris"), ("🇩🇪", "Берлин", "Europe/Berlin"),
    ("🇮🇹", "Рим", "Europe/Rome"), ("🇪🇸", "Мадрид", "Europe/Madrid"),
    ("🇵🇱", "Варшава", "Europe/Warsaw"), ("🇨🇿", "Прага", "Europe/Prague"),
    ("🇦🇹", "Вена", "Europe/Vienna"), ("🇿🇦", "Йоханнесбург", "Africa/Johannesburg"),
    ("🇷🇺", "Калининград", "Europe/Kaliningrad"), ("🇬🇷", "Афины", "Europe/Athens"),
    ("🇫🇮", "Хельсинки", "Europe/Helsinki"), ("🇱🇻", "Рига", "Europe/Riga"),
    ("🇱🇹", "Вильнюс", "Europe/Vilnius"), ("🇪🇪", "Таллин", "Europe/Tallinn"),
    ("🇺🇦", "Киев", "Europe/Kyiv"), ("🇷🇴", "Бухарест", "Europe/Bucharest"),
    ("🇲🇩", "Кишинёв", "Europe/Chisinau"), ("🇹🇷", "Стамбул", "Europe/Istanbul"),
    ("🇧🇾", "Минск", "Europe/Minsk"), ("🇷🇺", "Москва", "Europe/Moscow"),
    ("🇪🇬", "Каир", "Africa/Cairo"), ("🇮🇱", "Иерусалим", "Asia/Jerusalem"),
    ("🇸🇦", "Эр-Рияд", "Asia/Riyadh"), ("🇰🇪", "Найроби", "Africa/Nairobi"),
    ("🇮🇷", "Тегеран", "Asia/Tehran"), ("🇬🇪", "Тбилиси", "Asia/Tbilisi"),
    ("🇦🇿", "Баку", "Asia/Baku"), ("🇦🇲", "Ереван", "Asia/Yerevan"),
    ("🇦🇪", "Дубай", "Asia/Dubai"), ("🇷🇺", "Самара", "Europe/Samara"),
    ("🇺🇿", "Ташкент", "Asia/Tashkent"), ("🇷🇺", "Екатеринбург", "Asia/Yekaterinburg"),
    ("🇰🇿", "Алматы", "Asia/Almaty"), ("🇵🇰", "Карачи", "Asia/Karachi"),
    ("🇮🇳", "Дели", "Asia/Kolkata"), ("🇳🇵", "Катманду", "Asia/Kathmandu"),
    ("🇰🇬", "Бишкек", "Asia/Bishkek"), ("🇷🇺", "Омск", "Asia/Omsk"),
    ("🇧🇩", "Дакка", "Asia/Dhaka"), ("🇹🇭", "Бангкок", "Asia/Bangkok"),
    ("🇻🇳", "Хошимин", "Asia/Ho_Chi_Minh"), ("🇷🇺", "Новосибирск", "Asia/Novosibirsk"),
    ("🇷🇺", "Красноярск", "Asia/Krasnoyarsk"), ("🇮🇩", "Джакарта", "Asia/Jakarta"),
    ("🇷🇺", "Иркутск", "Asia/Irkutsk"), ("🇨🇳", "Пекин", "Asia/Shanghai"),
    ("🇸🇬", "Сингапур", "Asia/Singapore"), ("🇭🇰", "Гонконг", "Asia/Hong_Kong"),
    ("🇵🇭", "Манила", "Asia/Manila"), ("🇦🇺", "Перт", "Australia/Perth"),
    ("🇯🇵", "Токио", "Asia/Tokyo"), ("🇰🇷", "Сеул", "Asia/Seoul"),
    ("🇷🇺", "Якутск", "Asia/Yakutsk"), ("🇦🇺", "Аделаида", "Australia/Adelaide"),
    ("🇷🇺", "Владивосток", "Asia/Vladivostok"), ("🇦🇺", "Сидней", "Australia/Sydney"),
    ("🇷🇺", "Магадан", "Asia/Magadan"), ("🇷🇺", "Камчатка", "Asia/Kamchatka"),
    ("🇳🇿", "Окленд", "Pacific/Auckland"),
]
# Первый экран выбора: самые частые
POPULAR = ["Europe/Moscow", "Europe/Kyiv", "Europe/Minsk", "Europe/Kaliningrad", "Europe/Samara",
           "Asia/Yekaterinburg", "Asia/Almaty", "Asia/Omsk", "Asia/Novosibirsk", "Asia/Irkutsk",
           "Asia/Yakutsk", "Asia/Vladivostok"]


def offset_str(name):
    """Текущий сдвиг от UTC: +3, +5:30, -4. У городов с летним временем он меняется по сезону."""
    mins = int(datetime.now(zone(name)).utcoffset().total_seconds() // 60)
    h, m = divmod(abs(mins), 60)
    return ("+" if mins >= 0 else "-") + str(h) + (f":{m:02d}" if m else "")


def tz_label(flag, city, name):
    return f"{flag} {city} {offset_str(name)}"


def popular_entries():
    by = {n: (f, c, n) for f, c, n in WORLD}
    return [by[n] for n in POPULAR]


def world_sorted():
    """Все города от запада к востоку по текущему сдвигу."""
    return sorted(WORLD, key=lambda x: (datetime.now(zone(x[2])).utcoffset(), x[1]))


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
CITIES.update({c.lower().replace("ё", "е"): z for _, c, z in WORLD})

# Пока без базы: всё в памяти, при перезапуске пропадает
ITEMS = []       # дела: kind = "task" | "event"
SECTIONS = []    # разделы: {id, user_id, kind, name}
DRAFTS = {}      # черновики, ждущие выбора «задача / мероприятие»
STATE = {}       # user_id -> что бот ждёт от человека следующим сообщением
USER_TZ = {}
USER_SET = {}    # настройки по умолчанию
AI_USED = {}
USER_META = {}   # user_id -> {since, name}
SELECT = {}      # user_id -> id дел, отмеченных для массовых действий
PLANS = {}       # user_id -> {until: ts} подписка Pro
PAYMENTS = []    # журнал оплат (нужен для возвратов)
_ids, _sids, _dids = itertools.count(1), itertools.count(1), itertools.count(1)
URL_RE = re.compile(r"https?://\S+")

TZ_PROMPT = (
    "🌍 Выбери свой часовой пояс. По нему я буду ставить время и присылать напоминания.\n\n"
    f"Нет в списке? Нажми «🌐 Все города» (их {len(WORLD)}) или «Ввести вручную»: напиши город, пояс (+3 или UTC+5) "
    "или просто сколько сейчас у тебя времени (например 15:30)."
)
SETTINGS_TEXT = (
    "🔔 Напоминания\n\n"
    "Это настройки по умолчанию для новых дел. Для отдельного дела их можно поменять "
    "на его карточке кнопкой «🔔 Напоминания».\n\n"
    "📅 Мероприятия: предупреждаю заранее (за 10 м, 1 ч, 1 д и так далее).\n"
    "📝 Задачи: напоминаю в момент срока и заранее (за 1 д, за 3 д), можно напоминать снова каждые N ч, пока не нажмёшь «Готово».\n\n"
    "☀️ Утренняя сводка: каждое утро одним сообщением расскажу, что у тебя сегодня."
)


# ---------- настройки и общие помощники ----------

def chunk(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


def get_tz(uid):
    return USER_TZ.get(uid, DEFAULT_TZ)


def get_set(uid):
    s = USER_SET.setdefault(uid, {})
    s.setdefault("ev_offsets", [60, 0])   # мероприятия: за 1 ч и в момент
    s.setdefault("tk_offsets", [0])       # задачи: в срок
    s.setdefault("tk_hour", 9)            # время по умолчанию, если у задачи названа только дата
    s.setdefault("repeat", 0)             # «напоминать снова» для задач, часы (0 = выкл)
    s.setdefault("digest", {"on": False, "hour": 8, "last": None})   # утренняя сводка
    return s


def ai_allowed(uid):
    lim = limit(uid, "ai")
    today = date.today()
    d, n = AI_USED.get(uid, (today, 0))
    if d != today:
        n = 0
    if lim is not None and n >= lim:
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
    """Запасной разбор через dateparser (если установлен). Возвращает (название, ts) или None."""
    if search_dates is None:
        return None
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
    """Дата и время из текста одним числом (ts). Только дата: 09:00. Ничего не нашлось: None."""
    info = parse_text(text, tz)
    if info["due"]:
        return info["due"]
    if info["day"]:
        return int(datetime.combine(info["day"], dtime(9, 0), tzinfo=zone(tz)).timestamp())
    return None


def has_explicit_time(text):
    return bool(re.search(r"\d{1,2}:\d{2}|\bв\s*\d|через", text.lower()))


def extract_time(text, tz="Europe/Moscow"):
    """Достаёт время из текста: «Встреча в 18:30» или «Встреча\n18 23» -> («Встреча», 18, 30). Нет времени -> None."""
    r = extract_datetime(text, tz)
    if r["time"]:
        return (r["title"] or text.strip()), r["time"][0], r["time"][1]
    return None


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


def slot_ok(e, tz, day, h, m):
    """Можно ли выбрать это время. Для задач на сегодня время должно быть не раньше чем через 1 ч."""
    ts = int(datetime.combine(day, dtime(h, m), tzinfo=zone(tz)).timestamp())
    margin = 3600 if e["kind"] == "task" else 0
    return ts > time.time() + margin


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
    check_limit(uid, "sections", len(secs(uid, kind)))
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


def add_item(uid, kind, title, due=None, place=None, note=None, sec=0, rec=None, desc=None):
    check_limit(uid, "items", len(items_of(uid)))
    if rec:
        check_limit(uid, "recurring", count_recurring(uid))
    s = get_set(uid)
    task = kind == "task"
    e = {"id": next(_ids), "user_id": uid, "kind": kind, "title": title, "due": due,
         "place": place, "note": note, "sec": sec, "done": False, "desc": trim_desc(kind, desc),
         "offsets": list(s["tk_offsets"] if task else s["ev_offsets"]),
         "repeat": s["repeat"] if task else 0,
         "checks": [], "fired": set(), "nags": 0, "nag_at": None, "snooze": None, "rec": None}
    set_rec(e, rec, get_tz(uid))
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
    labels = EV_LABELS if e["kind"] == "event" else TK_LABELS
    parts = [labels[o] for o in sorted(e["offsets"], reverse=True) if o in labels]
    return ", ".join(parts) if parts else "без напоминаний"


def card(e, tz, show_desc=False):
    z = zone(tz)
    event = e["kind"] == "event"
    lines = [("📅 " if event else "📝 ") + e["title"]]
    s = next((x["name"] for x in SECTIONS if x["id"] == e["sec"]), None) if e["sec"] else None
    if s:
        lines.append("📂 Раздел: " + s)
    if e["due"]:
        lines.append(("🕐 " if event else "📅 Срок: ") + fmt_time(e["due"], tz))
    elif event:
        lines.append("🕐 Время не задано. Нажми «Задать время»")
    else:
        lines.append("📅 Срок не задан")
    if e.get("rec"):
        lines.append("♻️ Повторяется: " + rec_text(e["rec"]))
    if is_overdue(e, tz):
        lines.append(f"🔴 Просрочено на {overdue_days(e, tz)} д")
        lines.append("Срок прошёл. Нажми «✅ Готово», если уже сделал, или «📅 Изменить срок», чтобы перенести.")
    elif is_past_event(e):
        lines.append("⌛ Мероприятие уже прошло.")
        lines.append("Нажми «✅ Готово», чтобы убрать его в выполненные, «🕐 Изменить время», чтобы перенести, "
                     "или «🗑 Удалить».")
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
            lines.append(f"🔁 Напоминать снова: каждые {e['repeat']} ч, пока не «Готово»")
    if e["checks"]:
        done = sum(1 for c in e["checks"] if c["d"])
        lines.append(f"📋 Чек-лист: {done}/{len(e['checks'])}")
        lines += [("✅ " if c["d"] else "⬜️ ") + c["t"] for c in e["checks"][:10]]
    if e["done"]:
        lines.append("✅ Выполнено")
    desc = e.get("desc") or ""
    if desc:
        if show_desc:
            room = max(200, 3900 - len("\n".join(lines)) - 80)
            shown = desc if len(desc) <= room else desc[:room].rsplit(" ", 1)[0] + "…"
            lines += ["", f"📄 Описание ({len(desc)} симв.):", shown]
        else:
            lines.append(f"📄 Описание: {len(desc)} симв. Нажми «Показать описание».")
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
                "• «📍 Место» добавит адрес. А «📆 В календарь телефона» (по желанию) пришлёт файл, "
                "чтобы добавить мероприятие в календарь на телефоне.")
    if not e["due"]:
        return ("Что дальше:\n"
                "• Срок пока не задан, поэтому напоминаний не будет. Нажми «📅 Задать срок»: "
                "выбери дату, час и минуты, и я напомню.\n"
                "• «📋 Чек-лист» поможет разбить дело на пункты с галочками.\n"
                "• «📂 Раздел» разложит дела по папкам, например «Работа» или «Дом».")
    return ("Что будет дальше:\n"
            f"• Я напомню: {remind_text(e, tz)}.\n"
            "• Если дело большое, разбей его на пункты кнопкой «📋 Чек-лист».\n"
            "• Сделаешь раньше? Нажми «✅ Готово».")


def item_label(e, tz):
    z = zone(tz)
    if e["kind"] == "event":
        if not e["due"]:
            return "без времени  " + e["title"]
        past = "⌛ " if is_past_event(e) else ""
        return past + datetime.fromtimestamp(e["due"], z).strftime("%d.%m %H:%M  ") + e["title"]
    if e["due"]:
        if is_overdue(e, tz):
            return f"🔴 просрочено {overdue_days(e, tz)} д  {e['title']}"
        return f"до {datetime.fromtimestamp(e['due'], z):%d.%m %H:%M}  {e['title']}"
    return e["title"]


def list_sort_key(e):
    """Порядок в списках: ближайшие первыми, прошедшие мероприятия в конце, дела без времени после всех."""
    if e["due"] is None:
        return (2, 0)
    if is_past_event(e):
        return (1, -e["due"])
    return (0, e["due"])


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
        return (f"📝 Напиши задачу со сроком на {d}.\n\nМожно сразу указать время, например «Сдать отчёт в 15:00». "
                "Если времени не будет, я предложу выбрать час и минуты кнопками.")
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


_META_KEYS = {"og:title", "og:description", "twitter:title", "twitter:description", "description"}


def _decode(raw, charset):
    cs = charset
    if not cs:
        m = re.search(rb"charset=[\"']?([\w-]+)", raw[:3000], re.I)
        cs = m.group(1).decode("ascii", "ignore") if m else "utf-8"
    try:
        return raw.decode(cs, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def page_text(html_):
    """Текст страницы: сначала мета-теги (их отдают даже сайты на скриптах, вроде ВКонтакте), потом видимый текст."""
    parts = []
    t = re.search(r"<title[^>]*>(.*?)</title>", html_, re.I | re.S)
    if t:
        parts.append(unescape(" ".join(t.group(1).split())))
    for tag in re.findall(r"<meta\b[^>]*>", html_, re.I):
        key = re.search(r"(?:property|name)\s*=\s*[\"']([^\"']+)[\"']", tag, re.I)
        val = re.search(r"content\s*=\s*\"([^\"]*)\"|content\s*=\s*'([^']*)'", tag, re.I)
        if key and val and key.group(1).lower() in _META_KEYS:
            v = unescape(" ".join((val.group(1) or val.group(2) or "").split()))
            if v and v not in parts:
                parts.append(v)
    body = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", html_)
    body = unescape(re.sub(r"<[^>]+>", " ", body))
    body = re.sub(r"\s+", " ", body).strip()
    text = "\n".join(parts + ([body] if body else []))
    return text[:5000]


async def fetch_page(url):
    """Текст страницы по ссылке или None, если прочитать не получилось (закрыта, пустая, нет связи)."""
    if not await is_public(url):
        return None
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/124.0 Mobile Safari/537.36",
               "Accept-Language": "ru,en;q=0.8"}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(url) as r:
                if r.status != 200:
                    return None
                raw = await r.content.read(400_000)
                charset = r.charset
    except Exception:
        return None
    text = page_text(_decode(raw, charset))
    return text if len(text.strip()) >= 80 else None


class AIError(Exception):
    """Понятная причина, почему ИИ не сработал."""


GROQ_URL = "https://api.groq.com/openai/v1"
_MODEL = {"id": None}
_PREFERRED = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-20b", "openai/gpt-oss-120b",
              "qwen/qwen3-32b", "meta-llama/llama-4-scout-17b-16e-instruct", "gemma2-9b-it"]
_NOT_CHAT = ("whisper", "tts", "guard", "playai", "embed", "orpheus", "safeguard")


def current_model():
    return _MODEL["id"] or GROQ_MODEL


def _pick_model(ids, exclude=None):
    ids = [i for i in ids if i != exclude and not any(w in i.lower() for w in _NOT_CHAT)]
    for p in _PREFERRED:
        if p in ids:
            return p
    for i in ids:
        if any(w in i.lower() for w in ("llama", "gpt-oss", "qwen", "gemma", "mixtral")):
            return i
    return None


def _http_reason(status, data):
    msg = ""
    if isinstance(data, dict):
        err = data.get("error")
        msg = (err.get("message") if isinstance(err, dict) else str(err or ""))[:160]
    hints = {401: "проверь GROQ_API_KEY", 403: "доступ запрещён (иногда так бывает из-за региона хостинга)",
             404: "модель не найдена, проверь GROQ_MODEL", 429: "превышен лимит Groq, подожди немного"}
    hint = hints.get(status) or ("сервис Groq временно недоступен" if status >= 500 else "")
    return f"Groq вернул {status}" + (f": {msg}" if msg else "") + (f" ({hint})" if hint else "")


async def _groq_post(session, model, system, user):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    async with session.post(f"{GROQ_URL}/chat/completions", json=payload,
                            headers={"Authorization": f"Bearer {GROQ_KEY}"}) as r:
        try:
            data = await r.json(content_type=None)
        except Exception:  # noqa: BLE001
            data = {"error": {"message": (await r.text())[:200]}}
        return r.status, data


async def _groq_models(session):
    async with session.get(f"{GROQ_URL}/models", headers={"Authorization": f"Bearer {GROQ_KEY}"}) as r:
        if r.status != 200:
            return []
        data = await r.json(content_type=None)
        return [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]


def _loads_json(content):
    try:
        return json.loads(content)
    except Exception:  # noqa: BLE001
        a, b = content.find("{"), content.rfind("}")
        if a != -1 and b > a:
            try:
                return json.loads(content[a:b + 1])
            except Exception:  # noqa: BLE001
                pass
    raise AIError("ИИ ответил не в формате JSON")


async def ask_llm(text, tz):
    """Просит ИИ выделить дела и мероприятия. Возвращает список словарей, при ошибке бросает AIError с причиной."""
    now = datetime.now(zone(tz)).strftime("%Y-%m-%d %H:%M, %A")
    system = (
        "Ты помощник-планировщик. Из текста выдели дела и мероприятия, которые человеку нужно запланировать. "
        f"Сейчас {now}, часовой пояс {tz}. Отвечай ТОЛЬКО JSON такого вида: "
        '{"items":[{"title":"коротко, до 80 символов","date":"YYYY-MM-DD или null","time":"HH:MM или null",'
        '"place":"строка или null","description":"главное из текста (условия, цена, что взять, контакты), '
        'до 700 символов, или null"}]}. '
        "Год, если он не назван, выбирай так, чтобы дата была в будущем. Время не выдумывай: если его нет, time = null. "
        "Если в тексте нет ни дел, ни мероприятий, верни {\"items\": []}."
    )
    model = current_model()
    try:
        timeout = aiohttp.ClientTimeout(total=40)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            status, data = await _groq_post(s, model, system, text[:6000])
            if status in (400, 404) and "model" in json.dumps(data, ensure_ascii=False).lower():
                new = _pick_model(await _groq_models(s), exclude=model)
                if new:
                    logging.warning("Модель %s недоступна, перехожу на %s", model, new)
                    _MODEL["id"] = new
                    status, data = await _groq_post(s, new, system, text[:6000])
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as ex:
        raise AIError(f"нет связи с Groq ({type(ex).__name__})") from ex
    if status != 200:
        raise AIError(_http_reason(status, data))
    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as ex:  # noqa: BLE001
        raise AIError("Groq прислал ответ в неожиданном формате") from ex
    obj = _loads_json(content)
    items = obj.get("items") if isinstance(obj, dict) else obj
    if not isinstance(items, list):
        raise AIError("ответ ИИ в неожиданном формате")
    return [it for it in items if isinstance(it, dict)]


def ai_refund(uid):
    """Если ИИ не сработал по его вине, попытку не списываем."""
    d, n = AI_USED.get(uid, (date.today(), 0))
    AI_USED[uid] = (d, max(0, n - 1))


def ai_when(it, tz):
    """Поля ИИ date и time -> (готовое время ts или None, дата без времени или None)."""
    z = zone(tz)
    d, tv = None, None
    try:
        d = date.fromisoformat(str(it.get("date"))[:10]) if it.get("date") else None
    except Exception:  # noqa: BLE001
        d = None
    m = re.match(r"(\d{1,2}):(\d{2})", str(it.get("time") or ""))
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        tv = (int(m.group(1)), int(m.group(2)))
    if d is None and it.get("datetime"):  # старый формат ответа
        ts = to_ts(it["datetime"], tz)
        if ts:
            dt = datetime.fromtimestamp(ts, z)
            d, tv = dt.date(), (dt.hour, dt.minute)
    if d and tv:
        ts = int(datetime.combine(d, dtime(*tv), tzinfo=z).timestamp())
        return (ts, None) if ts > time.time() else (None, None)
    if d:
        return None, (d if d >= datetime.now(z).date() else None)
    return None, None


def trim_desc(kind, text):
    return (text or "").strip()[:DESC_MAX[kind]]


def split_title_desc(text):
    """Длинный текст -> (короткое название, полное описание)."""
    t = text.strip()
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    title = lines[0] if lines else t
    if len(title) > 90:
        cut = re.split(r"(?<=[.!?])\s", title, maxsplit=1)[0]
        title = cut if 10 <= len(cut) <= 90 else title[:90].rsplit(" ", 1)[0] + "…"
    return title.strip(), t


def analyze_long(text, tz):
    """Длинный текст без ИИ: дата и время ищем сами, название берём из первой строки, весь текст уходит в описание."""
    info = parse_text(text, tz)
    title, desc = split_title_desc(text)
    return {"title": title, "desc": desc, "due": info["due"], "day": info["day"], "rec": None, "past": info["past"]}


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

def new_draft(uid, title, due, place=None, note=None, day=None, rec=None, past=False, desc="", warn=""):
    did = next(_dids)
    DRAFTS[did] = {"uid": uid, "title": title, "due": due, "place": place, "note": note, "kind": None,
                   "day": ymd(day) if day else None, "rec": rec, "past": past, "desc": desc, "warn": warn}
    return did


def finalize_draft(did, sid):
    d = DRAFTS[did]  # удаляем черновик только после успеха: при лимите его можно сохранить позже
    e = create_item(d["uid"], d["kind"], d["title"], d["due"], d.get("day"), d.get("rec"), sid,
                    d["place"], d["note"], d.get("desc"))
    DRAFTS.pop(did, None)
    return e


def create_item(uid, kind, title, due, day, rec, sec=0, place=None, note=None, desc=None):
    """Создаёт дело. Если известна только дата: задаче ставим срок по умолчанию, у мероприятия запомним день для выбора часа."""
    e = add_item(uid, kind, title[:TITLE_MAX], due, place, note, sec, rec, desc)
    if due is None and day:
        d = parse_day(day) if isinstance(day, str) else day
        if kind == "task":
            e["due"] = task_ts(d, uid, get_tz(uid))
            set_rec(e, e["rec"], get_tz(uid))
            reset_fired(e)
        else:
            e["day_hint"] = ymd(d)
    return e


# ---------- профиль ----------

def touch_user(uid, name=None):
    meta = USER_META.setdefault(uid, {"since": int(time.time()), "name": None})
    if name:
        meta["name"] = name


def wipe_user(uid):
    """Стереть дела, разделы и настройки человека. Часовой пояс остаётся."""
    ITEMS[:] = [e for e in ITEMS if e["user_id"] != uid]
    SECTIONS[:] = [s for s in SECTIONS if s["user_id"] != uid]
    for did in [d for d, v in DRAFTS.items() if v["uid"] == uid]:
        DRAFTS.pop(did, None)
    USER_SET.pop(uid, None)
    STATE.pop(uid, None)


def profile_text(uid):
    tz = get_tz(uid)
    z = zone(tz)
    now = time.time()
    meta = USER_META.get(uid, {})
    lines = ["👤 Профиль", ""]
    lines.append(f"Имя: {meta['name']}" if meta.get("name") else "Имя: не указано")
    if meta.get("since"):
        days = max(0, int((now - meta["since"]) // 86400))
        lines.append(f"С ботом с {datetime.fromtimestamp(meta['since'], z):%d.%m.%Y} ({days} д)")
    lines += ["", f"🌍 Часовой пояс: {tz}", f"Сейчас у тебя {datetime.now(z):%H:%M}"]
    tasks, events, done = items_of(uid, "task"), items_of(uid, "event"), items_of(uid, done=True)
    over = sum(1 for e in tasks if is_overdue(e, tz))
    upcoming = sorted((e for e in events if e["due"] and e["due"] > now), key=lambda e: e["due"])
    lines += ["", "📊 Твои дела:",
              f"• Задач: {len(tasks)}" + (f" (просрочено: {over})" if over else ""),
              f"• Мероприятий: {len(events)}",
              f"• Выполнено: {len(done)}",
              f"• Разделов: {len([s for s in SECTIONS if s['user_id'] == uid])}"]
    if upcoming:
        lines.append(f"• Ближайшее мероприятие: {fmt_time(upcoming[0]['due'], tz)}, {upcoming[0]['title']}")
    s = get_set(uid)
    ev = ", ".join(EV_LABELS[o] for o in sorted(s["ev_offsets"], reverse=True) if o in EV_LABELS) or "без напоминаний"
    tk = ", ".join(TK_LABELS[o] for o in sorted(s["tk_offsets"], reverse=True) if o in TK_LABELS) or "без напоминаний"
    rep = f"напоминать снова каждые {s['repeat']} ч" if s["repeat"] else "без повторных напоминаний"
    dg = s["digest"]
    lines += ["", "🔔 Напоминания по умолчанию:", f"• Мероприятия: {ev}", f"• Задачи: {tk}, {rep}",
              f"• Время по умолчанию для задач: {s['tk_hour']:02d}:00",
              "• ☀️ Утренняя сводка: " + (f"включена на {dg['hour']:02d}:00" if dg["on"] else "выключена")]
    lines += ["", plan_line(uid)]
    return "\n".join(lines)


# ---------- просроченное и массовые действия ----------

def is_past_event(e):
    """Мероприятие уже закончилось (прошёл час после начала) и не отмечено выполненным."""
    return e["kind"] == "event" and bool(e["due"]) and not e["done"] and e["due"] + EVENT_MIN * 60 < time.time()


def overdue_days(e, tz):
    z = zone(tz)
    return max(0, (datetime.now(z).date() - datetime.fromtimestamp(e["due"], z).date()).days)


def overdue_items(uid, tz):
    """Задачи с прошедшим сроком и мероприятия, которые уже закончились."""
    res = [e for e in ITEMS if e["user_id"] == uid and not e["done"] and (is_overdue(e, tz) or is_past_event(e))]
    return sorted(res, key=lambda e: e["due"])


def ctx_items(uid, ctx, tz):
    """Дела, которые показывает экран списка. ctx: sec.<тип>.<раздел> | day.<дата> | over | done."""
    parts = ctx.split(".")
    if parts[0] == "sec":
        items = items_of(uid, parts[1], int(parts[2]))
        items.sort(key=list_sort_key)
        return items
    if parts[0] == "day":
        return items_on_day(uid, parse_day(parts[1]), tz)
    if parts[0] == "over":
        return overdue_items(uid, tz)
    if parts[0] == "done":
        return items_of(uid, done=True)[-30:]
    return []


# ---------- умный разбор даты, времени и повторов из текста ----------

_WD_PATS = [(0, r"понедельник\w*|пн"), (1, r"вторник\w*|вт"), (2, r"сред[аыуе]|ср"), (3, r"четверг\w*|чт"),
            (4, r"пятниц[аыуе]|пт"), (5, r"суббот[аыуе]|сб"), (6, r"воскресень[еяю]|воскр|вс")]
_WD_ALT = "|".join(p for _, p in _WD_PATS)
_MONTHS_RU = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
              "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
_MON_ALT = "янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек"

_RE_REC_N = re.compile(r"(?<![а-я])кажд\w+\s+(\d{1,3})\s*(дн\w*|д(?![а-я])|нед\w*|мес\w*|год\w*|лет)(?![а-я])")
_RE_REC_WORK = re.compile(r"(?<![а-я])(?:по\s+будням|в\s+будни|каждый\s+будний\s+день)(?![а-я])")
_RE_REC_WD = re.compile(r"(?<![а-я])кажд(?:ый|ую|ое)\s+((?:" + _WD_ALT + r")(?:[\s,]+(?:и\s+)?(?:" + _WD_ALT + r"))*)(?![а-я])")
_RE_REC_TOD = re.compile(r"(?<![а-я])кажд(?:ое\s+утро|ый\s+вечер)(?![а-я])")
_RE_REC_DAY = re.compile(r"(?<![а-я])(?:ежедневно|каждодневно|каждый\s+день)(?![а-я])")
_RE_REC_WEEK = re.compile(r"(?<![а-я])(?:еженедельно|кажд\w+\s+недел\w+|раз\s+в\s+недел\w+)(?![а-я])")
_RE_REC_MON = re.compile(r"(?<![а-я])(?:ежемесячно|каждый\s+месяц|раз\s+в\s+месяц)(?![а-я])")
_RE_REC_YEAR = re.compile(r"(?<![а-я])(?:ежегодно|каждый\s+год|раз\s+в\s+год)(?![а-я])")

_RE_NUMDATE = re.compile(r"(?<![\d.:/\-])(\d{1,2})([./\-])(\d{1,2})(?:\2(\d{4}|\d{2}))?(?![\d]|[./:]\d)")
_RE_MONDATE = re.compile(r"(?<![\d.:/\-])(\d{1,2})(?:-?го)?\s+(" + _MON_ALT + r")[а-я]*\.?(?:\s+(\d{4})(?![\d:]))?")
_RE_DOM = re.compile(r"(?<![\d.:/\-])(\d{1,2})(?:-?го)?\s+числа(?![а-я])")
_RE_THROUGH = re.compile(r"(?<![а-я])через\s+(?:(\d{1,3})\s*)?(полчаса|мин\w*|м(?![а-я])|час\w*|ч(?![а-я])|дн\w*|день|д(?![а-я])|недел\w*|нед(?![а-я])|мес\w*)")
_RE_DAYWORD = re.compile(r"(?<![а-я])(сегодня|завтра|послезавтра)(?![а-я])")
_RE_WD = re.compile(r"(?<![а-я0-9])(?:(?:в|во)\s+)?(" + _WD_ALT + r")\.?(?![а-я0-9])")

_RE_T_AMPM = re.compile(r"(?:(?<![а-я])в\s*)?(?<![\d:.])(\d{1,2})(?:[:.\s](\d{2}))?\s*(утра|дня|вечера|ночи)(?![а-я])")
_RE_T_HCH = re.compile(r"(?<!на )(?<!за )(?:(?<![а-я])в\s*)?(?<![\d:.])(\d{1,2})\s*(?:ч|час\w*)(?![а-я])(?:\s*(\d{1,2})\s*(?:м|мин\w*)?(?![а-я]))?")
_RE_T_COLON = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{2})(?![\d:])")
_RE_T_PREP = re.compile(r"(?<![а-я])(?:в|к|около|ровно\s+в)\s*(\d{1,2})(?:[\s.]\s*(\d{2}))?(?![\d:.])(?!\s*раз)")
_RE_T_LINE = re.compile(r"(?m)^[ \t]*(\d{1,2})(?:[ \t:.,]+(\d{2}))?[ \t]*$")
_RE_T_WORD = re.compile(r"(?<![а-я])(полдень|полночь|в\s+обед|утром|днем|вечером|ночью)(?![а-я])")
_WORD_TIMES = {"полдень": (12, 0), "полночь": (0, 0), "в обед": (13, 0), "утром": (9, 0),
               "днем": (13, 0), "вечером": (19, 0), "ночью": (23, 0)}


def _wd_index(tok):
    for i, pat in _WD_PATS:
        if re.fullmatch(pat, tok):
            return i
    return None


def _add_months(d, n):
    y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _next_date(today, month, day):
    """Ближайшая дата день.месяц, не раньше сегодняшнего дня: в этом году, а если уже прошла, то в следующем."""
    for y in range(today.year, today.year + 9):
        try:
            d = date(y, month, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _next_dom(today, day):
    """Ближайшее «N числа» не раньше сегодняшнего дня."""
    for i in range(0, 15):
        d0 = _add_months(date(today.year, today.month, 1), i)
        try:
            d = date(d0.year, d0.month, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _ampm(h, word):
    if word == "утра":
        return 0 if h == 12 else h
    if word == "дня":
        return h if h == 12 else (h + 12 if h < 12 else h)
    if word == "вечера":
        return h + 12 if h < 12 else h
    return 0 if h == 12 else (h + 12 if 6 <= h < 12 else h)  # ночи


def _clean_title(t):
    t = " ".join(t.split())
    prep = r"(?:в|во|на|к|до|от|с|по|и)"
    for _ in range(3):
        t = re.sub(rf"^{prep}\s+", "", t)
        t = re.sub(rf"\s+{prep}$", "", t)
    if re.fullmatch(prep, t):
        t = ""
    return t.strip(" ,.;:-—–!?")


def extract_datetime(text, tz, now=None):
    """Достаёт из текста дату, время и повтор. Понимает:
    «2.10», «2.8.26», «2 октября», «вт», «в пятницу», «завтра», «через 3 д», «5 числа»;
    «18:30», «18 23», «в 7 вечера», «в 18 часов», время отдельной строкой;
    «каждый день», «по будням», «каждый пн и ср», «раз в месяц».
    Дата без года: этот год, а если она уже прошла, то следующий."""
    z = zone(tz)
    now = now or datetime.now(z)
    today = now.date()
    low = text.lower().replace("ё", "е")
    chars = list(low)
    spans = []

    def cur():
        return "".join(chars)

    def take(m):
        s, e = m.span()
        spans.append((s, e))
        for i in range(s, e):
            if chars[i] != "\n":
                chars[i] = " "

    # 1. повторение
    rec, rec_tod = None, None
    m = _RE_REC_N.search(cur())
    if m:
        u = m.group(2)
        t = "d" if u.startswith("д") else "w" if u.startswith("нед") else "m" if u.startswith("мес") else "y"
        rec = {"t": t, "n": max(1, int(m.group(1)))}
        take(m)
    if not rec:
        m = _RE_REC_WORK.search(cur())
        if m:
            rec = {"t": "w", "n": 1, "wd": [0, 1, 2, 3, 4]}
            take(m)
    if not rec:
        m = _RE_REC_WD.search(cur())
        if m:
            wds = sorted({_wd_index(x) for x in re.findall(_WD_ALT, m.group(1)) if _wd_index(x) is not None})
            rec = {"t": "w", "n": 1, "wd": wds}
            take(m)
    if not rec:
        m = _RE_REC_TOD.search(cur())
        if m:
            rec, rec_tod = {"t": "d", "n": 1}, (9 if "утро" in m.group(0) else 19)
            take(m)
    for rx, t in ((_RE_REC_DAY, "d"), (_RE_REC_WEEK, "w"), (_RE_REC_MON, "m"), (_RE_REC_YEAR, "y")):
        if rec:
            break
        m = rx.search(cur())
        if m:
            rec = {"t": t, "n": 1}
            take(m)

    # 2. дата
    date_found, abs_ts, explicit_year = None, None, False
    for m in _RE_NUMDATE.finditer(cur()):
        d_, mo, ys = int(m.group(1)), int(m.group(3)), m.group(4)
        if not (1 <= mo <= 12 and 1 <= d_ <= 31):
            continue
        if ys:
            try:
                dt = date(int(ys) + (2000 if len(ys) == 2 else 0), mo, d_)
            except ValueError:
                continue
            explicit_year = True
        else:
            dt = _next_date(today, mo, d_)
            if dt is None:
                continue
        date_found = dt
        take(m)
        break
    if date_found is None:
        for m in _RE_MONDATE.finditer(cur()):
            d_, mo, ys = int(m.group(1)), _MONTHS_RU[m.group(2)], m.group(3)
            if ys:
                try:
                    dt = date(int(ys), mo, d_)
                except ValueError:
                    continue
                explicit_year = True
            else:
                dt = _next_date(today, mo, d_)
                if dt is None:
                    continue
            date_found = dt
            take(m)
            break
    if date_found is None:
        m = _RE_DOM.search(cur())
        if m:
            dt = _next_dom(today, int(m.group(1)))
            if dt:
                date_found = dt
                take(m)
    if date_found is None:
        m = _RE_THROUGH.search(cur())
        if m:
            num, u = (int(m.group(1)) if m.group(1) else 1), m.group(2)
            if u == "полчаса":
                abs_ts = int((now + timedelta(minutes=30)).timestamp())
            elif u.startswith("мин") or u == "м":
                abs_ts = int((now + timedelta(minutes=num)).timestamp())
            elif u.startswith("час") or u == "ч":
                abs_ts = int((now + timedelta(hours=num)).timestamp())
            elif u.startswith(("дн", "ден")) or u == "д":
                date_found = today + timedelta(days=num)
            elif u.startswith("нед"):
                date_found = today + timedelta(weeks=num)
            else:
                date_found = _add_months(today, num)
            take(m)
    if date_found is None and abs_ts is None:
        m = _RE_DAYWORD.search(cur())
        if m:
            date_found = today + timedelta(days={"сегодня": 0, "завтра": 1, "послезавтра": 2}[m.group(1)])
            take(m)
    wd_idx = None
    wd_matches = list(_RE_WD.finditer(cur()))
    if wd_matches:
        if date_found is None and abs_ts is None:
            wd_idx = _wd_index(wd_matches[0].group(1))
        for m in wd_matches:
            take(m)

    # 3. время
    time_val = None

    def first_valid(rx, conv):
        for mm in rx.finditer(cur()):
            r = conv(mm)
            if r and 0 <= r[0] <= 23 and 0 <= r[1] <= 59:
                take(mm)
                return r
        return None

    time_val = first_valid(_RE_T_AMPM, lambda mm: (_ampm(int(mm.group(1)), mm.group(3)), int(mm.group(2) or 0))
                           if 1 <= int(mm.group(1)) <= 12 else None)
    if time_val is None:
        time_val = first_valid(_RE_T_HCH, lambda mm: (int(mm.group(1)), int(mm.group(2) or 0)))
    if time_val is None:
        time_val = first_valid(_RE_T_COLON, lambda mm: (int(mm.group(1)), int(mm.group(2))))
    if time_val is None:
        time_val = first_valid(_RE_T_PREP, lambda mm: (int(mm.group(1)), int(mm.group(2) or 0)))
    if time_val is None:
        time_val = first_valid(_RE_T_LINE, lambda mm: (int(mm.group(1)), int(mm.group(2) or 0)))
    if time_val is None and spans:  # «Встреча 2.10 18 23» или «вт 9 05»: время сразу после даты в конце строки
        anchor = max(e for _, e in spans)
        mm = re.match(r"[ \t]+(\d{1,2})[ \t:.](\d{2})(?=[ \t]*(?:\n|$))", cur()[anchor:])
        if mm and int(mm.group(1)) <= 23 and int(mm.group(2)) <= 59:
            time_val = (int(mm.group(1)), int(mm.group(2)))
            s0, e0 = anchor + mm.start(), anchor + mm.end()
            spans.append((s0, e0))
            for i in range(s0, e0):
                if chars[i] != "\n":
                    chars[i] = " "
    if time_val is None:
        mm = _RE_T_WORD.search(cur())
        if mm:
            time_val = _WORD_TIMES[re.sub(r"\s+", " ", mm.group(1))]
            take(mm)
    if time_val is None and rec_tod is not None:
        time_val = (rec_tod, 0)

    # 4. собираем дату
    def at(d_, tv):
        return datetime.combine(d_, dtime(*tv), tzinfo=z)

    if abs_ts is None:
        if date_found is None and wd_idx is not None:
            delta = (wd_idx - today.weekday()) % 7
            if delta == 0 and (time_val is None or at(today, time_val) <= now):
                delta = 7
            date_found = today + timedelta(days=delta)
        if date_found is None and rec:
            t = rec["t"]
            if t == "w" and rec.get("wd"):
                for i in range(0, 8):
                    cand = today + timedelta(days=i)
                    if cand.weekday() in rec["wd"] and (i > 0 or time_val is None or at(today, time_val) > now):
                        date_found = cand
                        break
            elif t == "d" and time_val is not None and at(today, time_val) <= now:
                date_found = today + timedelta(days=1)
            else:
                date_found = today
        if date_found is None and time_val is not None:
            date_found = today if at(today, time_val) > now else today + timedelta(days=1)
    if rec and date_found:
        if rec["t"] == "w" and not rec.get("wd"):
            rec["wd"] = [date_found.weekday()]
        elif rec["t"] == "m":
            rec["dom"] = date_found.day
        elif rec["t"] == "y":
            rec["moy"], rec["dom"] = date_found.month, date_found.day

    spans.sort()
    out, pos = [], 0
    for s, e in spans:
        if s >= pos:
            out.append(text[pos:s])
            pos = e
    out.append(text[pos:])
    title = _clean_title(" ".join("".join(out).split("\n"))) if spans else " ".join(text.split())
    found = bool(date_found or time_val or abs_ts or rec)
    return {"title": title, "date": date_found, "time": time_val, "abs_ts": abs_ts, "rec": rec,
            "found": found, "explicit_year": explicit_year}


def parse_text(text, tz, now=None):
    """Разбор текста дела: название, срок, повтор.
    due: готовое время (ts) или None; day: дата без времени; past: названа уже прошедшая дата."""
    z = zone(tz)
    now = now or datetime.now(z)
    r = extract_datetime(text, tz, now)
    due, day, past = None, None, False
    if r["abs_ts"]:
        due = r["abs_ts"]
    elif r["date"] and r["time"]:
        ts = int(datetime.combine(r["date"], dtime(*r["time"]), tzinfo=z).timestamp())
        if ts > now.timestamp():
            due = ts
        elif r["rec"]:
            due = next_due(ts, r["rec"], tz, now.timestamp())
        else:
            past = True
    elif r["date"]:
        if r["date"] >= now.date():
            day = r["date"]
        else:
            past = True
    found, title, rec = r["found"], r["title"], r["rec"]
    if not found:
        loc = safe_local(text, tz)
        if loc:
            title, due, found = loc[0], loc[1], True
    return {"title": title or ("Без названия" if found else text.strip()), "due": due, "day": day,
            "rec": rec, "past": past, "found": found, "time": r["time"], "date": r["date"]}


# ---------- повторяющиеся дела ----------

def rec_text(rec):
    if not rec:
        return ""
    t, n = rec["t"], rec.get("n", 1)
    if t == "d":
        return "каждый день" if n == 1 else f"каждые {n} д"
    if t == "w":
        wd = sorted(rec.get("wd") or [])
        if wd == [0, 1, 2, 3, 4] and n == 1:
            return "по будням"
        names = ", ".join(WD[i].lower() for i in wd)
        base = "каждую неделю" if n == 1 else f"каждые {n} нед"
        return f"{base} ({names})" if names else base
    if t == "m":
        return "каждый месяц" if n == 1 else f"каждые {n} мес"
    return "каждый год" if n == 1 else f"каждые {n} г"


def _step(d, rec):
    """Следующее повторение (местное время без пояса)."""
    t, n = rec["t"], max(1, int(rec.get("n", 1)))
    if t == "d":
        return d + timedelta(days=n)
    if t == "w":
        wd = rec.get("wd") or [d.weekday()]
        monday = d.date() - timedelta(days=d.weekday())
        for i in range(1, 7 * n + 8):
            cand = d.date() + timedelta(days=i)
            if cand.weekday() in wd and ((cand - monday).days // 7) % n == 0:
                return datetime.combine(cand, d.time())
        return d + timedelta(weeks=n)
    if t == "m":
        y, m = d.year + (d.month - 1 + n) // 12, (d.month - 1 + n) % 12 + 1
        return d.replace(year=y, month=m, day=min(rec.get("dom", d.day), calendar.monthrange(y, m)[1]))
    y, moy = d.year + n, rec.get("moy", d.month)
    return d.replace(year=y, month=moy, day=min(rec.get("dom", d.day), calendar.monthrange(y, moy)[1]))


def next_due(due_ts, rec, tz, now=None):
    """Ближайшее повторение строго после «сейчас» (местное время суток сохраняется)."""
    z = zone(tz)
    now = now if now is not None else time.time()
    d = datetime.fromtimestamp(due_ts, z).replace(tzinfo=None)
    for _ in range(3000):
        d = _step(d, rec)
        ts = int(d.replace(tzinfo=z).timestamp())
        if ts > now:
            return ts
    return int(d.replace(tzinfo=z).timestamp())


def set_rec(e, rec, tz):
    """Ставит повтор делу и дополняет детали по его времени (день недели, число месяца)."""
    if rec and e["due"]:
        d = datetime.fromtimestamp(e["due"], zone(tz))
        if rec["t"] == "w" and not rec.get("wd"):
            rec["wd"] = [d.weekday()]
        elif rec["t"] == "m":
            rec["dom"] = d.day
        elif rec["t"] == "y":
            rec["moy"], rec["dom"] = d.month, d.day
    e["rec"] = rec or None


def roll_item(e, tz):
    """Переносит повторяющееся дело на следующее время и снимает отметки."""
    e["due"] = next_due(e["due"], e["rec"], tz)
    e["done"] = False
    reset_fired(e)
    for c in e["checks"]:
        c["d"] = False
    return e["due"]


# ---------- тарифы и лимиты ----------

class LimitError(Exception):
    """Достигнут лимит тарифа. Сообщение показываем человеку."""

    def __init__(self, message, name="", plan="free"):
        super().__init__(message)
        self.name, self.plan = name, plan


def plan_of(uid):
    if uid in ADMIN_IDS:
        return "admin"
    p = PLANS.get(uid)
    return "pro" if p and p["until"] > time.time() else "free"


def limit(uid, name):
    """Лимит для человека. None = без ограничений (администратор)."""
    plan = plan_of(uid)
    return None if plan == "admin" else LIMITS["pro" if plan == "pro" else "free"][name]


def count_recurring(uid):
    return sum(1 for e in ITEMS if e["user_id"] == uid and e.get("rec") and not e["done"])


def check_limit(uid, name, current):
    lim = limit(uid, name)
    if lim is not None and current >= lim:
        plan = plan_of(uid)
        who = "тарифа Pro" if plan == "pro" else "бесплатного тарифа"
        raise LimitError(f"Достигнут лимит {who}: {lim} {LIMIT_WORDS[name]}.", name, plan)


def grant_pro(uid, days):
    cur = PLANS.get(uid, {}).get("until", 0)
    PLANS[uid] = {"until": int(max(time.time(), cur) + days * 86400)}
    return PLANS[uid]["until"]


def revoke_pro(uid):
    return PLANS.pop(uid, None) is not None


def ai_used_today(uid):
    d, n = AI_USED.get(uid, (None, 0))
    return n if d == date.today() else 0


def plan_line(uid):
    plan, tz = plan_of(uid), get_tz(uid)
    if plan == "admin":
        return "👑 Тариф: администратор, без ограничений"
    if plan == "pro":
        return f"💎 Тариф: Pro до {datetime.fromtimestamp(PLANS[uid]['until'], zone(tz)):%d.%m.%Y}"
    f = LIMITS["free"]
    return (f"🆓 Тариф: бесплатный. Дел {len(items_of(uid))} из {f['items']}, "
            f"повторяющихся {count_recurring(uid)} из {f['recurring']}, "
            f"разборов ИИ сегодня {ai_used_today(uid)} из {f['ai']}. Подробнее: /pro")


def plan_text(uid):
    plan, f, p = plan_of(uid), LIMITS["free"], LIMITS["pro"]
    lines = ["💎 Тариф Pro", "", plan_line(uid), "",
             "Что меняется:",
             f"• Активных дел: {f['items']} → {p['items']}",
             f"• Разделов каждого типа: {f['sections']} → {p['sections']}",
             f"• Повторяющихся дел: {f['recurring']} → {p['recurring']}",
             f"• Разборов текста и ссылок ИИ в день: {f['ai']} → {p['ai']}", "",
             f"Цена: {PRO_PRICE} ⭐ Telegram Stars на {PRO_DAYS} дн. Платёж разовый: когда срок кончится, "
             "продли в любой момент. Всё, что ты создал, останется и после окончания Pro."]
    if plan == "admin":
        lines += ["", "Тебе покупка не нужна: у администратора всё без ограничений."]
    return "\n".join(lines)


# ---------- утренняя сводка ----------

def digest_text(uid):
    tz = get_tz(uid)
    z = zone(tz)
    today = datetime.now(z).date()
    items = items_on_day(uid, today, tz)
    events = [e for e in items if e["kind"] == "event" and not e["done"]]
    tasks = [e for e in items if e["kind"] == "task" and not e["done"]]
    over = overdue_items(uid, tz)
    if not (events or tasks or over):
        return ""
    lines = [f"☀️ Доброе утро! Сегодня {fmt_day(today)}"]
    if events:
        lines += ["", "📅 Мероприятия:"]
        for e in events:
            mark = "⚠️" if conflicts_for(e) else "🕐"
            lines.append(f"{mark} {datetime.fromtimestamp(e['due'], z):%H:%M} · {e['title']}")
    if tasks:
        lines += ["", "📝 Задачи на сегодня:"]
        lines += [f"• {e['title']} (срок в {datetime.fromtimestamp(e['due'], z):%H:%M})" for e in tasks]
    if over:
        lines += ["", f"🔴 Просрочено: {len(over)}. Открой «📋 Мои дела» → «🔴 Просрочено»."]
    tomorrow = [e for e in items_on_day(uid, today + timedelta(days=1), tz) if not e["done"]]
    if tomorrow:
        lines += ["", f"🌙 Завтра дел: {len(tomorrow)}"]
    return "\n".join(lines)


def digest_due(uid, now=None):
    """Пора ли присылать сводку: время наступило (но не больше 3 ч назад) и сегодня ещё не присылали."""
    d = get_set(uid)["digest"]
    if not d["on"]:
        return False
    z = zone(get_tz(uid))
    now = now or datetime.now(z)
    sched = datetime.combine(now.date(), dtime(d["hour"], 0), tzinfo=z)
    return sched <= now < sched + timedelta(hours=3) and d.get("last") != ymd(now.date())


def digest_set_on(uid, on):
    d = get_set(uid)["digest"]
    d["on"] = on
    if on:  # в день включения не присылаем, если время уже прошло
        z = zone(get_tz(uid))
        now = datetime.now(z)
        d["last"] = ymd(now.date()) if now >= datetime.combine(now.date(), dtime(d["hour"], 0), tzinfo=z) else None


# ---------- сохранение состояния ----------

def snapshot():
    """Всё, что нужно пережить перезапуск, в виде обычных словарей (для JSON)."""
    return {
        "items": [dict(e, fired=sorted(e["fired"])) for e in ITEMS],
        "sections": [dict(s) for s in SECTIONS],
        "user_tz": {str(k): v for k, v in USER_TZ.items()},
        "user_set": {str(k): v for k, v in USER_SET.items()},
        "user_meta": {str(k): v for k, v in USER_META.items()},
        "plans": {str(k): v for k, v in PLANS.items()},
        "payments": list(PAYMENTS),
        "ai_used": {str(k): [v[0].isoformat(), v[1]] for k, v in AI_USED.items()},
    }


def restore(state):
    """Заполняет данные из сохранённого состояния (на месте, чтобы все ссылки остались рабочими)."""
    global _ids, _sids
    ITEMS[:] = []
    for e in state.get("items", []):
        e = dict(e)
        e["fired"] = set(e.get("fired", []))
        e.setdefault("rec", None)
        e.setdefault("checks", [])
        e.setdefault("desc", "")
        ITEMS.append(e)
    SECTIONS[:] = [dict(s) for s in state.get("sections", [])]
    for target, key in ((USER_TZ, "user_tz"), (USER_SET, "user_set"), (USER_META, "user_meta"), (PLANS, "plans")):
        target.clear()
        target.update({int(k): v for k, v in state.get(key, {}).items()})
    PAYMENTS[:] = list(state.get("payments", []))
    AI_USED.clear()
    for k, v in state.get("ai_used", {}).items():
        try:
            AI_USED[int(k)] = (date.fromisoformat(v[0]), int(v[1]))
        except Exception:
            pass
    _ids = itertools.count(max([e["id"] for e in ITEMS], default=0) + 1)
    _sids = itertools.count(max([s["id"] for s in SECTIONS], default=0) + 1)


def stats():
    """Сводка для администратора."""
    users = set(USER_TZ) | set(USER_META)
    now = time.time()
    return {
        "users": len(users),
        "items": sum(1 for e in ITEMS if not e["done"]),
        "done": sum(1 for e in ITEMS if e["done"]),
        "pro": sum(1 for p in PLANS.values() if p["until"] > now),
        "payments": len(PAYMENTS),
        "stars": sum(p.get("amount", 0) for p in PAYMENTS),
    }
