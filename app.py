import os
import uuid
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from fastapi import FastAPI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("habit-bot")

BOT_TOKEN = os.environ.get("BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
TZ_NAME = os.environ.get("BOT_TZ", "Europe/Berlin")
TZ = ZoneInfo(TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")

NUDGE_TEMPLATES = [
    "{name} уже отметил(а) выполнение 💪 Ты сегодня в игре?",
    "Сегодня счёт {score}. Нужно поднажать?",
    "{name} держит темп. Не отставай.",
]

def now_tz() -> datetime:
    return datetime.now(TZ)

def today_date() -> date:
    return now_tz().date()

def parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))

def start_of_period(d: date, unit: str) -> date:
    if unit == "month":
        return d.replace(day=1)
    if unit == "week":
        return d - timedelta(days=(d.isoweekday() - 1))
    raise ValueError("Unknown period_unit")

def end_of_period(d: date, unit: str) -> date:
    sp = start_of_period(d, unit)
    if unit == "month":
        if sp.month == 12:
            nm = sp.replace(year=sp.year + 1, month=1, day=1)
        else:
            nm = sp.replace(month=sp.month + 1, day=1)
        return nm - timedelta(days=1)
    if unit == "week":
        return sp + timedelta(days=6)
    raise ValueError("Unknown period_unit")

POOL: asyncpg.Pool | None = None
scheduler: AsyncIOScheduler | None = None
BOT_APP: Application | None = None

async def db_init():
    global POOL
    POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with POOL.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id BIGSERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                goal_type TEXT NOT NULL, -- daily | period
                period_unit TEXT,        -- week | month
                period_target INTEGER,
                reminder_hhmm TEXT,
                created_by BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS goal_members (
                goal_id BIGINT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                first_name TEXT,
                joined_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(goal_id, user_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id BIGSERIAL PRIMARY KEY,
                goal_id BIGINT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                ts TIMESTAMPTZ NOT NULL,
                check_date DATE NOT NULL
            );
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_checkins_goal_date ON checkins(goal_id, check_date);")

async def fetchrow(q: str, *args):
    assert POOL is not None
    async with POOL.acquire() as conn:
        return await conn.fetchrow(q, *args)

async def fetch(q: str, *args):
    assert POOL is not None
    async with POOL.acquire() as conn:
        return await conn.fetch(q, *args)

async def execute(q: str, *args):
    assert POOL is not None
    async with POOL.acquire() as conn:
        return await conn.execute(q, *args)

@dataclass
class CreateGoalDraft:
    step: str
    title: str | None = None
    goal_type: str | None = None
    period_unit: str | None = None
    period_target: int | None = None
    reminder_hhmm: str | None = None

DRAFTS: dict[int, CreateGoalDraft] = {}

def kb_main():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Создать цель", callback_data="create_goal")],
            [InlineKeyboardButton("📌 Мои цели", callback_data="my_goals")],
        ]
    )

def kb_goal_type():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Ежедневная", callback_data="gt_daily")],
            [InlineKeyboardButton("📈 Количественная в периоде", callback_data="gt_period")],
        ]
    )

def kb_period_unit():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📆 На неделю", callback_data="pu_week")],
            [InlineKeyboardButton("🗓 На месяц", callback_data="pu_month")],
        ]
    )

def kb_goal_actions(goal_id: int, goal_type: str):
    rows = [
        [InlineKeyboardButton("✅ Отметить выполнение", callback_data=f"done:{goal_id}")],
        [InlineKeyboardButton("🏁 Прогресс/Таблица", callback_data=f"progress:{goal_id}")],
        [InlineKeyboardButton("👥 Участники", callback_data=f"members:{goal_id}")],
    ]
    if goal_type == "period":
        rows.append([InlineKeyboardButton("🚗 Визуализация гонки", callback_data=f"race:{goal_id}")])
    return InlineKeyboardMarkup(rows)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args or []

    # deep-link join
    if args:
        code = args[0].strip()
        goal = await fetchrow("SELECT * FROM goals WHERE code=$1", code)
        if not goal:
            await update.message.reply_text("Не нашёл цель по этой ссылке.")
            await update.message.reply_text("Меню:", reply_markup=kb_main())
            return

        await execute(
            """
            INSERT INTO goal_members(goal_id, user_id, first_name, joined_at)
            VALUES($1,$2,$3,$4)
            ON CONFLICT(goal_id, user_id) DO NOTHING
            """,
            int(goal["id"]), user.id, user.first_name or "", now_tz()
        )
        await update.message.reply_text(
            f"Ты присоединился(лась) к цели: <b>{goal['title']}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_goal_actions(int(goal["id"]), goal["goal_type"]),
        )
        return

    await update.message.reply_text(
        "Привет! Я бот для парных/групповых привычек.\n"
        "Создайте цель, добавьте друзей и отмечайте выполнение 🙂"
    )
    await update.message.reply_text("Меню:", reply_markup=kb_main())

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_my_goals(update, context, via_callback=False)

