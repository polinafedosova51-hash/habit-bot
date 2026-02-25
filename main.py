import os
import uuid
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, time, date
from zoneinfo import ZoneInfo

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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
DB_PATH = os.environ.get("BOT_DB_PATH", "bot.db")
TZ_NAME = os.environ.get("BOT_TZ", "Europe/Berlin")
TZ = ZoneInfo(TZ_NAME)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан.")

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                goal_type TEXT NOT NULL,
                period_unit TEXT,
                period_target INTEGER,
                reminder_hhmm TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS goal_members (
                goal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                first_name TEXT,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (goal_id, user_id)
            );
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                goal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ts TEXT NOT NULL,
                check_date TEXT NOT NULL
            );
        """)
        await db.commit()

def now_tz():
    return datetime.now(TZ)

def today_str():
    return now_tz().date().isoformat()

async def db_fetchone(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, p) as cur:
            return await cur.fetchone()

async def db_fetchall(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(q, p) as cur:
            return await cur.fetchall()

async def db_execute(q, p=()):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(q, p)
        await db.commit()

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать цель", callback_data="create_goal")],
        [InlineKeyboardButton("📌 Мои цели", callback_data="my_goals")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Это бот привычек.", reply_markup=kb_main())

async def cmd_goals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db_fetchall("SELECT * FROM goals")
    if not rows:
        await update.message.reply_text("Целей пока нет.", reply_markup=kb_main())
        return
    text = "Твои цели:\n"
    for r in rows:
        text += f"- {r['title']} (ID {r['id']})\n"
    await update.message.reply_text(text)

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Напиши название цели.")

async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажи ID цели.")
        return
    goal_id = int(context.args[0])
    await db_execute(
        "INSERT INTO checkins(goal_id,user_id,ts,check_date) VALUES(?,?,?,?)",
        (goal_id, update.effective_user.id, now_tz().isoformat(), today_str())
    )
    await update.message.reply_text("Отмечено ✅")

async def main():
    await db_init()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("done", cmd_done))
    logger.info("Bot started")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
