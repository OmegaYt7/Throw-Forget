"""Telegram-часть бота: кнопки, меню, сообщения. Вся логика лежит в logic.py.

Запуск: python bot.py (нужна переменная окружения BOT_TOKEN).
"""
import asyncio
import calendar
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup,
)

# Всё нужное из logic.py (данные, время, дела, разделы, календарь, ИИ)
from logic import *  # noqa: F401,F403
from logic import slot_ok

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("BOT_TOKEN")

# Кнопки главного меню (внизу экрана)
B_ADD = "➕ Добавить"
B_LIST = "📋 Мои дела"
B_CAL = "🗓 Календарь"
B_PROFILE = "👤 Профиль"
B_REM = "🔔 Напоминания"
B_TZ = "🌍 Часовой пояс"
B_HELP = "❓ Помощь"
MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=B_ADD), KeyboardButton(text=B_LIST)],
              [KeyboardButton(text=B_CAL), KeyboardButton(text=B_PROFILE)],
              [KeyboardButton(text=B_REM), KeyboardButton(text=B_TZ)],
              [KeyboardButton(text=B_HELP)]],
    resize_keyboard=True, is_persistent=True,
)

dp = Dispatcher()


# ---------- кнопки-помощники ----------

def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def markup(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_kb():
    return markup([
        [btn("🌍 Часовой пояс", "pf:tz"), btn("🔔 Напоминания", "pf:rem")],
        [btn("🗓 Календарь", "pf:cal"), btn("📋 Мои дела", "pf:list")],
        [btn("🗑 Стереть мои данные", "pf:wipe")],
    ])


def tz_kb():
    rows = chunk([btn(label, f"tz:{name}") for label, name in PRESETS], 2)
    rows.append([btn("✍️ Ввести вручную", "tz:manual")])
    return markup(rows)


# ---------- карточка дела: кнопки ----------

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
    """Карточка + подсказка «что дальше». Мероприятие без времени сразу ведёт к выбору времени."""
    if e["kind"] == "event" and not e["due"] and not e["done"]:
        return picker_days(e, tz, prefix)
    head = prefix + "\n\n" if prefix else ""
    hint = next_hint(e, tz)
    return head + card(e, tz) + (("\n\n" + hint) if hint else ""), kb(e)


def time_response(e, tz, word):
    hint = next_hint(e, tz)
    return f"✅ {word}\n\n" + card(e, tz) + (("\n\n" + hint) if hint else ""), kb(e)


# ---------- выбор времени (мероприятие) и срока (задача) кнопками ----------

def picker_start(e, tz, prefix=""):
    return picker_days(e, tz, prefix) if e["kind"] == "event" else picker_dates(e, tz, prefix)


def picker_dates(e, tz, prefix=""):
    """Задача: сначала дата, потом час и минуты (как у мероприятия)."""
    today = datetime.now(zone(tz)).date()
    eid = e["id"]
    rows = [[btn("Сегодня", f"tp:{eid}:d:{ymd(today)}"),
             btn("Завтра", f"tp:{eid}:d:{ymd(today + timedelta(days=1))}")]]
    days = [today + timedelta(days=i) for i in range(2, 14)]
    rows += chunk([btn(f"{WD[d.weekday()]} {d:%d.%m}", f"tp:{eid}:d:{ymd(d)}") for d in days], 4)
    rows.append([btn("✍️ Написать дату и время", f"tp:{eid}:txt")])
    rows.append([btn("◀️ Отмена", f"tp:{eid}:x")])
    head = prefix + "\n\n" if prefix else ""
    text = (f"{head}📝 {e['title']}\n\n📅 До какого числа нужно сделать?\n"
            "Выбери дату. Час и минуты выберешь следующим шагом (формат 24 ч). "
            "Если выберешь «Сегодня», время можно поставить не раньше чем через 1 ч.")
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
            "Выбери день или быстрый вариант «через…». Точное время выберешь следующим шагом. "
            "Если в это время уже есть другое мероприятие, я предупрежу, но добавить всё равно можно.")
    return text, markup(rows)


