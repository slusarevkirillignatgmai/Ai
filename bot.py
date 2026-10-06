import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "stats.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Vladivostok"))
NO_SIGNATURE = "Без подписи"

router = Router()
db: aiosqlite.Connection | None = None

PERIODS = {
    "day": ("за сегодня", 0),
    "week": ("за 7 дней", 6),
    "month": ("за 30 дней", 29),
    "all": ("за всё время", None),
}


def now() -> datetime:
    return datetime.now(TZ)


def period_start(key: str) -> str | None:
    days_back = PERIODS[key][1]
    if days_back is None:
        return None
    return (now() - timedelta(days=days_back)).strftime("%Y-%m-%d")


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS channels (
            chat_id INTEGER PRIMARY KEY,
            title   TEXT
        );
        CREATE TABLE IF NOT EXISTS stats (
            chat_id INTEGER NOT NULL,
            author  TEXT    NOT NULL,
            day     TEXT    NOT NULL,
            count   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, author, day)
        );
        CREATE TABLE IF NOT EXISTS seen_albums (
            chat_id        INTEGER NOT NULL,
            media_group_id TEXT    NOT NULL,
            PRIMARY KEY (chat_id, media_group_id)
        );
        """
    )
    await db.commit()


# ---------- Подсчёт постов в канале ----------

@router.channel_post()
async def count_post(message: Message) -> None:
    chat_id = message.chat.id

    # Альбом (несколько фото/видео) = один пост
    if message.media_group_id:
        cur = await db.execute(
            "INSERT OR IGNORE INTO seen_albums (chat_id, media_group_id) VALUES (?, ?)",
            (chat_id, message.media_group_id),
        )
        if cur.rowcount == 0:
            return

    author = message.author_signature or NO_SIGNATURE
    day = now().strftime("%Y-%m-%d")

    await db.execute(
        "INSERT INTO channels (chat_id, title) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
        (chat_id, message.chat.title),
    )
    await db.execute(
        "INSERT INTO stats (chat_id, author, day, count) VALUES (?, ?, ?, 1) "
        "ON CONFLICT(chat_id, author, day) DO UPDATE SET count = count + 1",
        (chat_id, author, day),
    )
    await db.commit()


# ---------- Команды (в личке с ботом) ----------

PRIVATE = F.chat.type == ChatType.PRIVATE


@router.message(Command("start", "help"), PRIVATE)
async def cmd_start(message: Message) -> None:
    await message.answer(
        "Привет! Я считаю, сколько постов опубликовал каждый админ канала.\n\n"
        "<b>Настройка:</b>\n"
        "1. Добавь меня в канал админом (право публикации не нужно, достаточно чтения).\n"
        "2. В настройках канала включи «Подписывать сообщения» — иначе я не вижу, кто автор.\n\n"
        "<b>Команды:</b>\n"
        "/channels — список каналов\n"
        "/top [day|week|month|all] [id канала] — посты по админам\n"
        "/total [day|week|month|all] [id канала] — всего постов"
    )


@router.message(Command("channels"), PRIVATE)
async def cmd_channels(message: Message) -> None:
    async with db.execute("SELECT chat_id, title FROM channels") as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Пока нет каналов. Добавь меня в канал админом и опубликуй пост.")
        return
    lines = ["<b>Каналы:</b>"] + [f"{html.escape(t or '—')}: <code>{cid}</code>" for cid, t in rows]
    await message.answer("\n".join(lines))


async def resolve_request(message: Message, command: CommandObject, bot: Bot):
    """Возвращает (period_key, chat_id, title) или None, если ответ уже отправлен."""
    key, chat_id = "day", None
    for arg in (command.args or "").split():
        if arg.lower() in PERIODS:
            key = arg.lower()
        else:
            try:
                chat_id = int(arg)
            except ValueError:
                pass

    async with db.execute("SELECT chat_id, title FROM channels") as cur:
        channels = await cur.fetchall()
    if not channels:
        await message.answer("Пока нет данных. Добавь меня в канал админом и опубликуй пост.")
        return None

    if chat_id is None:
        if len(channels) == 1:
            chat_id = channels[0][0]
        else:
            await message.answer("Каналов несколько — укажи id: /top week -100123456789\nСписок: /channels")
            return None

    title = next((t for cid, t in channels if cid == chat_id), None)
    if title is None and chat_id not in [c[0] for c in channels]:
        await message.answer("Такого канала нет в моей базе.")
        return None

    # Статистику видят только админы канала
    try:
        member = await bot.get_chat_member(chat_id, message.from_user.id)
    except Exception:
        member = None
    if not member or member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
        await message.answer("Статистику может смотреть только админ этого канала.")
        return None

    return key, chat_id, title


@router.message(Command("top"), PRIVATE)
async def cmd_top(message: Message, command: CommandObject, bot: Bot) -> None:
    resolved = await resolve_request(message, command, bot)
    if not resolved:
        return
    key, chat_id, title = resolved

    query = "SELECT author, SUM(count) FROM stats WHERE chat_id = ?"
    params: list = [chat_id]
    start = period_start(key)
    if start:
        query += " AND day >= ?"
        params.append(start)
    query += " GROUP BY author ORDER BY SUM(count) DESC"

    async with db.execute(query, params) as cur:
        rows = await cur.fetchall()
    if not rows:
        await message.answer("За этот период постов нет.")
        return

    total = sum(c for _, c in rows)
    lines = [f"<b>{html.escape(title or 'Канал')} — посты {PERIODS[key][0]}:</b>"]
    for i, (author, cnt) in enumerate(rows, 1):
        lines.append(f"{i}. {html.escape(author)} — {cnt}")
    lines.append(f"\nВсего: <b>{total}</b>")
    await message.answer("\n".join(lines))


@router.message(Command("total"), PRIVATE)
async def cmd_total(message: Message, command: CommandObject, bot: Bot) -> None:
    resolved = await resolve_request(message, command, bot)
    if not resolved:
        return
    key, chat_id, title = resolved

    query = "SELECT COALESCE(SUM(count), 0) FROM stats WHERE chat_id = ?"
    params: list = [chat_id]
    start = period_start(key)
    if start:
        query += " AND day >= ?"
        params.append(start)
    async with db.execute(query, params) as cur:
        (cnt,) = await cur.fetchone()
    await message.answer(f"{html.escape(title or 'Канал')}: <b>{cnt}</b> постов {PERIODS[key][0]}")


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в .env")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=["message", "channel_post"])
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
