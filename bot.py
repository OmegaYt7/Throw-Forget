import asyncio
import ipaddress
import itertools
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from dateparser.search import search_dates

# Всё секретное берётся из переменных окружения, а не из кода
TOKEN = os.getenv("BOT_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")  # бесплатный ключ с console.groq.com
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
DAILY_AI_LIMIT = int(os.getenv("DAILY_AI_LIMIT", "10"))
DEFAULT_TZ = "Europe/Moscow"

# Варианты напоминаний: минуты до события -> подпись / «через сколько»
LABELS = {0: "в момент", 10: "за 10 мин", 30: "за 30 мин",
          60: "за 1 час", 180: "за 3 часа", 1440: "за 1 день"}
LEFT = {10: "10 минут", 30: "30 минут", 60: "1 час", 180: "3 часа", 1440: "1 день"}
DEFAULT_OFFSETS = [60, 0]   # по умолчанию: за 1 час и в момент события
MAX_NAGS = 8                # максимум повторов в режиме «каждый час»

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

# Популярные часовые пояса для кнопок
PRESETS = [
    ("🇷🇺 Москва +3", "Europe/Moscow"), ("🇺🇦 Киев", "Europe/Kyiv"),
    ("🇧🇾 Минск +3", "Europe/Minsk"), ("🇷🇺 Калининград +2", "Europe/Kaliningrad"),
    ("🇷🇺 Самара +4", "Europe/Samara"), ("🇷🇺 Екатеринбург +5", "Asia/Yekaterinburg"),
    ("🇰🇿 Алматы +5", "Asia/Almaty"), ("🇷🇺 Омск +6", "Asia/Omsk"),
    ("🇷🇺 Новосибирск +7", "Asia/Novosibirsk"), ("🇷🇺 Иркутск +8", "Asia/Irkutsk"),
    ("🇷🇺 Якутск +9", "Asia/Yakutsk"), ("🇷🇺 Владивосток +10", "Asia/Vladivostok"),
]
# Города, которые можно написать текстом
CITIES = {
    "москва": "Europe/Moscow", "санкт-петербург": "Europe/Moscow", "спб": "Europe/Moscow",
    "питер": "Europe/Moscow", "казань": "Europe/Moscow", "нижний новгород": "Europe/Moscow",
    "краснодар": "Europe/Moscow", "воронеж": "Europe/Moscow", "ростов-на-дону": "Europe/Moscow",
    "киев": "Europe/Kyiv", "киiв": "Europe/Kyiv", "минск": "Europe/Minsk",
    "калининград": "Europe/Kaliningrad", "самара": "Europe/Samara",
    "екатеринбург": "Asia/Yekaterinburg", "уфа": "Asia/Yekaterinburg",
    "челябинск": "Asia/Yekaterinburg", "пермь": "Asia/Yekaterinburg",
    "омск": "Asia/Omsk", "новосибирск": "Asia/Novosibirsk", "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk", "якутск": "Asia/Yakutsk", "владивосток": "Asia/Vladivostok",
    "хабаровск": "Asia/Vladivostok", "алматы": "Asia/Almaty", "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent", "баку": "Asia/Baku", "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan", "кишинев": "Europe/Chisinau", "вильнюс": "Europe/Vilnius",
    "рига": "Europe/Riga", "таллин": "Europe/Tallinn", "варшава": "Europe/Warsaw",
    "берлин": "Europe/Berlin", "лондон": "Europe/London", "стамбул": "Europe/Istanbul",
    "дубай": "Asia/Dubai", "нью-йорк": "America/New_York",
}

# Пока без базы: всё в памяти, при перезапуске пропадает
ITEMS = []      # задачи и события
USER_TZ = {}    # user_id -> часовой пояс
USER_SET = {}   # user_id -> {"offsets": [...], "repeat": bool}
AI_USED = {}    # user_id -> (дата, сколько запросов к ИИ)
AWAIT_TZ = set()  # кто сейчас вводит пояс вручную
MODE = {}       # user_id -> "event" | "task" | "ai" (что добавляем следующим сообщением)
_ids = itertools.count(1)
URL_RE = re.compile(r"https?://\S+")