def picker_hours(e, tz, day):
    """24 часа кнопками. Для сегодняшнего дня прошедшее время не показывается (у задач ещё и ближайший 1 ч)."""
    eid = e["id"]
    task = e["kind"] == "task"
    hours = [h for h in range(24) if slot_ok(e, tz, day, h, 55)]
    rows = chunk([btn(f"{h:02d}:00", f"tp:{eid}:h:{ymd(day)}:{h:02d}") for h in hours], 4)
    if task:
        dh = get_set(e["user_id"])["tk_hour"]
        if slot_ok(e, tz, day, dh, 0):
            rows.append([btn(f"🕘 В обычное время ({dh:02d}:00)", f"tp:{eid}:dd:{ymd(day)}")])
    rows.append([btn("◀️ Другой день", f"tp:{eid}:back")])
    if hours:
        body = "Выбери час (формат 24 ч). Минуты выберешь следующим шагом."
        if task and day == datetime.now(zone(tz)).date():
            body += "\nДля сегодняшнего срока доступно время не раньше чем через 1 ч."
    else:
        body = "На этот день уже не успеть. Вернись и выбери другой день."
    return f"{e['title']}\n\n🗓 {fmt_day(day)}\n🕐 Во сколько? {body}", markup(rows)


def picker_minutes(e, tz, day, hour):
    eid = e["id"]
    mins = [x for x in range(0, 60, 5) if slot_ok(e, tz, day, hour, x)]
    rows = chunk([btn(f"{hour:02d}:{x:02d}", f"tp:{eid}:ok:{ymd(day)}:{hour:02d}{x:02d}") for x in mins], 4)
    rows.append([btn("◀️ Другой час", f"tp:{eid}:d:{ymd(day)}")])
    text = (f"{e['title']}\n\n🗓 {fmt_day(day)}, {hour:02d}:xx\n"
            "Выбери точное время. Нужно другое, например 18:07? Вернись назад и нажми «Написать дату и время».")
    return text, markup(rows)


# ---------- настройки напоминаний (для всех дел и для одного дела) ----------

def settings_kb():
    return markup([
        [btn("📅 Мероприятия", "cfg:0:e:main"), btn("📝 Задачи", "cfg:0:t:main")],
        [btn("👌 Закрыть", "cfg:0:e:close")],
    ])


def cfg_screen(uid, e, kind, scr):
    """Один экран настроек. e = дело или None (тогда настройки по умолчанию)."""
    s = get_set(uid)
    tz = get_tz(uid)
    eid = e["id"] if e else 0
    k = "e" if kind == "event" else "t"
    offs = e["offsets"] if e else s["ev_offsets" if kind == "event" else "tk_offsets"]
    rep = e["repeat"] if e else s["repeat"]
    head = (card(e, tz) + "\n\n") if e else ""
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

    if scr == "sh" and not e:  # время по умолчанию: все 24 часа
        rows = chunk([btn(("✅ " if h == s["tk_hour"] else "") + f"{h:02d}:00", f"cfg:0:t:h{h}")
                      for h in range(24)], 4)
        rows.append([back])
        return ("🕘 Время по умолчанию\n\nЕсли у задачи названа только дата (например «завтра»), "
                "я напомню в это время. Выбери час (формат 24 ч)."), markup(rows)

    if scr == "sr":
        row = [btn(("✅ " if rep == 0 else "") + "Выкл", f"cfg:{eid}:t:r0")]
        row += [btn(("✅ " if rep == n else "") + f"{n} ч", f"cfg:{eid}:t:r{n}") for n in REPEATS]
        custom = rep > 0 and rep not in REPEATS
        row.append(btn(f"✅ {rep} ч (изменить)" if custom else "✍️ Своё", f"cfg:{eid}:t:rx"))
        rows = chunk(row, 4)
        rows.append([back])
        return (head + "🔁 Повторять напоминание, пока не нажмёшь «Готово»?\n"
                f"Выбери, как часто. Минимум 1 ч, не больше {MAX_NAGS} раз."), markup(rows)

    summary = ", ".join(TK_LABELS[o] for o in sorted(offs, reverse=True)) or "не напоминать"
    if e:  # у конкретной задачи срок меняется тем же выбором даты, часа и минут
        time_row = [btn((f"🕘 Срок: {fmt_time(e['due'], tz)}" if e["due"] else "🕘 Срок: не задан")[:60], f"tp:{eid}:back")]
    else:
        time_row = [btn(f"🕘 Время по умолчанию: {s['tk_hour']:02d}:00", "cfg:0:t:sh")]
    rows = [
        [btn(f"🔔 Когда: {summary}", f"cfg:{eid}:t:so")],
        time_row,
        [btn("🔁 Повтор: " + (f"каждые {rep} ч" if rep else "выкл"), f"cfg:{eid}:t:sr")],
        [close],
    ]
    intro = ("👇 Выбери, что поменять" if e else
             "📝 Напоминания о задачах\n\nУ задачи есть срок: дата и время. Я напомню в момент срока и заранее. "
             "С повтором буду напоминать снова и снова, пока не нажмёшь «Готово».\n\nВыбери, что настроить:")
    return head + intro, markup(rows)


