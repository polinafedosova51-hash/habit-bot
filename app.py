# Файл app.py
# Полная версия бота для Render Web Service + Neon PostgreSQL
# ВАЖНО: перед запуском установите переменные окружения:
# BOT_TOKEN
# DATABASE_URL
# BOT_TZ=Europe/Berlin

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

def now_tz():
    return datetime.now(TZ)

def parse_hhmm(s: str):
    hh, mm = s.split(":")
    return time(hour=int(hh), minute=int(mm))

POOL = None
scheduler = None
BOT_APP = None

async def db_init():
    global POOL
    POOL = await asyncpg.create_pool(DATABASE_URL)
    async with POOL.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id BIGSERIAL PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            goal_type TEXT NOT NULL,
            period_unit TEXT,
            period_target INTEGER,
            reminder_hhmm TEXT,
            created_by BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS goal_members (
            goal_id BIGINT REFERENCES goals(id) ON DELETE CASCADE,
            user_id BIGINT,
            first_name TEXT,
            joined_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY(goal_id, user_id)
        );
        """)

        await conn.execute("""
        CREATE TABLE IF NOT EXISTS checkins (
            id BIGSERIAL PRIMARY KEY,
            goal_id BIGINT REFERENCES goals(id) ON DELETE CASCADE,
            user_id BIGINT,
            ts TIMESTAMPTZ NOT NULL,
            check_date DATE NOT NULL
        );
        """)

async def fetchrow(q, *args):
    async with POOL.acquire() as conn:
        return await conn.fetchrow(q, *args)

async def fetch(q, *args):
    async with POOL.acquire() as conn:
        return await conn.fetch(q, *args)

async def execute(q, *args):
    async with POOL.acquire() as conn:
        return await conn.execute(q, *args)

def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Создать цель", callback_data="create_goal")],
        [InlineKeyboardButton("📌 Мои цели", callback_data="my_goals")]
    ])

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Создай цель и добавь друзей 🙂",
        reply_markup=kb_main()
    )

app = FastAPI()

@app.get("/health")
async def health():
    return {"ok": True}

async def start_bot():
    global BOT_APP, scheduler

    await db_init()

    BOT_APP = Application.builder().token(BOT_TOKEN).build()

    BOT_APP.add_handler(CommandHandler("start", cmd_start))

    scheduler = AsyncIOScheduler(timezone=TZ)
    scheduler.start()

    await BOT_APP.initialize()
    await BOT_APP.start()
    await BOT_APP.updater.start_polling()

    await asyncio.Event().wait()

@app.on_event("startup")
async def on_startup():
    asyncio.create_task(start_bot())

@app.on_event("shutdown")
async def on_shutdown():
    global BOT_APP, scheduler, POOL

    if scheduler:
        scheduler.shutdown(wait=False)

    if BOT_APP:
        await BOT_APP.updater.stop()
        await BOT_APP.stop()
        await BOT_APP.shutdown()

    if POOL:
        await POOL.close()