dp = Dispatcher()


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
    """Понимает: «Москва», «+3», «UTC+5:30», «15:30» (твоё текущее время), «Europe/Kyiv»."""
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
    btns = [InlineKeyboardButton(text=label, callback_data=f"tz:{name}") for label, name in PRESETS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(text="✍️ Ввести вручную", callback_data="tz:manual")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- вспомогательное ----------

def get_tz(uid):
    return USER_TZ.get(uid, DEFAULT_TZ)


def get_set(uid):
    return USER_SET.setdefault(uid, {"offsets": list(DEFAULT_OFFSETS), "repeat": False})


def ai_allowed(uid):
    """Лимит запросов к ИИ на человека в день (чтобы не кончилась бесплатная квота)."""
    today = date.today()
    d, n = AI_USED.get(uid, (today, 0))
    if d != today:
        n = 0
    if n >= DAILY_AI_LIMIT:
        return False
    AI_USED[uid] = (today, n + 1)
    return True


def fmt_time(ts, tz):
    return datetime.fromtimestamp(ts, zone(tz)).strftime("%d.%m.%Y в %H:%M")


def remind_text(e):
    parts = [LABELS[o] for o in sorted(e["offsets"], reverse=True)]
    s = ", ".join(parts) if parts else "без напоминаний"
    if e["repeat"]:
        s += " + повтор каждый час, пока не «Готово»"
    return s


def card(e, tz):
    lines = [("📅 " if e["due"] else "📝 ") + e["title"]]
    if e["due"]:
        lines.append("🕐 " + fmt_time(e["due"], tz))
    if e["place"]:
        lines.append("📍 " + e["place"])
    if e["note"]:
        lines.append("💬 " + e["note"])
    if e["due"]:
        lines.append("🔔 " + remind_text(e))
    if e["done"]:
        lines.append("✅ Выполнено")
    return "\n".join(lines)


def kb(e):
    eid = e["id"]
    if e["done"]:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="↩️ Вернуть", callback_data=f"undo:{eid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{eid}"),
        ]])
    if e["due"]:
        rows = [
            [InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{eid}"),
             InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze:{eid}")],
            [InlineKeyboardButton(text="🔔 Напоминания", callback_data=f"cfg:{eid}:open"),
             InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{eid}")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{eid}"),
             InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del:{eid}")],
            [InlineKeyboardButton(text="🌅 Утром", callback_data=f"when:{eid}:am"),
             InlineKeyboardButton(text="🌆 Вечером", callback_data=f"when:{eid}:pm"),
             InlineKeyboardButton(text="⏳ Через 3 ч", callback_data=f"when:{eid}:3h")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_kb(offsets, repeat, eid):
    """Меню выбора напоминаний. eid=0 — настройки по умолчанию, иначе конкретное событие."""
    btns = [
        InlineKeyboardButton(
            text=("✅ " if o in offsets else "▫️ ") + label,
            callback_data=f"cfg:{eid}:t{o}",
        )
        for o, label in LABELS.items()
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(
        text="🔁 Повтор каждый час: " + ("вкл" if repeat else "выкл"),
        callback_data=f"cfg:{eid}:rep",
    )])
    rows.append([InlineKeyboardButton(text="👌 Закрыть", callback_data=f"cfg:{eid}:close")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_list(uid, kind):
    """Список дел с вкладками. Нажатие на дело открывает его карточку."""
    z = zone(get_tz(uid))
    mine = [e for e in ITEMS if e["user_id"] == uid]
    if kind == "tasks":
        items = [e for e in mine if not e["due"] and not e["done"]][:10]
        head = "📝 Задачи"
    elif kind == "done":
        items = [e for e in mine if e["done"]][-10:]
        head = "✅ Выполненные"
    else:
        kind = "events"
        items = sorted((e for e in mine if e["due"] and not e["done"]), key=lambda e: e["due"])[:10]
        head = "📅 События"
    tabs = [("📅 События", "events"), ("📝 Задачи", "tasks"), ("✅ Готовые", "done")]
    rows = [[InlineKeyboardButton(text=("▸ " if k == kind else "") + label, callback_data=f"lst:{k}")
             for label, k in tabs]]
    for e in items:
        when = datetime.fromtimestamp(e["due"], z).strftime("%d.%m %H:%M  ") if e["due"] else ""
        rows.append([InlineKeyboardButton(text=(when + e["title"])[:60], callback_data=f"open:{e['id']}")])
    if kind == "done" and items:
        rows.append([InlineKeyboardButton(text="🧹 Очистить выполненные", callback_data="lst:clear")])
    text = head + (":\nНажми на дело, чтобы открыть." if items else "\nПока пусто.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def reset_fired(e):
    """Уже прошедшие напоминания не шлём задним числом."""
    now = int(time.time())
    e["nags"] = 0
    e["fired"] = {o for o in e["offsets"] if e["due"] and e["due"] - o * 60 <= now}


def add_item(uid, title, due=None, place=None, note=None):
    s = get_set(uid)
    e = {"id": next(_ids), "user_id": uid, "title": title, "due": due,
         "place": place, "note": note, "done": False,
         "offsets": list(s["offsets"]), "repeat": s["repeat"],
         "fired": set(), "nags": 0}
    reset_fired(e)
    ITEMS.append(e)
    return e


def find(eid, uid):
    return next((e for e in ITEMS if e["id"] == eid and e["user_id"] == uid), None)


async def safe_edit(msg, text, markup):
    try:
        await msg.edit_text(text, reply_markup=markup)
    except Exception:
        pass


async def need_tz(m):
    """Пока человек не выбрал пояс, просим выбрать. True = дальше не идём."""
    if m.from_user.id in USER_TZ:
        return False
    await m.answer(TZ_PROMPT, reply_markup=tz_kb())
    return True


async def guard(m):
    MODE.pop(m.from_user.id, None)
    return await need_tz(m)


async def tz_done(message, uid):
    name = get_tz(uid)
    now = datetime.now(zone(name)).strftime("%H:%M")
    await message.answer(
        f"✅ Часовой пояс: {name}\nСейчас у тебя {now}. Если время неверное, "
        f"нажми «{B_TZ}» и выбери другой.\n\n"
        "Теперь просто кинь мне текст, например: «Встреча с Олей завтра в 18:00»",
        reply_markup=MAIN_KB,
    )


# ---------- разбор без ИИ (бесплатно и быстро) ----------

def parse_local(text, tz):
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
    # Если в тексте только день без времени («завтра», «в пятницу»), ставим 09:00
    if not re.search(r"\d|через|час|минут|секунд|утр|вечер|дн[её]м|ночь|полдень|полночь", fragment.lower()):
        dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        if dt <= now:
            dt = now + timedelta(hours=1)
    title = text.replace(fragment, "").strip(" ,.-—:") or text
    return title, int(dt.timestamp())


# ---------- ссылки ----------

async def is_public(url):
    """Не даём боту ходить на внутренние адреса."""
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


# ---------- нейросеть (Groq, бесплатный тариф) ----------

async def ask_llm(text, tz):
    """Возвращает список {title, datetime, place, note} или None."""
    now = datetime.now(zone(tz)).strftime("%Y-%m-%d %H:%M, %A")
    system = (
        "Ты помощник-планировщик. Из текста пользователя выдели задачи и события. "
        f"Сейчас {now}, часовой пояс {tz}. Отвечай ТОЛЬКО JSON такого вида: "
        '{"items":[{"title":"коротко","datetime":"YYYY-MM-DDTHH:MM или null",'
        '"place":"строка или null","note":"строка или null"}]}. '
        "Если времени у дела нет, datetime = null (это задача). "
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


# ---------- команды и кнопки меню ----------

@dp.message(CommandStart())
async def start(m: Message):
    MODE.pop(m.from_user.id, None)
    if m.from_user.id not in USER_TZ:
        await m.answer(
            "Привет! 👋 Я превращаю любой текст, пересланное сообщение или ссылку "
            "в задачу или событие и напоминаю о них вовремя.\n\n"
            "Для начала скажи, какой у тебя часовой пояс, чтобы я не путал время.",
        )
        await m.answer(TZ_PROMPT, reply_markup=tz_kb())
        return
    await m.answer("С возвращением! Выбирай в меню внизу или просто кидай текст.", reply_markup=MAIN_KB)


@dp.message(Command("menu"))
async def menu(m: Message):
    if await guard(m):
        return
    await m.answer("Меню 👇", reply_markup=MAIN_KB)


@dp.message(Command("help"))
@dp.message(F.text == B_HELP)
async def help_cmd(m: Message):
    MODE.pop(m.from_user.id, None)
    await m.answer(
        "Как пользоваться:\n"
        "• Просто кинь текст: «Встреча с Олей завтра в 18:00», «купить молоко», "
        "пересланное сообщение или ссылку на мероприятие.\n"
        "• Под каждой карточкой есть кнопки: выполнить, отложить, настроить напоминания.\n\n"
        "Кнопки внизу:\n"
        f"{B_ADD} — добавить дело\n{B_LIST} — события, задачи, выполненные\n"
        f"{B_REM} — когда напоминать по умолчанию\n{B_TZ} — сменить пояс\n\n"
        "Команды: /add /list /settings /tz /menu /help"
    )


@dp.message(Command("add"))
@dp.message(F.text == B_ADD)
async def add_menu(m: Message):
    if await guard(m):
        return
    await m.answer(
        "Что добавим? Можно выбрать тип или просто кинуть текст, пересланное сообщение или ссылку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Событие", callback_data="add:event"),
             InlineKeyboardButton(text="📝 Задача", callback_data="add:task")],
            [InlineKeyboardButton(text="🤖 Из текста или ссылки", callback_data="add:ai")],
        ]),
    )


@dp.message(Command("list"))
@dp.message(F.text == B_LIST)
async def list_cmd(m: Message):
    if await guard(m):
        return
    text, markup = build_list(m.from_user.id, "events")
    await m.answer(text, reply_markup=markup)


@dp.message(Command("settings"))
@dp.message(F.text == B_REM)
async def settings(m: Message):
    if await guard(m):
        return
    s = get_set(m.from_user.id)
    await m.answer(
        "🔔 Напоминания по умолчанию\n"
        "Отметь, когда присылать напоминание о новых событиях. "
        "Для каждого события это можно поменять кнопкой «🔔 Напоминания» на его карточке.",
        reply_markup=menu_kb(s["offsets"], s["repeat"], 0),
    )


@dp.message(Command("tz"))
@dp.message(F.text == B_TZ)
async def tz_cmd(m: Message):
    uid = m.from_user.id
    MODE.pop(uid, None)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[0].startswith("/"):
        name = parse_tz_input(parts[1])
        if name:
            USER_TZ[uid] = name
            AWAIT_TZ.discard(uid)
            await tz_done(m, uid)
        else:
            await m.answer("Не понял такой пояс. Выбери кнопкой или напиши город, +3 или время.",
                           reply_markup=tz_kb())
        return
    if uid in USER_TZ:
        now = datetime.now(zone(get_tz(uid))).strftime("%H:%M")
        await m.answer(f"Сейчас: {get_tz(uid)} (у тебя {now}).\n\n{TZ_PROMPT}", reply_markup=tz_kb())
    else:
        await m.answer(TZ_PROMPT, reply_markup=tz_kb())


# ---------- главный обработчик текста ----------

@dp.message(F.text)
async def on_text(m: Message):
    uid = m.from_user.id
    text = m.text.strip()

    # Пока нет часового пояса (или человек вводит его вручную), читаем текст как пояс
    if uid not in USER_TZ or uid in AWAIT_TZ:
        name = parse_tz_input(text)
        if name:
            USER_TZ[uid] = name
            AWAIT_TZ.discard(uid)
            await tz_done(m, uid)
        else:
            await m.answer("Не понял. Выбери пояс кнопкой или напиши город, «+3» "
                           "или сколько сейчас у тебя времени (например 15:30).",
                           reply_markup=tz_kb())
        return

    tz = get_tz(uid)
    mode = MODE.pop(uid, None)

    # Режим «Задача»: сохраняем как есть, без разбора
    if mode == "task":
        e = add_item(uid, text[:200])
        await m.answer("✅ Добавлено\n" + card(e, tz), reply_markup=kb(e))
        return

    url_match = URL_RE.search(text)
    results = []  # (title, due, place, note)

    # 1) короткий текст с датой: разбираем бесплатно, без ИИ
    if mode != "ai" and not url_match and len(text) < 200:
        loc = parse_local(text, tz)
        if loc:
            results.append((loc[0], loc[1], None, None))

    # 2) сложное: ссылки, длинные тексты, непонятные даты -> нейросеть
    if not results:
        await m.bot.send_chat_action(m.chat.id, "typing")
        source = text
        if url_match:
            page = await fetch_page(url_match.group(0))
            if not page:
                await m.answer("Не смог открыть ссылку. Скинь текст мероприятия сообщением.")
                return
            comment = text.replace(url_match.group(0), "").strip()
            source = f"{comment}\nСсылка: {url_match.group(0)}\nСтраница: {page}"

        items = None
        if GROQ_KEY and ai_allowed(uid):
            items = await ask_llm(source, tz)
        elif GROQ_KEY:
            await m.answer("Лимит разбора ИИ на сегодня закончился. Напиши коротко с датой, например «завтра в 18:00».")
            return

        if items:
            for it in items[:10]:
                if it.get("title"):
                    results.append((str(it["title"]), to_ts(it.get("datetime"), tz),
                                    it.get("place"), it.get("note")))
        elif not url_match and len(text) < 200:
            # ИИ недоступен: сохраняем как обычную задачу без времени
            results.append((text, None, None, None))

    if not results:
        await m.answer("Не смог ничего выделить. Попробуй написать подробнее.")
        return

    for title, due, place, note in results:
        e = add_item(uid, title, due, place, note)
        await m.answer("✅ Добавлено\n" + card(e, tz), reply_markup=kb(e))


# ---------- инлайн-кнопки ----------

@dp.callback_query(F.data.startswith("tz:"))
async def on_tz(c: CallbackQuery):
    uid = c.from_user.id
    val = c.data[3:]
    if val == "manual":
        AWAIT_TZ.add(uid)
        await c.message.answer("Напиши город (например, Москва), пояс (+3 или UTC+5) "
                               "или сколько сейчас у тебя времени (например 15:30).")
        await c.answer()
        return
    USER_TZ[uid] = val
    AWAIT_TZ.discard(uid)
    await c.answer("Готово")
    await tz_done(c.message, uid)


@dp.callback_query(F.data.startswith("add:"))
async def on_add(c: CallbackQuery):
    mode = c.data.split(":")[1]
    MODE[c.from_user.id] = mode
    prompts = {
        "event": "📅 Напиши, что и когда. Например: «Встреча с Олей завтра в 18:00»",
        "task": "📝 Напиши задачу. Например: «купить молоко»",
        "ai": "🤖 Кинь длинный текст, пересланное сообщение или ссылку, я выделю всё нужное.",
    }
    await c.message.answer(prompts.get(mode, "Напиши текст."))
    await c.answer()


@dp.callback_query(F.data.startswith("lst:"))
async def on_list(c: CallbackQuery):
    uid = c.from_user.id
    kind = c.data.split(":")[1]
    if kind == "clear":
        ITEMS[:] = [e for e in ITEMS if not (e["user_id"] == uid and e["done"])]
        kind = "done"
    text, markup = build_list(uid, kind)
    await safe_edit(c.message, text, markup)
    await c.answer()


@dp.callback_query(F.data.startswith("open:"))
async def on_open(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if not e:
        await c.answer("Не найдено")
        return
    await c.message.answer(card(e, get_tz(c.from_user.id)), reply_markup=kb(e))
    await c.answer()


@dp.callback_query(F.data.startswith("done:"))
async def on_done(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        e["done"] = True
        await safe_edit(c.message, "✅ Выполнено: " + e["title"], None)
    await c.answer("Готово!")


@dp.callback_query(F.data.startswith("undo:"))
async def on_undo(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        e["done"] = False
        reset_fired(e)
        await safe_edit(c.message, card(e, get_tz(c.from_user.id)), kb(e))
    await c.answer("Вернул в список")


@dp.callback_query(F.data.startswith("del:"))
async def on_del(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        ITEMS.remove(e)
    await safe_edit(c.message, "🗑 Удалено", None)
    await c.answer("Удалено")


@dp.callback_query(F.data.startswith("snooze:"))
async def on_snooze(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        e["due"] = int(time.time()) + 3600
        reset_fired(e)
        await safe_edit(c.message, card(e, get_tz(c.from_user.id)), kb(e))
    await c.answer("Напомню через час")


@dp.callback_query(F.data.startswith("when:"))
async def on_when(c: CallbackQuery):
    """Для задачи без времени: быстро выбрать, когда напомнить."""
    _, eid, mode = c.data.split(":")
    uid = c.from_user.id
    e = find(int(eid), uid)
    if not e:
        await c.answer("Не найдено")
        return
    tzname = get_tz(uid)
    now = datetime.now(zone(tzname))
    if mode == "3h":
        dt = now + timedelta(hours=3)
    else:
        hour = 9 if mode == "am" else 19
        dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if dt <= now + timedelta(minutes=1):
            dt += timedelta(days=1)
    e["due"] = int(dt.timestamp())
    reset_fired(e)
    await safe_edit(c.message, card(e, tzname), kb(e))
    await c.answer("Поставил напоминание")


@dp.callback_query(F.data.startswith("cfg:"))
async def on_cfg(c: CallbackQuery):
    _, eid, what = c.data.split(":")
    eid, uid = int(eid), c.from_user.id
    tzname = get_tz(uid)
    e = None
    if eid == 0:
        target = get_set(uid)
    else:
        e = find(eid, uid)
        if not e:
            await c.answer("Не найдено")
            return
        target = e

    if what == "close":
        if e:
            await safe_edit(c.message, card(e, tzname), kb(e))
        else:
            try:
                await c.message.delete()
            except Exception:
                pass
        await c.answer()
        return

    if what == "rep":
        target["repeat"] = not target["repeat"]
    elif what.startswith("t"):
        o = int(what[1:])
        if o in target["offsets"]:
            target["offsets"].remove(o)
        else:
            target["offsets"].append(o)
        target["offsets"].sort(reverse=True)
    # what == "open" просто показывает меню

    markup = menu_kb(target["offsets"], target["repeat"], eid)
    if e:
        now = int(time.time())
        e["fired"] |= {o for o in e["offsets"] if e["due"] and e["due"] - o * 60 <= now}
        await safe_edit(c.message, card(e, tzname), markup)
    else:
        try:
            await c.message.edit_reply_markup(reply_markup=markup)
        except Exception:
            pass
    await c.answer()


# ---------- напоминания ----------

async def send_safe(bot, uid, text, markup):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
    except Exception:
        pass


async def reminder_loop(bot: Bot):
    while True:
        now = int(time.time())
        for e in list(ITEMS):
            if not e["due"] or e["done"]:
                continue
            tzname = get_tz(e["user_id"])
            for o in sorted(e["offsets"], reverse=True):
                if o not in e["fired"] and now >= e["due"] - o * 60:
                    e["fired"].add(o)
                    head = "⏰ Пора!" if o == 0 else f"🔔 Через {LEFT[o]}"
                    await send_safe(bot, e["user_id"], head + "\n" + card(e, tzname), kb(e))
            # режим «повторять каждый час, пока не отмечено Готово»
            if e["repeat"] and e["nags"] < MAX_NAGS and now >= e["due"] + 3600 * (e["nags"] + 1):
                e["nags"] += 1
                await send_safe(bot, e["user_id"],
                                "🔁 Ещё не отмечено «Готово»\n" + card(e, tzname), kb(e))
        await asyncio.sleep(20)


async def main():
    if not TOKEN:
        raise SystemExit("Не задана переменная BOT_TOKEN")
    bot = Bot(TOKEN)
    # Команды появятся в меню «/» автоматически, в BotFather ничего вводить не нужно
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Начать"),
            BotCommand(command="menu", description="Показать меню"),
            BotCommand(command="add", description="Добавить дело"),
            BotCommand(command="list", description="Мои задачи и события"),
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