# ---------- чек-лист ----------

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


# ---------- календарь ----------

def month_view(uid, year, month, tz):
    counts = month_counts(uid, year, month, tz)
    today = datetime.now(zone(tz)).date()
    py, pm = (year, month - 1) if month > 1 else (year - 1, 12)
    ny, nm = (year, month + 1) if month < 12 else (year + 1, 1)
    rows = [[btn("◀️", f"cal:{py}{pm:02d}"), btn(f"{MONTHS[month - 1]} {year}", "noop"),
             btn("▶️", f"cal:{ny}{nm:02d}")],
            [btn(w, "noop") for w in WD]]
    for week in calendar.monthcalendar(year, month):
        row = []
        for d in week:
            if d == 0:
                row.append(btn(" ", "noop"))
                continue
            label = f"{d}•" if counts.get(d) else str(d)
            if date(year, month, d) == today:
                label = f"[{label}]"
            row.append(btn(label, f"cd:{ymd(date(year, month, d))}"))
        rows.append(row)
    rows.append([btn("🗓 Сегодня", f"cd:{ymd(today)}")])
    ev, tk = month_summary(uid, year, month, tz)
    text = (
        f"🗓 Календарь\n\n{MONTHS[month - 1]} {year}\n\n"
        "Точка рядом с числом значит, что в этот день есть дела. Число в скобках это сегодня.\n"
        "Нажми на число, чтобы открыть день: там все мероприятия и задачи со сроком, "
        "и можно сразу добавить новое.\n\n"
        f"В этом месяце: мероприятий {ev}, задач со сроком {tk}."
    )
    return text, markup(rows)


def day_view(uid, day, tz):
    z = zone(tz)
    today = datetime.now(z).date()
    items = items_on_day(uid, day, tz)
    events = [e for e in items if e["kind"] == "event"]
    tasks = [e for e in items if e["kind"] == "task"]
    title = f"🗓 {fmt_day(day)}" + (" (сегодня)" if day == today else "")
    lines = [title]
    clash = False
    if not items:
        lines += ["", "В этот день пока ничего нет."]
        if day >= today:
            lines.append("Нажми «➕ Мероприятие» или «➕ Задача», чтобы добавить дело на этот день.")
    if events:
        lines += ["", "📅 Мероприятия:"]
        for e in events:
            hhmm = datetime.fromtimestamp(e["due"], z).strftime("%H:%M")
            if e["done"]:
                mark = "✅"
            elif conflicts_for(e):
                mark, clash = "⚠️", True
            else:
                mark = "🕐"
            lines.append(f"{mark} {hhmm} · {e['title']}")
    if tasks:
        lines += ["", "📝 Задачи со сроком в этот день:"]
        for e in tasks:
            hhmm = datetime.fromtimestamp(e["due"], z).strftime("%H:%M")
            lines.append(("✅ " if e["done"] else "• ") + f"{e['title']} (срок в {hhmm})")
    if clash:
        lines += ["", "⚠️ Некоторые мероприятия пересекаются по времени. Открой их и поменяй время, если нужно."]
    rows = []
    for e in items[:10]:
        hhmm = datetime.fromtimestamp(e["due"], z).strftime("%H:%M")
        rows.append([btn(f"{'📅' if e['kind'] == 'event' else '📝'} {hhmm} {e['title']}"[:60], f"open:{e['id']}")])
    if day >= today:
        rows.append([btn("➕ Мероприятие", f"ca:{ymd(day)}:event"), btn("➕ Задача", f"ca:{ymd(day)}:task")])
    rows.append([btn("◀️ День", f"cd:{ymd(day - timedelta(days=1))}"),
                 btn("День ▶️", f"cd:{ymd(day + timedelta(days=1))}")])
    rows.append([btn("🗓 К календарю", f"cal:{day.year}{day.month:02d}")])
    return "\n".join(lines), markup(rows)