async def show_my_goals(update: Update, context: ContextTypes.DEFAULT_TYPE, via_callback: bool):
    user = update.effective_user
    rows = await fetch(
        """
        SELECT g.*
        FROM goals g
        JOIN goal_members gm ON gm.goal_id = g.id
        WHERE gm.user_id=$1
        ORDER BY g.created_at DESC
        """,
        user.id
    )

    if not rows:
        msg = "У тебя пока нет целей. Нажми «Создать цель»."
        if via_callback and update.callback_query:
            await update.callback_query.message.reply_text(msg, reply_markup=kb_main())
        else:
            await update.message.reply_text(msg, reply_markup=kb_main())
        return

    lines = ["<b>Твои цели:</b>"]
    for g in rows:
        if g["goal_type"] == "daily":
            t = "Ежедневная"
        else:
            t = f"Период: {g['period_unit']}, цель: {g['period_target']}"
        lines.append(f"• <b>{g['title']}</b> — {t} (ID: <code>{int(g['id'])}</code>)")
    text = "\n".join(lines)

    if via_callback and update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not update.message or not update.message.text:
        return
    if user.id not in DRAFTS:
        return

    draft = DRAFTS[user.id]
    txt = update.message.text.strip()

    if draft.step == "title":
        draft.title = txt
        draft.step = "type"
        await update.message.reply_text(
            f"Название: <b>{draft.title}</b>\nВыбери тип цели:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_goal_type(),
        )
        return

    if draft.step == "period_target":
        try:
            n = int(txt)
            if n <= 0 or n > 9999:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Нужно число > 0. Например 12.")
            return
        draft.period_target = n
        draft.step = "reminder_time"
        await update.message.reply_text("Во сколько слать напоминание? (например 20:30)")
        return

    if draft.step == "reminder_time":
        try:
            _ = parse_hhmm(txt)
        except Exception:
            await update.message.reply_text("Формат времени: HH:MM (например 20:30).")
            return
        draft.reminder_hhmm = txt

        goal_id, code = await create_goal_in_db(user.id, user.first_name or "", draft)
        del DRAFTS[user.id]

        deep_link = f"https://t.me/{context.bot.username}?start={code}"
        goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)

        await update.message.reply_text(
            "Готово ✅\n\n"
            f"Цель: <b>{goal['title']}</b>\n"
            f"Ссылка для друзей: {deep_link}",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_goal_actions(int(goal_id), goal["goal_type"]),
        )

        await schedule_goal_reminder(context.application, int(goal_id))
        return

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    # КРИТИЧНО: отвечаем на callback, иначе кнопка "крутится"
    await query.answer()

    user = update.effective_user
    data = query.data or ""

    if data == "create_goal":
        DRAFTS[user.id] = CreateGoalDraft(step="title")
        await query.message.reply_text("Напиши название цели (например: Спорт).")
        return

    if data == "my_goals":
        await show_my_goals(update, context, via_callback=True)
        return

    if data.startswith("gt_"):
        if user.id not in DRAFTS:
            await query.message.reply_text("Нажми «Создать цель» и начни заново.")
            return
        draft = DRAFTS[user.id]
        if data == "gt_daily":
            draft.goal_type = "daily"
            draft.step = "reminder_time"
            await query.message.reply_text("Во сколько слать напоминание? (например 20:30)")
            return
        if data == "gt_period":
            draft.goal_type = "period"
            draft.step = "period_unit"
            await query.message.reply_text("Выбери период:", reply_markup=kb_period_unit())
            return

    if data.startswith("pu_"):
        if user.id not in DRAFTS:
            await query.message.reply_text("Нажми «Создать цель» и начни заново.")
            return
        draft = DRAFTS[user.id]
        draft.period_unit = "week" if data == "pu_week" else "month"
        draft.step = "period_target"
        await query.message.reply_text("Сколько нужно сделать за период? (например 12)")
        return

    if data.startswith("done:"):
        goal_id = int(data.split(":")[1])
        await do_checkin(update, context, goal_id, via_callback=True)
        return

    if data.startswith("progress:"):
        goal_id = int(data.split(":")[1])
        text, markup = await build_progress(goal_id)
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return

    if data.startswith("members:"):
        goal_id = int(data.split(":")[1])
        rows = await fetch(
            "SELECT first_name FROM goal_members WHERE goal_id=$1 ORDER BY joined_at ASC",
            goal_id
        )
        if not rows:
            await query.message.reply_text("Участников пока нет.")
            return
        lines = ["Участники:"]
        for r in rows:
            lines.append(f"• {(r['first_name'] or 'Без имени').strip()}")
        await query.message.reply_text("\n".join(lines))
        return

    if data.startswith("race:"):
        goal_id = int(data.split(":")[1])
        await query.message.reply_text(await build_race_ascii(goal_id), parse_mode=ParseMode.HTML)
        return

