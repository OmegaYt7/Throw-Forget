import asyncio
import ipaddress
import itertools
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
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

# Пока без базы: всё в памяти, при перезапуске пропадает
ITEMS = []      # задачи и события
USER_TZ = {}    # user_id -> часовой пояс
USER_SET = {}   # user_id -> {"offsets": [...], "repeat": bool}
AI_USED = {}    # user_id -> (дата, сколько запросов к ИИ)
_ids = itertools.count(1)
URL_RE = re.compile(r"https?://\S+")

dp = Dispatcher()


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
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%d.%m.%Y в %H:%M")


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
    return "\n".join(lines)


def kb(e):
    rows = []
    row = [InlineKeyboardButton(text="✅ Готово", callback_data=f"done:{e['id']}")]
    if e["due"]:
        row.append(InlineKeyboardButton(text="⏰ +1 час", callback_data=f"snooze:{e['id']}"))
        rows.append(row)
        rows.append([InlineKeyboardButton(text="🔔 Напоминания", callback_data=f"cfg:{e['id']}:open")])
    else:
        rows.append(row)
        rows.append([
            InlineKeyboardButton(text="🌅 Утром", callback_data=f"when:{e['id']}:am"),
            InlineKeyboardButton(text="🌆 Вечером", callback_data=f"when:{e['id']}:pm"),
            InlineKeyboardButton(text="⏳ Через 3 ч", callback_data=f"when:{e['id']}:3h"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def menu_kb(offsets, repeat, eid):
    """Меню выбора напоминаний. eid=0 — настройки по умолчанию, иначе конкретное событие."""
    btns = [
        InlineKeyboardButton(
            text=("✅ " if o in offsets else "▫️ ") + label,
            callback_data=f"cfg:{eid}:o{o}",
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


# ---------- разбор без ИИ (бесплатно и быстро) ----------

def parse_local(text, tz):
    found = search_dates(
        text, languages=["ru"],
        settings={"PREFER_DATES_FROM": "future", "TIMEZONE": tz,
                  "RETURN_AS_TIMEZONE_AWARE": True},
    )
    if not found:
        return None
    fragment, dt = found[0]
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
    now = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M, %A")
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
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        return int(dt.timestamp())
    except Exception:
        return None


# ---------- команды ----------

@dp.message(CommandStart())
@dp.message(Command("help"))
async def start(m: Message):
    await m.answer(
        "Привет! Я превращаю любой текст в задачи и события с напоминаниями.\n\n"
        "Просто кинь мне:\n"
        "• «Встреча с Олей завтра в 18:00»\n"
        "• «купить молоко» (задача без времени)\n"
        "• длинный текст или пересланное сообщение\n"
        "• ссылку на афишу или мероприятие\n\n"
        "/list — всё, что запланировано\n"
        "/settings — когда напоминать по умолчанию\n"
        "/tz Europe/Kyiv — часовой пояс"
    )


@dp.message(Command("tz"))
async def set_tz(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2:
        await m.answer(f"Сейчас: {get_tz(m.from_user.id)}\nПример: /tz Asia/Almaty")
        return
    try:
        ZoneInfo(parts[1])
    except Exception:
        await m.answer("Не знаю такой пояс. Пример: Europe/Moscow")
        return
    USER_TZ[m.from_user.id] = parts[1]
    await m.answer(f"Часовой пояс: {parts[1]}")


@dp.message(Command("settings"))
async def settings(m: Message):
    s = get_set(m.from_user.id)
    await m.answer(
        "🔔 Напоминания по умолчанию\n"
        "Отметь, когда присылать напоминание о новых событиях. "
        "Для каждого события это можно поменять кнопкой «🔔 Напоминания».",
        reply_markup=menu_kb(s["offsets"], s["repeat"], 0),
    )


@dp.message(Command("list"))
async def list_items(m: Message):
    tz = get_tz(m.from_user.id)
    mine = [e for e in ITEMS if e["user_id"] == m.from_user.id and not e["done"]]
    events = sorted((e for e in mine if e["due"]), key=lambda e: e["due"])[:20]
    tasks = [e for e in mine if not e["due"]][:20]
    if not events and not tasks:
        await m.answer("Пока пусто.")
        return
    out = []
    if events:
        out.append("📅 События:")
        out += [f"• {datetime.fromtimestamp(e['due'], ZoneInfo(tz)).strftime('%d.%m %H:%M')} — {e['title']}"
                for e in events]
    if tasks:
        out.append("\n📝 Задачи:")
        out += [f"• {e['title']}" for e in tasks]
    await m.answer("\n".join(out))


# ---------- главный обработчик текста ----------

@dp.message(F.text)
async def on_text(m: Message):
    uid, tz = m.from_user.id, get_tz(m.from_user.id)
    text = m.text.strip()
    url_match = URL_RE.search(text)
    results = []  # (title, due, place, note)

    # 1) короткий текст с датой: разбираем бесплатно, без ИИ
    if not url_match and len(text) < 200:
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


# ---------- кнопки ----------

@dp.callback_query(F.data.startswith("done:"))
async def on_done(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        e["done"] = True
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Готово!")


@dp.callback_query(F.data.startswith("snooze:"))
async def on_snooze(c: CallbackQuery):
    e = find(int(c.data.split(":")[1]), c.from_user.id)
    if e:
        e["due"] = int(time.time()) + 3600
        reset_fired(e)
    await c.message.edit_reply_markup(reply_markup=None)
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
    now = datetime.now(ZoneInfo(tzname))
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
            await c.message.delete()
        await c.answer()
        return

    if what == "rep":
        target["repeat"] = not target["repeat"]
    elif what.startswith("o"):
        o = int(what[1:])
        if o in target["offsets"]:
            target["offsets"].remove(o)
        else:
            target["offsets"].append(o)
        target["offsets"].sort(reverse=True)

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
    asyncio.create_task(reminder_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
