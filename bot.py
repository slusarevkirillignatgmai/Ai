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
from aiogram.types import ChatMemberUpdated, Message
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = os.getenv("DB_PATH", "stats.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Vladivostok"))
NO_SIGNATURE = "Без подписи"
ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)

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


async def save_channel(chat_id: int, title: str | None) -> None:
    await db.execute(
        "INSERT INTO channels (chat_id, title) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title = excluded.title",
        (chat_id, title),
    )
    await db.commit()


# ---------- Бота добавили в канал / убрали из канала ----------

@router.my_chat_member(F.chat.type == ChatType.CHANNEL)
async def on_bot_status_change(event: ChatMemberUpdated) -> None:
    status = event.new_chat_member.status
    if status == ChatMemberStatus.ADMINISTRATOR:
        await save_channel(event.chat.id, event.chat.title)
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
        # Статистика остаётся в базе, но канал пропадает из списков
        await db.execute("DELETE FROM channels WHERE chat_id = ?", (event.chat.id,))
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
        "1. Добавь меня в канал админом.\n"
        "2. В настройках канала включи «Подписывать сообщения» — иначе я не вижу, кто автор.\n\n"
        "<b>Команды:</b>\n"
        "/channels — каналы, где ты админ и я тоже\n"
        "/top [day|week|month|all] [id канала] — посты по админам\n"
        "/total [day|week|month|all] [id канала] — всего постов\n\n"
        "Без id канала команды покажут все твои каналы."
    )


async def get_admin_channels(bot: Bot, user_id: int) -> list[tuple[int, str | None]]:
    """Каналы из базы, где данный пользователь — админ."""
    async with db.execute("SELECT chat_id, title FROM channels") as cur:
        rows = await cur.fetchall()
    result = []
    for chat_id, title in rows:
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except Exception:
            continue  # бота убрали из канала или нет доступа
        if member.status in ADMIN_STATUSES:
            result.append((chat_id, title))
    return result


def parse_args(command: CommandObject) -> tuple[str, int | None]:
    key, chat_id = "day", None
    for arg in (command.args or "").split():
        if arg.lower() in PERIODS:
            key = arg.lower()
        else:
            try:
                chat_id = int(arg)
            except ValueError:
                pass
    return key, chat_id


async def pick_channels(message: Message, command: CommandObject, bot: Bot):
    """Возвращает (period_key, [каналы]) или None, если ответ уже отправлен."""
    key, wanted_id = parse_args(command)
    channels = await get_admin_channels(bot, message.from_user.id)
    if wanted_id is not None:
        channels = [c for c in channels if c[0] == wanted_id]
    if not channels:
        await message.answer(
            "Не нашёл каналов, где ты админ и я тоже.\n"
            "Добавь меня в канал админом, затем повтори команду."
        )
        return None
    return key, channels


def where_clause(chat_id: int, key: str):
    where, params = "chat_id = ?", [chat_id]
    start = period_start(key)
    if start:
        where += " AND day >= ?"
        params.append(start)
    return where, params


@router.message(Command("channels"), PRIVATE)
async def cmd_channels(message: Message, bot: Bot) -> None:
    channels = await get_admin_channels(bot, message.from_user.id)
    if not channels:
        await message.answer(
            "Не нашёл каналов, где ты админ и я тоже.\n"
            "Добавь меня в канал админом, затем повтори команду."
        )
        return
    lines = ["<b>Твои каналы:</b>"]
    lines += [f"{html.escape(t or '—')}: <code>{cid}</code>" for cid, t in channels]
    await message.answer("\n".join(lines))


@router.message(Command("top"), PRIVATE)
async def cmd_top(message: Message, command: CommandObject, bot: Bot) -> None:
    picked = await pick_channels(message, command, bot)
    if not picked:
        return
    key, channels = picked

    for chat_id, title in channels:
        where, params = where_clause(chat_id, key)
        async with db.execute(
            f"SELECT author, SUM(count) FROM stats WHERE {where} "
            "GROUP BY author ORDER BY SUM(count) DESC",
            params,
        ) as cur:
            rows = await cur.fetchall()

        header = f"<b>{html.escape(title or 'Канал')} — посты {PERIODS[key][0]}:</b>"
        if not rows:
            await message.answer(f"{header}\nПостов нет.")
            continue
        lines = [header]
        for i, (author, cnt) in enumerate(rows, 1):
            lines.append(f"{i}. {html.escape(author)} — {cnt}")
        lines.append(f"\nВсего: <b>{sum(c for _, c in rows)}</b>")
        await message.answer("\n".join(lines))


@router.message(Command("total"), PRIVATE)
async def cmd_total(message: Message, command: CommandObject, bot: Bot) -> None:
    picked = await pick_channels(message, command, bot)
    if not picked:
        return
    key, channels = picked

    lines = [f"<b>Постов {PERIODS[key][0]}:</b>"]
    grand_total = 0
    for chat_id, title in channels:
        where, params = where_clause(chat_id, key)
        async with db.execute(
            f"SELECT COALESCE(SUM(count), 0) FROM stats WHERE {where}", params
        ) as cur:
            (cnt,) = await cur.fetchone()
        grand_total += cnt
        lines.append(f"{html.escape(title or 'Канал')}: {cnt}")
    if len(channels) > 1:
        lines.append(f"\nВсего по всем каналам: <b>{grand_total}</b>")
    await message.answer("\n".join(lines))


async def main() -> None:
    if not TOKEN:
        raise SystemExit("Укажи BOT_TOKEN в .env")
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    # Если у бота был включён вебхук, getUpdates не работает — снимаем его
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(
            bot, allowed_updates=["message", "channel_post", "my_chat_member"]
        )
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
