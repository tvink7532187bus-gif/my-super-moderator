import os
import re
import asyncio
import logging
from datetime import timedelta
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from aiogram.enums import ChatMemberStatus
import aiosqlite
from aiohttp import web

# --- БАЗА ДАННЫХ (SQLite) ---
DB_NAME = "bot_data.db"

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                is_locked INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warns (
                user_id INTEGER,
                chat_id INTEGER,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, chat_id)
            )
        """)
        await db.commit()

async def add_chat(chat_id: int, title: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title))
        await db.commit()

async def get_all_chats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM chats") as cursor:
            return await cursor.fetchall()

async def set_lock(chat_id: int, locked: bool):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE chats SET is_locked = ? WHERE chat_id = ?", (1 if locked else 0, chat_id))
        await db.commit()

async def is_chat_locked(chat_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT is_locked FROM chats WHERE chat_id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            return bool(row[0]) if row else False

async def add_warn(user_id: int, chat_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count FROM warns WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)) as cursor:
            row = await cursor.fetchone()
            count = (row[0] + 1) if row else 1
        await db.execute("INSERT OR REPLACE INTO warns (user_id, chat_id, count) VALUES (?, ?, ?)", (user_id, chat_id, count))
        await db.commit()
        return count

async def reset_warns(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM warns WHERE user_id = ? AND chat_id = ?", (user_id, chat_id))
        await db.commit()

# --- ОСНОВНОЙ БОТ ---
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

URL_REGEX = re.compile(r"(https?://\S+|t\.me/\S+|@[a-zA-Z0-9_]+)")

async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except:
        return False

# Капча для новичков
@dp.message(F.new_chat_members)
async def welcome_new_member(message: types.Message):
    await add_chat(message.chat.id, message.chat.title)
    for user in message.new_chat_members:
        if user.id == bot.id: continue
        try:
            await bot.restrict_chat_member(message.chat.id, user.id, permissions=ChatPermissions(can_send_messages=False))
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я не бот", callback_data=f"captcha_{user.id}")]])
            await message.reply(f"Привет, {user.first_name}! Нажмите кнопку ниже, чтобы писать в чате.", reply_markup=kb)
        except: pass

@dp.callback_query(F.data.startswith("captcha_"))
async def process_captcha(query: types.CallbackQuery):
    user_id = int(query.data.split("_")[1])
    if query.from_user.id != user_id:
        await query.answer("Эта кнопка не для вас!", show_alert=True)
        return
    try:
        await bot.restrict_chat_member(
            query.message.chat.id, user_id,
            permissions=ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True)
        )
        await query.message.delete()
        await query.answer("Проверка пройдена!")
    except: pass

# Команды модерации
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    target = message.reply_to_message.from_user
    await bot.ban_chat_member(message.chat.id, target.id)
    await message.reply(f"🚫 Пользователь {target.full_name} забанен.")

@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    time_str = command.args or "10m"
    minutes = int(re.search(r'\d+', time_str).group()) if re.search(r'\d+', time_str) else 10
    target = message.reply_to_message.from_user
    await bot.restrict_chat_member(
        message.chat.id, target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=timedelta(minutes=minutes)
    )
    await message.reply(f"🔇 {target.full_name} отправлен в мут на {minutes} минут.")

@dp.message(Command("warn"))
async def cmd_warn(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id) or not message.reply_to_message: return
    target = message.reply_to_message.from_user
    warns = await add_warn(target.id, message.chat.id)
    if warns >= 3:
        await reset_warns(target.id, message.chat.id)
        await bot.restrict_chat_member(message.chat.id, target.id, permissions=ChatPermissions(can_send_messages=False), until_date=timedelta(hours=24))
        await message.reply(f"⚠️ {target.full_name} получил 3/3 варнов и замучен на 24 часа.")
    else:
        await message.reply(f"⚠️ {target.full_name} получил предупреждение ({warns}/3).")

@dp.message(Command("clear"))
async def cmd_clear(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id): return
    count = int(command.args) if command.args and command.args.isdigit() else 10
    for i in range(count):
        try: await bot.delete_message(message.chat.id, message.message_id - i)
        except: pass

# Голос бота
@dp.message(Command("say"))
async def cmd_say(message: types.Message, command: CommandObject):
    if not await is_admin(message.chat.id, message.from_user.id) or not command.args: return
    await message.delete()
    await message.answer(command.args)

# Lock / Unlock чата
@dp.message(Command("lock"))
async def cmd_lock(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id): return
    await set_lock(message.chat.id, True)
    await message.reply("🔒 Чат заблокирован. Все новые сообщения от обычных участников будут удаляться.")

@dp.message(Command("unlock"))
async def cmd_unlock(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id): return
    await set_lock(message.chat.id, False)
    await message.reply("🔓 Чат разблокирован.")

# Функция Владельца в ЛС (Просмотр групп и Рассылка)
@dp.message(Command("chats"), F.chat.type == "private")
async def cmd_chats(message: types.Message):
    if message.from_user.id != OWNER_ID: return
    chats = await get_all_chats()
    text = "📋 **Список групп бота:**\n\n" + "\n".join([f"• {title} (ID: `{chat_id}`)" for chat_id, title in chats])
    await message.reply(text if chats else "Бот пока не добавлен ни в одну группу.", parse_mode="Markdown")

@dp.message(Command("broadcast"), F.chat.type == "private")
async def cmd_broadcast(message: types.Message, command: CommandObject):
    if message.from_user.id != OWNER_ID or not command.args: return
    chats = await get_all_chats()
    sent = 0
    for chat_id, _ in chats:
        try:
            await bot.send_message(chat_id, command.args)
            sent += 1
        except: pass
    await message.reply(f"📢 Сообщение успешно отправлено в {sent} групп.")

# Авто-удаление ссылок и сообщений в закрытом чате
@dp.message(F.chat.type.in_(["group", "supergroup"]))
async def message_filter(message: types.Message):
    await add_chat(message.chat.id, message.chat.title)
    if await is_admin(message.chat.id, message.from_user.id): return

    if await is_chat_locked(message.chat.id):
        try: await message.delete()
        except: pass
        return

    if URL_REGEX.search(message.text or ""):
        try:
            await message.delete()
            warning = await message.answer(f"🚫 {message.from_user.first_name}, ссылки запрещены!")
            await asyncio.sleep(5)
            await warning.delete()
        except: pass

# Веб-сервер для бесплатного тарифа Render Web Service
async def handle(request):
    return web.Response(text="Bot is running!")

async def main():
    await init_db()
    
    # Запуск мини-сервера для Render
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    
    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
    