# ---------- экраны «Мои дела» ----------

def hub_view(uid):
    t, ev, dn = len(items_of(uid, "task")), len(items_of(uid, "event")), len(items_of(uid, done=True))
    text = (
        "📋 Мои дела\n\n"
        "📝 Задачи: дела со сроком (дата и время). Напоминаю в срок и заранее, можно вести чек-лист, "
        "просроченные подсвечиваются 🔴.\n"
        "📅 Мероприятия: события в точное время и с местом. Предупреждаю заранее "
        "и могу добавить в календарь телефона.\n\n"
        "В каждой группе можно создавать свои разделы: «Работа», «Дом», «Учёба».\n"
        "А все дела по дням смотри в «🗓 Календарь» в меню внизу.\n\n"
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


def section_view(uid, kind, sid, tz):
    s = get_sec(sid, uid) if sid else None
    items = items_of(uid, kind, sid)
    items.sort(key=list_sort_key)
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


def draft_view(did, tz):
    d = DRAFTS[did]
    lines = ["🤔 Что это?", "", f"«{d['title']}»"]
    if d["due"]:
        lines.append("🕐 Нашёл время: " + fmt_time(d["due"], tz))
    if d["place"]:
        lines.append("📍 " + str(d["place"]))
    lines += [
        "",
        "📝 Задача: дело со сроком (дата и время). Напомню в срок и заранее, можно вести чек-лист и повторять напоминания.",
        "📅 Мероприятие: событие в точное время и с местом. Предупрежу заранее и добавлю в календарь телефона.",
        "",
        "Выбери, как сохранить 👇",
    ]
    return "\n".join(lines), markup([
        [btn("📝 Задача", f"dk:{did}:task"), btn("📅 Мероприятие", f"dk:{did}:event")],
        [btn("✖️ Не сохранять", f"dx:{did}")],
    ])


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
    touch_user(m.from_user.id, getattr(m.from_user, "first_name", None))
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


# ---------- команды и кнопки меню ----------

@dp.message(CommandStart())
async def start(m: Message):
    touch_user(m.from_user.id, getattr(m.from_user, "first_name", None))
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
        "📝 Задача: дело со сроком.\n"
        "• срок выбираешь кнопками: дата, ч, м (формат 24 ч)\n"
        "• напомню в срок и заранее (за 1 д, за 3 д)\n"
        "• чек-лист с галочками\n"
        "• повтор каждые N ч, пока не нажмёшь «Готово»\n"
        "• просроченные подсвечиваются 🔴\n\n"
        "📅 Мероприятие: событие в точное время.\n"
        "• время выбираешь кнопками: день, ч, м\n"
        "• место\n"
        "• предупреждение заранее: за 10 м, 1 ч, 1 д\n"
        "• если в это время уже есть другое мероприятие, предупрежу (добавить всё равно можно)\n"
        "• кнопка «📆 В календарь» добавит его в календарь телефона\n\n"
        "🗓 Календарь: все дела по дням. Нажми на число, чтобы увидеть день и добавить в него дело.\n\n"
        "👤 Профиль: твоя статистика, настройки и кнопка, чтобы стереть свои данные.\n\n"
        "Разделы («Работа», «Дом» и так далее) создаются отдельно для задач и мероприятий в «📋 Мои дела».\n\n"
        "Команды: /add /list /calendar /profile /settings /tz /menu /help"
    )


