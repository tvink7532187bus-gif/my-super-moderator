import asyncio
import logging
import os
import re
from datetime import timedelta
from aiohttp import web
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatMemberStatus
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

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
    await db.execute(
        "INSERT OR REPLACE INTO chats (chat_id, title) VALUES (?, ?)",
        (chat_id, title),
    )
    await db.commit()


async def get_all_chats():
  async with aiosqlite.connect(DB_NAME) as db:
    async with db.execute("SELECT chat_id, title FROM chats") as cursor:
      return await cursor.fetchall()


async def set_lock(chat_id: int, locked: bool):
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute(
        "UPDATE chats SET is_locked = ? WHERE chat_id = ?",
        (1 if locked else 0, chat_id),
    )
    await db.commit()


async def is_chat_locked(chat_id: int) -> bool:
  async with aiosqlite.connect(DB_NAME) as db:
    async with db.execute(
        "SELECT is_locked FROM chats WHERE chat_id = ?", (chat_id,)
    ) as cursor:
      row = await cursor.fetchone()
      return bool(row[0]) if row else False


async def add_warn(user_id: int, chat_id: int) -> int:
  async with aiosqlite.connect(DB_NAME) as db:
    async with db.execute(
        "SELECT count FROM warns WHERE user_id = ? AND chat_id = ?",
        (user_id, chat_id),
    ) as cursor:
      row = await cursor.fetchone()
      count = (row[0] + 1) if row else 1
    await db.execute(
        "INSERT OR REPLACE INTO warns (user_id, chat_id, count) VALUES (?, ?, ?)",
        (user_id, chat_id, count),
    )
    await db.commit()
    return count


async def reset_warns(user_id: int, chat_id: int):
  async with aiosqlite.connect(DB_NAME) as db:
    await db.execute(
        "DELETE FROM warns WHERE user_id = ? AND chat_id = ?", (user_id, chat_id)
    )
    await db.commit()


# --- ОСНОВНОЙ БОТ ---
TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

URL_REGEX = re.compile(r"(https?://\S+|t\.me/\S+|@[a-zA-Z0-9_]+)")

# Кэш для анти-флуда: {(chat_id, user_id): [timestamps]}
user_flood_cache = {}


async def is_admin(chat_id: int, user_id: int) -> bool:
  try:
    member = await bot.get_chat_member(chat_id, user_id)
    return member.status in [
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.CREATOR,
    ]
  except:
    return False


# Приветствие и список команд
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
  text = (
      "🤖 **Привет! Я бот-модератор этого чата.**\n\n"
      "🛡 **Что я умею:**\n"
      "• Автоматически удалять ссылки и рекламу\n"
      "• Защищать чат от флуда и спама\n"
      "• Проверять новичков через капчу\n\n"
      "📋 **Команды для модераторов:**\n"
      "• `/ban [ответ / ID / @username]` — забанить пользователя\n"
      "• `/mute [время]` — дать мут (например, `/mute 10m` или `/mute 1h`)\n"
      "• `/warn` — выдать предупреждение (3 варна = мут на 24 часа)\n"
      "• `/id [ответ / @username]` — узнать ID пользователя или чата\n"
      "• `/clear [кол-во]` — очистить последние сообщения\n"
      "• `/say [текст]` — написать сообщение от имени бота (анонимно)\n"
      "• `/lock` — закрыть чат\n"
      "• `/unlock` — открыть чат\n"
  )
  await message.answer(text, parse_mode="Markdown")


# Команда /id (поддерживает ответ, юзернейм и собственный ID)
@dp.message(Command("id"))
async def cmd_id(message: types.Message, command: CommandObject):
  if message.reply_to_message:
    target = message.reply_to_message.from_user
    await message.reply(
        f"👤 **Пользователь:** {target.full_name}\n🆔 **ID:** `{target.id}`\n🏷"
        f" **Username:** @{target.username or 'отсутствует'}",
        parse_mode="Markdown",
    )
  elif command.args:
    arg = command.args.strip()
    username = arg if arg.startswith("@") else f"@{arg}"
    try:
      chat_user = await bot.get_chat(username)
      await message.reply(
          f"👤 **Пользователь:**"
          f" {chat_user.full_name or 'Без имени'}\n🆔 **ID:**"
          f" `{chat_user.id}`\n🏷 **Username:** @{chat_user.username or arg}",
          parse_mode="Markdown",
      )
    except Exception:
      await message.reply(
          "❌ Не удалось найти пользователя по такому юзернейму."
      )
  else:
    user = message.from_user
    await message.reply(
        f"👤 **Ваш ID:** `{user.id}`\n💬 **ID этого чата:** `{message.chat.id}`",
        parse_mode="Markdown",
    )


