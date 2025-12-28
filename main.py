import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

locales: dict[str,dict[str,str]] = {} # lang iso -> {key, str}

TZ_MSK = ZoneInfo("Europe/Moscow")
DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")
PROMPTS_PATH = os.getenv("PROMPTS_PATH", "prompts.json")
LOCALES = os.getenv("LOCALE_DIR","locale")

def load_locales():
    for i in os.listdir(LOCALES):
        with open(f"{LOCALES}/{i}","r",encoding="utf-8") as f:
            locales[i.split(".",1)[0]]=json.load(f)

def L(key:str,update:Update|int,*args:str):
    # if expanding to other languages is needed later, add language column to db
    try:
        return locales["ru_RU"][key].format(*args)
    except KeyError:
        return f"ru_RU:{key}"

def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def db_init():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                prompt_index INTEGER NOT NULL DEFAULT 0,
                last_prompt_ts TEXT DEFAULT NULL,
                answered INTEGER NOT NULL DEFAULT 0,
                reminder_sent INTEGER NOT NULL DEFAULT 0
            )
            """
        )

def db_ensure_user(user_id: int):
    with db_connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
            (user_id,),
        )

def db_get_user(user_id: int):
    with db_connect() as conn:
        cur = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        return cur.fetchone()

def db_mark_prompt_sent(user_id: int, prompt_index: int, sent_dt: datetime):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET prompt_index = ?,
                last_prompt_ts = ?,
                answered = 0,
                reminder_sent = 0
            WHERE user_id = ?
            """,
            (prompt_index, sent_dt.isoformat(), user_id),
        )

def db_mark_answered(user_id: int):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET answered = 1
            WHERE user_id = ?
            """,
            (user_id,),
        )

def db_mark_reminder_sent(user_id: int):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET reminder_sent = 1
            WHERE user_id = ?
            """,
            (user_id,),
        )

def db_all_users():
    with db_connect() as conn:
        cur = conn.execute("SELECT * FROM users")
        return cur.fetchall()


def load_prompts() -> list[str]:
    with open(PROMPTS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        raise ValueError("prompts.json must be a JSON array of strings")
    if not data:
        raise ValueError("prompts.json is empty")
    return data


def now_msk() -> datetime:
    return datetime.now(TZ_MSK)

async def send_prompt_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int, prompt_text: str):
    await context.bot.send_message(chat_id=user_id, text=L("message.sendprompt",user_id,prompt_text), parse_mode=ParseMode.MARKDOWN)

async def send_reminder_to_user(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    await context.bot.send_message(
        chat_id=user_id,
        text=L("message.reminder",user_id),
    )

async def maybe_send_next_prompt(context: ContextTypes.DEFAULT_TYPE, user_row, prompts: list[str], sent_time: datetime):
    user_id = int(user_row["user_id"])
    answered = int(user_row["answered"])
    idx = int(user_row["prompt_index"])

    if answered != 1:
        await context.bot.send_message(
            chat_id=user_id,
            text=L("message.reminder",user_id),
        )
        return

    next_idx = idx

    next_idx = (idx + 1) % len(prompts)

    await send_prompt_to_user(context, user_id, prompts[next_idx])
    db_mark_prompt_sent(user_id, next_idx, sent_time)

async def daily_9_msk_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs at 9:00 MSK daily: send next prompt to users who answered the previous."""
    prompts = load_prompts()
    sent_time = now_msk()
    for u in db_all_users():
        await maybe_send_next_prompt(context, u, prompts, sent_time)

async def reminder_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Runs periodically: if 24h passed since last prompt and no answer, remind once."""
    now = now_msk()
    for u in db_all_users():
        user_id = int(u["user_id"])
        answered = int(u["answered"])
        reminder_sent = int(u["reminder_sent"])
        last_prompt_ts = u["last_prompt_ts"]

        if last_prompt_ts is None:
            continue  # never sent anything yet

        if answered == 1:
            continue

        if reminder_sent == 1:
            continue

        try:
            last_dt = datetime.fromisoformat(last_prompt_ts)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=TZ_MSK)
            else:
                last_dt = last_dt.astimezone(TZ_MSK)
        except Exception:
            continue

        if now - last_dt >= timedelta(hours=24):
            await send_reminder_to_user(context, user_id)
            db_mark_reminder_sent(user_id)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    prompts = load_prompts()
    u = db_get_user(user_id)

    if u["last_prompt_ts"] is None:
        await update.message.reply_text(L("reply.sub",update))
        first_idx = 0
        await send_prompt_to_user(context, user_id, prompts[first_idx])
        db_mark_prompt_sent(user_id, first_idx, now_msk())
        return

    await update.message.reply_text(L("reply.err.alreadysub",update))

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db_connect() as conn:
        conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    await update.message.reply_text(L("reply.unsub",update))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    user_id = update.effective_user.id
    db_ensure_user(user_id)

    u = db_get_user(user_id)
    if u["last_prompt_ts"] is None:
        await update.message.reply_text(L("reply.err.noinitprompt",update))
        return

    if int(u["answered"]) == 1:
        await update.message.reply_text(L("reply.complete.received",update))
        return

    db_mark_answered(user_id)
    await update.message.reply_text(L("reply.complete.received",update))


def main():
    if not load_dotenv():
        raise FileNotFoundError("В этой папке нет файла \".env\".")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN?")

    load_locales()
    db_init()

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Daily at 9:00 MSK
    app.job_queue.run_daily(
        daily_9_msk_job,
        time=time(hour=9, minute=0, second=0, tzinfo=TZ_MSK),
        name="daily_9_msk",
    )

    # # Reminder scan every 30 minutes
    # app.job_queue.run_repeating(
    #     reminder_scan_job,
    #     interval=30 * 60,
    #     first=30,
    #     name="reminder_scan",
    # )

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