@dp.message(Command("add"))
@dp.message(F.text == B_ADD)
async def add_menu(m: Message):
    if await guard(m):
        return
    await m.answer(
        "➕ Что добавляем?\n\n"
        "📝 Задача: дело со сроком, датой и временем (есть чек-лист).\n"
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


@dp.message(Command("calendar"))
@dp.message(F.text == B_CAL)
async def calendar_cmd(m: Message):
    if await guard(m):
        return
    uid = m.from_user.id
    today = datetime.now(zone(get_tz(uid))).date()
    text, mk = month_view(uid, today.year, today.month, get_tz(uid))
    await m.answer(text, reply_markup=mk)


@dp.message(Command("profile"))
@dp.message(F.text == B_PROFILE)
async def profile_cmd(m: Message):
    if await guard(m):
        return
    await m.answer(profile_text(m.from_user.id), reply_markup=profile_kb())


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
    touch_user(uid, getattr(m.from_user, "first_name", None))
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

        if t == "add":  # «Добавить» или «Календарь» -> тип уже выбран
            if st.get("day"):
                await add_on_day(m, text, st, tz)
                return
            title, due = text, None
            loc = safe_local(text, tz) if len(text) < 200 else None
            if loc:
                title, due = loc
            e = add_item(uid, st["kind"], title[:200], due, sec=st["sec"])
            resp, mk = item_response(e, tz, saved_title(e))
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
            resp, mk = time_response(e, tz, "Срок поставлен" if e["kind"] == "task" else "Время поставлено")
            await m.answer(resp, reply_markup=mk)
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
                await m.answer(f"✅ Повтор для задач по умолчанию: каждые {n} ч, пока не нажмёшь «Готово». "
                               "Он будет у новых задач. У старых его можно поменять на их карточках.")
            else:
                e = find(st["eid"], uid)
                if e:
                    e["repeat"] = n
                    e["nags"] = 0
                    e["nag_at"] = None
                    resp, mk = time_response(e, tz, "Повтор настроен")
                    await m.answer(resp, reply_markup=mk)
            return

        if t == "place":
            e = find(st["eid"], uid)
            if e:
                e["place"] = None if text in ("-", "—") else text[:100]
                resp, mk = time_response(e, tz, "Место сохранено" if e["place"] else "Место убрано")
                await m.answer(resp, reply_markup=mk)
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


async def add_on_day(m, text, st, tz):
    """Дело, добавленное из календаря на выбранный день."""
    uid = m.from_user.id
    z = zone(tz)
    day = parse_day(st["day"])
    ex = extract_time(text)
    title, due, note = text, None, ""
    if ex:
        title, hh, mm = ex
        ts = int(datetime.combine(day, dtime(hh, mm), tzinfo=z).timestamp())
        if ts > time.time():
            due = ts
        else:
            note = "⚠️ Указанное время уже прошло, поэтому выбери его заново.\n\n"
    e = add_item(uid, st["kind"], title[:200], due, sec=st["sec"])
    if due:
        resp, mk = item_response(e, tz, saved_title(e) + f" на {fmt_day(day)}")
    else:
        resp, mk = picker_hours(e, tz, day)
        resp = note + resp
    await m.answer(resp, reply_markup=mk)


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
        resp, mk = item_response(e, tz, saved_title(e))
        await m.answer(resp, reply_markup=mk)
        await retire(m.bot, st.get("msg"), "✅ Сохранено")
    elif then == "move":
        e = find(st["ref"], uid)
        if e:
            e["sec"] = s["id"]
            await m.answer(f"✅ Раздел «{s['name']}» создан, дело перенесено в него.\n\n" + card(e, tz),
                           reply_markup=kb(e))
    elif then == "add":
        STATE[uid] = {"type": "add", "kind": st["kind"], "sec": s["id"]}
        await m.answer(f"✅ Раздел «{s['name']}» создан.\n\n" + add_prompt(st["kind"], s["id"], uid))
    else:
        await m.answer(
            f"✅ Раздел «{s['name']}» создан.\n\nТеперь можно добавлять в него дела: нажми «➕ Добавить сюда» "
            "или открой раздел.",
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
                   "со временем это мероприятие, без времени задача. Любое можно поменять кнопкой «🔄 Сменить тип» на карточке.")
    for title, due, place, note in drafts:
        e = add_item(uid, "event" if due else "task", title, due, place, note)
        await m.answer(saved_title(e) + "\n\n" + card(e, tz), reply_markup=kb(e))


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
    touch_user(uid, getattr(c.from_user, "first_name", None))
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

    # --- профиль ---
    if cmd == "pf":
        act = p[1]
        if act == "tz":
            STATE[uid] = {"type": "tz"}
            now = datetime.now(zone(tz)).strftime("%H:%M")
            await msg.answer(f"Сейчас: {tz} (у тебя {now}).\n\n{TZ_PROMPT}", reply_markup=tz_kb())
        elif act == "rem":
            await msg.answer(SETTINGS_TEXT, reply_markup=settings_kb())
        elif act == "cal":
            today = datetime.now(zone(tz)).date()
            text, mk = month_view(uid, today.year, today.month, tz)
            await msg.answer(text, reply_markup=mk)
        elif act == "list":
            text, mk = hub_view(uid)
            await msg.answer(text, reply_markup=mk)
        elif act == "wipe":
            await safe_edit(
                msg, "🗑 Стереть все данные?\n\nУдалю все твои задачи, мероприятия, разделы и настройки "
                     "напоминаний. Часовой пояс останется. Это нельзя отменить.",
                markup([[btn("Да, стереть", "pf:wipey"), btn("Отмена", "pf:back")]]))
        elif act == "wipey":
            wipe_user(uid)
            await safe_edit(msg, "✅ Данные стёрты\n\n" + profile_text(uid), profile_kb())
        else:  # back
            await safe_edit(msg, profile_text(uid), profile_kb())
        await c.answer()
        return

    # --- календарь ---
    if cmd == "cal":
        y, mo = int(p[1][:4]), min(max(int(p[1][4:]), 1), 12)
        text, mk = month_view(uid, y, mo, tz)
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "cd":
        text, mk = day_view(uid, parse_day(p[1]), tz)
        await safe_edit(msg, text, mk)
        await c.answer()
        return
    if cmd == "ca":
        day, kind = parse_day(p[1]), p[2]
        STATE[uid] = {"type": "add", "kind": kind, "sec": 0, "day": p[1]}
        await msg.answer(day_add_prompt(kind, day))
        await c.answer()
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
            await safe_edit(msg, "Это уже сохранено или отменено.\n\nОткрой «📋 Мои дела», чтобы посмотреть список.", None)
            await c.answer()
            return
        if cmd == "dx":
            DRAFTS.pop(did, None)
            await safe_edit(msg, "❌ Не сохранил\n\nЭтот текст я не сохранил. Если передумаешь, отправь его мне ещё раз.", None)
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
        resp, mk = item_response(e, tz, saved_title(e))
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
        await safe_edit(msg, f"✅ Выполнено\n\n«{e['title']}» отмечено как сделанное. Найти его можно в "
                             "«📋 Мои дела» → «✅ Выполненные», оттуда его можно вернуть в список.", None)
        await c.answer("Готово!")
        return
    elif cmd == "undo":
        e["done"] = False
        reset_fired(e)
        text, mk = item_response(e, tz, "↩️ Вернул в список")
        await safe_edit(msg, text, mk)
    elif cmd == "del":
        ITEMS.remove(e)
        await safe_edit(msg, f"🗑 Удалено\n\n«{e['title']}» удалено насовсем. Вернуть его нельзя, "
                             "но можно создать заново.", None)
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
        sid = int(p[2])
        sec = get_sec(sid, uid) if sid else None
        if sid and (not sec or sec["kind"] != e["kind"]):
            await c.answer("Этот раздел из другой группы. Открой «📂 Раздел» заново.", show_alert=True)
            return
        e["sec"] = sid
        await safe_edit(msg, "✅ Перенёс\n\n" + card(e, tz), kb(e))
    elif cmd == "mvn":
        STATE[uid] = {"type": "sec_new", "kind": e["kind"], "then": "move", "ref": e["id"]}
        await msg.answer("✏️ Напиши название нового раздела (до 30 символов).")
    elif cmd == "snz":
        mins = int(p[2])
        e["snooze"] = int(time.time()) + mins * 60
        label = f"{mins // 60} ч" if mins >= 60 else f"{mins} м"
        await safe_edit(msg, f"⏰ Отложено\n\nНапомню про «{e['title']}» через {label}. "
                             "Время самого дела при этом не меняется.", None)
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
        text, mk = time_response(e, tz, "Срок поставлен")
    elif act in ("ok", "in"):
        if act == "in":
            ts = int((datetime.now(z) + timedelta(minutes=int(args[1]))).timestamp())
        else:
            d = parse_day(args[1])
            hh, mm = int(args[2][:2]), int(args[2][2:])
            if not slot_ok(e, tz, d, hh, mm):
                await c.answer("Для сегодняшнего срока выбери время не раньше чем через 1 ч"
                               if e["kind"] == "task" else "Это время уже прошло, выбери другое",
                               show_alert=True)
                return
            ts = int(datetime.combine(d, dtime(hh, mm), tzinfo=z).timestamp())
        e["due"] = ts
        reset_fired(e)
        text, mk = time_response(e, tz, "Срок поставлен" if e["kind"] == "task" else "Время поставлено")
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
        if (letter == "o" and n not in (EV_LABELS if kind == "event" else TK_LABELS)) or \
                (letter == "r" and kind == "event"):
            await c.answer("Эта кнопка устарела. Открой «🔔 Напоминания» заново.", show_alert=True)
            return
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
        return "⏰ Пора! Начинается" if o == 0 else f"🔔 Через {EV_LEFT.get(o, '')}".strip()
    return {0: "⏰ Срок наступил", 1440: "📅 Срок завтра", 4320: "📅 Срок через 3 д"}.get(o, "📅 Срок скоро")


FOOTER = "\n\nСделал? Нажми «✅ Готово». Нужно позже? Отложи кнопкой."


async def tick(bot):
    """Один проход: кому пора напомнить."""
    now = int(time.time())
    for e in list(ITEMS):
        if e["done"]:
            continue
        tzname = get_tz(e["user_id"])
        if e.get("snooze") and now >= e["snooze"]:
            e["snooze"] = None
            await send_safe(bot, e["user_id"], "⏰ Напоминаю ещё раз\n\n" + card(e, tzname) + FOOTER, kb_remind(e))
        if not e["due"]:
            continue
        valid = EV_LABELS if e["kind"] == "event" else TK_LABELS
        for o in sorted(e["offsets"], reverse=True):
            if o in valid and o not in e["fired"] and now >= e["due"] - o * 60:
                e["fired"].add(o)
                await send_safe(bot, e["user_id"], remind_head(e, o) + "\n\n" + card(e, tzname) + FOOTER, kb_remind(e))
        # повтор только для задач: каждые N часов, пока не нажато «Готово»
        if e["kind"] == "task" and e["repeat"] and e["nags"] < MAX_NAGS and now >= e["due"]:
            step = e["repeat"] * 3600
            if e["nag_at"] is None:
                e["nag_at"] = (e["due"] if now - e["due"] < 120 else now) + step
            if now >= e["nag_at"]:
                e["nags"] += 1
                e["nag_at"] = now + step
                await send_safe(bot, e["user_id"],
                                "🔁 Ещё не отмечено «Готово»\n\n" + card(e, tzname) + FOOTER, kb_remind(e))


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
            BotCommand(command="calendar", description="Календарь по дням"),
            BotCommand(command="profile", description="Профиль и статистика"),
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