async def create_goal_in_db(created_by: int, first_name: str, draft: CreateGoalDraft) -> tuple[int, str]:
    code = uuid.uuid4().hex[:10]
    created_at = now_tz()

    row = await fetchrow(
        """
        INSERT INTO goals(code, title, goal_type, period_unit, period_target, reminder_hhmm, created_by, created_at)
        VALUES($1,$2,$3,$4,$5,$6,$7,$8)
        RETURNING id
        """,
        code, draft.title, draft.goal_type, draft.period_unit, draft.period_target, draft.reminder_hhmm, created_by, created_at
    )
    goal_id = int(row["id"])

    await execute(
        """
        INSERT INTO goal_members(goal_id, user_id, first_name, joined_at)
        VALUES($1,$2,$3,$4)
        ON CONFLICT(goal_id, user_id) DO NOTHING
        """,
        goal_id, created_by, first_name, created_at
    )
    return goal_id, code

async def do_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE, goal_id: int, via_callback: bool):
    user = update.effective_user
    goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not goal:
        await (update.callback_query.message if via_callback and update.callback_query else update.message).reply_text("Цель не найдена.")
        return

    member = await fetchrow("SELECT 1 FROM goal_members WHERE goal_id=$1 AND user_id=$2", goal_id, user.id)
    if not member:
        await (update.callback_query.message if via_callback and update.callback_query else update.message).reply_text("Ты не участник этой цели.")
        return

    d = today_date()
    if goal["goal_type"] == "daily":
        already = await fetchrow(
            "SELECT 1 FROM checkins WHERE goal_id=$1 AND user_id=$2 AND check_date=$3",
            goal_id, user.id, d
        )
        if already:
            await (update.callback_query.message if via_callback and update.callback_query else update.message).reply_text("Сегодня уже отмечено ✅")
            return

    await execute(
        "INSERT INTO checkins(goal_id, user_id, ts, check_date) VALUES($1,$2,$3,$4)",
        goal_id, user.id, now_tz(), d
    )

    msg = "Отмечено ✅" if goal["goal_type"] == "daily" else "Засчитано ✅"
    await (update.callback_query.message if via_callback and update.callback_query else update.message).reply_text(
        msg, reply_markup=kb_goal_actions(goal_id, goal["goal_type"])
    )