# Капча для новичков
@dp.message(F.new_chat_members)
async def welcome_new_member(message: types.Message):
  await add_chat(message.chat.id, message.chat.title)
  for user in message.new_chat_members:
    if user.id == bot.id:
      continue
    try:
      await bot.restrict_chat_member(
          message.chat.id,
          user.id,
          permissions=ChatPermissions(can_send_messages=False),
      )
      kb = InlineKeyboardMarkup(
          inline_keyboard=[[
              InlineKeyboardButton(
                  text="✅ Я не бот", callback_data=f"captcha_{user.id}"
              )
          ]]
      )
      await message.reply(
          f"Привет, {user.first_name}! Нажмите кнопку ниже, чтобы писать в"
          " чате.",
          reply_markup=kb,
      )
    except:
      pass


@dp.callback_query(F.data.startswith("captcha_"))
async def process_captcha(query: types.CallbackQuery):
  user_id = int(query.data.split("_")[1])
  if query.from_user.id != user_id:
    await query.answer("Эта кнопка не для вас!", show_alert=True)
    return
  try:
    await bot.restrict_chat_member(
        query.message.chat.id,
        user_id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
        ),
    )
    await query.message.delete()
    await query.answer("Проверка пройдена!")
  except:
    pass


# Бан (поддерживает ответ, ID и @username)
@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
  if not await is_admin(message.chat.id, message.from_user.id):
    return

  target_id = None
  target_name = "Пользователь"

  if message.reply_to_message:
    target_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.full_name
  elif command.args:
    arg = command.args.strip()
    if arg.isdigit():
      target_id = int(arg)
      target_name = f"ID {target_id}"
    elif arg.startswith("@") or not arg.isdigit():
      username = arg if arg.startswith("@") else f"@{arg}"
      try:
        chat_user = await bot.get_chat(username)
        target_id = chat_user.id
        target_name = chat_user.full_name or arg
      except Exception:
        await message.reply(
            "❌ Не удалось найти пользователя по такому юзернейму."
        )
        return

  if target_id:
    try:
      await bot.ban_chat_member(message.chat.id, target_id)
      await message.reply(f"🚫 Пользователь {target_name} забанен.")
    except Exception as e:
      await message.reply(f"❌ Не удалось забанить: {e}")
  else:
    await message.reply(
        "⚠️ Ответьте на сообщение пользователя, укажите его ID или"
        " юзернейм (@username).\nПример: `/ban @username`",
        parse_mode="Markdown",
    )


@dp.message(Command("mute"))
async def cmd_mute(message: types.Message, command: CommandObject):
  if not await is_admin(
      message.chat.id, message.from_user.id
  ) or not message.reply_to_message:
    return
  time_str = command.args or "10m"
  minutes = (
      int(re.search(r"\d+", time_str).group())
      if re.search(r"\d+", time_str)
      else 10
  )
  target = message.reply_to_message.from_user
  await bot.restrict_chat_member(
      message.chat.id,
      target.id,
      permissions=ChatPermissions(can_send_messages=False),
      until_date=timedelta(minutes=minutes),
  )
  await message.reply(
      f"🔇 {target.full_name} отправлен в мут на {minutes} минут."
  )


@dp.message(Command("warn"))
async def cmd_warn(message: types.Message):
  if not await is_admin(
      message.chat.id, message.from_user.id
  ) or not message.reply_to_message:
    return
  target = message.reply_to_message.from_user
  warns = await add_warn(target.id, message.chat.id)
  if warns >= 3:
    await reset_warns(target.id, message.chat.id)
    await bot.restrict_chat_member(
        message.chat.id,
        target.id,
        permissions=ChatPermissions(can_send_messages=False),
        until_date=timedelta(hours=24),
    )
    await message.reply(
        f"⚠️ {target.full_name} получил 3/3 варнов и замучен на 24 часа."
    )
  else:
    await message.reply(
        f"⚠️ {target.full_name} получил предупреждение ({warns}/3)."
    )