async def build_progress(goal_id: int) -> tuple[str, InlineKeyboardMarkup]:
    goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not goal:
        return ("Цель не найдена.", kb_main())

    members = await fetch("SELECT user_id, first_name FROM goal_members WHERE goal_id=$1", goal_id)
    if not members:
        return ("Участников нет.", kb_main())

    if goal["goal_type"] == "daily":
        d = today_date()
        done_rows = await fetch("SELECT user_id FROM checkins WHERE goal_id=$1 AND check_date=$2", goal_id, d)
        done = {r["user_id"] for r in done_rows}
        lines = [f"<b>{goal['title']}</b>", f"Сегодня: <code>{d.isoformat()}</code>", ""]
        for m in members:
            nm = (m["first_name"] or "Без имени").strip()
            lines.append(("✅ " if m["user_id"] in done else "— ") + nm)
        return ("\n".join(lines), kb_goal_actions(goal_id, goal["goal_type"]))

    unit = goal["period_unit"]
    target = int(goal["period_target"] or 0)
    td = today_date()
    sp = start_of_period(td, unit)
    ep = end_of_period(td, unit)

    lines = [
        f"<b>{goal['title']}</b>",
        f"Период: <code>{sp.isoformat()}</code> — <code>{ep.isoformat()}</code>",
        f"Цель: <b>{target}</b>",
        "",
        "<b>Прогресс:</b>",
    ]

    stats = []
    for m in members:
        c = await fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM checkins
            WHERE goal_id=$1 AND user_id=$2 AND check_date BETWEEN $3 AND $4
            """,
            goal_id, m["user_id"], sp, ep
        )
        stats.append((int(c["cnt"]), (m["first_name"] or "Без имени").strip()))

    stats.sort(reverse=True, key=lambda x: x[0])
    for i, (cnt, nm) in enumerate(stats, start=1):
        lines.append(f"{i}. {nm}: <b>{cnt}</b> / {target}")

    return ("\n".join(lines), kb_goal_actions(goal_id, goal["goal_type"]))

async def build_race_ascii(goal_id: int) -> str:
    goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not goal or goal["goal_type"] != "period":
        return "Гонка доступна только для количественных целей."

    members = await fetch("SELECT user_id, first_name FROM goal_members WHERE goal_id=$1", goal_id)
    unit = goal["period_unit"]
    target = int(goal["period_target"] or 0)

    td = today_date()
    sp = start_of_period(td, unit)
    ep = end_of_period(td, unit)

    stats = []
    for m in members:
        c = await fetchrow(
            """
            SELECT COUNT(*) AS cnt
            FROM checkins
            WHERE goal_id=$1 AND user_id=$2 AND check_date BETWEEN $3 AND $4
            """,
            goal_id, m["user_id"], sp, ep
        )
        stats.append((int(c["cnt"]), (m["first_name"] or "Без имени").strip()))

    stats.sort(reverse=True, key=lambda x: x[0])

    track_len = 20
    lines = [f"<b>🏃 Гонка: {goal['title']}</b>", f"Цель: <b>{target}</b>", ""]
    for cnt, name in stats:
        pos = 0 if target <= 0 else min(track_len, int(round((cnt / target) * track_len)))
        track = "—" * pos + "🏃" + "—" * (track_len - pos)
        lines.append(f"{name}: <b>{cnt}</b>/{target} |{track}|")
    return "\n".join(lines)

async def schedule_goal_reminder(app_: Application, goal_id: int):
    global scheduler
    if scheduler is None:
        return
    goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not goal or not goal["reminder_hhmm"]:
        return
    t = parse_hhmm(goal["reminder_hhmm"])
    job_id = f"reminder_goal_{goal_id}"
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    scheduler.add_job(
        func=lambda: asyncio.create_task(send_goal_reminder(app_, goal_id)),
        trigger=CronTrigger(hour=t.hour, minute=t.minute, timezone=TZ),
        id=job_id,
        replace_existing=True,
        misfire_grace_time=600,
    )

async def send_goal_reminder(app_: Application, goal_id: int):
    goal = await fetchrow("SELECT * FROM goals WHERE id=$1", goal_id)
    if not goal:
        return
    members = await fetch("SELECT user_id FROM goal_members WHERE goal_id=$1", goal_id)
    if not members:
        return

    if goal["goal_type"] == "daily":
        d = today_date()
        done_rows = await fetch("SELECT user_id FROM checkins WHERE goal_id=$1 AND check_date=$2", goal_id, d)
        done = {r["user_id"] for r in done_rows}
        for m in members:
            uid = m["user_id"]
            if uid in done:
                continue
            try:
                await app_.bot.send_message(
                    chat_id=uid,
                    text=f"Напоминание: <b>{goal['title']}</b>\nОтметь выполнение ✅",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_goal_actions(goal_id, goal["goal_type"]),
                )
            except Exception as e:
                logger.warning("Reminder send failed: %s", e)

# ---------------- FastAPI wrapper for Render
app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

async def start_bot_forever():
    global BOT_APP, scheduler
    await db_init()

    BOT_APP = Application.builder().token(BOT_TOKEN).build()

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.start()

    BOT_APP.add_handler(CommandHandler("start", cmd_start))
    BOT_APP.add_handler(CommandHandler("goals", cmd_goals))
    BOT_APP.add_handler(CallbackQueryHandler(on_callback))
    BOT_APP.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await BOT_APP.initialize()
    await BOT_APP.start()
    await BOT_APP.updater.start_polling()

    logger.info("Bot polling started")
    await asyncio.Event().wait()

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_bot_forever())
    logger.info("Startup complete")

@app.on_event("shutdown")
async def on_shutdown():
    global BOT_APP, scheduler, POOL

    try:
        if scheduler:
            scheduler.shutdown(wait=False)
    except Exception:
        pass

    if BOT_APP:
        try:
            await BOT_APP.updater.stop()
        except Exception:
            pass
        try:
            await BOT_APP.stop()
        except Exception:
            pass
        try:
            await BOT_APP.shutdown()
        except Exception:
            pass

    if POOL:
        await POOL.close()