@dp.message(Command("clear"))
async def cmd_clear(message: types.Message, command: CommandObject):
  if not await is_admin(message.chat.id, message.from_user.id):
    return
  count = int(command.args) if command.args and command.args.isdigit() > 0 else 10
  for i in range(count):
    try:
      await bot.delete_message(message.chat.id, message.message_id - i)
    except:
      pass


# Голос бота
@dp.message(Command("say"))
async def cmd_say(message: types.Message, command: CommandObject):
  if not await is_admin(message.chat.id, message.from_user.id) or not command.args:
    return
  await message.delete()
  await message.answer(command.args)


# Lock / Unlock чата
@dp.message(Command("lock"))
async def cmd_lock(message: types.Message):
  if not await is_admin(message.chat.id, message.from_user.id):
    return
  await set_lock(message.chat.id, True)
  await message.reply(
      "🔒 Чат заблокирован. Все новые сообщения от обычных участников будут"
      " удаляться."
  )


@dp.message(Command("unlock"))
async def cmd_unlock(message: types.Message):
  if not await is_admin(message.chat.id, message.from_user.id):
    return
  await set_lock(message.chat.id, False)
  await message.reply("🔓 Чат разблокирован.")


# Функция Владельца в ЛС (Просмотр групп и Рассылка)
@dp.message(Command("chats"), F.chat.type == "private")
async def cmd_chats(message: types.Message):
  if message.from_user.id != OWNER_ID:
    return
  chats = await get_all_chats()
  text = "📋 **Список групп бота:**\n\n" + "\n".join([
      f"• {title} (ID: `{chat_id}`)" for chat_id, title in chats
  ])
  await message.reply(
      text if chats else "Бот пока не добавлен ни в одну группу.",
      parse_mode="Markdown",
  )


@dp.message(Command("broadcast"), F.chat.type == "private")
async def cmd_broadcast(message: types.Message, command: CommandObject):
  if message.from_user.id != OWNER_ID or not command.args:
    return
  chats = await get_all_chats()
  sent = 0
  for chat_id, _ in chats:
    try:
      await bot.send_message(chat_id, command.args)
      sent += 1
    except:
      pass
  await message.reply(f"📢 Сообщение успешно отправлено в {sent} групп.")


# Главный фильтр сообщений (Анти-флуд, Анти-ссылки, Закрытый чат)
@dp.message(F.chat.type.in_(["group", "supergroup"]))
async def message_filter(message: types.Message):
  await add_chat(message.chat.id, message.chat.title)

  # Админов не проверяем на флуд и ссылки
  if await is_admin(message.chat.id, message.from_user.id):
    return

  # 1. Проверка на анти-флуд (больше 4 сообщений за 3 секунды)
  chat_id = message.chat.id
  user_id = message.from_user.id
  cache_key = (chat_id, user_id)
  now = asyncio.get_event_loop().time()

  if cache_key not in user_flood_cache:
    user_flood_cache[cache_key] = []

  user_flood_cache[cache_key] = [
      t for t in user_flood_cache[cache_key] if now - t < 3.0
  ]
  user_flood_cache[cache_key].append(now)

  if len(user_flood_cache[cache_key]) > 4:
    try:
      await message.delete()
      await bot.restrict_chat_member(
          chat_id,
          user_id,
          permissions=ChatPermissions(can_send_messages=False),
          until_date=timedelta(minutes=5),
      )
      warn_msg = await message.answer(
          f"🔇 {message.from_user.first_name}, анти-флуд! Вы отправляете"
          " сообщения слишком быстро (мут на 5 минут)."
      )
      await asyncio.sleep(5)
      await warn_msg.delete()
    except:
      pass
    return

  # 2. Проверка на закрытый чат
  if await is_chat_locked(chat_id):
    try:
      await message.delete()
    except:
      pass
    return

  # 3. Проверка на ссылки
  if URL_REGEX.search(message.text or ""):
    try:
      await message.delete()
      warning = await message.answer(
          f"🚫 {message.from_user.first_name}, ссылки запрещены!"
      )
      await asyncio.sleep(5)
      await warning.delete()
    except:
      pass


# Веб-сервер для бесплатного тарифа Render Web Service
async def handle(request):
  return web.Response(text="Bot is running!")


async def main():
  await init_db()

  app = web.Application()
  app.router.add_get("/", handle)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.environ.get("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()

  await dp.start_polling(bot)


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO)
  asyncio.run(main())
        
